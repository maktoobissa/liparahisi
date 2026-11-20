import asyncio
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, validator
from psycopg.rows import dict_row
from backend.email_delivery import deliver_email, email_is_configured, email_provider


logger = logging.getLogger("notifications")
MAX_ATTEMPTS = 5


class EmailNotification(BaseModel):
    to_email: str = Field(min_length=5, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20000)
    html: str | None = Field(default=None, max_length=50000)

    @validator("to_email")
    def validate_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Weka barua pepe sahihi.")
        return value


def database_url():
    value = os.getenv("NOTIFICATIONS_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("NOTIFICATIONS_DATABASE_URL haijawekwa.")
    return value


def connect_db():
    return psycopg.connect(database_url(), row_factory=dict_row)


def initialize_db():
    with connect_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS email_outbox (
                id TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                html_body TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'QUEUED',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                locked_at TIMESTAMPTZ,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMPTZ
            )
            """
        )
        connection.execute(
            """
            ALTER TABLE email_outbox
            ADD COLUMN IF NOT EXISTS html_body TEXT NOT NULL DEFAULT ''
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS email_outbox_ready_idx
            ON email_outbox(status, next_attempt_at)
            """
        )


def require_internal_token(value):
    expected = os.getenv("NOTIFICATION_SERVICE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="NOTIFICATION_SERVICE_TOKEN haijawekwa.",
        )
    if not value or not secrets.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="Ombi la ndani halijaidhinishwa.")


def claim_email():
    with connect_db() as connection:
        with connection.transaction():
            row = connection.execute(
                """
                SELECT id, recipient, subject, body, html_body, attempts
                FROM email_outbox
                WHERE (
                    status IN ('QUEUED', 'RETRY')
                    AND next_attempt_at <= CURRENT_TIMESTAMP
                ) OR (
                    status = 'SENDING'
                    AND locked_at < CURRENT_TIMESTAMP - INTERVAL '2 minutes'
                )
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            ).fetchone()
            if row is None:
                return None
            row = dict(row)
            connection.execute(
                """
                UPDATE email_outbox
                SET status = 'SENDING', attempts = attempts + 1,
                    locked_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (row["id"],),
            )
            row["attempts"] += 1
            return row


def send_email(row):
    deliver_email(
        row["recipient"],
        row["subject"],
        row["body"],
        row.get("html_body") or None,
    )


def finish_email(row, error=None):
    if error is None:
        with connect_db() as connection:
            connection.execute(
                """
                UPDATE email_outbox
                SET status = 'SENT', sent_at = CURRENT_TIMESTAMP,
                    locked_at = NULL, last_error = '',
                    recipient = '', subject = '', body = '', html_body = ''
                WHERE id = %s
                """,
                (row["id"],),
            )
        return

    failed = row["attempts"] >= MAX_ATTEMPTS
    delay_seconds = min(3600, 30 * (2 ** (row["attempts"] - 1)))
    with connect_db() as connection:
        connection.execute(
            """
            UPDATE email_outbox
            SET status = %s,
                next_attempt_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                locked_at = NULL, last_error = %s,
                recipient = CASE WHEN %s = 'FAILED' THEN '' ELSE recipient END,
                subject = CASE WHEN %s = 'FAILED' THEN '' ELSE subject END,
                body = CASE WHEN %s = 'FAILED' THEN '' ELSE body END,
                html_body = CASE WHEN %s = 'FAILED' THEN '' ELSE html_body END
            WHERE id = %s
            """,
            (
                "FAILED" if failed else "RETRY",
                delay_seconds,
                type(error).__name__,
                "FAILED" if failed else "RETRY",
                "FAILED" if failed else "RETRY",
                "FAILED" if failed else "RETRY",
                "FAILED" if failed else "RETRY",
                row["id"],
            ),
        )
    logger.warning(
        "Email delivery failed for outbox item %s (attempt %s/%s)",
        row["id"],
        row["attempts"],
        MAX_ATTEMPTS,
    )


def process_one_email():
    row = claim_email()
    if row is None:
        return False
    try:
        send_email(row)
    except (RuntimeError, ValueError) as error:
        finish_email(row, error)
    else:
        finish_email(row)
    return True


async def process_outbox():
    while True:
        try:
            processed = await asyncio.to_thread(process_one_email)
        except Exception:
            logger.exception("Email outbox worker failed.")
            processed = False
        await asyncio.sleep(0.25 if processed else 2)


@asynccontextmanager
async def lifespan(_app):
    initialize_db()
    worker = asyncio.create_task(process_outbox())
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


app = FastAPI(title="LipaRahisi Notifications", lifespan=lifespan)


@app.get("/health")
def health():
    with connect_db() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/internal/status")
def internal_status(x_internal_token: str | None = Header(default=None)):
    require_internal_token(x_internal_token)
    provider = email_provider()
    email_configured = email_is_configured()
    return {
        "smtp_configured": provider == "smtp" and email_configured,
        "email_configured": email_configured,
        "email_provider": provider,
    }


@app.post("/internal/emails", status_code=202)
def enqueue_email(
    payload: EmailNotification,
    x_internal_token: str | None = Header(default=None),
):
    require_internal_token(x_internal_token)
    notification_id = secrets.token_hex(16)
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO email_outbox (id, recipient, subject, body, html_body)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                notification_id,
                payload.to_email,
                payload.subject,
                payload.text,
                payload.html or "",
            ),
        )
    return {"notification_id": notification_id, "status": "QUEUED"}
