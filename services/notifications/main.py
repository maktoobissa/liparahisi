import asyncio
import json
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
from backend.notification_providers import (
    mobile_push_configured,
    send_mobile_push,
    send_sms,
    send_web_push,
    send_whatsapp,
    sms_configured,
    web_push_configured,
    whatsapp_configured,
)


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


class ChannelNotification(BaseModel):
    event: str = Field(min_length=2, max_length=100)
    channel: str = Field(pattern=r"^(EMAIL|SMS|WEB_PUSH|MOBILE_PUSH|WHATSAPP)$")
    recipient: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=200)
    text: str = Field(min_length=1, max_length=20000)
    html: str | None = Field(default=None, max_length=50000)
    link: str = Field(default="", max_length=2048)


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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id TEXT PRIMARY KEY,
                event TEXT NOT NULL,
                channel TEXT NOT NULL,
                recipient TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                html_body TEXT NOT NULL DEFAULT '',
                link TEXT NOT NULL DEFAULT '',
                provider_reference TEXT NOT NULL DEFAULT '',
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


def claim_notification():
    with connect_db() as connection:
        with connection.transaction():
            row = connection.execute(
                """
                SELECT id, event, channel, recipient, title, body, html_body, link,
                       attempts
                FROM notification_outbox
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
                """,
            ).fetchone()
            if row is None:
                return None
            row = dict(row)
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = 'SENDING', attempts = attempts + 1,
                    locked_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (row["id"],),
            )
            row["attempts"] += 1
            return row


def send_notification(row):
    channel = row["channel"]
    if channel == "EMAIL":
        deliver_email(
            row["recipient"],
            row["title"],
            row["body"],
            row.get("html_body") or None,
        )
        return ""
    if channel == "SMS":
        return send_sms(row["recipient"], row["body"])
    if channel == "WHATSAPP":
        return send_whatsapp(row["recipient"], row["body"])
    if channel == "WEB_PUSH":
        subscription = json.loads(row["recipient"])
        return send_web_push(subscription, row["title"], row["body"], row["link"])
    if channel == "MOBILE_PUSH":
        subscription = json.loads(row["recipient"])
        return send_mobile_push(subscription, row["title"], row["body"], row["link"])
    raise ValueError("Notification channel si sahihi.")


def finish_notification(row, error=None, provider_reference=""):
    with connect_db() as connection:
        if error is None:
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = 'SENT', provider_reference = %s,
                    sent_at = CURRENT_TIMESTAMP, locked_at = NULL, last_error = ''
                WHERE id = %s
                """,
                (provider_reference, row["id"]),
            )
            return
        failed = row["attempts"] >= MAX_ATTEMPTS
        delay_seconds = min(3600, 30 * (2 ** (row["attempts"] - 1)))
        connection.execute(
            """
            UPDATE notification_outbox
            SET status = %s,
                next_attempt_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                locked_at = NULL, last_error = %s
            WHERE id = %s
            """,
            (
                "FAILED" if failed else "RETRY",
                delay_seconds,
                type(error).__name__,
                row["id"],
            ),
        )


def process_one_notification():
    row = claim_notification()
    if row is None:
        return False
    try:
        provider_reference = send_notification(row)
    except Exception as error:
        finish_notification(row, error)
    else:
        finish_notification(row, provider_reference=provider_reference)
    return True


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
            email_processed = await asyncio.to_thread(process_one_email)
            notification_processed = await asyncio.to_thread(process_one_notification)
            processed = email_processed or notification_processed
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
        "sms_configured": sms_configured(),
        "sms_provider": os.getenv("SMS_PROVIDER", "").strip().lower() or None,
        "whatsapp_configured": whatsapp_configured(),
        "web_push_configured": web_push_configured(),
        "mobile_push_configured": mobile_push_configured(),
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


@app.post("/internal/notifications", status_code=202)
def enqueue_notification(
    payload: ChannelNotification,
    x_internal_token: str | None = Header(default=None),
):
    require_internal_token(x_internal_token)
    notification_id = secrets.token_hex(16)
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO notification_outbox
                (id, event, channel, recipient, title, body, html_body, link)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                notification_id,
                payload.event,
                payload.channel,
                payload.recipient,
                payload.title,
                payload.text,
                payload.html or "",
                payload.link,
            ),
        )
    return {"notification_id": notification_id, "status": "QUEUED"}


@app.get("/internal/history")
def notification_history(
    x_internal_token: str | None = Header(default=None),
):
    require_internal_token(x_internal_token)
    with connect_db() as connection:
        email_rows = connection.execute(
            """
            SELECT id, 'EMAIL' AS channel, recipient, subject AS title,
                   status, attempts, last_error, created_at, sent_at
            FROM email_outbox
            ORDER BY created_at DESC LIMIT 100
            """
        ).fetchall()
        rows = connection.execute(
            """
            SELECT id, event, channel, title, status, attempts, last_error,
                   provider_reference, created_at, sent_at
            FROM notification_outbox
            ORDER BY created_at DESC LIMIT 500
            """
        ).fetchall()
    return {
        "items": [
            {**dict(row), "recipient": ""} for row in email_rows
        ] + [dict(row) for row in rows],
    }
