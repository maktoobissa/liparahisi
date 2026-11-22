import asyncio
import logging
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import boto3

from backend import main


logger = logging.getLogger(__name__)
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
last_backup_at = None


def backup_is_configured():
    return bool(
        os.getenv("BACKUP_S3_BUCKET", "").strip()
        and os.getenv("AWS_ACCESS_KEY_ID", "").strip()
        and os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
    )


def _create_database_snapshot(target):
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        pg_dump = os.getenv("PG_DUMP_PATH", "pg_dump")
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]
        parsed_url = urlsplit(database_url)
        query = parse_qsl(parsed_url.query, keep_blank_values=True)
        query_password = next(
            (value for name, value in query if name.lower() == "password"),
            "",
        )
        password = unquote(parsed_url.password or query_password)
        safe_query = [
            (name, value)
            for name, value in query
            if name.lower() != "password"
        ]
        hostname = parsed_url.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        authority = f"{parsed_url.username}@" if parsed_url.username else ""
        authority += hostname
        if parsed_url.port:
            authority += f":{parsed_url.port}"
        safe_url = urlunsplit(
            (
                parsed_url.scheme,
                authority,
                parsed_url.path,
                urlencode(safe_query),
                parsed_url.fragment,
            )
        )
        command_environment = os.environ.copy()
        if password:
            command_environment["PGPASSWORD"] = password
        try:
            subprocess.run(
                [
                    pg_dump,
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--file",
                    str(target),
                    safe_url,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30 * 60,
                env=command_environment,
            )
        except subprocess.CalledProcessError as error:
            raise RuntimeError(error.stderr.strip() or "pg_dump failed.") from error
        return

    source = sqlite3.connect(main.DATABASE_PATH, timeout=30)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def create_and_upload_backup():
    global last_backup_at
    if not backup_is_configured():
        raise RuntimeError(
            "Weka BACKUP_S3_BUCKET, AWS_ACCESS_KEY_ID na AWS_SECRET_ACCESS_KEY."
        )

    now = datetime.now(timezone.utc)
    extension = ".dump" if os.getenv("DATABASE_URL", "").strip() else ".sqlite3"
    prefix = os.getenv("BACKUP_S3_PREFIX", "liparahisi").strip("/")
    prefix = prefix or "liparahisi"
    key = f"{prefix}/{now:%Y/%m/%d}/liparahisi-{now:%Y%m%dT%H%M%SZ}{extension}"
    endpoint = os.getenv("BACKUP_S3_ENDPOINT_URL", "").strip() or None
    region = os.getenv("AWS_DEFAULT_REGION", "").strip() or None
    bucket = os.getenv("BACKUP_S3_BUCKET", "").strip()

    with tempfile.TemporaryDirectory(prefix="liparahisi-backup-") as directory:
        snapshot = Path(directory) / f"database{extension}"
        _create_database_snapshot(snapshot)
        s3 = boto3.client("s3", endpoint_url=endpoint, region_name=region)
        s3.upload_file(
            str(snapshot),
            bucket,
            key,
            ExtraArgs={"ServerSideEncryption": "AES256"},
        )

    last_backup_at = now.isoformat()
    try:
        with main.db_connection() as connection:
            connection.execute(
                "INSERT INTO backup_runs (id, object_key) VALUES (?, ?)",
                (os.urandom(16).hex(), key),
            )
    except Exception:
        logger.exception("Backup uploaded, but its success record could not be saved.")
    logger.info("Database backup uploaded to s3://%s/%s", bucket, key)
    return {"created_at": last_backup_at, "object_key": key}


async def run_backup_scheduler():
    if not backup_is_configured():
        logger.warning("Automated database backups are disabled: S3 settings are missing.")
        return

    while True:
        try:
            await asyncio.to_thread(create_and_upload_backup)
        except Exception:
            logger.exception("Automated database backup failed.")
        await asyncio.sleep(BACKUP_INTERVAL_SECONDS)


if __name__ == "__main__":
    create_and_upload_backup()
