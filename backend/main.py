import asyncio
import base64
import collections
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import csv
import io
import unicodedata
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID

import httpx
import psycopg
from redis import Redis
from redis.exceptions import RedisError
from psycopg_pool import ConnectionPool
from backend.abliner import AblinerClient, AblinerError, verify_webhook_signature
from backend.clickpesa import ClickPesaClient, ClickPesaError, verify_webhook_checksum
from backend.pesapal import PesapalClient, PesapalError
from backend.fraud_ai import MAX_ALERTS_PER_SCAN, analyze_security_alerts
from backend.business_assistant import (
    ai_recommendations,
    ai_unavailable_text,
    classify_question,
    recommendation_text,
    render_answer,
)
from backend.email_delivery import (
    EmailDeliveryError,
    deliver_email,
    email_is_configured,
    email_provider,
)


def record_system_alert(alert_type, message):
    alert_id = secrets.token_hex(16)
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO system_alerts (id, alert_type, message)
            VALUES (?, ?, ?)
            """,
            (alert_id, alert_type, message),
        )
    return alert_id


def send_system_alert_email(subject, message):
    with db_connection() as connection:
        admins = connection.execute(
            """
            SELECT email FROM users
            WHERE role = 'ADMIN' AND status = 'ACTIVE' AND email <> ''
            """
        ).fetchall()
    content = EmailContent(
        subject=subject,
        text=message,
        html=f"<p>{message}</p>",
    )
    for admin in admins:
        try:
            send_smtp_email(
                admin["email"],
                content.subject,
                content.text,
                content.html,
            )
        except HTTPException as error:
            logger.warning(
                "Admin system-alert email failed (HTTP %s).",
                error.status_code,
            )
from backend.email_templates import (
    EmailContent,
    abandoned_checkout_email,
    password_changed_email,
    password_reset_email,
    payment_email,
    seller_payment_email,
    verification_email,
    welcome_email,
)
from backend.notification_templates import notification_template
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, validator
from backend.security import (
    decrypt_totp_secret,
    encrypt_totp_secret,
    new_totp_secret,
    totp_counter,
    upload_media_type,
    encrypt_kyc_document,
    decrypt_kyc_document,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
logger = logging.getLogger(__name__)

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "backend" / "data.sqlite3")))
FRONTEND_DIST = PROJECT_ROOT / "dist"
SESSION_COOKIE = "liparahisi_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
DATABASE_POOL_MIN_SIZE = 1
DATABASE_POOL_MAX_SIZE = 5
DATABASE_POOL_TIMEOUT_SECONDS = 10
PRODUCT_LIST_CACHE_TTL_SECONDS = 15
PUBLIC_PRODUCT_CACHE_PREFIX = "liparahisi:public-products:v3:"
COMMISSION_PERCENT = 5
ORDER_TTL_SECONDS = 60 * 60 * 24
ABANDONED_CHECKOUT_REMINDER_DELAY_SECONDS = ORDER_TTL_SECONDS
ABANDONED_CHECKOUT_REMINDER_POLL_SECONDS = 60
ABANDONED_CHECKOUT_REMINDER_MAX_ATTEMPTS = 5
ABANDONED_CHECKOUT_REMINDER_LEASE_SECONDS = 15 * 60
RETURN_REQUEST_WINDOW_SECONDS = 7 * 24 * 60 * 60
RESET_TOKEN_TTL_SECONDS = 60 * 60
PASSWORD_RESET_COOLDOWN_SECONDS = 15 * 60
MAX_PRODUCT_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_started_at = time.time()
_server_error_times = collections.deque(maxlen=10000)
_server_error_lock = threading.Lock()
MAX_PRODUCT_GALLERY_IMAGES = 8
MAX_PRODUCT_VIDEO_BYTES = 20 * 1024 * 1024
MAX_PRODUCT_VIDEOS = 3
ABLINER_PAYMENT_METHOD = "Abliner Mobile Money"
PAYMENT_METHODS = {
    "M-Pesa",
    "Halo Pesa",
    "Tigo Pesa",
    "Airtel Money",
    "NMB",
    "CRDB",
    "NBC",
    "NMB Branch",
    "CRDB Branch",
    ABLINER_PAYMENT_METHOD,
}


_database_pool = None
_database_pool_url = None
_database_pool_lock = threading.Lock()
_redis_client = None
_redis_client_address = None
_redis_client_lock = threading.Lock()


class DatabaseConnection:
    def __init__(self, connection, postgres=False, release=None):
        self.connection = connection
        self.postgres = postgres
        self.release = release

    def __enter__(self):
        return self

    def __exit__(self, exception_type, _exception, _traceback):
        try:
            if (
                exception_type is None
                and _exception is None
                and _traceback is None
            ):
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()

    def execute(self, query, parameters=()):
        if self.postgres:
            query = query.replace("?", "%s")
        return self.connection.execute(query, parameters)

    def executescript(self, script):
        if self.postgres:
            for statement in script.split(";"):
                if statement.strip():
                    self.connection.execute(statement)
        else:
            self.connection.executescript(script)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        if self.release:
            self.release(self.connection)
        else:
            self.connection.close()


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        return response


def get_database_pool(database_url):
    global _database_pool, _database_pool_url
    with _database_pool_lock:
        if _database_pool_url != database_url:
            if _database_pool is not None:
                _database_pool.close()
            _database_pool = ConnectionPool(
                database_url,
                min_size=DATABASE_POOL_MIN_SIZE,
                max_size=DATABASE_POOL_MAX_SIZE,
                timeout=DATABASE_POOL_TIMEOUT_SECONDS,
                kwargs={"row_factory": dict_row},
            )
            _database_pool_url = database_url
        return _database_pool


def close_database_pool():
    global _database_pool, _database_pool_url
    with _database_pool_lock:
        pool = _database_pool
        _database_pool = None
        _database_pool_url = None
    if pool is not None:
        pool.close()


def get_redis_client():
    global _redis_client, _redis_client_address
    host = os.getenv("REDIS_HOST", "").strip()
    port = os.getenv("REDIS_PORT", "").strip()
    if not host and not port:
        return None
    if not host or not port:
        raise RuntimeError("REDIS_HOST and REDIS_PORT must be set together.")

    address = (host, port)
    with _redis_client_lock:
        if _redis_client_address != address:
            if _redis_client is not None:
                _redis_client.close()
            _redis_client = Redis(
                host=host,
                port=int(port),
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
                max_connections=10,
            )
            _redis_client_address = address
        return _redis_client


def close_redis_client():
    global _redis_client, _redis_client_address
    with _redis_client_lock:
        client = _redis_client
        _redis_client = None
        _redis_client_address = None
    if client is not None:
        client.close()


def cached_product_list(cache_key):
    client = get_redis_client()
    if client is None:
        return None
    try:
        cached = client.get(cache_key)
    except RedisError as error:
        logger.warning("Redis cache read failed (%s)", type(error).__name__)
        return None
    if cached is None:
        return None
    try:
        products = json.loads(cached)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid Redis product cache entry")
        return None
    if not isinstance(products, list):
        logger.warning("Ignoring invalid Redis product cache entry")
        return None
    return products


def cache_product_list(cache_key, products):
    client = get_redis_client()
    if client is None:
        return
    try:
        client.setex(
            cache_key,
            PRODUCT_LIST_CACHE_TTL_SECONDS,
            json.dumps(products, default=str, separators=(",", ":")),
        )
    except RedisError as error:
        logger.warning("Redis cache write failed (%s)", type(error).__name__)


def invalidate_public_product_lists():
    client = get_redis_client()
    if client is None:
        return
    try:
        keys = []
        for key in client.scan_iter(
            match=f"{PUBLIC_PRODUCT_CACHE_PREFIX}*",
            count=100,
        ):
            keys.append(key)
            if len(keys) == 100:
                client.delete(*keys)
                keys.clear()
        if keys:
            client.delete(*keys)
    except RedisError as error:
        logger.warning("Redis product cache invalidation failed (%s)", type(error).__name__)


def connect_db():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]
        pool = get_database_pool(database_url)
        return DatabaseConnection(
            pool.getconn(timeout=DATABASE_POOL_TIMEOUT_SECONDS),
            postgres=True,
            release=pool.putconn,
        )

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return DatabaseConnection(connection)


@contextmanager
def db_connection():
    connection = connect_db()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def table_columns(connection, table):
    if connection.postgres:
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            (table,),
        ).fetchall()
        return {row["column_name"] for row in rows}
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def ensure_column(connection, table, column, declaration):
    if column not in table_columns(connection, table):
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
        )


def initialize_db():
    with db_connection() as connection:
        timestamp_type = "TIMESTAMPTZ" if connection.postgres else "TEXT"
        connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                shop_name TEXT NOT NULL DEFAULT '',
                store_slug TEXT NOT NULL DEFAULT '',
                store_description TEXT NOT NULL DEFAULT '',
                store_image_url TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email_verified INTEGER NOT NULL DEFAULT 0,
                verification_token_hash TEXT NOT NULL DEFAULT '',
                reset_token_hash TEXT NOT NULL DEFAULT '',
                reset_expires REAL NOT NULL DEFAULT 0,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_marketplace_admin
                ON users(role) WHERE role = 'ADMIN';
            CREATE TABLE IF NOT EXISTS businesses (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                name TEXT NOT NULL,
                business_type TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                settings_json TEXT NOT NULL DEFAULT '{{}}',
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS branches (
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                address TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS business_memberships (
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
                branch_id TEXT REFERENCES branches(id) ON DELETE SET NULL,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                permissions_json TEXT NOT NULL DEFAULT '[]',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (business_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS business_activity (
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
                branch_id TEXT REFERENCES branches(id) ON DELETE SET NULL,
                actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS business_assistant_conversations (
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
                branch_id TEXT REFERENCES branches(id) ON DELETE SET NULL,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS business_assistant_messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES business_assistant_conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('USER', 'ASSISTANT')),
                content TEXT NOT NULL,
                period_start TEXT NOT NULL DEFAULT '',
                period_end TEXT NOT NULL DEFAULT '',
                sources_json TEXT NOT NULL DEFAULT '[]',
                recommendations_json TEXT NOT NULL DEFAULT '[]',
                ai_status TEXT NOT NULL DEFAULT 'NOT_CONFIGURED',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL,
                user_agent TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                last_seen_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                amount INTEGER NOT NULL CHECK (amount > 0),
                seller_id TEXT REFERENCES users(id),
                cost_amount INTEGER NOT NULL DEFAULT 0,
                stock_quantity INTEGER NOT NULL DEFAULT 0,
                low_stock_threshold INTEGER NOT NULL DEFAULT 5,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS product_images (
                product_id TEXT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
                content_type TEXT NOT NULL,
                image_data {"BYTEA" if connection.postgres else "BLOB"} NOT NULL,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS product_gallery_images (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                sort_order INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                image_data {"BYTEA" if connection.postgres else "BLOB"} NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS product_gallery_by_product
                ON product_gallery_images(product_id, sort_order, id);
            CREATE TABLE IF NOT EXISTS product_videos (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                content_type TEXT NOT NULL,
                video_data {"BYTEA" if connection.postgres else "BLOB"} NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS product_videos_by_product
                ON product_videos(product_id, created_at, id);
            CREATE TABLE IF NOT EXISTS product_variants (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (product_id, label)
            );
            CREATE INDEX IF NOT EXISTS product_variants_by_product
                ON product_variants(product_id, label, id);
            CREATE TABLE IF NOT EXISTS categories (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                actor_id TEXT,
                actor_email TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS security_alerts (
                id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                category TEXT NOT NULL,
                risk_score INTEGER NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
                severity TEXT NOT NULL DEFAULT 'MEDIUM',
                status TEXT NOT NULL DEFAULT 'OPEN',
                reasons_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                detection_model TEXT NOT NULL DEFAULT 'RULES_V1',
                ai_used INTEGER NOT NULL DEFAULT 0,
                ai_risk_score INTEGER,
                ai_reasons_json TEXT NOT NULL DEFAULT '[]',
                ai_model TEXT NOT NULL DEFAULT '',
                ai_analyzed_at {timestamp_type},
                occurred_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at {timestamp_type},
                resolution_note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS security_alert_actions (
                id TEXT PRIMARY KEY,
                alert_id TEXT NOT NULL REFERENCES security_alerts(id) ON DELETE CASCADE,
                actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                previous_status TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS security_appeals (
                id TEXT PRIMARY KEY,
                alert_id TEXT NOT NULL REFERENCES security_alerts(id) ON DELETE CASCADE,
                appellant_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                admin_note TEXT NOT NULL DEFAULT '',
                resolved_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at {timestamp_type}
            );
            CREATE TABLE IF NOT EXISTS rate_limits (
                bucket_key TEXT PRIMARY KEY,
                window_started INTEGER NOT NULL,
                request_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                code_hash TEXT NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, code_hash)
            );
            CREATE TABLE IF NOT EXISTS account_images (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                content_type TEXT NOT NULL,
                image_data {"BYTEA" if connection.postgres else "BLOB"} NOT NULL,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS seller_documents (
                id TEXT PRIMARY KEY,
                seller_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id TEXT REFERENCES products(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                document_role TEXT NOT NULL DEFAULT '',
                expires_at {timestamp_type},
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                document_data {"BYTEA" if connection.postgres else "BLOB"} NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS backup_runs (
                id TEXT PRIMARY KEY,
                object_key TEXT NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS seller_kyc (
                seller_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                seller_type TEXT NOT NULL DEFAULT 'INDIVIDUAL',
                phone TEXT NOT NULL DEFAULT '',
                identity_name TEXT NOT NULL DEFAULT '',
                identity_number TEXT NOT NULL DEFAULT '',
                business_name TEXT NOT NULL DEFAULT '',
                business_registration_number TEXT NOT NULL DEFAULT '',
                tin_number TEXT NOT NULL DEFAULT '',
                business_license_number TEXT NOT NULL DEFAULT '',
                business_address TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'NOT_VERIFIED',
                rejection_reason TEXT NOT NULL DEFAULT '',
                reviewed_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                reviewed_at {timestamp_type},
                expires_at {timestamp_type},
                submitted_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS kyc_history (
                id TEXT PRIMARY KEY,
                seller_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS kyc_requirements (
                id TEXT PRIMARY KEY,
                seller_type TEXT NOT NULL,
                requirement_key TEXT NOT NULL,
                label TEXT NOT NULL,
                is_required INTEGER NOT NULL DEFAULT 1,
                document_kind TEXT NOT NULL DEFAULT '',
                expires_policy_days INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (seller_type, requirement_key)
            );
            CREATE TABLE IF NOT EXISTS product_questions (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                buyer_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                answered_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                answered_at {timestamp_type},
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS quantity_pricing (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                min_quantity INTEGER NOT NULL CHECK (min_quantity > 0),
                amount INTEGER NOT NULL CHECK (amount > 0),
                UNIQUE (product_id, min_quantity)
            );
            CREATE TABLE IF NOT EXISTS promotions (
                id TEXT PRIMARY KEY,
                seller_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id TEXT REFERENCES products(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                discount_percent INTEGER NOT NULL CHECK (discount_percent BETWEEN 1 AND 90),
                starts_at {timestamp_type},
                ends_at {timestamp_type},
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS system_alerts (
                id TEXT PRIMARY KEY,
                alert_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                acknowledged_at {timestamp_type}
            );
            CREATE INDEX IF NOT EXISTS system_alerts_by_created
                ON system_alerts(created_at DESC);
            CREATE TABLE IF NOT EXISTS login_attempts (
                email TEXT PRIMARY KEY,
                failed_count INTEGER NOT NULL DEFAULT 0,
                blocked_until REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                request_key TEXT NOT NULL UNIQUE,
                product_id TEXT NOT NULL REFERENCES products(id),
                seller_id TEXT REFERENCES users(id),
                amount INTEGER NOT NULL CHECK (amount > 0),
                buyer_name TEXT NOT NULL DEFAULT '',
                buyer_email TEXT NOT NULL DEFAULT '',
                buyer_phone TEXT NOT NULL DEFAULT '',
                buyer_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                payment_method TEXT NOT NULL DEFAULT '',
                bank_branch TEXT NOT NULL DEFAULT '',
                delivery_address TEXT NOT NULL DEFAULT '',
                delivery_status TEXT NOT NULL DEFAULT 'UNFULFILLED',
                delivery_carrier TEXT NOT NULL DEFAULT '',
                tracking_code TEXT NOT NULL DEFAULT '',
                delivery_fee INTEGER NOT NULL DEFAULT 0 CHECK (delivery_fee >= 0),
                delivered_at {timestamp_type},
                cancelled_at {timestamp_type},
                payment_status TEXT NOT NULL DEFAULT 'PENDING',
                checkout_url TEXT,
                provider_reference TEXT NOT NULL DEFAULT '',
                provider_payment_id TEXT NOT NULL DEFAULT '',
                product_cost INTEGER NOT NULL DEFAULT 0,
                provider_fee INTEGER NOT NULL DEFAULT 0,
                platform_fee INTEGER NOT NULL DEFAULT 0,
                seller_payout INTEGER NOT NULL DEFAULT 0,
                net_profit INTEGER NOT NULL DEFAULT 0,
                checkout_id TEXT,
                cart_request_key TEXT,
                expires_at DOUBLE PRECISION,
                completed_at {timestamp_type},
                archived_at {timestamp_type},
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS order_items (
                order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
                product_id TEXT NOT NULL REFERENCES products(id),
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_amount INTEGER NOT NULL CHECK (unit_amount > 0),
                unit_cost INTEGER NOT NULL DEFAULT 0 CHECK (unit_cost >= 0),
                variant_id TEXT NOT NULL DEFAULT '',
                variant_label TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (order_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS merchant_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                password TEXT NOT NULL,
                phone TEXT
            );
            CREATE TABLE IF NOT EXISTS wishlists (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, product_id)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                buyer_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                comment TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (order_id, product_id, buyer_id)
            );
            CREATE TABLE IF NOT EXISTS inventory_movements (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                seller_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                quantity_delta INTEGER NOT NULL CHECK (quantity_delta != 0),
                reason TEXT NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS delivery_agents (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                phone TEXT NOT NULL DEFAULT '',
                vehicle_type TEXT NOT NULL DEFAULT '',
                vehicle_plate TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS delivery_tracking_events (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                latitude REAL,
                longitude REAL,
                evidence_url TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS delivery_locations (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
                agent_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                party_role TEXT NOT NULL DEFAULT 'AGENT',
                party_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                accuracy_m REAL,
                is_live INTEGER NOT NULL DEFAULT 1,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS proof_of_delivery (
                order_id TEXT PRIMARY KEY REFERENCES orders(order_id) ON DELETE CASCADE,
                otp_hash TEXT NOT NULL DEFAULT '',
                otp_expires_at REAL NOT NULL DEFAULT 0,
                otp_attempts INTEGER NOT NULL DEFAULT 0,
                recipient_name TEXT NOT NULL DEFAULT '',
                received_at {timestamp_type},
                image_url TEXT NOT NULL DEFAULT '',
                latitude REAL,
                longitude REAL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS cancellation_requests (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id) ON DELETE CASCADE,
                requester_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                admin_note TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at {timestamp_type}
            );
            CREATE TABLE IF NOT EXISTS return_requests (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id) ON DELETE CASCADE,
                requester_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                admin_note TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at {timestamp_type}
            );
            CREATE TABLE IF NOT EXISTS seller_payouts (
                id TEXT PRIMARY KEY,
                seller_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL CHECK (amount > 0),
                payment_method TEXT NOT NULL,
                payment_reference TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                recorded_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (seller_id, payment_reference)
            );
            CREATE TABLE IF NOT EXISTS email_reminder_preferences (
                email TEXT PRIMARY KEY,
                opted_out_at {timestamp_type},
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS abandoned_checkout_reminders (
                id TEXT PRIMARY KEY,
                checkout_type TEXT NOT NULL,
                checkout_key TEXT NOT NULL,
                recipient TEXT NOT NULL,
                buyer_name TEXT NOT NULL DEFAULT '',
                product_id TEXT NOT NULL DEFAULT '',
                amount INTEGER NOT NULL DEFAULT 0,
                send_at DOUBLE PRECISION NOT NULL,
                next_attempt_at DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at {timestamp_type},
                UNIQUE (checkout_type, checkout_key)
            );
            CREATE TABLE IF NOT EXISTS email_unsubscribe_tokens (
                token_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL REFERENCES email_reminder_preferences(email) ON DELETE CASCADE,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS support_tickets (
                id TEXT PRIMARY KEY,
                user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                email TEXT NOT NULL,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                admin_response TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                email_enabled INTEGER NOT NULL DEFAULT 1,
                sms_enabled INTEGER NOT NULL DEFAULT 0,
                web_push_enabled INTEGER NOT NULL DEFAULT 1,
                mobile_push_enabled INTEGER NOT NULL DEFAULT 0,
                whatsapp_enabled INTEGER NOT NULL DEFAULT 0,
                security_alerts_enabled INTEGER NOT NULL DEFAULT 1,
                marketing_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                channel TEXT NOT NULL,
                subscription_json TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, subscription_json)
            );
            CREATE TABLE IF NOT EXISTS notification_history (
                id TEXT PRIMARY KEY,
                user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                event TEXT NOT NULL,
                channel TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED',
                provider_reference TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                link TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                delivered_at {timestamp_type}
            );
            CREATE TABLE IF NOT EXISTS coupons (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                discount_type TEXT NOT NULL CHECK (discount_type IN ('PERCENT', 'FIXED')),
                discount_value INTEGER NOT NULL CHECK (discount_value > 0),
                max_redemptions INTEGER,
                redemption_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS coupon_redemptions (
                coupon_id TEXT NOT NULL REFERENCES coupons(id) ON DELETE CASCADE,
                redemption_key TEXT NOT NULL,
                buyer_key TEXT NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (coupon_id, redemption_key),
                UNIQUE (coupon_id, buyer_key)
            );
            """
        )

        product_columns = {
            "seller_id": "TEXT",
            "business_id": "TEXT",
            "branch_id": "TEXT",
            "description": "TEXT NOT NULL DEFAULT ''",
            "image_url": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT ''",
            "subcategory": "TEXT NOT NULL DEFAULT ''",
            "video_url": "TEXT NOT NULL DEFAULT ''",
            "brand": "TEXT NOT NULL DEFAULT ''",
            "location_lat": "REAL",
            "location_lng": "REAL",
            "cost_amount": "INTEGER NOT NULL DEFAULT 0",
            "stock_quantity": "INTEGER NOT NULL DEFAULT 0",
            "low_stock_threshold": "INTEGER NOT NULL DEFAULT 5",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
        }
        user_columns = {
            "store_slug": "TEXT NOT NULL DEFAULT ''",
            "store_description": "TEXT NOT NULL DEFAULT ''",
            "store_image_url": "TEXT NOT NULL DEFAULT ''",
            "profile_image_url": "TEXT NOT NULL DEFAULT ''",
            "email_verified": "INTEGER NOT NULL DEFAULT 0",
            "verification_token_hash": "TEXT NOT NULL DEFAULT ''",
            "reset_token_hash": "TEXT NOT NULL DEFAULT ''",
            "reset_expires": "REAL NOT NULL DEFAULT 0",
            "mfa_secret": "TEXT NOT NULL DEFAULT ''",
            "mfa_pending_secret": "TEXT NOT NULL DEFAULT ''",
            "mfa_enabled": "INTEGER NOT NULL DEFAULT 0",
            "mfa_last_counter": "INTEGER NOT NULL DEFAULT -1",
        }
        last_seen_declaration = (
            "TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP"
            if connection.postgres
            else "TEXT NOT NULL DEFAULT ''"
        )
        session_columns = {
            "user_agent": "TEXT NOT NULL DEFAULT ''",
            "ip_address": "TEXT NOT NULL DEFAULT ''",
            "last_seen_at": last_seen_declaration,
        }
        order_columns = {
            "seller_id": "TEXT",
            "business_id": "TEXT",
            "branch_id": "TEXT",
            "delivery_agent_id": "TEXT",
            "carrier_name": "TEXT NOT NULL DEFAULT ''",
            "carrier_tracking_number": "TEXT NOT NULL DEFAULT ''",
            "carrier_status": "TEXT NOT NULL DEFAULT ''",
            "delivery_mode": "TEXT NOT NULL DEFAULT 'INTERNAL'",
            "buyer_name": "TEXT NOT NULL DEFAULT ''",
            "buyer_email": "TEXT NOT NULL DEFAULT ''",
            "buyer_phone": "TEXT NOT NULL DEFAULT ''",
            "buyer_id": "TEXT",
            "payment_method": "TEXT NOT NULL DEFAULT ''",
            "provider_reference": "TEXT NOT NULL DEFAULT ''",
            "provider_payment_id": "TEXT NOT NULL DEFAULT ''",
            "bank_branch": "TEXT NOT NULL DEFAULT ''",
            "delivery_address": "TEXT NOT NULL DEFAULT ''",
            "delivery_status": "TEXT NOT NULL DEFAULT 'UNFULFILLED'",
            "delivery_carrier": "TEXT NOT NULL DEFAULT ''",
            "tracking_code": "TEXT NOT NULL DEFAULT ''",
            "delivery_fee": "INTEGER NOT NULL DEFAULT 0",
            "delivered_at": timestamp_type,
            "cancelled_at": timestamp_type,
            "product_cost": "INTEGER NOT NULL DEFAULT 0",
            "provider_fee": "INTEGER NOT NULL DEFAULT 0",
            "platform_fee": "INTEGER NOT NULL DEFAULT 0",
            "seller_payout": "INTEGER NOT NULL DEFAULT 0",
            "net_profit": "INTEGER NOT NULL DEFAULT 0",
            "checkout_id": "TEXT",
            "cart_request_key": "TEXT",
            "expires_at": "DOUBLE PRECISION",
            "completed_at": timestamp_type,
            "archived_at": timestamp_type,
            "coupon_code": "TEXT NOT NULL DEFAULT ''",
            "coupon_discount": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in product_columns.items():
            ensure_column(connection, "products", column, declaration)
        ensure_column(connection, "categories", "parent_id", "TEXT")
        ensure_column(connection, "users", "kyc_status", "TEXT NOT NULL DEFAULT 'NOT_VERIFIED'")
        ensure_column(connection, "users", "preferred_language", "TEXT NOT NULL DEFAULT 'sw'")
        ensure_column(connection, "seller_documents", "document_role", "TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "seller_documents", "expires_at", timestamp_type)
        requirement_defaults = [
            ("INDIVIDUAL", "identity_name", "Majina ya utambulisho", 1, "identity"),
            ("INDIVIDUAL", "identity_number", "Namba ya kitambulisho kinachohitajika", 1, "identity"),
            ("INDIVIDUAL", "identity_document", "Nyaraka ya utambulisho", 1, "seller_verification"),
            ("BUSINESS", "business_name", "Jina la biashara", 1, "business"),
            ("BUSINESS", "business_registration_number", "Taarifa za usajili wa biashara", 1, "business"),
            ("BUSINESS", "tin_number", "TIN pale linapohitajika", 0, "business"),
            ("BUSINESS", "business_license_number", "Leseni ya biashara pale inapohitajika", 0, "business"),
            ("BUSINESS", "business_address", "Anwani ya biashara", 1, "business"),
            ("BUSINESS", "registration_document", "Nyaraka za usajili au uthibitisho", 1, "seller_verification"),
        ]
        for seller_type, key, label, required, document_kind in requirement_defaults:
            connection.execute(
                """
                INSERT INTO kyc_requirements
                    (id, seller_type, requirement_key, label, is_required, document_kind)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (seller_type, requirement_key) DO NOTHING
                """,
                (secrets.token_hex(16), seller_type, key, label, required, document_kind),
            )
        for column, declaration in user_columns.items():
            ensure_column(connection, "users", column, declaration)
        for column, declaration in session_columns.items():
            ensure_column(connection, "sessions", column, declaration)
        if not connection.postgres:
            connection.execute(
                "UPDATE sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE last_seen_at = ''"
            )
        existing_sellers = connection.execute(
            """
            SELECT id, shop_name FROM users
            WHERE role = 'SELLER' AND (store_slug IS NULL OR store_slug = '')
            """
        ).fetchall()
        for seller in existing_sellers:
            connection.execute(
                "UPDATE users SET store_slug = ? WHERE id = ?",
                (make_store_slug(seller["shop_name"], seller["id"]), seller["id"]),
            )
        for column, declaration in order_columns.items():
            ensure_column(connection, "orders", column, declaration)
        ensure_column(connection, "inventory_movements", "business_id", "TEXT")
        ensure_column(connection, "inventory_movements", "branch_id", "TEXT")
        existing_sellers = connection.execute(
            "SELECT id, shop_name, phone FROM users WHERE role = 'SELLER'"
        ).fetchall()
        for seller in existing_sellers:
            membership = connection.execute(
                "SELECT business_id FROM business_memberships WHERE user_id = ? LIMIT 1",
                (seller["id"],),
            ).fetchone()
            if membership:
                business_id = membership["business_id"]
                branch = connection.execute(
                    "SELECT id FROM branches WHERE business_id = ? ORDER BY created_at LIMIT 1",
                    (business_id,),
                ).fetchone()
                branch_id = branch["id"] if branch else None
                if branch_id:
                    connection.execute(
                        "UPDATE products SET business_id = ?, branch_id = ? WHERE seller_id = ? AND business_id IS NULL",
                        (business_id, branch_id, seller["id"]),
                    )
                    connection.execute(
                        "UPDATE orders SET business_id = ?, branch_id = ? WHERE seller_id = ? AND business_id IS NULL",
                        (business_id, branch_id, seller["id"]),
                    )
            else:
                business_id = secrets.token_hex(16)
                connection.execute(
                    """
                    INSERT INTO businesses
                        (id, owner_id, name, business_type, phone, status)
                    VALUES (?, ?, ?, 'RETAIL', ?, 'ACTIVE')
                    """,
                    (
                        business_id,
                        seller["id"],
                        seller["shop_name"] or f"Duka {seller['id'][:8]}",
                        seller["phone"] or "",
                    ),
                )
                branch_id = secrets.token_hex(16)
                connection.execute(
                    """
                    INSERT INTO branches (id, business_id, name)
                    VALUES (?, ?, 'Tawi Kuu')
                    """,
                    (branch_id, business_id),
                )
                connection.execute(
                    """
                    INSERT INTO business_memberships
                        (id, business_id, branch_id, user_id, role, status)
                    VALUES (?, ?, NULL, ?, 'OWNER', 'ACTIVE')
                    """,
                    (secrets.token_hex(16), business_id, seller["id"]),
                )
                connection.execute(
                    """
                    UPDATE products SET business_id = ?, branch_id = ?
                    WHERE seller_id = ?
                    """,
                    (business_id, branch_id, seller["id"]),
                )
                connection.execute(
                    """
                    UPDATE orders SET business_id = ?, branch_id = ?
                    WHERE seller_id = ?
                    """,
                    (business_id, branch_id, seller["id"]),
                )
        ensure_column(connection, "delivery_locations", "party_role", "TEXT NOT NULL DEFAULT 'AGENT'")
        ensure_column(connection, "delivery_locations", "party_user_id", "TEXT")
        missing_tracking = connection.execute(
            "SELECT order_id FROM orders WHERE tracking_code IS NULL OR tracking_code = ''"
        ).fetchall()
        for order_row in missing_tracking:
            for _ in range(20):
                tracking_code = f"LR-TZ-{date.today().year}-{secrets.randbelow(900000) + 100000}"
                existing = connection.execute(
                    "SELECT 1 FROM orders WHERE tracking_code = ?",
                    (tracking_code,),
                ).fetchone()
                if existing is None:
                    break
            else:
                continue
            connection.execute(
                "UPDATE orders SET tracking_code = ? WHERE order_id = ?",
                (tracking_code, order_row["order_id"]),
            )
        ensure_column(connection, "order_items", "variant_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "order_items", "variant_label", "TEXT NOT NULL DEFAULT ''")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS products_seller_idx ON products(seller_id)"
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_store_slug_idx
            ON users(store_slug) WHERE store_slug <> ''
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS orders_seller_status_idx ON orders(seller_id, payment_status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS order_items_order_idx ON order_items(order_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS seller_documents_seller_idx ON seller_documents(seller_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS security_alerts_subject_idx ON security_alerts(subject_type, subject_id, status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS security_alerts_score_idx ON security_alerts(risk_score, created_at)"
        )
        ensure_column(
            connection,
            "security_alert_actions",
            "previous_status",
            "TEXT NOT NULL DEFAULT ''",
        )
        ensure_column(connection, "security_alerts", "ai_risk_score", "INTEGER")
        ensure_column(
            connection,
            "security_alerts",
            "ai_reasons_json",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        ensure_column(
            connection,
            "security_alerts",
            "ai_model",
            "TEXT NOT NULL DEFAULT ''",
        )
        ensure_column(
            connection,
            "security_alerts",
            "ai_analyzed_at",
            f"{timestamp_type}",
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS abandoned_checkout_reminders_due_idx
            ON abandoned_checkout_reminders(status, next_attempt_at)
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS orders_provider_reference_idx
            ON orders(provider_reference) WHERE provider_reference <> ''
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS orders_checkout_seller_idx
            ON orders(checkout_id, seller_id)
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS products_business_branch_idx ON products(business_id, branch_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS orders_business_branch_idx ON orders(business_id, branch_id)"
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS orders_tracking_code_idx
            ON orders(tracking_code) WHERE tracking_code <> ''
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS delivery_events_order_idx ON delivery_tracking_events(order_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS delivery_locations_order_idx ON delivery_locations(order_id, created_at)"
        )


@asynccontextmanager
async def lifespan(_app):
    backup_task = None
    reminder_worker = None
    try:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if database_url:
            if database_url.startswith("postgres://"):
                database_url = "postgresql://" + database_url[len("postgres://"):]
            get_database_pool(database_url)
        initialize_db()
        from backend.backup import run_backup_scheduler

        backup_task = asyncio.create_task(run_backup_scheduler())
        reminder_worker = asyncio.create_task(abandoned_checkout_reminder_worker())
        yield
    finally:
        if backup_task is not None:
            backup_task.cancel()
            try:
                await backup_task
            except asyncio.CancelledError:
                pass
        if reminder_worker is not None:
            reminder_worker.cancel()
            try:
                await reminder_worker
            except asyncio.CancelledError:
                pass
        close_database_pool()
        close_redis_client()


app = FastAPI(title="LipaRahisi API", lifespan=lifespan)

_rate_limit_cleanup_at = 0
_rate_limit_cleanup_lock = threading.Lock()


def rate_limit_policy(path):
    if path.startswith("/api/auth/login"):
        return "login", 10
    if path.startswith("/api/auth/"):
        return "auth", 30
    if path.endswith("/image") or path == "/api/documents":
        return "uploads", 12
    if path.endswith("/checkout") or path == "/api/cart/checkout":
        return "checkout", 30
    return "api", 240


def check_rate_limit(request):
    global _rate_limit_cleanup_at
    client_ip = request.client.host if request.client else "unknown"
    bucket, limit = rate_limit_policy(request.url.path)
    window = int(time.time() // 60)
    key = hashlib.sha256(f"{client_ip}:{bucket}".encode("utf-8")).hexdigest()

    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO rate_limits (bucket_key, window_started, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT (bucket_key) DO UPDATE SET
                request_count = CASE
                    WHEN rate_limits.window_started != excluded.window_started
                    THEN 1 ELSE rate_limits.request_count + 1
                END,
                window_started = excluded.window_started
            """,
            (key, window),
        )
        count = connection.execute(
            "SELECT request_count FROM rate_limits WHERE bucket_key = ?",
            (key,),
        ).fetchone()["request_count"]
        now = time.time()
        if now - _rate_limit_cleanup_at > 300:
            with _rate_limit_cleanup_lock:
                if now - _rate_limit_cleanup_at > 300:
                    connection.execute(
                        "DELETE FROM rate_limits WHERE window_started < ?",
                        (window - 2,),
                    )
                    _rate_limit_cleanup_at = now

    if count > limit:
        retry_after = max(1, int((window + 1) * 60 - time.time()))
        return JSONResponse(
            status_code=429,
            content={"detail": "Umefanya maombi mengi. Jaribu tena baada ya muda mfupi."},
            headers={"Retry-After": str(retry_after)},
        )
    return None


@app.middleware("http")
async def protect_api_and_measure_errors(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        response = check_rate_limit(request)
        if response is not None:
            return response
    try:
        response = await call_next(request)
    except Exception:
        with _server_error_lock:
            _server_error_times.append(time.time())
        logger.exception("Unhandled API error for %s %s", request.method, request.url.path)
        raise
    if response.status_code >= 500:
        with _server_error_lock:
            _server_error_times.append(time.time())
    return response


class AdminSetupInput(BaseModel):
    setup_token: str = Field(min_length=16, max_length=256)
    full_name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class SellerRegistrationInput(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)
    shop_name: str = Field(min_length=2, max_length=100)
    phone: str | None = Field(default=None, max_length=20)


class CustomerRegistrationInput(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)
    phone: str | None = Field(default=None, max_length=20)


class LoginInput(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    totp_code: str = Field(default="", max_length=32)


class TotpCodeInput(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class MfaDisableInput(TotpCodeInput):
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeInput(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class PasswordResetRequestInput(BaseModel):
    email: str = Field(min_length=5, max_length=254)

    @validator("email")
    def validate_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Weka barua pepe sahihi.")
        return value


class PasswordResetInput(BaseModel):
    reset_token: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=10, max_length=128)


class SellerStatusInput(BaseModel):
    status: Literal["ACTIVE", "REJECTED", "SUSPENDED"]


class CategoryInput(BaseModel):
    name: str = Field(min_length=2, max_length=60)


class ProductVariantInput(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    stock_quantity: int = Field(ge=0, le=999999999)

    @validator("label")
    def strip_variant_label(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Jina la variant linahitajika.")
        return value


class ProductInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    image_url: str = Field(default="", max_length=2048)
    category: str = Field(default="", max_length=60)
    subcategory: str = Field(default="", max_length=60)
    brand: str = Field(default="", max_length=100)
    video_url: str = Field(default="", max_length=2048)
    amount: int = Field(gt=0, le=999999999)
    cost_amount: int = Field(default=0, ge=0, le=999999999)
    stock_quantity: int = Field(default=0, ge=0, le=999999999)
    low_stock_threshold: int = Field(default=5, ge=0, le=999999999)
    variants: list[ProductVariantInput] = Field(default_factory=list, max_items=100)

    @validator("name")
    def strip_product_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Jina la bidhaa linahitajika.")
        return value

    @validator("category")
    def normalize_category(cls, value):
        return value.strip()

    @validator("variants")
    def validate_variants(cls, variants):
        labels = [variant.label.casefold() for variant in variants]
        if len(labels) != len(set(labels)):
            raise ValueError("Majina ya variant yanapaswa kuwa tofauti.")
        if sum(variant.stock_quantity for variant in variants) > 999999999:
            raise ValueError("Jumla ya stock ya variant imezidi kikomo.")
        return variants

    @validator("image_url")
    def validate_image_url(cls, value):
        value = value.strip()
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Picha lazima iwe na URL ya HTTP au HTTPS.")
        return value


class ProductUpdateInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    image_url: str | None = Field(default=None, max_length=2048)
    category: str | None = Field(default=None, max_length=60)
    subcategory: str | None = Field(default=None, max_length=60)
    brand: str | None = Field(default=None, max_length=100)
    video_url: str | None = Field(default=None, max_length=2048)
    amount: int | None = Field(default=None, gt=0, le=999999999)
    cost_amount: int | None = Field(default=None, ge=0, le=999999999)
    stock_quantity: int | None = Field(default=None, ge=0, le=999999999)
    low_stock_threshold: int | None = Field(default=None, ge=0, le=999999999)

    @validator("name")
    def strip_product_name(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Jina la bidhaa linahitajika.")
        return value

    @validator("image_url")
    def validate_image_url(cls, value):
        if value is None or value == "":
            return value
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Picha lazima iwe na URL ya HTTP au HTTPS.")
        return value.strip()

    @validator("category")
    def normalize_category(cls, value):
        return value.strip() if value is not None else None


class ProductVariantStockInput(BaseModel):
    stock_quantity: int = Field(ge=0, le=999999999)


class ProductCsvInput(BaseModel):
    csv_text: str = Field(min_length=1, max_length=1_000_000)


class StorefrontInput(BaseModel):
    shop_name: str = Field(min_length=2, max_length=100)
    store_description: str = Field(default="", max_length=1000)
    store_image_url: str = Field(default="", max_length=2048)


class KycSubmitInput(BaseModel):
    seller_type: Literal["INDIVIDUAL", "BUSINESS"]
    phone: str = Field(min_length=9, max_length=20)
    identity_name: str = Field(default="", max_length=100)
    identity_number: str = Field(default="", max_length=100)
    business_name: str = Field(default="", max_length=150)
    business_registration_number: str = Field(default="", max_length=100)
    tin_number: str = Field(default="", max_length=100)
    business_license_number: str = Field(default="", max_length=100)
    business_address: str = Field(default="", max_length=500)

    @validator("phone")
    def normalize_kyc_phone(cls, value):
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10 and digits.startswith("0"):
            digits = "255" + digits[1:]
        if not re.fullmatch(r"255\d{9}", digits):
            raise ValueError("Weka namba ya simu ya Tanzania, mfano 07XX XXX XXX.")
        return digits


class KycReviewInput(BaseModel):
    status: Literal["UNDER_REVIEW", "VERIFIED", "REJECTED", "RESUBMISSION_REQUIRED"]
    reason: str = Field(default="", max_length=1000)
    expires_at: str = Field(default="", max_length=40)


class KycRequirementInput(BaseModel):
    seller_type: Literal["INDIVIDUAL", "BUSINESS"]
    requirement_key: str = Field(min_length=2, max_length=100)
    label: str = Field(min_length=2, max_length=200)
    is_required: bool = True
    document_kind: str = Field(default="", max_length=100)
    expires_policy_days: int = Field(default=0, ge=0, le=3650)
    is_active: bool = True


class ProductQuestionInput(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class ProductQuestionAnswerInput(BaseModel):
    answer: str = Field(min_length=2, max_length=1000)


class DeliveryUpdateInput(BaseModel):
    delivery_status: Literal[
        "UNFULFILLED", "PROCESSING", "ORDER_CONFIRMED", "SELLER_PREPARING",
        "READY_FOR_PICKUP", "AGENT_ASSIGNED", "PICKED_UP", "IN_TRANSIT",
        "NEAR_DESTINATION", "OUT_FOR_DELIVERY", "DELIVERED",
        "DELIVERY_FAILED", "RETURNED", "SHIPPED",
    ]
    tracking_code: str = Field(default="", max_length=100)
    delivery_carrier: str = Field(default="", max_length=100)
    delivery_fee: int = Field(default=0, ge=0, le=999999999)
    description: str = Field(default="", max_length=1000)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class DeliveryLocationInput(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)
    sharing_consent: bool = Field(default=True)


class ProofOfDeliveryInput(BaseModel):
    otp: str = Field(min_length=4, max_length=10)
    recipient_name: str = Field(min_length=2, max_length=100)
    image_url: str = Field(default="", max_length=2048)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class BuyerDeliveryConfirmationInput(BaseModel):
    otp: str = Field(min_length=4, max_length=10)
    recipient_name: str = Field(min_length=2, max_length=100)


class ExternalCarrierUpdateInput(BaseModel):
    carrier_name: str = Field(min_length=2, max_length=100)
    carrier_tracking_number: str = Field(min_length=3, max_length=100)
    carrier_status: str = Field(default="", max_length=100)


class DeliveryAgentAssignmentInput(BaseModel):
    agent_user_id: str = Field(min_length=1, max_length=64)


class DeliveryAgentCreateInput(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=10, max_length=128)
    phone: str = Field(min_length=9, max_length=20)
    vehicle_type: str = Field(default="", max_length=50)
    vehicle_plate: str = Field(default="", max_length=30)

    @validator("phone")
    def normalize_delivery_phone(cls, value):
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10 and digits.startswith("0"):
            digits = "255" + digits[1:]
        if not re.fullmatch(r"255\d{9}", digits):
            raise ValueError("Weka namba ya simu ya Tanzania.")
        return digits


class CancellationInput(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


class CancellationResolutionInput(BaseModel):
    status: Literal["APPROVED", "REJECTED"]
    admin_note: str = Field(default="", max_length=1000)


class ReturnRequestInput(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)

    @validator("reason")
    def normalize_reason(cls, value):
        normalized = value.strip()
        if len(normalized) < 5:
            raise ValueError("Sababu lazima iwe na angalau herufi 5.")
        return normalized


class ReturnResolutionInput(BaseModel):
    status: Literal["APPROVED", "REJECTED"]
    admin_note: str = Field(default="", max_length=1000)


class PayoutEntryInput(BaseModel):
    seller_id: str = Field(min_length=1, max_length=64)
    amount: int = Field(gt=0, le=999999999)
    payment_method: Literal["MOBILE_MONEY", "BANK_TRANSFER", "CASH", "OTHER"]
    payment_reference: str = Field(min_length=3, max_length=100)
    note: str = Field(default="", max_length=500)

    @validator("payment_reference")
    def normalize_payment_reference(cls, value):
        normalized = value.strip()
        if len(normalized) < 3:
            raise ValueError("Rejea ya malipo lazima iwe na angalau herufi 3.")
        return normalized


class ReviewInput(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


class ReviewModerationInput(BaseModel):
    status: Literal["APPROVED", "REJECTED"]


class SupportTicketInput(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    subject: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=10, max_length=4000)


class SupportTicketUpdateInput(BaseModel):
    status: Literal["OPEN", "IN_PROGRESS", "RESOLVED"]
    admin_response: str = Field(default="", max_length=4000)


class NotificationPreferenceInput(BaseModel):
    email_enabled: bool = True
    sms_enabled: bool = False
    web_push_enabled: bool = True
    mobile_push_enabled: bool = False
    whatsapp_enabled: bool = False
    security_alerts_enabled: bool = True
    marketing_enabled: bool = False


class PushSubscriptionInput(BaseModel):
    channel: Literal["WEB_PUSH", "MOBILE_PUSH"]
    subscription: dict = Field(default_factory=dict)


class BusinessInput(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    business_type: str = Field(min_length=2, max_length=100)
    phone: str = Field(default="", max_length=20)
    email: str = Field(default="", max_length=254)
    address: str = Field(default="", max_length=500)
    settings: dict = Field(default_factory=dict)


class LanguagePreferenceInput(BaseModel):
    language: Literal["sw", "en", "fr"]


class BusinessAssistantMessageInput(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    conversation_id: str | None = Field(default=None, max_length=64)


class BranchInput(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    address: str = Field(default="", max_length=500)
    phone: str = Field(default="", max_length=20)
    email: str = Field(default="", max_length=254)
    is_active: bool = True


class MembershipInput(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    role: Literal["OWNER", "BRANCH_MANAGER", "STAFF"]
    branch_id: str | None = Field(default=None, max_length=64)


class MembershipUpdateInput(BaseModel):
    role: Literal["OWNER", "BRANCH_MANAGER", "STAFF"]
    branch_id: str | None = Field(default=None, max_length=64)
    status: Literal["ACTIVE", "SUSPENDED"] = "ACTIVE"


class FraudAlertActionInput(BaseModel):
    action: Literal["WARN", "ACCOUNT_REVIEW", "SUSPEND", "RESTORE", "FALSE_POSITIVE", "RESOLVE"]
    note: str = Field(min_length=2, max_length=2000)


class SecurityAppealInput(BaseModel):
    message: str = Field(min_length=10, max_length=2000)


class SecurityAppealReviewInput(BaseModel):
    status: Literal["ACCEPTED", "REJECTED"]
    note: str = Field(min_length=2, max_length=2000)


class CouponInput(BaseModel):
    code: str = Field(min_length=3, max_length=32)
    discount_type: Literal["PERCENT", "FIXED"]
    discount_value: int = Field(gt=0, le=999999999)
    max_redemptions: int | None = Field(default=None, ge=1, le=999999999)

    @validator("code")
    def normalize_coupon_code(cls, value):
        code = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,31}", code):
            raise ValueError("Msimbo wa kuponi utumie herufi, namba, - au _ pekee.")
        return code

    @validator("discount_value")
    def validate_coupon_discount(cls, value, values):
        if values.get("discount_type") == "PERCENT" and value > 100:
            raise ValueError("Punguzo la asilimia haliwezi kuzidi 100.")
        return value


class CouponStatusInput(BaseModel):
    is_active: bool


class CheckoutInput(BaseModel):
    request_key: str = Field(min_length=32, max_length=64)
    buyer_name: str = Field(min_length=2, max_length=100)
    buyer_email: str = Field(min_length=5, max_length=254)
    buyer_phone: str = Field(min_length=9, max_length=20)
    payment_method: str
    bank_branch: str | None = Field(default=None, max_length=100)
    delivery_address: str = Field(default="", max_length=500)
    coupon_code: str = Field(default="", max_length=32)
    variant_id: str | None = Field(default=None, max_length=64)
    abandoned_checkout_consent: bool = False

    @validator("buyer_name")
    def strip_buyer_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Jina la mnunuzi linahitajika.")
        return value

    @validator("buyer_email")
    def validate_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Weka barua pepe sahihi.")
        return value

    @validator("buyer_phone")
    def normalize_tanzania_phone(cls, value):
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10 and digits.startswith("0"):
            digits = "255" + digits[1:]
        if not re.fullmatch(r"255\d{9}", digits):
            raise ValueError("Weka namba ya simu ya Tanzania, mfano 07XX XXX XXX.")
        return digits

    @validator("payment_method")
    def validate_payment_method(cls, value):
        if value not in PAYMENT_METHODS:
            raise ValueError("Chagua mtandao au huduma ya benki iliyopo.")
        return value

    @validator("coupon_code")
    def normalize_checkout_coupon_code(cls, value):
        return value.strip().upper()

    @validator("variant_id")
    def normalize_variant_id(cls, value):
        return value.strip() if value else None


class CartItemInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=99)
    variant_id: str | None = Field(default=None, max_length=64)


class CartCheckoutInput(CheckoutInput):
    items: list[CartItemInput] = Field(min_items=1, max_items=100)


def clean_identity(full_name, email):
    name = full_name.strip()
    address = email.strip().lower()
    if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise HTTPException(status_code=422, detail="Jina au barua pepe si sahihi.")
    return name, address


def make_store_slug(value, suffix=""):
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if suffix:
        slug = f"{slug or 'duka'}-{suffix[:8].lower()}"
    return slug[:80]


def normalize_phone(value):
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10 and digits.startswith("0"):
        digits = "255" + digits[1:]
    if not re.fullmatch(r"255\d{9}", digits):
        raise HTTPException(
            status_code=422,
            detail="Weka namba sahihi ya simu ya Tanzania.",
        )
    return digits


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return "pbkdf2_sha256:310000:{}:{}".format(
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def create_email_verification_token():
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, token_hash


def send_smtp_email(to_email, subject, text, html=None):
    notification_service_url = os.getenv("NOTIFICATION_SERVICE_URL", "").strip()
    if notification_service_url:
        service_token = os.getenv("NOTIFICATION_SERVICE_TOKEN", "").strip()
        if not service_token:
            raise HTTPException(
                status_code=503,
                detail="NOTIFICATION_SERVICE_TOKEN haijawekwa.",
            )
        try:
            with httpx.Client(timeout=5.0) as client:
                notification = {
                    "to_email": to_email,
                    "subject": subject,
                    "text": text,
                }
                if html:
                    notification["html"] = html
                response = client.post(
                    f"{notification_service_url.rstrip('/')}/internal/emails",
                    headers={"X-Internal-Token": service_token},
                    json=notification,
                )
                response.raise_for_status()
        except httpx.TimeoutException as error:
            raise HTTPException(
                status_code=503,
                detail="Huduma ya barua pepe haijajibu kwa wakati.",
            ) from error
        except httpx.HTTPStatusError as error:
            raise HTTPException(
                status_code=502,
                detail="Huduma ya barua pepe imekataa ombi.",
            ) from error
        except httpx.RequestError as error:
            raise HTTPException(
                status_code=503,
                detail="Imeshindikana kuwasiliana na huduma ya barua pepe.",
            ) from error
        return

    try:
        deliver_email(to_email, subject, text, html)
    except EmailDeliveryError as error:
        raise HTTPException(
            status_code=502 if email_is_configured() else 503,
            detail=str(error),
        ) from error


def send_notification_email(event, to_email, content):
    try:
        send_smtp_email(to_email, content.subject, content.text, content.html)
    except HTTPException as error:
        logger.warning(
            "Email notification failed: %s (HTTP %s).",
            event,
            error.status_code,
        )
        message = "Barua pepe ya mfumo imeshindwa kutumwa."
        record_system_alert("EMAIL_DELIVERY_FAILED", message)
        send_system_alert_email("LipaRahisi: email imeshindwa", message)
        return False
    logger.info("Email notification queued for delivery: %s", event)
    return True


def send_app_email(event, to_email, content_builder, request_base_url=None):
    try:
        app_url = public_app_url(request_base_url)
    except HTTPException as error:
        logger.warning(
            "Email notification skipped: %s (HTTP %s).",
            event,
            error.status_code,
        )
        return False
    return send_notification_email(event, to_email, content_builder(app_url))


def send_channel_notification(
    event,
    channel,
    recipient,
    context=None,
    link="",
    language="sw",
):
    recipient = (recipient or "").strip()
    if not recipient:
        return False
    service_url = os.getenv("NOTIFICATION_SERVICE_URL", "").strip()
    if not service_url:
        logger.warning("Notification skipped: service URL not configured (%s).", event)
        return False
    service_token = os.getenv("NOTIFICATION_SERVICE_TOKEN", "").strip()
    if not service_token:
        logger.warning("Notification skipped: service token not configured (%s).", event)
        return False
    template = notification_template(event, context, language)
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.post(
                f"{service_url.rstrip('/')}/internal/notifications",
                headers={"X-Internal-Token": service_token},
                json={
                    "event": event,
                    "channel": channel,
                    "recipient": recipient,
                    "title": template["title"],
                    "text": template["text"],
                    "html": template["html"] or None,
                    "link": link,
                },
            )
            response.raise_for_status()
            result = response.json()
            return bool(result.get("notification_id"))
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError):
        logger.warning("Notification queue rejected event=%s channel=%s", event, channel)
        return False


def queue_user_notification(
    user_id,
    event,
    context=None,
    link="",
    email=None,
    phone=None,
):
    queued = []
    with db_connection() as connection:
        language_row = connection.execute(
            "SELECT preferred_language FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        language = (
            language_row["preferred_language"]
            if language_row and language_row["preferred_language"] in {"sw", "en", "fr"}
            else "sw"
        )
        template = notification_template(event, context, language)
        prefs = connection.execute(
            "SELECT * FROM notification_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        prefs = dict(prefs) if prefs else {
            "email_enabled": 1,
            "sms_enabled": 0,
            "web_push_enabled": 1,
            "mobile_push_enabled": 0,
            "whatsapp_enabled": 0,
            "security_alerts_enabled": 1,
        }
        if event in {"SECURITY_ALERT", "SUSPICIOUS_ACTIVITY"} and not prefs.get("security_alerts_enabled", 1):
            return queued
        if prefs["email_enabled"] and email:
            if send_channel_notification(event, "EMAIL", email, context, link, language):
                queued.append("EMAIL")
        if prefs["sms_enabled"] and phone:
            if send_channel_notification(event, "SMS", phone, context, link, language):
                queued.append("SMS")
        if prefs["whatsapp_enabled"] and phone:
            if send_channel_notification(event, "WHATSAPP", phone, context, link, language):
                queued.append("WHATSAPP")
        if prefs["web_push_enabled"] or prefs["mobile_push_enabled"]:
            subscriptions = connection.execute(
                """
                SELECT channel, subscription_json
                FROM push_subscriptions
                WHERE user_id = ? AND is_active = 1
                """,
                (user_id,),
            ).fetchall()
            for subscription in subscriptions:
                channel = subscription["channel"]
                if channel == "WEB_PUSH" and prefs["web_push_enabled"]:
                    if send_channel_notification(
                        event,
                        "WEB_PUSH",
                        subscription["subscription_json"],
                        context,
                        link,
                        language,
                    ):
                        queued.append("WEB_PUSH")
                if channel == "MOBILE_PUSH" and prefs["mobile_push_enabled"]:
                    if send_channel_notification(
                        event,
                        "MOBILE_PUSH",
                        subscription["subscription_json"],
                        context,
                        link,
                        language,
                    ):
                        queued.append("MOBILE_PUSH")
        if queued:
            for channel in queued:
                connection.execute(
                    """
                    INSERT INTO notification_history
                        (id, user_id, event, channel, title, body, status, link)
                    VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?)
                    """,
                    (
                        secrets.token_hex(16),
                        user_id,
                        event,
                        channel,
                        template["title"],
                        template["text"],
                        link,
                    ),
                )
    return queued


def schedule_abandoned_checkout_reminder(
    connection,
    checkout_type,
    checkout_key,
    recipient,
    buyer_name,
    amount,
    product_id="",
    consent=False,
):
    if not consent:
        return
    email = recipient.strip().lower()
    now = time.time()
    connection.execute(
        """
        INSERT INTO email_reminder_preferences (email)
        VALUES (?)
        ON CONFLICT(email) DO UPDATE SET
            opted_out_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        """,
        (email,),
    )
    connection.execute(
        """
        INSERT INTO abandoned_checkout_reminders
            (id, checkout_type, checkout_key, recipient, buyer_name, product_id,
             amount, send_at, next_attempt_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            secrets.token_hex(16),
            checkout_type,
            checkout_key,
            email,
            buyer_name.strip(),
            product_id,
            amount,
            now + ABANDONED_CHECKOUT_REMINDER_DELAY_SECONDS,
            now + ABANDONED_CHECKOUT_REMINDER_DELAY_SECONDS,
        ),
    )


def cancel_checkout_reminders_for_order(connection, order):
    connection.execute(
        """
        UPDATE abandoned_checkout_reminders
        SET status = 'CANCELLED'
        WHERE status IN ('PENDING', 'SENDING')
          AND (
              (checkout_type = 'ORDER' AND checkout_key = ?)
              OR (
                  ? <> ''
                  AND checkout_type = 'CART'
                  AND checkout_key = ?
              )
          )
        """,
        (
            order["order_id"],
            order.get("checkout_id") or "",
            order.get("checkout_id") or "",
        ),
    )


def claim_abandoned_checkout_reminder(now=None):
    current_time = time.time() if now is None else now
    with db_connection() as connection:
        reminder = connection.execute(
            """
            SELECT id FROM abandoned_checkout_reminders
            WHERE status IN ('PENDING', 'SENDING') AND next_attempt_at <= ?
            ORDER BY next_attempt_at, created_at
            LIMIT 1
            """,
            (current_time,),
        ).fetchone()
        if reminder is None:
            return None
        claimed = connection.execute(
            """
            UPDATE abandoned_checkout_reminders
            SET status = 'SENDING',
                attempts = CASE WHEN status = 'PENDING' THEN attempts + 1 ELSE attempts END,
                next_attempt_at = ?
            WHERE id = ? AND status IN ('PENDING', 'SENDING') AND next_attempt_at <= ?
            """,
            (
                current_time + ABANDONED_CHECKOUT_REMINDER_LEASE_SECONDS,
                reminder["id"],
                current_time,
            ),
        )
        if claimed.rowcount != 1:
            return None
        row = connection.execute(
            "SELECT * FROM abandoned_checkout_reminders WHERE id = ?",
            (reminder["id"],),
        ).fetchone()
        return dict(row) if row is not None else None


def finish_abandoned_checkout_reminder(reminder_id, sent, now=None):
    current_time = time.time() if now is None else now
    with db_connection() as connection:
        reminder = connection.execute(
            """
            SELECT attempts FROM abandoned_checkout_reminders
            WHERE id = ? AND status = 'SENDING'
            """,
            (reminder_id,),
        ).fetchone()
        if reminder is None:
            return
        if sent:
            connection.execute(
                """
                UPDATE abandoned_checkout_reminders
                SET status = 'SENT', sent_at = CURRENT_TIMESTAMP, last_error = ''
                WHERE id = ? AND status = 'SENDING'
                """,
                (reminder_id,),
            )
        elif reminder["attempts"] >= ABANDONED_CHECKOUT_REMINDER_MAX_ATTEMPTS:
            connection.execute(
                """
                UPDATE abandoned_checkout_reminders
                SET status = 'FAILED', last_error = 'Email delivery failed'
                WHERE id = ? AND status = 'SENDING'
                """,
                (reminder_id,),
            )
        else:
            retry_delay = min(60 * (2 ** reminder["attempts"]), 60 * 60)
            connection.execute(
                """
                UPDATE abandoned_checkout_reminders
                SET status = 'PENDING', next_attempt_at = ?,
                    last_error = 'Email delivery failed'
                WHERE id = ? AND status = 'SENDING'
                """,
                (current_time + retry_delay, reminder_id),
            )


def process_due_abandoned_checkout_reminders(now=None, limit=20):
    current_time = time.time() if now is None else now
    expire_pending_orders()
    processed = 0
    while processed < limit:
        reminder = claim_abandoned_checkout_reminder(current_time)
        if reminder is None:
            break
        processed += 1

        with db_connection() as connection:
            preference = connection.execute(
                """
                SELECT opted_out_at FROM email_reminder_preferences
                WHERE email = ?
                """,
                (reminder["recipient"],),
            ).fetchone()
            if preference is None or preference["opted_out_at"] is not None:
                connection.execute(
                    """
                    UPDATE abandoned_checkout_reminders
                    SET status = 'UNSUBSCRIBED'
                    WHERE id = ? AND status = 'SENDING'
                    """,
                    (reminder["id"],),
                )
                continue

            if reminder["checkout_type"] == "ORDER":
                orders = connection.execute(
                    """
                    SELECT order_id, payment_status, expires_at
                    FROM orders WHERE order_id = ?
                    """,
                    (reminder["checkout_key"],),
                ).fetchall()
            else:
                orders = connection.execute(
                    """
                    SELECT order_id, payment_status, expires_at
                    FROM orders WHERE checkout_id = ?
                    """,
                    (reminder["checkout_key"],),
                ).fetchall()

            if not orders:
                connection.execute(
                    """
                    UPDATE abandoned_checkout_reminders SET status = 'CANCELLED'
                    WHERE id = ? AND status = 'SENDING'
                    """,
                    (reminder["id"],),
                )
                continue

            has_open_cancellation = any(
                connection.execute(
                    """
                    SELECT 1 FROM cancellation_requests
                    WHERE order_id = ? AND status IN ('PENDING', 'APPROVED')
                    LIMIT 1
                    """,
                    (order["order_id"],),
                ).fetchone()
                for order in orders
            )
            if has_open_cancellation:
                connection.execute(
                    """
                    UPDATE abandoned_checkout_reminders SET status = 'CANCELLED'
                    WHERE id = ? AND status = 'SENDING'
                    """,
                    (reminder["id"],),
                )
                continue

            if any(order["payment_status"] != "EXPIRED" for order in orders):
                if all(
                    order["payment_status"] == "PENDING"
                    and order["expires_at"] is not None
                    and order["expires_at"] > current_time
                    for order in orders
                ):
                    next_attempt_at = max(
                        order["expires_at"] for order in orders
                    ) + 1
                    connection.execute(
                        """
                        UPDATE abandoned_checkout_reminders
                        SET status = 'PENDING', next_attempt_at = ?,
                            attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END
                        WHERE id = ? AND status = 'SENDING'
                        """,
                        (next_attempt_at, reminder["id"]),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE abandoned_checkout_reminders SET status = 'CANCELLED'
                        WHERE id = ? AND status = 'SENDING'
                        """,
                        (reminder["id"],),
                    )
                continue

            if reminder["checkout_type"] == "ORDER":
                product_id = reminder["product_id"]
                resume_url_path = f"/pay/{quote(product_id)}"
                items = connection.execute(
                    """
                    SELECT p.name, oi.quantity
                    FROM orders o
                    JOIN order_items oi ON oi.order_id = o.order_id
                    LEFT JOIN products p ON p.id = oi.product_id
                    WHERE o.order_id = ?
                    ORDER BY o.created_at, p.name
                    """,
                    (reminder["checkout_key"],),
                ).fetchall()
            else:
                resume_url_path = "/"
                items = connection.execute(
                    """
                    SELECT p.name, oi.quantity
                    FROM orders o
                    JOIN order_items oi ON oi.order_id = o.order_id
                    LEFT JOIN products p ON p.id = oi.product_id
                    WHERE o.checkout_id = ?
                    ORDER BY o.created_at, p.name
                    """,
                    (reminder["checkout_key"],),
                ).fetchall()

            current_reminder = connection.execute(
                "SELECT status FROM abandoned_checkout_reminders WHERE id = ?",
                (reminder["id"],),
            ).fetchone()
            current_preference = connection.execute(
                """
                SELECT opted_out_at FROM email_reminder_preferences
                WHERE email = ?
                """,
                (reminder["recipient"],),
            ).fetchone()
            if current_reminder is None or current_reminder["status"] != "SENDING":
                continue
            if current_preference is None or current_preference["opted_out_at"] is not None:
                connection.execute(
                    """
                    UPDATE abandoned_checkout_reminders
                    SET status = 'UNSUBSCRIBED'
                    WHERE id = ? AND status = 'SENDING'
                    """,
                    (reminder["id"],),
                )
                continue

        product_names = ", ".join(
            dict.fromkeys(
                f"{item['name'] or 'Bidhaa'} x{item['quantity']}" for item in items
            )
        ) or "Bidhaa ulizochagua"
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with db_connection() as connection:
            connection.execute(
                """
                INSERT INTO email_unsubscribe_tokens (token_hash, email)
                VALUES (?, ?)
                """,
                (token_hash, reminder["recipient"]),
            )

        sent = send_app_email(
            "abandoned_checkout_reminder",
            reminder["recipient"],
            lambda app_url: abandoned_checkout_email(
                reminder["buyer_name"] or "Mteja",
                product_names,
                reminder["amount"],
                f"{app_url.rstrip('/')}{resume_url_path}",
                f"{app_url.rstrip('/')}/api/email/reminders/unsubscribe?token={quote(token)}",
                app_url,
            ),
        )
        finish_abandoned_checkout_reminder(reminder["id"], sent, current_time)
    return processed


async def abandoned_checkout_reminder_worker():
    while True:
        batch = asyncio.create_task(
            asyncio.to_thread(process_due_abandoned_checkout_reminders)
        )
        try:
            await asyncio.shield(batch)
        except asyncio.CancelledError:
            try:
                await batch
            except Exception:
                logger.exception("Abandoned-checkout reminder batch failed during shutdown.")
            raise
        except Exception:
            logger.exception("Abandoned-checkout reminder worker failed.")
        await asyncio.sleep(ABANDONED_CHECKOUT_REMINDER_POLL_SECONDS)


def unsubscribe_reminder_email(token):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        token_row = connection.execute(
            "SELECT email FROM email_unsubscribe_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if token_row is None:
            return None
        email = token_row["email"]
        connection.execute(
            """
            UPDATE email_reminder_preferences
            SET opted_out_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE email = ?
            """,
            (email,),
        )
        connection.execute(
            """
            UPDATE abandoned_checkout_reminders
            SET status = 'UNSUBSCRIBED'
            WHERE recipient = ? AND status IN ('PENDING', 'SENDING')
            """,
            (email,),
        )
        return email


@app.get("/api/email/reminders/unsubscribe", response_class=HTMLResponse)
def confirm_reminder_unsubscribe(token: str):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        token_row = connection.execute(
            "SELECT 1 FROM email_unsubscribe_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    if token_row is None:
        return HTMLResponse(
            "<html><body><p>Link si sahihi au muda wake umeisha.</p></body></html>",
            status_code=404,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    action_url = f"/api/email/reminders/unsubscribe?token={quote(token)}"
    return HTMLResponse(
        "<!doctype html><html lang='sw'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Simamisha vikumbusho | LipaRahisi</title>"
        "<body style='font-family:Arial,sans-serif;max-width:560px;margin:12vh auto;padding:24px;color:#172554'>"
        "<h1>Simamisha vikumbusho vya checkout</h1>"
        "<p>Thibitisha kama hutaki tena kupokea vikumbusho vya checkout kupitia barua pepe.</p>"
        f"<form method='post' action='{action_url}'>"
        "<button type='submit' style='padding:12px 18px;background:#174DAA;color:white;border:0;border-radius:8px;cursor:pointer'>"
        "Jiondoe kwenye vikumbusho</button></form></body></html>",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/api/email/reminders/unsubscribe", response_class=HTMLResponse)
def unsubscribe_reminder(token: str):
    email = unsubscribe_reminder_email(token)
    if email is None:
        return HTMLResponse(
            "<html><body><p>Link si sahihi au muda wake umeisha.</p></body></html>",
            status_code=404,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    return HTMLResponse(
        "<!doctype html><html lang='sw'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Vikumbusho vimesimamishwa | LipaRahisi</title>"
        "<body style='font-family:Arial,sans-serif;max-width:560px;margin:12vh auto;padding:24px;color:#172554'>"
        "<h1>Vikumbusho vimesimamishwa</h1>"
        "<p>Hatutatuma tena vikumbusho vya checkout kwenye barua pepe hii.</p>"
        "</body></html>",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split(":", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def safe_user(user):
    store_slug = user["store_slug"] if "store_slug" in user.keys() else ""
    return {
        "id": user["id"],
        "full_name": user["full_name"],
        "email": user["email"],
        "role": user["role"],
        "status": user["status"],
        "shop_name": user["shop_name"],
        "store_slug": store_slug,
        "profile_image_url": user["profile_image_url"] if "profile_image_url" in user.keys() else "",
        "phone": user["phone"],
        "preferred_language": user["preferred_language"] if "preferred_language" in user.keys() else "sw",
    }


def get_current_user(request, allow_suspended=False):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Ingia kwenye akaunti yako.")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        user = connection.execute(
            """
            SELECT u.id, u.full_name, u.email, u.role, u.status, u.shop_name,
                   u.store_slug, u.profile_image_url, u.phone, u.preferred_language
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, time.time()),
        ).fetchone()
    if user is None:
        raise HTTPException(status_code=401, detail="Kikao kimeisha; ingia tena.")
    if user["status"] != "ACTIVE" and not (
        allow_suspended and user["status"] == "SUSPENDED"
    ):
        raise HTTPException(status_code=403, detail="Akaunti yako haijaidhinishwa.")
    with db_connection() as connection:
        connection.execute(
            "UPDATE sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE token_hash = ?",
            (token_hash,),
        )
    return dict(user)


def optional_current_user(request):
    if not request.cookies.get(SESSION_COOKIE):
        return None
    try:
        return get_current_user(request)
    except HTTPException as error:
        if error.status_code == 401:
            return None
        raise


def checkout_buyer_email(payload, buyer):
    if buyer and buyer["role"] == "BUYER":
        return buyer["email"].strip().lower()
    return payload.buyer_email.strip().lower()


def require_admin(request):
    user = get_current_user(request)
    if user["role"] != "ADMIN":
        raise HTTPException(status_code=403, detail="Huduma hii ni ya msimamizi mkuu.")
    return user


def require_seller(request):
    user = get_current_user(request)
    if user["role"] != "SELLER":
        raise HTTPException(status_code=403, detail="Huduma hii ni ya akaunti ya muuzaji.")
    return user


def require_buyer(request):
    user = get_current_user(request)
    if user["role"] != "BUYER":
        raise HTTPException(status_code=403, detail="Huduma hii ni ya akaunti ya mnunuzi.")
    return user


BUSINESS_PERMISSIONS = {
    "OWNER": {
        "business.manage",
        "branch.manage",
        "staff.manage",
        "product.manage",
        "stock.manage",
        "order.manage",
        "sales.view",
        "analytics.view",
    },
    "BRANCH_MANAGER": {
        "branch.manage",
        "product.manage",
        "stock.manage",
        "order.manage",
        "sales.view",
        "analytics.view",
    },
    "STAFF": {
        "product.manage",
        "stock.manage",
        "order.manage",
        "sales.view",
    },
}


def get_business_context(request, business_id, branch_id=None, permission=None):
    user = get_current_user(request)
    if user["role"] == "ADMIN":
        business = None
        with db_connection() as connection:
            business = connection.execute(
                "SELECT * FROM businesses WHERE id = ?",
                (business_id,),
            ).fetchone()
        if business is None:
            raise HTTPException(status_code=404, detail="Biashara haikupatikana.")
        return user, dict(business), None
    with db_connection() as connection:
        membership = connection.execute(
            """
            SELECT * FROM business_memberships
            WHERE business_id = ? AND user_id = ? AND status = 'ACTIVE'
            """,
            (business_id, user["id"]),
        ).fetchone()
        business = connection.execute(
            "SELECT * FROM businesses WHERE id = ? AND status = 'ACTIVE'",
            (business_id,),
        ).fetchone()
    if membership is None or business is None:
        raise HTTPException(status_code=403, detail="Huna ufikiaji wa biashara hii.")
    membership = dict(membership)
    if membership["role"] != "OWNER" and branch_id:
        if membership["branch_id"] and membership["branch_id"] != branch_id:
            raise HTTPException(status_code=403, detail="Huna ufikiaji wa tawi hili.")
    if membership["role"] != "OWNER" and membership["role"] == "BRANCH_MANAGER" and not branch_id:
        branch_id = membership["branch_id"]
    if permission:
        allowed = set(BUSINESS_PERMISSIONS.get(membership["role"], set()))
        try:
            custom = set(json.loads(membership["permissions_json"]))
            allowed.update(custom)
        except (json.JSONDecodeError, TypeError):
            pass
        if permission not in allowed:
            raise HTTPException(status_code=403, detail="Huna ruhusa ya shughuli hii.")
    return user, dict(business), membership


def create_session(connection, user_id, request=None):
    connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO sessions (token_hash, user_id, expires_at, user_agent, ip_address)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            token_hash,
            user_id,
            time.time() + SESSION_TTL_SECONDS,
            request.headers.get("user-agent", "")[:300] if request else "",
            request.client.host if request and request.client else "",
        ),
    )
    return token


def set_session_cookie(response, request, token):
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    secure = forwarded_proto.split(",")[0].strip() == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def is_integrity_error(error):
    return isinstance(error, (sqlite3.IntegrityError, psycopg.IntegrityError))


def record_audit(action, target_type="", target_id="", detail="", actor=None, request=None):
    actor_id = actor["id"] if actor else ""
    actor_email = actor["email"] if actor else ""
    ip_address = ""
    if request is not None:
        forwarded = request.headers.get("x-forwarded-for", "")
        ip_address = forwarded.split(",")[0].strip() if forwarded else request.client.host if request.client else ""
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO audit_log
                (id, actor_id, actor_email, action, target_type, target_id, detail, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                secrets.token_hex(16),
                actor_id,
                actor_email,
                action,
                target_type,
                target_id,
                detail,
                ip_address,
            ),
        )


def record_business_activity(
    business_id,
    branch_id,
    actor,
    action,
    target_type="",
    target_id="",
    detail="",
):
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO business_activity
                (id, business_id, branch_id, actor_id, action,
                 target_type, target_id, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                secrets.token_hex(16),
                business_id,
                branch_id,
                actor["id"] if actor else None,
                action,
                target_type,
                target_id,
                detail,
            ),
        )


def create_or_update_security_alert(
    subject_type,
    subject_id,
    category,
    risk_score,
    reasons,
    evidence,
):
    if risk_score < 35:
        return None
    severity = "LOW" if risk_score < 50 else "MEDIUM" if risk_score < 75 else "HIGH"
    if severity == "HIGH":
        severity = "CRITICAL" if risk_score >= 90 else "HIGH"
    with db_connection() as connection:
        existing = connection.execute(
            """
            SELECT id FROM security_alerts
            WHERE subject_type = ? AND subject_id = ? AND category = ?
              AND status IN ('OPEN', 'IN_REVIEW', 'ACCOUNT_REVIEW', 'WARNED', 'APPEAL_PENDING')
            ORDER BY created_at DESC LIMIT 1
            """,
            (subject_type, subject_id, category),
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE security_alerts
                SET risk_score = ?, severity = ?, reasons_json = ?,
                    evidence_json = ?, occurred_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    risk_score,
                    severity,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    existing["id"],
                ),
            )
            return existing["id"]
        alert_id = secrets.token_hex(16)
        connection.execute(
            """
            INSERT INTO security_alerts
                (id, subject_type, subject_id, category, risk_score, severity,
                 status, reasons_json, evidence_json, detection_model)
            VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, 'LOCAL_RULES_V1')
            """,
            (
                alert_id,
                subject_type,
                subject_id,
                category,
                risk_score,
                severity,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            INSERT INTO security_alert_actions
                (id, alert_id, actor_id, action, note)
            VALUES (?, ?, NULL, 'AUTO_CREATED', 'Rule-based alert created from observed platform data')
            """,
            (secrets.token_hex(16), alert_id),
        )
        return alert_id


def run_security_intelligence_scan():
    created_or_updated = []
    with db_connection() as connection:
        recent_90_days = (
            "CURRENT_TIMESTAMP - INTERVAL '90 days'"
            if connection.postgres
            else "datetime('now', '-90 days')"
        )
        older_than_48_hours = (
            "CURRENT_TIMESTAMP - INTERVAL '48 hours'"
            if connection.postgres
            else "datetime('now', '-48 hours')"
        )
        recent_24_hours = (
            "CURRENT_TIMESTAMP - INTERVAL '24 hours'"
            if connection.postgres
            else "datetime('now', '-24 hours')"
        )
        blocked = connection.execute(
            """
            SELECT email, failed_count, blocked_until
            FROM login_attempts
            WHERE failed_count >= 5 OR blocked_until > ?
            """,
            (time.time(),),
        ).fetchall()
        for row in blocked:
            score = min(95, 55 + (int(row["failed_count"]) - 5) * 8)
            alert_id = create_or_update_security_alert(
                "ACCOUNT",
                row["email"],
                "BRUTE_FORCE",
                score,
                ["Repeated login failures", "Account is currently locked or has recent blocked attempts"],
                [
                    {"failed_count": int(row["failed_count"])},
                    {"blocked_until": float(row["blocked_until"] or 0)},
                ],
            )
            if alert_id:
                created_or_updated.append(alert_id)

        failed_logins = connection.execute(
            f"""
            SELECT ip_address, COUNT(*) AS failure_count
            FROM audit_log
            WHERE action = 'LOGIN_FAILED' AND ip_address <> ''
              AND created_at >= {recent_24_hours}
            GROUP BY ip_address
            HAVING COUNT(*) >= 10
            ORDER BY failure_count DESC
            LIMIT 200
            """
        ).fetchall()
        for row in failed_logins:
            alert_id = create_or_update_security_alert(
                "IP",
                row["ip_address"],
                "UNUSUAL_LOGIN_ACTIVITY",
                min(90, 50 + int(row["failure_count"]) * 2),
                ["Many failed login attempts originate from one IP"],
                [{"ip_address": row["ip_address"], "failed_login_count": int(row["failure_count"])}],
            )
            if alert_id:
                created_or_updated.append(alert_id)

        sellers = connection.execute(
            "SELECT id FROM users WHERE role = 'SELLER'"
        ).fetchall()
        for seller in sellers:
            seller_id = seller["id"]
            repeat_complaints = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM cancellation_requests cr
                JOIN orders o ON o.order_id = cr.order_id
                WHERE o.seller_id = ? AND cr.created_at >= {recent_90_days}
                """,
                (seller_id,),
            ).fetchone()["count"]
            unfulfilled = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM orders
                WHERE seller_id = ? AND payment_status = 'COMPLETED'
                  AND delivery_status NOT IN ('DELIVERED', 'RETURNED')
                  AND created_at <= {older_than_48_hours}
                """,
                (seller_id,),
            ).fetchone()["count"]
            reasons = []
            evidence = []
            score = 0
            if unfulfilled >= 3:
                score += min(45, 25 + int(unfulfilled) * 8)
                reasons.append("Repeated paid orders remain unfulfilled; review required, not proof of fraud")
                evidence.append({"unfulfilled_completed_orders": int(unfulfilled)})
            if repeat_complaints >= 3:
                score += min(40, 20 + int(repeat_complaints) * 5)
                reasons.append("Repeated cancellation requests on seller orders; review required, not proof of fraud")
                evidence.append({"cancellation_requests": int(repeat_complaints)})
            if score:
                alert_id = create_or_update_security_alert(
                    "SELLER",
                    seller_id,
                    "SELLER_RISK",
                    min(score, 100),
                    reasons,
                    evidence,
                )
                if alert_id:
                    created_or_updated.append(alert_id)

        buyers = connection.execute(
            "SELECT id, status FROM users WHERE role = 'BUYER'"
        ).fetchall()
        for buyer in buyers:
            buyer_id = buyer["id"]
            cancellations = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM cancellation_requests
                WHERE requester_id = ? AND created_at >= {recent_90_days}
                """,
                (buyer_id,),
            ).fetchone()["count"]
            completed_orders = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM orders
                WHERE buyer_id = ? AND payment_status = 'COMPLETED'
                """,
                (buyer_id,),
            ).fetchone()["count"]
            if cancellations >= 3:
                rate = cancellations / max(completed_orders, 1)
                score = min(75, 35 + (int(cancellations) - 3) * 5)
                if rate >= 0.5:
                    score = min(85, score + 10)
                alert_id = create_or_update_security_alert(
                    "BUYER",
                    buyer_id,
                    "REPEATED_CANCELLATIONS",
                    score,
                    ["Repeated cancellation requests compared with completed orders; review signal, not proof of abuse"],
                    [
                        {"cancellation_requests": int(cancellations)},
                        {"completed_orders": int(completed_orders)},
                        {"request_rate": round(rate, 3)},
                    ],
                )
                if alert_id:
                    created_or_updated.append(alert_id)

    return list(dict.fromkeys(created_or_updated))


def enrich_security_alerts_with_ai(alert_ids):
    if not alert_ids:
        analysis = analyze_security_alerts([])
        return {key: analysis[key] for key in ("status", "model", "analyzed", "skipped")}

    batch_ids = alert_ids[:MAX_ALERTS_PER_SCAN]
    placeholders = ", ".join("?" for _ in batch_ids)
    with db_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT id, category, risk_score, evidence_json
            FROM security_alerts
            WHERE id IN ({placeholders})
            """,
            tuple(batch_ids),
        ).fetchall()
    alerts = [
        {
            "id": row["id"],
            "category": row["category"],
            "risk_score": row["risk_score"],
            "evidence": json.loads(row["evidence_json"] or "[]"),
        }
        for row in rows
    ]
    analysis = analyze_security_alerts(alerts)
    if analysis["assessments"]:
        with db_connection() as connection:
            for assessment in analysis["assessments"]:
                connection.execute(
                    """
                    UPDATE security_alerts
                    SET ai_used = 1, ai_risk_score = ?, ai_reasons_json = ?,
                        ai_model = ?, ai_analyzed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        assessment["risk_score"],
                        json.dumps(assessment["reasons"], ensure_ascii=False),
                        analysis["model"],
                        assessment["alert_id"],
                    ),
                )
    return {
        "status": analysis["status"],
        "model": analysis["model"],
        "analyzed": analysis["analyzed"],
        "skipped": analysis["skipped"] + max(0, len(alert_ids) - len(batch_ids)),
    }


@app.post("/api/admin/security/scan")
def run_security_scan(request: Request):
    actor = require_admin(request)
    alert_ids = run_security_intelligence_scan()
    ai_analysis = enrich_security_alerts_with_ai(alert_ids)
    record_audit("SECURITY_INTELLIGENCE_SCAN", "security", "", f"alerts={len(alert_ids)}", actor=actor, request=request)
    return {
        "scanned": True,
        "alert_ids": alert_ids,
        "alerts_updated": len(alert_ids),
        "ai_analysis": ai_analysis,
    }


@app.get("/api/admin/security/alerts")
def list_security_alerts(
    request: Request,
    status: str | None = None,
    severity: str | None = None,
    category: str | None = None,
):
    require_admin(request)
    filters = []
    values = []
    allowed_statuses = {
        "OPEN", "IN_REVIEW", "ACCOUNT_REVIEW", "WARNED", "SUSPENDED",
        "APPEAL_PENDING", "FALSE_POSITIVE", "RESOLVED",
    }
    allowed_severities = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    if status:
        if status not in allowed_statuses:
            raise HTTPException(status_code=422, detail="Status ya alert si sahihi.")
        filters.append("status = ?")
        values.append(status)
    if severity:
        if severity not in allowed_severities:
            raise HTTPException(status_code=422, detail="Severity si sahihi.")
        filters.append("severity = ?")
        values.append(severity)
    if category:
        if not re.fullmatch(r"[A-Z0-9_]{1,64}", category):
            raise HTTPException(status_code=422, detail="Category si sahihi.")
        filters.append("category = ?")
        values.append(category)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    with db_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM security_alerts
            {where_clause}
            ORDER BY risk_score DESC, created_at DESC
            LIMIT 500
            """,
            tuple(values),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        item["ai_reasons"] = json.loads(item.pop("ai_reasons_json") or "[]")
        result.append(item)
    return result


@app.get("/api/admin/security/alerts/{alert_id}")
def get_security_alert(alert_id: str, request: Request):
    require_admin(request)
    with db_connection() as connection:
        alert = connection.execute(
            "SELECT * FROM security_alerts WHERE id = ?",
            (alert_id,),
        ).fetchone()
        if alert is None:
            raise HTTPException(status_code=404, detail="Security alert haikupatikana.")
        actions = connection.execute(
            """
            SELECT sa.*, u.full_name AS actor_name
            FROM security_alert_actions sa
            LEFT JOIN users u ON u.id = sa.actor_id
            WHERE sa.alert_id = ?
            ORDER BY sa.created_at DESC
            """,
            (alert_id,),
        ).fetchall()
        appeals = connection.execute(
            """
            SELECT ap.*, u.full_name AS appellant_name
            FROM security_appeals ap
            JOIN users u ON u.id = ap.appellant_id
            WHERE ap.alert_id = ?
            ORDER BY ap.created_at DESC
            """,
            (alert_id,),
        ).fetchall()
    item = dict(alert)
    item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
    item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
    item["ai_reasons"] = json.loads(item.pop("ai_reasons_json") or "[]")
    item["actions"] = [dict(row) for row in actions]
    item["appeals"] = [dict(row) for row in appeals]
    return item


@app.patch("/api/admin/security/alerts/{alert_id}")
def action_security_alert(
    alert_id: str,
    payload: FraudAlertActionInput,
    request: Request,
):
    actor = require_admin(request)
    notification_user = None
    with db_connection() as connection:
        alert = connection.execute(
            "SELECT * FROM security_alerts WHERE id = ?",
            (alert_id,),
        ).fetchone()
        if alert is None:
            raise HTTPException(status_code=404, detail="Security alert haikupatikana.")
        if payload.action == "SUSPEND":
            if alert["subject_type"] not in {"ACCOUNT", "SELLER", "BUYER"}:
                raise HTTPException(
                    status_code=409,
                    detail="Aina hii ya alert haihusishi akaunti inayoweza kusimamishwa.",
                )
            account_column = "email" if alert["subject_type"] == "ACCOUNT" else "id"
            subject = connection.execute(
                f"SELECT id FROM users WHERE {account_column} = ?",
                (alert["subject_id"],),
            ).fetchone()
            if subject is None:
                raise HTTPException(status_code=409, detail="Akaunti inayolengwa haipo tena.")
        if payload.action in {"WARN", "SUSPEND"} and alert["subject_type"] in {"ACCOUNT", "SELLER", "BUYER"}:
            account_column = "email" if alert["subject_type"] == "ACCOUNT" else "id"
            notification_user = connection.execute(
                f"SELECT id, email, phone FROM users WHERE {account_column} = ?",
                (alert["subject_id"],),
            ).fetchone()
        status = {
            "WARN": "WARNED",
            "ACCOUNT_REVIEW": "ACCOUNT_REVIEW",
            "SUSPEND": "SUSPENDED",
            "RESTORE": "IN_REVIEW",
            "FALSE_POSITIVE": "FALSE_POSITIVE",
            "RESOLVE": "RESOLVED",
        }[payload.action]
        previous_subject_status = ""
        subject_user_id = ""
        if payload.action == "SUSPEND":
            account_column = "email" if alert["subject_type"] == "ACCOUNT" else "id"
            subject = connection.execute(
                f"SELECT id, status FROM users WHERE {account_column} = ?",
                (alert["subject_id"],),
            ).fetchone()
            previous_subject_status = subject["status"]
            subject_user_id = subject["id"]
        elif payload.action == "RESTORE":
            if alert["status"] != "SUSPENDED" or alert["subject_type"] not in {"ACCOUNT", "SELLER", "BUYER"}:
                raise HTTPException(status_code=409, detail="Alert hii haina kusimamishwa kwa akaunti kunakoweza kurejeshwa.")
            account_column = "email" if alert["subject_type"] == "ACCOUNT" else "id"
            subject = connection.execute(
                f"SELECT id, status FROM users WHERE {account_column} = ?",
                (alert["subject_id"],),
            ).fetchone()
            previous_action = connection.execute(
                """
                SELECT previous_status
                FROM security_alert_actions
                WHERE alert_id = ? AND action = 'SUSPEND'
                ORDER BY created_at DESC LIMIT 1
                """,
                (alert_id,),
            ).fetchone()
            if (
                subject is None
                or subject["status"] != "SUSPENDED"
                or previous_action is None
                or not previous_action["previous_status"]
            ):
                raise HTTPException(status_code=409, detail="Hali ya akaunti haiwezi kurejeshwa salama.")
            previous_subject_status = previous_action["previous_status"]
            subject_user_id = subject["id"]
        connection.execute(
            """
            UPDATE security_alerts
            SET status = ?, resolution_note = ?, resolved_at =
                CASE WHEN ? IN ('FALSE_POSITIVE', 'RESOLVED') THEN CURRENT_TIMESTAMP ELSE resolved_at END
            WHERE id = ?
            """,
            (
                status,
                payload.note.strip(),
                status,
                alert_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO security_alert_actions
                (id, alert_id, actor_id, action, note, previous_status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                secrets.token_hex(16),
                alert_id,
                actor["id"],
                payload.action,
                payload.note.strip(),
                previous_subject_status if payload.action == "SUSPEND" else "",
            ),
        )
        if payload.action == "SUSPEND":
            connection.execute(
                "UPDATE users SET status = 'SUSPENDED' WHERE id = ?",
                (subject_user_id,),
            )
        elif payload.action == "RESTORE":
            connection.execute(
                "UPDATE users SET status = ? WHERE id = ?",
                (previous_subject_status, subject_user_id),
            )
    notification_channels = []
    if notification_user:
        detail = (
            "Msimamizi ametoa onyo la usalama. Kagua tahadhari za akaunti yako au wasiliana na msaada."
            if payload.action == "WARN"
            else "Akaunti yako imesimamishwa baada ya ukaguzi wa msimamizi. Wasiliana na msaada kwa hatua zaidi."
        )
        notification_channels = queue_user_notification(
            notification_user["id"],
            "SECURITY_ALERT",
            context={"detail": detail},
            link="/dashboard",
            email=notification_user["email"],
            phone=notification_user["phone"],
        )
    record_audit(
        "SECURITY_ALERT_ACTION",
        "security",
        alert_id,
        f"{payload.action}: {payload.note}",
        actor=actor,
        request=request,
    )
    return {
        "id": alert_id,
        "status": status,
        "notification_queued": bool(notification_channels),
        "notification_channels": notification_channels,
    }


@app.post("/api/security/alerts/{alert_id}/appeal")
def submit_security_appeal(
    alert_id: str,
    payload: SecurityAppealInput,
    request: Request,
):
    user = get_current_user(request, allow_suspended=True)
    with db_connection() as connection:
        alert = connection.execute(
            "SELECT subject_type, subject_id, status FROM security_alerts WHERE id = ?",
            (alert_id,),
        ).fetchone()
        if alert is None:
            raise HTTPException(status_code=404, detail="Security alert haikupatikana.")
        owns_alert = (
            alert["subject_type"] == "ACCOUNT" and alert["subject_id"] == user["email"]
        ) or (
            alert["subject_type"] in {"SELLER", "BUYER"}
            and alert["subject_id"] == user["id"]
        )
        if not owns_alert:
            raise HTTPException(status_code=403, detail="Huhusiki na security alert hii.")
        existing = connection.execute(
            "SELECT id FROM security_appeals WHERE alert_id = ? AND status = 'PENDING'",
            (alert_id,),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Appeal tayari imesubiriwa.")
        appeal_id = secrets.token_hex(16)
        connection.execute(
            """
            INSERT INTO security_appeals
                (id, alert_id, appellant_id, message)
            VALUES (?, ?, ?, ?)
            """,
            (appeal_id, alert_id, user["id"], payload.message.strip()),
        )
        connection.execute(
            "UPDATE security_alerts SET status = 'APPEAL_PENDING' WHERE id = ?",
            (alert_id,),
        )
    record_audit("SECURITY_APPEAL_SUBMITTED", "security", alert_id, "Appeal submitted", actor=user, request=request)
    return {"id": appeal_id, "status": "PENDING"}


@app.get("/api/admin/security/appeals")
def list_security_appeals(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT ap.*, u.full_name AS appellant_name, u.email AS appellant_email,
                   a.subject_type, a.subject_id, a.category, a.risk_score, a.severity
            FROM security_appeals ap
            JOIN users u ON u.id = ap.appellant_id
            JOIN security_alerts a ON a.id = ap.alert_id
            WHERE ap.status = 'PENDING'
            ORDER BY ap.created_at ASC
            LIMIT 200
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/admin/security/appeals/{appeal_id}")
def review_security_appeal(
    appeal_id: str,
    payload: SecurityAppealReviewInput,
    request: Request,
):
    actor = require_admin(request)
    with db_connection() as connection:
        appeal = connection.execute(
            """
            SELECT ap.id, ap.alert_id, ap.status, a.subject_type, a.subject_id
            FROM security_appeals ap
            JOIN security_alerts a ON a.id = ap.alert_id
            WHERE ap.id = ?
            """,
            (appeal_id,),
        ).fetchone()
        if appeal is None:
            raise HTTPException(status_code=404, detail="Appeal haikupatikana.")
        if appeal["status"] != "PENDING":
            raise HTTPException(status_code=409, detail="Appeal hii tayari imeamuliwa.")
        connection.execute(
            """
            UPDATE security_appeals
            SET status = ?, admin_note = ?, resolved_by = ?, resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payload.status, payload.note.strip(), actor["id"], appeal_id),
        )
        alert_status = "IN_REVIEW"
        if appeal["subject_type"] in {"ACCOUNT", "SELLER", "BUYER"}:
            account_column = "email" if appeal["subject_type"] == "ACCOUNT" else "id"
            subject = connection.execute(
                f"SELECT id, status FROM users WHERE {account_column} = ?",
                (appeal["subject_id"],),
            ).fetchone()
            suspension = connection.execute(
                """
                SELECT previous_status
                FROM security_alert_actions
                WHERE alert_id = ? AND action = 'SUSPEND'
                ORDER BY created_at DESC LIMIT 1
                """,
                (appeal["alert_id"],),
            ).fetchone()
            if (
                subject is not None
                and subject["status"] == "SUSPENDED"
                and suspension is not None
                and suspension["previous_status"] != "SUSPENDED"
            ):
                alert_status = "SUSPENDED"
        connection.execute(
            """
            UPDATE security_alerts
            SET status = ?, resolution_note = ?,
                resolved_at = CASE WHEN ? = 'FALSE_POSITIVE' THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE id = ?
            """,
            (alert_status, payload.note.strip(), alert_status, appeal["alert_id"]),
        )
        connection.execute(
            """
            INSERT INTO security_alert_actions
                (id, alert_id, actor_id, action, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                secrets.token_hex(16),
                appeal["alert_id"],
                actor["id"],
                f"APPEAL_{payload.status}",
                payload.note.strip(),
            ),
        )
    record_audit(
        "SECURITY_APPEAL_REVIEWED",
        "security",
        appeal_id,
        payload.status,
        actor=actor,
        request=request,
    )
    return {"id": appeal_id, "status": payload.status, "alert_status": alert_status}


@app.get("/api/security/alerts")
def list_my_security_alerts(request: Request):
    user = get_current_user(request, allow_suspended=True)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT a.*,
                   (
                       SELECT ap.status
                       FROM security_appeals ap
                       WHERE ap.alert_id = a.id
                       ORDER BY ap.created_at DESC
                       LIMIT 1
                   ) AS appeal_status
            FROM security_alerts a
            WHERE (a.subject_type = 'ACCOUNT' AND a.subject_id = ?)
               OR (a.subject_type IN ('SELLER', 'BUYER') AND a.subject_id = ?)
            ORDER BY a.created_at DESC LIMIT 100
            """,
            (user["email"], user["id"]),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        item["ai_reasons"] = json.loads(item.pop("ai_reasons_json") or "[]")
        result.append(item)
    return result


@app.get("/api/businesses")
def list_businesses(request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        if user["role"] == "ADMIN":
            rows = connection.execute(
                """
                SELECT b.*, u.full_name AS owner_name, u.email AS owner_email
                FROM businesses b JOIN users u ON u.id = b.owner_id
                ORDER BY b.created_at DESC
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT b.*, bm.role AS membership_role, bm.branch_id AS membership_branch_id
                FROM businesses b
                JOIN business_memberships bm ON bm.business_id = b.id
                WHERE bm.user_id = ? AND bm.status = 'ACTIVE'
                ORDER BY b.created_at DESC
                """,
                (user["id"],),
            ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/businesses")
def create_business(payload: BusinessInput, request: Request):
    owner = require_seller(request)
    business_id = secrets.token_hex(16)
    branch_id = secrets.token_hex(16)
    with db_connection() as connection:
        existing = connection.execute(
            """
            SELECT b.id FROM businesses b
            JOIN business_memberships bm ON bm.business_id = b.id
            WHERE bm.user_id = ? AND bm.role = 'OWNER' AND b.name = ?
            """,
            (owner["id"], payload.name.strip()),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Biashara yenye jina hili tayari ipo.")
        connection.execute(
            """
            INSERT INTO businesses
                (id, owner_id, name, business_type, address, phone, email, settings_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                business_id,
                owner["id"],
                payload.name.strip(),
                payload.business_type.strip(),
                payload.address.strip(),
                payload.phone.strip(),
                payload.email.strip().lower(),
                json.dumps(payload.settings, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        connection.execute(
            """
            INSERT INTO branches (id, business_id, name, address, phone, email)
            VALUES (?, ?, 'Tawi Kuu', ?, ?, ?)
            """,
            (
                branch_id,
                business_id,
                payload.address.strip(),
                payload.phone.strip(),
                payload.email.strip().lower(),
            ),
        )
        connection.execute(
            """
            INSERT INTO business_memberships
                (id, business_id, branch_id, user_id, role, status)
            VALUES (?, ?, NULL, ?, 'OWNER', 'ACTIVE')
            """,
            (secrets.token_hex(16), business_id, owner["id"]),
        )
    record_business_activity(
        business_id,
        branch_id,
        owner,
        "BUSINESS_CREATED",
        "business",
        business_id,
        payload.name.strip(),
    )
    record_audit("BUSINESS_CREATED", "business", business_id, payload.name.strip(), actor=owner, request=request)
    return {
        "id": business_id,
        "name": payload.name.strip(),
        "business_type": payload.business_type.strip(),
        "branch_id": branch_id,
    }


@app.get("/api/businesses/{business_id}")
def get_business(business_id: str, request: Request):
    user, business, membership = get_business_context(request, business_id)
    with db_connection() as connection:
        branches = connection.execute(
            "SELECT * FROM branches WHERE business_id = ? ORDER BY created_at",
            (business_id,),
        ).fetchall()
        members = connection.execute(
            """
            SELECT bm.id, bm.user_id, bm.branch_id, bm.role, bm.status,
                   bm.permissions_json, bm.created_at,
                   u.full_name, u.email, u.phone
            FROM business_memberships bm
            JOIN users u ON u.id = bm.user_id
            WHERE bm.business_id = ?
            ORDER BY bm.created_at
            """,
            (business_id,),
        ).fetchall() if membership is None or membership["role"] == "OWNER" else []
    result = dict(business)
    result["branches"] = [dict(row) for row in branches]
    result["members"] = [dict(row) for row in members]
    result["membership_role"] = membership["role"] if membership else "ADMIN"
    return result


@app.patch("/api/businesses/{business_id}")
def update_business(business_id: str, payload: BusinessInput, request: Request):
    user, business, membership = get_business_context(
        request,
        business_id,
        permission="business.manage",
    )
    with db_connection() as connection:
        connection.execute(
            """
            UPDATE businesses
            SET name = ?, business_type = ?, address = ?, phone = ?, email = ?,
                settings_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                payload.name.strip(),
                payload.business_type.strip(),
                payload.address.strip(),
                payload.phone.strip(),
                payload.email.strip().lower(),
                json.dumps(payload.settings, ensure_ascii=False, separators=(",", ":")),
                business_id,
            ),
        )
    record_business_activity(
        business_id,
        None,
        user,
        "BUSINESS_UPDATED",
        "business",
        business_id,
        payload.name.strip(),
    )
    record_audit("BUSINESS_UPDATED", "business", business_id, payload.name.strip(), actor=user, request=request)
    return {"id": business_id, "updated": True}


@app.get("/api/businesses/{business_id}/branches")
def list_branches(business_id: str, request: Request):
    user, business, membership = get_business_context(request, business_id)
    with db_connection() as connection:
        if membership and membership["role"] != "OWNER":
            if membership["role"] == "BRANCH_MANAGER" and membership["branch_id"]:
                rows = connection.execute(
                    "SELECT * FROM branches WHERE business_id = ? AND id = ?",
                    (business_id, membership["branch_id"]),
                ).fetchall()
            else:
                rows = []
        else:
            rows = connection.execute(
                "SELECT * FROM branches WHERE business_id = ? ORDER BY created_at",
                (business_id,),
            ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/businesses/{business_id}/branches")
def create_branch(business_id: str, payload: BranchInput, request: Request):
    user, business, membership = get_business_context(
        request,
        business_id,
        permission="branch.manage",
    )
    branch_id = secrets.token_hex(16)
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO branches
                (id, business_id, name, address, phone, email, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                branch_id,
                business_id,
                payload.name.strip(),
                payload.address.strip(),
                payload.phone.strip(),
                payload.email.strip().lower(),
                payload.is_active,
            ),
        )
    record_business_activity(
        business_id,
        branch_id,
        user,
        "BRANCH_CREATED",
        "branch",
        branch_id,
        payload.name.strip(),
    )
    record_audit("BRANCH_CREATED", "branch", branch_id, payload.name.strip(), actor=user, request=request)
    return {"id": branch_id, "business_id": business_id, "name": payload.name.strip()}


@app.post("/api/businesses/{business_id}/memberships")
def create_membership(business_id: str, payload: MembershipInput, request: Request):
    user, business, membership = get_business_context(
        request,
        business_id,
        payload.branch_id,
        permission="staff.manage",
    )
    if payload.role == "OWNER":
        raise HTTPException(status_code=422, detail="Hatua ya kuongeza OWNER haijaruhusiwa hapa.")
    email = payload.email.strip().lower()
    with db_connection() as connection:
        staff = connection.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if staff is None:
            raise HTTPException(status_code=404, detail="Mtumiaji huyu hajajisajili kwenye LipaRahisi.")
        if payload.branch_id:
            branch = connection.execute(
                "SELECT id FROM branches WHERE id = ? AND business_id = ?",
                (payload.branch_id, business_id),
            ).fetchone()
            if branch is None:
                raise HTTPException(status_code=404, detail="Tawi la biashara halikupatikana.")
        existing = connection.execute(
            "SELECT id FROM business_memberships WHERE business_id = ? AND user_id = ?",
            (business_id, staff["id"]),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Mtumiaji tayari ni mwanachama wa biashara hii.")
        membership_id = secrets.token_hex(16)
        permissions = sorted(BUSINESS_PERMISSIONS.get(payload.role, set()))
        connection.execute(
            """
            INSERT INTO business_memberships
                (id, business_id, branch_id, user_id, role, status, permissions_json)
            VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)
            """,
            (
                membership_id,
                business_id,
                payload.branch_id,
                staff["id"],
                payload.role,
                json.dumps(permissions),
            ),
        )
    record_business_activity(
        business_id,
        payload.branch_id,
        user,
        "STAFF_ADDED",
        "staff",
        staff["id"],
        f"{payload.role}:{email}",
    )
    record_audit("BUSINESS_STAFF_ADDED", "business", business_id, f"{email}:{payload.role}", actor=user, request=request)
    return {"id": membership_id, "email": email, "role": payload.role, "branch_id": payload.branch_id}


@app.patch("/api/businesses/{business_id}/memberships/{membership_id}")
def update_membership(
    business_id: str,
    membership_id: str,
    payload: MembershipUpdateInput,
    request: Request,
):
    user, business, membership = get_business_context(
        request,
        business_id,
        payload.branch_id,
        permission="staff.manage",
    )
    if payload.role == "OWNER":
        raise HTTPException(status_code=422, detail="Ruhusa ya OWNER haiwezi kubadilishwa kupitia endpoint hii.")
    with db_connection() as connection:
        row = connection.execute(
            "SELECT id FROM business_memberships WHERE id = ? AND business_id = ?",
            (membership_id, business_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Mwanachama hakupatikana.")
        connection.execute(
            """
            UPDATE business_memberships
            SET role = ?, branch_id = ?, status = ?, permissions_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND business_id = ?
            """,
            (
                payload.role,
                payload.branch_id,
                payload.status,
                json.dumps(sorted(BUSINESS_PERMISSIONS.get(payload.role, set()))),
                membership_id,
                business_id,
            ),
        )
    record_business_activity(
        business_id,
        payload.branch_id,
        user,
        "STAFF_UPDATED",
        "membership",
        membership_id,
        f"{payload.role}:{payload.status}",
    )
    record_audit("BUSINESS_STAFF_UPDATED", "membership", membership_id, payload.role, actor=user, request=request)
    return {"id": membership_id, "role": payload.role, "branch_id": payload.branch_id, "status": payload.status}


@app.get("/api/businesses/{business_id}/activity")
def list_business_activity(business_id: str, request: Request):
    user, business, membership = get_business_context(request, business_id)
    with db_connection() as connection:
        if membership and membership["role"] != "OWNER":
            if membership["role"] == "BRANCH_MANAGER" and membership["branch_id"]:
                rows = connection.execute(
                    """
                    SELECT ba.*, u.full_name AS actor_name
                    FROM business_activity ba LEFT JOIN users u ON u.id = ba.actor_id
                    WHERE ba.business_id = ? AND ba.branch_id = ?
                    ORDER BY ba.created_at DESC LIMIT 500
                    """,
                    (business_id, membership["branch_id"]),
                ).fetchall()
            else:
                rows = []
        else:
            rows = connection.execute(
                """
                SELECT ba.*, u.full_name AS actor_name
                FROM business_activity ba LEFT JOIN users u ON u.id = ba.actor_id
                WHERE ba.business_id = ?
                ORDER BY ba.created_at DESC LIMIT 500
                """,
                (business_id,),
            ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/businesses/{business_id}/summary")
def business_branch_summary(business_id: str, request: Request):
    user, business, membership = get_business_context(
        request,
        business_id,
        permission="analytics.view",
    )
    allowed_branch_id = (
        membership["branch_id"]
        if membership and membership["role"] != "OWNER"
        else None
    )
    with db_connection() as connection:
        branch_rows = connection.execute(
            """
            SELECT id, name, address, is_active
            FROM branches WHERE business_id = ?
            """,
            (business_id,),
        ).fetchall()
        summaries = []
        for branch in branch_rows:
            if allowed_branch_id and branch["id"] != allowed_branch_id:
                continue
            row = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT o.order_id) AS order_count,
                    COALESCE(SUM(CASE WHEN o.payment_status='COMPLETED' THEN o.amount ELSE 0 END), 0) AS gross_sales,
                    (SELECT COUNT(*) FROM products p WHERE p.business_id=? AND p.branch_id=? AND p.is_active=1) AS active_products,
                    (SELECT COUNT(*) FROM products p WHERE p.business_id=? AND p.branch_id=? AND p.stock_quantity <= p.low_stock_threshold) AS low_stock
                FROM orders o
                WHERE o.business_id=? AND o.branch_id=? AND o.payment_method != 'PesaPal Sandbox'
                """,
                (
                    business_id,
                    branch["id"],
                    business_id,
                    branch["id"],
                    business_id,
                    branch["id"],
                ),
            ).fetchone()
            summaries.append({
                "branch_id": branch["id"],
                "branch_name": branch["name"],
                "is_active": bool(branch["is_active"]),
                **dict(row),
            })
    return {"business": dict(business), "branches": summaries}


def _business_assistant_facts(business_id: str, branch_id: str | None):
    today = date.today()
    period_start = today.replace(day=1)
    next_day = today + timedelta(days=1)
    slow_period_start = today - timedelta(days=30)
    branch_filter = " AND branch_id = ?" if branch_id else ""
    order_branch_filter = " AND o.branch_id = ?" if branch_id else ""
    sales_params = [business_id, period_start.isoformat(), next_day.isoformat()]
    item_params = [business_id, period_start.isoformat(), next_day.isoformat()]
    low_stock_params = [business_id]
    if branch_id:
        sales_params.append(branch_id)
        item_params.append(branch_id)
        low_stock_params.append(branch_id)
    with db_connection() as connection:
        sales = connection.execute(
            """
            SELECT COUNT(*) AS orders, COALESCE(SUM(amount), 0) AS amount,
                   COALESCE(SUM(net_profit), 0) AS estimated_profit
            FROM orders
            WHERE business_id = ? AND payment_status = 'COMPLETED'
              AND provider_reference != ''
              AND payment_method != 'PesaPal Sandbox'
              AND created_at >= ? AND created_at < ?
            """ + branch_filter,
            sales_params,
        ).fetchone()
        top_products = connection.execute(
            """
            SELECT p.name, COALESCE(SUM(oi.quantity), 0) AS quantity,
                   COALESCE(SUM(oi.quantity * oi.unit_amount), 0) AS sales
            FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            JOIN products p ON p.id = oi.product_id AND p.business_id = o.business_id
            WHERE o.business_id = ? AND o.payment_status = 'COMPLETED'
              AND o.provider_reference != ''
              AND o.payment_method != 'PesaPal Sandbox'
              AND o.created_at >= ? AND o.created_at < ?
            """ + order_branch_filter + """
            GROUP BY p.id, p.name
            ORDER BY quantity DESC, sales DESC
            LIMIT 5
            """,
            item_params,
        ).fetchall()
        low_stock = connection.execute(
            """
            SELECT name, stock_quantity AS stock, low_stock_threshold AS threshold
            FROM products
            WHERE business_id = ? AND is_active = 1
              AND stock_quantity <= low_stock_threshold
            """ + branch_filter + """
            ORDER BY stock_quantity ASC, name ASC
            LIMIT 10
            """,
            low_stock_params,
        ).fetchall()
        slow_products = connection.execute(
            """
            SELECT p.name
            FROM products p
            WHERE p.business_id = ? AND p.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM order_items oi
                  JOIN orders o ON o.order_id = oi.order_id
                  WHERE oi.product_id = p.id
                    AND p.business_id = o.business_id
                    AND o.business_id = ? AND o.payment_status = 'COMPLETED'
                    AND o.provider_reference != ''
                    AND o.payment_method != 'PesaPal Sandbox'
                    AND o.created_at >= ? AND o.created_at < ?
            """ + (" AND o.branch_id = ?" if branch_id else "") + """
              )
            """ + (" AND p.branch_id = ?" if branch_id else "") + """
            ORDER BY p.name ASC
            LIMIT 10
            """,
            [business_id, business_id, slow_period_start.isoformat(), next_day.isoformat()]
            + ([branch_id] if branch_id else [])
            + ([branch_id] if branch_id else []),
        ).fetchall()
        branches = connection.execute(
            """
            SELECT b.name, COUNT(o.order_id) AS orders,
                   COALESCE(SUM(o.amount), 0) AS sales
            FROM branches b
            LEFT JOIN orders o ON o.branch_id = b.id
              AND o.business_id = b.business_id
              AND o.payment_status = 'COMPLETED'
              AND o.provider_reference != ''
              AND o.payment_method != 'PesaPal Sandbox'
              AND o.created_at >= ? AND o.created_at < ?
            WHERE b.business_id = ?
            """ + (" AND b.id = ?" if branch_id else "") + """
            GROUP BY b.id, b.name
            ORDER BY sales DESC, b.name ASC
            """,
            [period_start.isoformat(), next_day.isoformat(), business_id]
            + ([branch_id] if branch_id else []),
        ).fetchall()
    return {
        "period": {"start": period_start.isoformat(), "end": today.isoformat()},
        "sales": dict(sales),
        "top_products": [dict(row) for row in top_products],
        "low_stock": [dict(row) for row in low_stock],
        "slow_products": [dict(row) for row in slow_products],
        "branches": [dict(row) for row in branches],
    }


@app.get("/api/businesses/{business_id}/assistant/conversations")
def list_business_assistant_conversations(business_id: str, request: Request):
    user, _business, membership = get_business_context(
        request,
        business_id,
        permission="analytics.view",
    )
    branch_id = (
        membership["branch_id"]
        if membership and membership["role"] == "BRANCH_MANAGER"
        else None
    )
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, title, created_at, updated_at
            FROM business_assistant_conversations
            WHERE business_id = ? AND user_id = ?
              AND (? IS NULL OR branch_id = ?)
            ORDER BY updated_at DESC
            LIMIT 50
            """,
            (business_id, user["id"], branch_id, branch_id),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/businesses/{business_id}/assistant/conversations/{conversation_id}")
def get_business_assistant_conversation(
    business_id: str,
    conversation_id: str,
    request: Request,
):
    user, _business, membership = get_business_context(
        request,
        business_id,
        permission="analytics.view",
    )
    branch_id = (
        membership["branch_id"]
        if membership and membership["role"] == "BRANCH_MANAGER"
        else None
    )
    with db_connection() as connection:
        conversation = connection.execute(
            """
            SELECT id FROM business_assistant_conversations
            WHERE id = ? AND business_id = ? AND user_id = ?
              AND (? IS NULL OR branch_id = ?)
            """,
            (conversation_id, business_id, user["id"], branch_id, branch_id),
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Mazungumzo hayakupatikana.")
        rows = connection.execute(
            """
            SELECT id, role, content, period_start, period_end,
                   sources_json, recommendations_json, ai_status, created_at
            FROM business_assistant_messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC
            LIMIT 100
            """,
            (conversation_id,),
        ).fetchall()
    messages = []
    for row in rows:
        item = dict(row)
        item["sources"] = json.loads(item.pop("sources_json") or "[]")
        item["recommendations"] = json.loads(item.pop("recommendations_json") or "[]")
        messages.append(item)
    return {"id": conversation_id, "messages": messages}


@app.post("/api/businesses/{business_id}/assistant/messages")
def send_business_assistant_message(
    business_id: str,
    payload: BusinessAssistantMessageInput,
    request: Request,
):
    user, _business, membership = get_business_context(
        request,
        business_id,
        permission="analytics.view",
    )
    branch_id = (
        membership["branch_id"]
        if membership and membership["role"] == "BRANCH_MANAGER"
        else None
    )
    intent = classify_question(payload.question)
    facts = _business_assistant_facts(business_id, branch_id)
    answer = render_answer(intent, facts, user["preferred_language"])
    try:
        recommendation_result = ai_recommendations(intent, facts)
    except RuntimeError as error:
        raise HTTPException(
            status_code=502,
            detail="Huduma ya AI haipatikani kwa sasa. Takwimu zako hazijabadilishwa.",
        ) from error
    recommendation_codes = recommendation_result["codes"]
    recommendations = [
        text
        for code in recommendation_codes
        if (text := recommendation_text(code, user["preferred_language"]))
    ]
    sources_by_intent = {
        "sales": ["orders"],
        "profit": ["orders"],
        "top_products": ["orders", "order_items", "products"],
        "low_stock": ["products"],
        "slow_products": ["orders", "order_items", "products"],
        "branches": ["orders", "branches"],
        "expenses": [],
    }
    sources = sources_by_intent.get(intent, [])
    conversation_id = payload.conversation_id or secrets.token_hex(16)
    user_message_id = secrets.token_hex(16)
    assistant_message_id = secrets.token_hex(16)
    with db_connection() as connection:
        if payload.conversation_id:
            conversation = connection.execute(
                """
                SELECT id FROM business_assistant_conversations
                WHERE id = ? AND business_id = ? AND user_id = ?
                  AND (? IS NULL OR branch_id = ?)
                """,
                (conversation_id, business_id, user["id"], branch_id, branch_id),
            ).fetchone()
            if conversation is None:
                raise HTTPException(status_code=404, detail="Mazungumzo hayakupatikana.")
            connection.execute(
                """
                UPDATE business_assistant_conversations
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (conversation_id,),
            )
        else:
            connection.execute(
                """
                INSERT INTO business_assistant_conversations
                    (id, business_id, branch_id, user_id, title)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, business_id, branch_id, user["id"], payload.question.strip()[:120]),
            )
        connection.execute(
            """
            INSERT INTO business_assistant_messages (id, conversation_id, role, content)
            VALUES (?, ?, 'USER', ?)
            """,
            (user_message_id, conversation_id, payload.question.strip()),
        )
        connection.execute(
            """
            INSERT INTO business_assistant_messages
                (id, conversation_id, role, content, period_start, period_end,
                 sources_json, recommendations_json, ai_status)
            VALUES (?, ?, 'ASSISTANT', ?, ?, ?, ?, ?, ?)
            """,
            (
                assistant_message_id,
                conversation_id,
                answer,
                facts["period"]["start"],
                facts["period"]["end"],
                json.dumps(sources),
                json.dumps(recommendation_codes),
                recommendation_result["status"],
            ),
        )
    record_audit(
        "BUSINESS_ASSISTANT_QUERY",
        "business",
        business_id,
        intent or "UNSUPPORTED",
        actor=user,
        request=request,
    )
    return {
        "conversation_id": conversation_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "question": payload.question.strip(),
        "answer": answer,
        "period": facts["period"],
        "sources": sources,
        "recommendations": recommendations,
        "recommendation_codes": recommendation_codes,
        "ai_status": recommendation_result["status"],
        "ai_note": ai_unavailable_text(user["preferred_language"])
        if recommendation_result["status"] == "NOT_CONFIGURED"
        else "",
    }


def get_product(product_id):
    with db_connection() as connection:
        product = connection.execute(
            """
            SELECT p.id, p.name, p.amount, p.cost_amount, p.stock_quantity, p.seller_id,
                   p.description, p.image_url, p.category, p.low_stock_threshold,
                   u.shop_name, u.full_name AS seller_name, u.store_slug
            FROM products p
            LEFT JOIN users u ON u.id = p.seller_id
            WHERE p.id = ? AND p.is_active = 1
              AND (p.seller_id IS NULL OR u.status = 'ACTIVE')
            """,
            (product_id,),
        ).fetchone()
    if product is None:
        raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
    return dict(product)


def attach_product_gallery(products):
    results = [dict(product) for product in products]
    product_ids = list(dict.fromkeys(product["id"] for product in results))
    gallery_by_product = {}
    with db_connection() as connection:
        for offset in range(0, len(product_ids), 400):
            batch = product_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT id, product_id FROM product_gallery_images
                WHERE product_id IN ({placeholders})
                ORDER BY product_id, sort_order, id
                """,
                tuple(batch),
            ).fetchall()
            for row in rows:
                gallery_by_product.setdefault(row["product_id"], []).append(row["id"])

    for product in results:
        product_id = product["id"]
        primary_url = product.get("image_url") or ""
        images = []
        if primary_url:
            primary_id = next(
                (
                    image_id
                    for image_id in gallery_by_product.get(product_id, [])
                    if primary_url == f"/api/products/{product_id}/gallery/{image_id}"
                ),
                None,
            )
            images.append({"id": primary_id, "url": primary_url, "primary": True})
        for image_id in gallery_by_product.get(product_id, []):
            image_url = f"/api/products/{product_id}/gallery/{image_id}"
            if image_url != primary_url:
                images.append({"id": image_id, "url": image_url, "primary": False})
        product["gallery_images"] = images
    return results


def attach_product_videos(products):
    results = [dict(product) for product in products]
    product_ids = list(dict.fromkeys(product["id"] for product in results))
    videos_by_product = {}
    with db_connection() as connection:
        for offset in range(0, len(product_ids), 400):
            batch = product_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT id, product_id, content_type, created_at
                FROM product_videos
                WHERE product_id IN ({placeholders})
                ORDER BY product_id, created_at, id
                """,
                tuple(batch),
            ).fetchall()
            for row in rows:
                videos_by_product.setdefault(row["product_id"], []).append(
                    {
                        "id": row["id"],
                        "url": f"/api/products/{row['product_id']}/videos/{row['id']}",
                        "content_type": row["content_type"],
                        "created_at": row["created_at"],
                    }
                )
    for product in results:
        product["videos"] = videos_by_product.get(product["id"], [])
    return results


def product_variants_for(connection, product_id):
    rows = connection.execute(
        """
        SELECT id, label, stock_quantity
        FROM product_variants
        WHERE product_id = ?
        ORDER BY LOWER(label), id
        """,
        (product_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def attach_product_variants(products):
    results = [dict(product) for product in products]
    product_ids = list(dict.fromkeys(product["id"] for product in results))
    variants_by_product = {}
    with db_connection() as connection:
        for offset in range(0, len(product_ids), 400):
            batch = product_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT id, product_id, label, stock_quantity
                FROM product_variants
                WHERE product_id IN ({placeholders})
                ORDER BY product_id, LOWER(label), id
                """,
                tuple(batch),
            ).fetchall()
            for row in rows:
                variants_by_product.setdefault(row["product_id"], []).append(
                    {
                        "id": row["id"],
                        "label": row["label"],
                        "stock_quantity": row["stock_quantity"],
                    }
                )
    for product in results:
        product["variants"] = variants_by_product.get(product["id"], [])
    return results


def validate_variant_selection(connection, product, variant_id, quantity):
    variants = product_variants_for(connection, product["id"])
    if not variants:
        if variant_id:
            raise HTTPException(status_code=404, detail="Aina ya bidhaa haikupatikana.")
        if product["stock_quantity"] < quantity:
            raise HTTPException(status_code=409, detail="Stock ya bidhaa haitoshi.")
        return {"variant_id": "", "variant_label": ""}
    if not variant_id:
        raise HTTPException(status_code=422, detail="Chagua aina ya bidhaa.")
    variant = next((row for row in variants if row["id"] == variant_id), None)
    if variant is None:
        raise HTTPException(status_code=404, detail="Aina ya bidhaa haikupatikana.")
    if variant["stock_quantity"] < quantity:
        raise HTTPException(status_code=409, detail="Stock ya aina hii ya bidhaa haitoshi.")
    return {
        "variant_id": variant["id"],
        "variant_label": variant["label"],
    }


def get_order(order_id):
    expire_pending_orders()
    with db_connection() as connection:
        order = connection.execute(
            "SELECT * FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if order is None:
        raise HTTPException(status_code=404, detail="Oda haikupatikana.")
    return dict(order)


def is_demo_mode():
    return os.getenv("DEMO_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}


_clickpesa_client = None
_clickpesa_client_credentials = None
_pesapal_client = None
_pesapal_client_credentials = None
_abliner_client = None
_abliner_client_credentials = None


def clickpesa_is_configured():
    return all(
        os.getenv(key, "").strip()
        for key in (
            "CLICKPESA_CLIENT_ID",
            "CLICKPESA_API_KEY",
            "CLICKPESA_CHECKSUM_KEY",
        )
    )


def clickpesa_live_enabled():
    return (
        not is_demo_mode()
        and os.getenv("CLICKPESA_LIVE_ENABLED", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and os.getenv("CLICKPESA_MANUAL_PAYOUTS_CONFIRMED", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and clickpesa_is_configured()
    )


def pesapal_sandbox_requested():
    return os.getenv("PESAPAL_SANDBOX_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def abliner_live_requested():
    return os.getenv("ABLINER_LIVE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def pesapal_sandbox_is_configured():
    values = tuple(
        os.getenv(key, "").strip()
        for key in (
            "PESAPAL_CONSUMER_KEY",
            "PESAPAL_CONSUMER_SECRET",
            "PESAPAL_NOTIFICATION_ID",
        )
    )
    if not all(values):
        return False
    try:
        UUID(values[2])
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def stored_pesapal_payment_url(order):
    if (
        order["payment_method"] != "PesaPal Sandbox"
        or order["payment_status"] != "PENDING"
    ):
        return None
    payment_url = order["checkout_url"] or ""
    parsed_url = urlsplit(payment_url)
    if parsed_url.scheme == "https" and parsed_url.hostname == "cybqa.pesapal.com":
        return payment_url
    return None


def pesapal_sandbox_enabled():
    return (
        is_demo_mode()
        and pesapal_sandbox_requested()
        and pesapal_sandbox_is_configured()
    )


def validate_pesapal_sandbox_configuration():
    if not pesapal_sandbox_requested():
        return False
    if not is_demo_mode():
        raise HTTPException(
            status_code=503,
            detail="PesaPal Sandbox inahitaji DEMO_MODE=true; malipo ya live hayajawezeshwa.",
        )
    if not pesapal_sandbox_is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Weka PesaPal sandbox credentials na PESAPAL_NOTIFICATION_ID "
                "sahihi kutoka IPN iliyosajiliwa."
            ),
        )
    return True


def get_pesapal_client():
    global _pesapal_client, _pesapal_client_credentials
    credentials = (
        os.getenv("PESAPAL_CONSUMER_KEY", "").strip(),
        os.getenv("PESAPAL_CONSUMER_SECRET", "").strip(),
    )
    if not all(credentials):
        raise PesapalError("PesaPal sandbox credentials hazijawekwa.")
    if credentials != _pesapal_client_credentials:
        _pesapal_client = PesapalClient(*credentials)
        _pesapal_client_credentials = credentials
    return _pesapal_client


def abliner_credentials_configured():
    return all(
        os.getenv(key, "").strip()
        for key in ("ABLINER_API_KEY", "ABLINER_WEBHOOK_SECRET")
    )


def abliner_live_enabled():
    return abliner_live_requested() and abliner_credentials_configured()


def require_abliner_live():
    if not abliner_live_requested():
        raise HTTPException(
            status_code=503,
            detail="Malipo ya Abliner hayajawashwa kwenye Render.",
        )
    if not abliner_credentials_configured():
        raise HTTPException(
            status_code=503,
            detail="Abliner inahitaji API key na webhook signing secret kwenye Render.",
        )


def get_clickpesa_client():
    global _clickpesa_client, _clickpesa_client_credentials
    credentials = (
        os.getenv("CLICKPESA_CLIENT_ID", "").strip(),
        os.getenv("CLICKPESA_API_KEY", "").strip(),
        os.getenv("CLICKPESA_CHECKSUM_KEY", "").strip(),
    )
    if not all(credentials):
        raise ClickPesaError("ClickPesa credentials hazijawekwa.")
    if credentials != _clickpesa_client_credentials:
        _clickpesa_client = ClickPesaClient(*credentials)
        _clickpesa_client_credentials = credentials
    return _clickpesa_client


def get_abliner_client():
    global _abliner_client, _abliner_client_credentials
    credentials = (
        os.getenv("ABLINER_API_KEY", "").strip(),
        os.getenv("ABLINER_WEBHOOK_SECRET", "").strip(),
    )
    if not all(credentials):
        raise AblinerError("Abliner credentials hazijawekwa.")
    if credentials != _abliner_client_credentials:
        _abliner_client = AblinerClient(*credentials)
        _abliner_client_credentials = credentials
    return _abliner_client


def public_app_url(request_base_url=None):
    frontend_url = os.getenv("FRONTEND_URL", "").strip()
    public_url = os.getenv("PUBLIC_APP_URL", "").strip()
    candidates = [
        frontend_url,
        public_url,
        str(request_base_url).rstrip("/") if request_base_url else "",
        "https://liparahisi.onrender.com",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate = candidate.rstrip("/")
        parsed = urlsplit(candidate)
        host = parsed.hostname or ""
        if (
            parsed.scheme == "https"
            and parsed.netloc
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and not host.startswith("192.")
            and not host.startswith("10.")
            and not host.startswith("172.")
            and host not in {"127.0.0.1", "localhost"}
        ):
            return candidate
    raise HTTPException(
        status_code=503,
        detail="PUBLIC_APP_URL haukubali: tumia HTTPS origin ya tovuti ya umma.",
    )


def generate_tracking_code(connection):
    year = date.today().year
    for _ in range(50):
        tracking_code = f"LR-TZ-{year}-{secrets.randbelow(900000) + 100000}"
        existing = connection.execute(
            "SELECT 1 FROM orders WHERE tracking_code = ?",
            (tracking_code,),
        ).fetchone()
        if existing is None:
            return tracking_code
    raise RuntimeError("Could not allocate a unique tracking number.")


def commission_amount(amount):
    return (amount * COMMISSION_PERCENT + 50) // 100


def expire_pending_orders(connection=None):
    now = time.time()
    should_close = connection is None
    if connection is None:
        context = db_connection()
        connection = context.__enter__()
    try:
        connection.execute(
            """
            UPDATE orders
            SET payment_status = 'EXPIRED'
            WHERE payment_status = 'PENDING'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            """,
            (now,),
        )
    finally:
        if should_close:
            connection.commit()
            context.__exit__(None, None, None)


def send_payment_notification(order_ids, failed=False, demo=False):
    orders = []
    with db_connection() as connection:
        for order_id in order_ids:
            row = connection.execute(
                """
                SELECT orders.*, products.name AS product_name,
                       sellers.email AS seller_email,
                       sellers.full_name AS seller_name
                FROM orders
                LEFT JOIN products ON products.id = orders.product_id
                LEFT JOIN users AS sellers ON sellers.id = orders.seller_id
                WHERE orders.order_id = ?
                """,
                (order_id,),
            ).fetchone()
            if row is not None:
                orders.append(dict(row))
    if not orders:
        return False
    if failed:
        message = "Malipo ya checkout yameshindwa."
        record_system_alert("PAYMENT_FAILED", message)
        send_system_alert_email("LipaRahisi: malipo yameshindwa", message)
    recipients = {
        (order.get("buyer_email") or "").strip().lower()
        for order in orders
    }
    if len(recipients) != 1 or not next(iter(recipients), ""):
        logger.warning("Payment email skipped: checkout has no single valid recipient.")
        return False
    recipient = next(iter(recipients))
    full_name = orders[0].get("buyer_name") or "Mteja"
    buyer_email_sent = send_app_email(
        "payment_failed" if failed else "payment_receipt",
        recipient,
        lambda app_url: payment_email(
            full_name,
            orders,
            app_url,
            failed=failed,
            demo=demo,
        ),
    )
    if failed:
        return buyer_email_sent

    seller_orders = {}
    for order in orders:
        seller_email = (order.get("seller_email") or "").strip()
        seller_id = order.get("seller_id")
        if not seller_id or not seller_email:
            logger.warning("Seller payment email skipped: seller email is unavailable.")
            continue
        seller_orders.setdefault(
            (seller_id, seller_email.lower()),
            {"email": seller_email, "name": order.get("seller_name") or "Muuzaji", "orders": []},
        )["orders"].append(order)

    for seller in seller_orders.values():
        send_app_email(
            "seller_payment_receipt",
            seller["email"],
            lambda app_url, seller=seller: seller_payment_email(
                seller["name"],
                seller["orders"],
                app_url,
                demo=demo,
            ),
        )
    return buyer_email_sent


def complete_order_group(order_ids, provider_verified=False, background_tasks=None):
    expire_pending_orders()
    newly_completed_ids = []
    with db_connection() as connection:
        orders = []
        for order_id in order_ids:
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Oda haikupatikana.")
            order = dict(row)
            if not provider_verified:
                if not order["order_id"].startswith("DEMO"):
                    raise HTTPException(status_code=400, detail="Hili si oda ya majaribio.")
                if order["payment_status"] == "EXPIRED":
                    raise HTTPException(
                        status_code=410,
                        detail="Oda hii imekwisha muda; anzisha oda mpya.",
                    )
            orders.append(order)

        record_coupon_redemption(connection, orders)
        for order in orders:
            if order["payment_status"] == "COMPLETED":
                continue
            if order["payment_method"] == "PesaPal Sandbox":
                if not provider_verified:
                    raise HTTPException(
                        status_code=400,
                        detail="Oda ya PesaPal Sandbox inahitaji uthibitisho wa seva.",
                    )
                connection.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'COMPLETED', platform_fee = 0,
                        seller_payout = 0, net_profit = 0,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE order_id = ? AND payment_status = 'PENDING'
                    """,
                    (order["order_id"],),
                )
                connection.execute(
                    """
                    INSERT INTO delivery_tracking_events
                        (id, order_id, status, description)
                    VALUES (?, ?, 'ORDER_CONFIRMED', 'Payment verified and order confirmed')
                    """,
                    (secrets.token_hex(16), order["order_id"]),
                )
                continue
            fee = commission_amount(order["amount"])
            payout = order["amount"] - fee - order["provider_fee"]
            profit = payout - order["product_cost"]
            claimed = connection.execute(
                """
                UPDATE orders
                SET payment_status = 'COMPLETED',
                    platform_fee = ?, seller_payout = ?, net_profit = ?,
                    completed_at = CURRENT_TIMESTAMP
                WHERE order_id = ? AND payment_status != 'COMPLETED'
                """,
                (fee, payout, profit, order["order_id"]),
            )
            if claimed.rowcount == 0:
                continue
            cancel_checkout_reminders_for_order(connection, order)
            newly_completed_ids.append(order["order_id"])
            connection.execute(
                """
                INSERT INTO delivery_tracking_events
                    (id, order_id, status, description)
                VALUES (?, ?, 'ORDER_CONFIRMED', 'Payment verified and order confirmed')
                """,
                (secrets.token_hex(16), order["order_id"]),
            )
            items = connection.execute(
                """
                SELECT product_id, quantity, variant_id
                FROM order_items WHERE order_id = ?
                """,
                (order["order_id"],),
            ).fetchall()
            if items:
                for item in items:
                    if item["variant_id"]:
                        variant_update = connection.execute(
                            """
                            UPDATE product_variants
                            SET stock_quantity = stock_quantity - ?
                            WHERE id = ? AND product_id = ? AND stock_quantity >= ?
                            """,
                            (
                                item["quantity"],
                                item["variant_id"],
                                item["product_id"],
                                item["quantity"],
                            ),
                        )
                        if variant_update.rowcount == 0:
                            raise HTTPException(
                                status_code=409,
                                detail="Stock ya aina hii ya bidhaa imeisha.",
                            )
                    result = connection.execute(
                        """
                        UPDATE products
                        SET stock_quantity = stock_quantity - ?
                        WHERE id = ? AND stock_quantity >= ?
                        """,
                        (item["quantity"], item["product_id"], item["quantity"]),
                    )
                    if result.rowcount == 0:
                        raise HTTPException(status_code=409, detail="Stock ya bidhaa imeisha.")
                    product = connection.execute(
                        "SELECT seller_id FROM products WHERE id = ?",
                        (item["product_id"],),
                    ).fetchone()
                    record_inventory_movement(
                        connection,
                        item["product_id"],
                        product["seller_id"] if product else order["seller_id"],
                        None,
                        -item["quantity"],
                        "ORDER_SALE",
                    )
            else:
                product = connection.execute(
                    "SELECT stock_quantity FROM products WHERE id = ?",
                    (order["product_id"],),
                ).fetchone()
                if product and product["stock_quantity"] <= 0:
                    raise HTTPException(status_code=409, detail="Stock ya bidhaa imeisha.")
                connection.execute(
                    "UPDATE products SET stock_quantity = stock_quantity - 1 WHERE id = ?",
                    (order["product_id"],),
                )
                product = connection.execute(
                    "SELECT seller_id FROM products WHERE id = ?",
                    (order["product_id"],),
                ).fetchone()
                record_inventory_movement(
                    connection,
                    order["product_id"],
                    product["seller_id"] if product else order["seller_id"],
                    None,
                    -1,
                    "ORDER_SALE",
                )
    completed_orders = [get_order(order_id) for order_id in order_ids]
    if newly_completed_ids:
        for completed_order_id in newly_completed_ids:
            completed_order = get_order(completed_order_id)
            buyer_id = completed_order["buyer_id"]
            if buyer_id:
                queue_user_notification(
                    buyer_id,
                    "ORDER_CONFIRMED",
                    {"order_id": completed_order_id},
                    link=f"/pay/{completed_order['product_id']}?order_id={completed_order_id}",
                    email=completed_order["buyer_email"],
                    phone=completed_order["buyer_phone"],
                )
        if background_tasks is not None:
            background_tasks.add_task(
                send_payment_notification,
                newly_completed_ids,
                demo=not provider_verified,
            )
        else:
            send_payment_notification(
                newly_completed_ids,
                demo=not provider_verified,
            )
    return completed_orders


def complete_demo_order(order_id):
    orders = complete_order_group([order_id])
    return orders[0]


def orders_for_provider_reference(order_reference):
    with db_connection() as connection:
        anchor = connection.execute(
            "SELECT order_id, checkout_id FROM orders WHERE provider_reference = ?",
            (order_reference,),
        ).fetchone()
        if anchor is None:
            raise HTTPException(status_code=404, detail="Rejea ya malipo haikupatikana.")
        if anchor["checkout_id"]:
            rows = connection.execute(
                "SELECT * FROM orders WHERE checkout_id = ? ORDER BY created_at",
                (anchor["checkout_id"],),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?",
                (anchor["order_id"],),
            ).fetchall()
    return [dict(row) for row in rows]


def clickpesa_orders_for_reference(order_reference):
    return orders_for_provider_reference(order_reference)


def update_clickpesa_reference_status(order_reference, status, payment_id=""):
    orders = clickpesa_orders_for_reference(order_reference)
    newly_failed_ids = []
    with db_connection() as connection:
        for order in orders:
            updated = connection.execute(
                """
                UPDATE orders
                SET payment_status = ?,
                    provider_payment_id = CASE WHEN ? <> '' THEN ? ELSE provider_payment_id END
                WHERE order_id = ? AND payment_status != 'COMPLETED'
                """,
                (status, payment_id, payment_id, order["order_id"]),
            )
            if (
                status == "FAILED"
                and order["payment_status"] != "FAILED"
                and updated.rowcount
            ):
                newly_failed_ids.append(order["order_id"])
    if newly_failed_ids:
        send_payment_notification(newly_failed_ids, failed=True)


def complete_clickpesa_payment(
    order_reference,
    collected_amount,
    collected_currency,
    payment_id="",
):
    if collected_currency != "TZS":
        raise HTTPException(status_code=422, detail="Sarafu ya malipo ya ClickPesa si sahihi.")
    try:
        amount = Decimal(str(collected_amount))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="Kiasi cha malipo ya ClickPesa si sahihi.")
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise HTTPException(status_code=422, detail="Kiasi cha malipo ya ClickPesa si sahihi.")

    orders = clickpesa_orders_for_reference(order_reference)
    expected_amount = sum(order["amount"] for order in orders)
    if amount != expected_amount:
        raise HTTPException(status_code=422, detail="Kiasi cha malipo hakilingani na oda.")
    completed = complete_order_group(
        [order["order_id"] for order in orders],
        provider_verified=True,
    )
    if payment_id:
        with db_connection() as connection:
            connection.execute(
                "UPDATE orders SET provider_payment_id = ? WHERE provider_reference = ?",
                (payment_id, order_reference),
            )
    return completed


async def begin_clickpesa_payment(order_reference, phone_number, amount):
    try:
        result = await get_clickpesa_client().initiate_ussd_push(
            phone_number,
            amount,
            order_reference,
        )
    except ClickPesaError as error:
        if error.uncertain:
            logger.warning(
                "ClickPesa payment outcome is uncertain for reference %s.",
                order_reference,
            )
            return "PENDING"
        update_clickpesa_reference_status(order_reference, "FAILED")
        raise HTTPException(status_code=502, detail=str(error)) from error

    status = result["status"]
    payment_id = result.get("id", "")
    if status in {"SUCCESS", "SETTLED"}:
        if "collectedAmount" in result and "collectedCurrency" in result:
            complete_clickpesa_payment(
                order_reference,
                result["collectedAmount"],
                result["collectedCurrency"],
                payment_id,
            )
            return "COMPLETED"
        update_clickpesa_reference_status(
            order_reference,
            "PENDING",
            payment_id,
        )
        return "PENDING"
    if status == "FAILED":
        update_clickpesa_reference_status(order_reference, "FAILED", payment_id)
        return "FAILED"
    update_clickpesa_reference_status(order_reference, "PENDING", payment_id)
    return "PENDING"


def update_pesapal_reference_status(order_reference, status, payment_id=""):
    orders = clickpesa_orders_for_reference(order_reference)
    if any(order["payment_method"] != "PesaPal Sandbox" for order in orders):
        raise HTTPException(status_code=422, detail="Rejea si ya oda ya PesaPal Sandbox.")
    with db_connection() as connection:
        for order in orders:
            connection.execute(
                """
                UPDATE orders
                SET payment_status = ?,
                    provider_payment_id = CASE WHEN ? <> '' THEN ? ELSE provider_payment_id END
                WHERE order_id = ? AND payment_status = 'PENDING'
                """,
                (status, payment_id, payment_id, order["order_id"]),
            )


def validate_abliner_transaction(order_reference, amount, currency):
    if currency != "TZS":
        raise HTTPException(status_code=422, detail="Sarafu ya malipo ya Abliner si sahihi.")
    orders = orders_for_provider_reference(order_reference)
    if any(order["payment_method"] != ABLINER_PAYMENT_METHOD for order in orders):
        raise HTTPException(status_code=422, detail="Rejea si ya oda ya Abliner.")
    try:
        collected_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="Kiasi cha malipo ya Abliner si sahihi.")
    expected_amount = sum(order["amount"] for order in orders)
    if (
        not collected_amount.is_finite()
        or collected_amount != collected_amount.to_integral_value()
        or collected_amount != expected_amount
    ):
        raise HTTPException(status_code=422, detail="Kiasi cha malipo hakilingani na oda.")
    return orders


def update_abliner_reference_status(
    order_reference,
    status,
    payment_id="",
    background_tasks=None,
):
    orders = orders_for_provider_reference(order_reference)
    if any(order["payment_method"] != ABLINER_PAYMENT_METHOD for order in orders):
        raise HTTPException(status_code=422, detail="Rejea si ya oda ya Abliner.")
    newly_failed_ids = []
    with db_connection() as connection:
        for order in orders:
            updated = connection.execute(
                """
                UPDATE orders
                SET payment_status = ?,
                    provider_payment_id = CASE WHEN ? <> '' THEN ? ELSE provider_payment_id END
                WHERE order_id = ? AND payment_status = 'PENDING'
                """,
                (status, payment_id, payment_id, order["order_id"]),
            )
            if status == "FAILED" and updated.rowcount:
                newly_failed_ids.append(order["order_id"])
    if newly_failed_ids:
        if background_tasks is not None:
            background_tasks.add_task(
                send_payment_notification,
                newly_failed_ids,
                failed=True,
            )
        else:
            send_payment_notification(newly_failed_ids, failed=True)


async def begin_pesapal_payment(
    order_reference,
    amount,
    description,
    callback_url,
    cancellation_url,
    buyer_name,
    buyer_email,
    buyer_phone,
):
    try:
        result = await get_pesapal_client().submit_order(
            merchant_reference=order_reference,
            amount=amount,
            description=description,
            callback_url=callback_url,
            cancellation_url=cancellation_url,
            notification_id=os.getenv("PESAPAL_NOTIFICATION_ID", "").strip(),
            buyer_name=buyer_name,
            buyer_email=buyer_email,
            buyer_phone=buyer_phone,
        )
    except PesapalError as error:
        logger.warning(
            "PesaPal sandbox checkout failed for reference %s (uncertain=%s).",
            order_reference,
            error.uncertain,
        )
        update_pesapal_reference_status(order_reference, "FAILED")
        return "FAILED", ""

    with db_connection() as connection:
        connection.execute(
            """
            UPDATE orders
            SET checkout_url = ?, provider_payment_id = ?
            WHERE provider_reference = ? AND payment_status = 'PENDING'
            """,
            (
                result["redirect_url"],
                result["order_tracking_id"],
                order_reference,
            ),
        )
    return "PENDING", result["redirect_url"]


async def verify_pesapal_transaction(tracking_id, order_reference):
    if not pesapal_sandbox_enabled():
        raise HTTPException(
            status_code=503,
            detail="PesaPal Sandbox imezimwa au haijasanidiwa.",
        )
    expire_pending_orders()
    orders = clickpesa_orders_for_reference(order_reference)
    if any(order["payment_method"] != "PesaPal Sandbox" for order in orders):
        raise HTTPException(status_code=422, detail="Rejea si ya oda ya PesaPal Sandbox.")
    try:
        result = await get_pesapal_client().get_transaction_status(tracking_id)
    except PesapalError as error:
        logger.warning("PesaPal status verification failed for reference %s.", order_reference)
        raise HTTPException(
            status_code=502,
            detail="PesaPal haijaweza kuthibitisha hali ya malipo kwa sasa.",
        ) from error

    if result.get("merchant_reference") != order_reference:
        raise HTTPException(status_code=422, detail="Rejea ya PesaPal hailingani na oda.")
    try:
        status_code = int(result.get("status_code"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=502, detail="PesaPal imerudisha hali isiyotambulika.")

    if status_code == 1:
        if result.get("currency") != "TZS":
            raise HTTPException(status_code=422, detail="Sarafu ya malipo ya PesaPal si sahihi.")
        try:
            amount = Decimal(str(result.get("amount")))
        except (InvalidOperation, ValueError):
            raise HTTPException(status_code=422, detail="Kiasi cha malipo ya PesaPal si sahihi.")
        expected_amount = sum(order["amount"] for order in orders)
        if (
            not amount.is_finite()
            or amount != amount.to_integral_value()
            or amount != expected_amount
        ):
            raise HTTPException(status_code=422, detail="Kiasi cha malipo hakilingani na oda.")
        if any(order["payment_status"] not in {"PENDING", "COMPLETED"} for order in orders):
            raise HTTPException(status_code=409, detail="Oda hii haiwezi kukamilishwa tena.")
        completed_orders = complete_order_group(
            [order["order_id"] for order in orders],
            provider_verified=True,
        )
        if any(order["payment_status"] != "COMPLETED" for order in completed_orders):
            raise HTTPException(status_code=409, detail="Oda hii imekwisha muda.")
        update_pesapal_reference_status(
            order_reference,
            "COMPLETED",
            tracking_id,
        )
    elif status_code in {2, 3}:
        update_pesapal_reference_status(order_reference, "FAILED", tracking_id)
    elif status_code != 0:
        raise HTTPException(status_code=502, detail="PesaPal imerudisha hali isiyotambulika.")
    return result


async def pesapal_notification_fields(request):
    fields = dict(request.query_params)
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(status_code=413, detail="Ombi la PesaPal ni kubwa mno.")
    if body:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            if content_type == "application/json":
                body_fields = json.loads(body)
            elif content_type == "application/x-www-form-urlencoded":
                parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
                body_fields = {key: values[-1] for key, values in parsed.items() if values}
            else:
                body_fields = {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail="Taarifa za PesaPal si sahihi.") from error
        if not isinstance(body_fields, dict):
            raise HTTPException(status_code=400, detail="Taarifa za PesaPal si sahihi.")
        fields.update(body_fields)
    tracking_id = (
        fields.get("OrderTrackingId")
        or fields.get("orderTrackingId")
        or fields.get("order_tracking_id")
    )
    order_reference = (
        fields.get("OrderMerchantReference")
        or fields.get("orderMerchantReference")
        or fields.get("merchant_reference")
    )
    if not isinstance(tracking_id, str) or not tracking_id:
        raise HTTPException(status_code=400, detail="PesaPal haikutuma OrderTrackingId.")
    if not isinstance(order_reference, str) or not order_reference:
        raise HTTPException(status_code=400, detail="PesaPal haikutuma rejea ya oda.")
    return tracking_id, order_reference


async def process_pesapal_webhook(request: Request):
    tracking_id = request.query_params.get("OrderTrackingId", "")
    order_reference = request.query_params.get("OrderMerchantReference", "")
    try:
        tracking_id, order_reference = await pesapal_notification_fields(request)
        await verify_pesapal_transaction(tracking_id, order_reference)
    except HTTPException as error:
        logger.warning("PesaPal IPN verification failed: %s", error.detail)
        return JSONResponse(
            status_code=500,
            content={
                "orderNotificationType": "IPNCHANGE",
                "orderTrackingId": tracking_id,
                "orderMerchantReference": order_reference,
                "status": 500,
            },
        )
    return {
        "orderNotificationType": "IPNCHANGE",
        "orderTrackingId": tracking_id,
        "orderMerchantReference": order_reference,
        "status": 200,
    }


async def process_pesapal_callback(request: Request):
    tracking_id, order_reference = await pesapal_notification_fields(request)
    await verify_pesapal_transaction(tracking_id, order_reference)
    orders = clickpesa_orders_for_reference(order_reference)
    order = orders[0]
    app_url = public_app_url(str(request.base_url))
    if order["checkout_id"]:
        return RedirectResponse(f"{app_url}/cart/{quote(order['checkout_id'])}")
    return RedirectResponse(
        f"{app_url}/pay/{quote(order['product_id'])}"
        f"?order_id={quote(order['order_id'])}&sandbox=1"
    )

async def begin_abliner_payment(order_reference, phone_number, amount, callback_url):
    try:
        result = await get_abliner_client().create_mobile_deposit(
            amount=amount,
            phone=phone_number,
            reference=order_reference,
            callback_url=callback_url,
        )
    except AblinerError as error:
        logger.warning(
            "Abliner deposit failed for reference %s (uncertain=%s).",
            order_reference,
            error.uncertain,
        )
        if not error.uncertain:
            update_abliner_reference_status(order_reference, "FAILED")
            return "FAILED"
        return "PENDING"

    orders = orders_for_provider_reference(order_reference)
    with db_connection() as connection:
        for order in orders:
            connection.execute(
                "UPDATE orders SET provider_payment_id = ? WHERE order_id = ?",
                (result["id"], order["order_id"]),
            )
    return "PENDING"


def complete_abliner_payment(
    order_reference,
    amount,
    currency,
    payment_id="",
    background_tasks=None,
):
    orders = validate_abliner_transaction(order_reference, amount, currency)
    if any(
        order["payment_status"] not in {"PENDING", "COMPLETED", "EXPIRED"}
        for order in orders
    ):
        raise HTTPException(status_code=409, detail="Oda hii haiwezi kukamilishwa tena.")
    completed = complete_order_group(
        [order["order_id"] for order in orders],
        provider_verified=True,
        background_tasks=background_tasks,
    )
    if payment_id:
        with db_connection() as connection:
            for order in orders:
                connection.execute(
                    "UPDATE orders SET provider_payment_id = ? WHERE order_id = ?",
                    (payment_id, order["order_id"]),
                )
    return completed


async def process_abliner_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    webhook_secret = os.getenv("ABLINER_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="Abliner webhook signing secret haijawekwa.",
        )
    raw_body = await request.body()
    if len(raw_body) > 65536:
        raise HTTPException(status_code=413, detail="Ombi la webhook ya Abliner ni kubwa mno.")
    timestamp = request.headers.get("x-webhook-timestamp", "")
    signature = request.headers.get("x-webhook-signature", "")
    if not verify_webhook_signature(raw_body, timestamp, signature, webhook_secret):
        raise HTTPException(status_code=401, detail="Saini ya webhook ya Abliner si sahihi.")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Taarifa za webhook ya Abliner si sahihi.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Taarifa za webhook ya Abliner si sahihi.")

    event = payload.get("event")
    if event not in {"transaction.completed", "transaction.failed"}:
        return {"received": True, "ignored": True}
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Taarifa za malipo ya Abliner si sahihi.")
    if data.get("type") != "deposit":
        return {"received": True, "ignored": True}
    order_reference = data.get("customer_reference")
    if not isinstance(order_reference, str) or not order_reference:
        raise HTTPException(status_code=400, detail="Webhook ya Abliner haina rejea ya oda.")
    if event == "transaction.failed":
        if data.get("status") != "failed":
            raise HTTPException(status_code=400, detail="Hali ya webhook ya Abliner si sahihi.")
        validate_abliner_transaction(
            order_reference,
            data.get("amount"),
            data.get("currency"),
        )
        update_abliner_reference_status(
            order_reference,
            "FAILED",
            data.get("id", ""),
            background_tasks,
        )
        return {"received": True}
    if data.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Hali ya webhook ya Abliner si sahihi.")
    complete_abliner_payment(
        order_reference,
        data.get("amount"),
        data.get("currency"),
        data.get("id", ""),
        background_tasks,
    )
    return {"received": True}


def process_clickpesa_webhook(payload: dict):
    checksum_key = os.getenv("CLICKPESA_CHECKSUM_KEY", "").strip()
    if not verify_webhook_checksum(payload, checksum_key):
        raise HTTPException(status_code=401, detail="Saini ya callback ya ClickPesa si sahihi.")
    event = payload.get("event")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Taarifa za callback ya ClickPesa si sahihi.")
    client_id = data.get("clientId")
    configured_client_id = os.getenv("CLICKPESA_CLIENT_ID", "").strip()
    if client_id and client_id != configured_client_id:
        raise HTTPException(status_code=401, detail="Callback imetoka kwa akaunti isiyotambulika.")
    if event not in {"PAYMENT RECEIVED", "PAYMENT FAILED"}:
        return {"received": True, "ignored": True}

    order_reference = data.get("orderReference")
    if not isinstance(order_reference, str) or not order_reference:
        raise HTTPException(status_code=400, detail="Callback haina rejea ya oda.")
    if event == "PAYMENT FAILED":
        update_clickpesa_reference_status(
            order_reference,
            "FAILED",
            data.get("id", ""),
        )
        return {"received": True}

    if data.get("status") not in {"SUCCESS", "SETTLED"}:
        raise HTTPException(status_code=409, detail="Malipo ya ClickPesa bado hayajathibitishwa.")
    complete_clickpesa_payment(
        order_reference,
        data.get("collectedAmount"),
        data.get("collectedCurrency"),
        data.get("paymentReference", ""),
    )
    return {"received": True}


@app.get("/api/notifications/preferences")
def notification_preferences(request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM notification_preferences WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO notification_preferences (user_id) VALUES (?)",
                (user["id"],),
            )
            row = connection.execute(
                "SELECT * FROM notification_preferences WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
    return dict(row)


@app.put("/api/notifications/preferences")
def update_notification_preferences(
    payload: NotificationPreferenceInput,
    request: Request,
):
    user = get_current_user(request)
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO notification_preferences
                (user_id, email_enabled, sms_enabled, web_push_enabled,
                 mobile_push_enabled, whatsapp_enabled, security_alerts_enabled,
                 marketing_enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                email_enabled = excluded.email_enabled,
                sms_enabled = excluded.sms_enabled,
                web_push_enabled = excluded.web_push_enabled,
                mobile_push_enabled = excluded.mobile_push_enabled,
                whatsapp_enabled = excluded.whatsapp_enabled,
                security_alerts_enabled = excluded.security_alerts_enabled,
                marketing_enabled = excluded.marketing_enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user["id"],
                payload.email_enabled,
                payload.sms_enabled,
                payload.web_push_enabled,
                payload.mobile_push_enabled,
                payload.whatsapp_enabled,
                payload.security_alerts_enabled,
                payload.marketing_enabled,
            ),
        )
    record_audit("NOTIFICATION_PREFERENCES_UPDATED", "account", user["id"], "", actor=user, request=request)
    return payload.model_dump()


@app.post("/api/notifications/subscriptions")
def save_push_subscription(payload: PushSubscriptionInput, request: Request):
    user = get_current_user(request)
    if not payload.subscription:
        raise HTTPException(status_code=422, detail="Push subscription haipo.")
    subscription_json = json.dumps(payload.subscription, sort_keys=True, separators=(",", ":"))
    subscription_id = secrets.token_hex(16)
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO push_subscriptions
                (id, user_id, channel, subscription_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id, subscription_json) DO UPDATE SET
                is_active = 1, updated_at = CURRENT_TIMESTAMP
            """,
            (subscription_id, user["id"], payload.channel, subscription_json),
        )
    record_audit("NOTIFICATION_PUSH_SUBSCRIPTION_ADDED", "account", user["id"], payload.channel, actor=user, request=request)
    return {"channel": payload.channel, "active": True}


@app.delete("/api/notifications/subscriptions/{subscription_id}")
def remove_push_subscription(subscription_id: str, request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        result = connection.execute(
            "UPDATE push_subscriptions SET is_active = 0 WHERE id = ? AND user_id = ?",
            (subscription_id, user["id"]),
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Push subscription haikupatikana.")
    record_audit("NOTIFICATION_PUSH_SUBSCRIPTION_REMOVED", "account", user["id"], "", actor=user, request=request)
    return {"active": False}


@app.get("/api/notifications/history")
def notification_history(request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, event, channel, title, body, status,
                   provider_reference, error, link, created_at, delivered_at
            FROM notification_history
            WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 500
            """,
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/config/push")
def public_push_configuration():
    return {
        "vapid_public_key": os.getenv("VAPID_PUBLIC_KEY", "").strip(),
        "web_push_configured": bool(os.getenv("VAPID_PUBLIC_KEY", "").strip()),
    }


@app.get("/api/admin/notification-settings")
def admin_notification_settings(request: Request):
    require_admin(request)
    notification_url = os.getenv("NOTIFICATION_SERVICE_URL", "").strip()
    return {
        "email": {
            "configured": email_is_configured(),
            "provider": email_provider(),
        },
        "sms": {
            "configured": bool(
                os.getenv("SMS_PROVIDER", "").strip().lower() == "twilio"
                and os.getenv("TWILIO_ACCOUNT_SID", "").strip()
                and os.getenv("TWILIO_AUTH_TOKEN", "").strip()
                and os.getenv("TWILIO_FROM_NUMBER", "").strip()
            ),
            "provider": os.getenv("SMS_PROVIDER", "").strip().lower() or None,
        },
        "whatsapp": {
            "configured": bool(
                os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
                and os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
            ),
            "provider": "whatsapp_cloud_api",
        },
        "web_push": {
            "configured": bool(
                os.getenv("VAPID_PRIVATE_KEY", "").strip()
                and os.getenv("VAPID_PUBLIC_KEY", "").strip()
                and os.getenv("VAPID_SUBJECT", "").strip()
            ),
            "provider": "vapid",
        },
        "mobile_push": {
            "configured": bool(os.getenv("FCM_SERVER_KEY", "").strip()),
            "provider": "fcm",
        },
        "queue": {
            "service_configured": bool(notification_url),
            "service_url": notification_url,
        },
    }


@app.get("/api/health")
def health():
    from backend.backup import backup_is_configured

    checks = {"database": "ok", "redis": "disabled", "backup": "configured" if backup_is_configured() else "not_configured"}
    try:
        with db_connection() as connection:
            connection.execute("SELECT 1").fetchone()
    except (sqlite3.Error, psycopg.Error, RuntimeError):
        logger.exception("Database health check failed.")
        checks["database"] = "unavailable"
    if os.getenv("REDIS_HOST", "").strip() or os.getenv("REDIS_PORT", "").strip():
        try:
            client = get_redis_client()
            client.ping()
            checks["redis"] = "ok"
        except (RedisError, RuntimeError, ValueError):
            logger.exception("Redis health check failed.")
            checks["redis"] = "unavailable"
    required_checks = ("database", "redis")
    status = "ok" if all(checks[key] in {"ok", "disabled"} for key in required_checks) else "degraded"
    return JSONResponse(
        status_code=200 if status == "ok" else 503,
        content={"status": status, "checks": checks},
    )


def system_health(request: Request):
    require_admin(request)
    from backend.backup import backup_is_configured, last_backup_at

    database_status = "ok"
    last_backup = None
    try:
        with db_connection() as connection:
            connection.execute("SELECT 1").fetchone()
            last_backup = connection.execute(
                """
                SELECT object_key, created_at FROM backup_runs
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
    except (sqlite3.Error, psycopg.Error, RuntimeError):
        logger.exception("Admin database health check failed.")
        database_status = "unavailable"

    redis_status = "disabled"
    if os.getenv("REDIS_HOST", "").strip() or os.getenv("REDIS_PORT", "").strip():
        try:
            get_redis_client().ping()
            redis_status = "ok"
        except (RedisError, RuntimeError, ValueError):
            logger.exception("Admin Redis health check failed.")
            redis_status = "unavailable"

    cutoff = time.time() - 60 * 60
    with _server_error_lock:
        while _server_error_times and _server_error_times[0] < cutoff:
            _server_error_times.popleft()
        recent_server_errors = len(_server_error_times)
    backup_configured = backup_is_configured()
    return {
        "status": "ok" if database_status == "ok" and redis_status != "unavailable" else "degraded",
        "uptime_seconds": int(time.time() - _started_at),
        "database": database_status,
        "redis": redis_status,
        "backups": {
            "configured": backup_configured,
            "last_success_at": (
                last_backup["created_at"] if last_backup else last_backup_at
            ),
            "last_object_key": last_backup["object_key"] if last_backup else "",
        },
        "server_errors_last_hour": recent_server_errors,
    }


@app.get("/api/config/integrations")
def integration_status():
    selcom_configured = all(
        os.getenv(key, "").strip()
        for key in ("SELCOM_BASE_URL", "SELCOM_API_KEY", "SELCOM_API_SECRET", "SELCOM_VENDOR")
    )
    notification_service_url = os.getenv("NOTIFICATION_SERVICE_URL", "").strip()
    notification_service_available = False
    configured_email_provider = email_provider()
    email_configured = False
    if notification_service_url:
        notification_service_token = os.getenv(
            "NOTIFICATION_SERVICE_TOKEN",
            "",
        ).strip()
        try:
            with httpx.Client(timeout=2.0) as client:
                response = client.get(
                    f"{notification_service_url.rstrip('/')}/internal/status",
                    headers={"X-Internal-Token": notification_service_token},
                )
                response.raise_for_status()
                notification_status = response.json()
            notification_service_available = True
            configured_email_provider = notification_status.get(
                "email_provider",
                configured_email_provider,
            )
            email_configured = notification_status.get(
                "email_configured",
                notification_status.get("smtp_configured"),
            ) is True
        except (httpx.RequestError, ValueError):
            email_configured = False
        except httpx.HTTPStatusError:
            email_configured = False
    else:
        email_configured = email_is_configured()
    smtp_configured = configured_email_provider == "smtp" and email_configured
    return {
        "demo_mode": is_demo_mode(),
        "live_payments_enabled": clickpesa_live_enabled(),
        "clickpesa_configured": clickpesa_is_configured(),
        "clickpesa_live_enabled": clickpesa_live_enabled(),
        "pesapal_sandbox_configured": pesapal_sandbox_is_configured(),
        "pesapal_sandbox_requested": pesapal_sandbox_requested(),
        "pesapal_sandbox_enabled": pesapal_sandbox_enabled(),
        "pesapal_live_enabled": False,
        "abliner_live_requested": abliner_live_requested(),
        "abliner_credentials_configured": abliner_credentials_configured(),
        "abliner_live_enabled": abliner_live_enabled(),
        "selcom_configured": selcom_configured,
        "smtp_configured": smtp_configured,
        "email_configured": email_configured,
        "email_provider": configured_email_provider,
        "notification_service_available": notification_service_available,
    }


def create_internal_service_client(service_name: str):
    service_url_key = f"{service_name.upper()}_SERVICE_URL"
    if os.getenv(service_url_key, "").strip():
        return httpx.AsyncClient(timeout=20.0)

    service_app = IN_PROCESS_SERVICE_APPS.get(service_name)
    if service_app is None:
        return httpx.AsyncClient(timeout=20.0)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=service_app,
            raise_app_exceptions=False,
        ),
        base_url=f"http://{service_name}",
        timeout=20.0,
    )


def create_admin_service_client():
    return create_internal_service_client("admin")


def create_catalog_service_client():
    return create_internal_service_client("catalog")


def create_identity_service_client():
    return create_internal_service_client("identity")


def create_orders_service_client():
    return create_internal_service_client("orders")


async def proxy_internal_api(
    request: Request,
    path: str,
    service_name: str,
    service_url_key: str,
    service_token_key: str,
    service_token_header: str,
    create_client,
):
    service_url = os.getenv(service_url_key, "").strip().rstrip("/")
    service_token = os.getenv(service_token_key, "").strip()
    if not service_token or (
        not service_url and service_name.lower() not in IN_PROCESS_SERVICE_APPS
    ):
        raise HTTPException(
            status_code=503,
            detail=f"{service_name} API haijawekwa.",
        )

    hop_by_hop_headers = {
        "connection",
        "content-encoding",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "x-admin-service-token",
        "x-catalog-service-token",
        "x-identity-service-token",
        "x-orders-service-token",
    }
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in hop_by_hop_headers
    }
    headers[service_token_header] = service_token

    try:
        async with create_client() as client:
            upstream = await client.request(
                request.method,
                f"{service_url}/{path}" if service_url else f"/{path}",
                params=request.query_params,
                headers=headers,
                content=await request.body(),
            )
    except httpx.TimeoutException as error:
        logger.exception("%s API request timed out.", service_name)
        raise HTTPException(
            status_code=503,
            detail=f"{service_name} API haijajibu kwa wakati.",
        ) from error
    except httpx.RequestError as error:
        logger.exception("Could not reach %s API.", service_name)
        raise HTTPException(
            status_code=503,
            detail=f"Imeshindikana kuwasiliana na {service_name} API.",
        ) from error

    response_headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() not in hop_by_hop_headers
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )


@app.api_route(
    "/api/admin/{admin_path:path}",
    methods=["GET", "POST", "PATCH"],
)
async def admin_api_gateway(admin_path: str, request: Request):
    return await proxy_internal_api(
        request,
        admin_path,
        "Admin",
        "ADMIN_SERVICE_URL",
        "ADMIN_SERVICE_TOKEN",
        "X-Admin-Service-Token",
        create_admin_service_client,
    )


@app.api_route("/api/support-tickets", methods=["GET", "POST"])
async def support_tickets_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "support-tickets",
        "Admin",
        "ADMIN_SERVICE_URL",
        "ADMIN_SERVICE_TOKEN",
        "X-Admin-Service-Token",
        create_admin_service_client,
    )


@app.api_route("/api/products", methods=["GET", "POST"])
async def catalog_collection_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "products",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route("/api/categories", methods=["GET", "POST"])
async def catalog_categories_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "categories",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route("/api/products/import", methods=["POST"])
async def catalog_products_import_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "products/import",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route("/api/products/export", methods=["GET"])
async def catalog_products_export_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "products/export",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route("/api/storefront", methods=["PUT"])
async def catalog_storefront_update_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "storefront",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route("/api/inventory/low-stock", methods=["GET"])
async def catalog_low_stock_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "inventory/low-stock",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route(
    "/api/products/{product_id}/movements",
    methods=["GET"],
)
async def catalog_stock_movements_gateway(product_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"products/{product_id}/movements",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route("/api/storefronts/{store_slug}", methods=["GET"])
async def catalog_storefront_gateway(store_slug: str, request: Request):
    return await proxy_internal_api(
        request,
        f"storefronts/{store_slug}",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route("/api/wishlist", methods=["GET"])
async def catalog_wishlist_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "wishlist",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route(
    "/api/wishlist/{product_id}",
    methods=["POST", "DELETE"],
)
async def catalog_wishlist_product_gateway(product_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"wishlist/{product_id}",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route(
    "/api/products/{product_id}/image",
    methods=["GET", "POST", "DELETE"],
)
async def catalog_product_image_gateway(product_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"products/{product_id}/image",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route("/api/products/{product_id}/gallery", methods=["POST"])
async def catalog_product_gallery_gateway(product_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"products/{product_id}/gallery",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route("/api/products/{product_id}/videos", methods=["POST"])
async def catalog_product_videos_gateway(product_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"products/{product_id}/videos",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route(
    "/api/products/{product_id}/videos/{video_id}",
    methods=["GET", "DELETE"],
)
async def catalog_product_video_gateway(
    product_id: str,
    video_id: str,
    request: Request,
):
    return await proxy_internal_api(
        request,
        f"products/{product_id}/videos/{video_id}",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route(
    "/api/products/{product_id}/gallery/{image_id}",
    methods=["GET", "DELETE"],
)
async def catalog_product_gallery_image_gateway(
    product_id: str,
    image_id: str,
    request: Request,
):
    return await proxy_internal_api(
        request,
        f"products/{product_id}/gallery/{image_id}",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route(
    "/api/products/{product_id}/variants",
    methods=["POST"],
)
async def catalog_product_variants_gateway(
    product_id: str,
    request: Request,
):
    return await proxy_internal_api(
        request,
        f"products/{product_id}/variants",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route(
    "/api/products/{product_id}/variants/{variant_id}",
    methods=["PATCH"],
)
async def catalog_product_variant_gateway(
    product_id: str,
    variant_id: str,
    request: Request,
):
    return await proxy_internal_api(
        request,
        f"products/{product_id}/variants/{variant_id}",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route(
    "/api/products/{product_id}",
    methods=["GET", "PUT", "DELETE"],
)
async def catalog_product_gateway(product_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"products/{product_id}",
        "Catalog",
        "CATALOG_SERVICE_URL",
        "CATALOG_SERVICE_TOKEN",
        "X-Catalog-Service-Token",
        create_catalog_service_client,
    )


@app.api_route(
    "/api/auth/{auth_path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def identity_api_gateway(auth_path: str, request: Request):
    return await proxy_internal_api(
        request,
        auth_path,
        "Identity",
        "IDENTITY_SERVICE_URL",
        "IDENTITY_SERVICE_TOKEN",
        "X-Identity-Service-Token",
        create_identity_service_client,
    )


@app.api_route(
    "/api/products/{product_id}/checkout",
    methods=["POST"],
)
async def order_product_checkout_gateway(product_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"products/{product_id}/checkout",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/cart/checkout", methods=["POST"])
async def order_cart_checkout_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "cart/checkout",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/webhooks/clickpesa", methods=["POST"])
async def clickpesa_webhook_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "webhooks/clickpesa",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/webhooks/pesapal", methods=["GET", "POST"])
async def pesapal_webhook_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "webhooks/pesapal",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/callbacks/pesapal", methods=["GET"])
async def pesapal_callback_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "callbacks/pesapal",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/webhooks/abliner", methods=["POST"])
async def abliner_webhook_gateway(
    request: Request,
    background_tasks: BackgroundTasks,
):
    if not os.getenv("ORDERS_SERVICE_URL", "").strip():
        return await process_abliner_webhook(request, background_tasks)
    return await proxy_internal_api(
        request,
        "webhooks/abliner",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route(
    "/api/checkouts/{checkout_id}",
    methods=["GET"],
)
async def order_cart_status_gateway(checkout_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"checkouts/{checkout_id}",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route(
    "/api/checkouts/{checkout_id}/demo-complete",
    methods=["POST"],
)
async def order_cart_demo_gateway(checkout_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"checkouts/{checkout_id}/demo-complete",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/orders/history", methods=["GET"])
async def order_history_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "orders/history",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/orders/{order_id}", methods=["GET"])
async def order_status_gateway(order_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"orders/{order_id}",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/orders/{order_id}/delivery", methods=["PUT"])
async def order_delivery_gateway(order_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"orders/{order_id}/delivery",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route(
    "/api/orders/{order_id}/cancellation",
    methods=["POST"],
)
async def order_cancellation_gateway(order_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"orders/{order_id}/cancellation",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/orders/{order_id}/return", methods=["POST"])
async def order_return_gateway(order_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"orders/{order_id}/return",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route(
    "/api/orders/{order_id}/products/{product_id}/review",
    methods=["POST"],
)
async def order_review_gateway(order_id: str, product_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"orders/{order_id}/products/{product_id}/review",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route(
    "/api/orders/{order_id}/demo-complete",
    methods=["POST"],
)
async def order_demo_gateway(order_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"orders/{order_id}/demo-complete",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/dashboard", methods=["GET"])
async def order_dashboard_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "dashboard",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/payouts", methods=["GET"])
async def order_payout_ledger_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "payouts",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/payouts", methods=["POST"])
async def order_manual_payout_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "payouts",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/sales", methods=["GET"])
async def order_sales_gateway(request: Request):
    return await proxy_internal_api(
        request,
        "sales",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


@app.api_route("/api/sales/{order_id}/archive", methods=["POST"])
async def order_archive_gateway(order_id: str, request: Request):
    return await proxy_internal_api(
        request,
        f"sales/{order_id}/archive",
        "Orders",
        "ORDERS_SERVICE_URL",
        "ORDERS_SERVICE_TOKEN",
        "X-Orders-Service-Token",
        create_orders_service_client,
    )


def auth_status(request: Request):
    with db_connection() as connection:
        admin = connection.execute(
            "SELECT id FROM users WHERE role = 'ADMIN' LIMIT 1"
        ).fetchone()
    user = None
    try:
        user = safe_user(get_current_user(request, allow_suspended=True))
    except HTTPException as error:
        if error.status_code not in {401, 403}:
            raise
    return {
        "setup_required": admin is None,
        "setup_available": bool(os.getenv("ADMIN_SETUP_TOKEN", "").strip()),
        "user": user,
    }


def get_language_preference(request: Request):
    user = get_current_user(request)
    return {"language": user["preferred_language"]}


def update_language_preference(payload: LanguagePreferenceInput, request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        connection.execute(
            "UPDATE users SET preferred_language = ? WHERE id = ?",
            (payload.language, user["id"]),
        )
    record_audit(
        "LANGUAGE_PREFERENCE_UPDATED",
        "account",
        user["id"],
        payload.language,
        actor=user,
        request=request,
    )
    return {"language": payload.language}


def mfa_status(request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT mfa_enabled FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
    return {
        "enabled": bool(row and row["mfa_enabled"]),
        "enrollment_available": bool(os.getenv("MFA_ENCRYPTION_KEY", "").strip()),
    }


def setup_mfa(request: Request):
    user = get_current_user(request)
    secret = new_totp_secret()
    try:
        encrypted = encrypt_totp_secret(secret)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    with db_connection() as connection:
        connection.execute(
            "UPDATE users SET mfa_pending_secret = ? WHERE id = ?",
            (encrypted, user["id"]),
        )
    label = urllib.parse.quote(f"LipaRahisi:{user['email']}", safe="")
    issuer = urllib.parse.quote("LipaRahisi", safe="")
    uri = (
        f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"
        "&algorithm=SHA1&digits=6&period=30"
    )
    return {"secret": secret, "otpauth_uri": uri}


def verify_mfa_code(connection, user, code, consume_recovery=True):
    try:
        secret = decrypt_totp_secret(user["mfa_secret"])
    except RuntimeError as error:
        logger.exception("Could not decrypt MFA secret for user %s.", user["id"])
        raise HTTPException(status_code=503, detail="Usanidi wa 2FA haupatikani.") from error
    counter = totp_counter(secret, code)
    if counter is not None and counter > user["mfa_last_counter"]:
        connection.execute(
            "UPDATE users SET mfa_last_counter = ? WHERE id = ?",
            (counter, user["id"]),
        )
        return True
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    recovery = connection.execute(
        "SELECT code_hash FROM mfa_recovery_codes WHERE user_id = ? AND code_hash = ?",
        (user["id"], code_hash),
    ).fetchone()
    if recovery is None:
        return False
    if consume_recovery:
        connection.execute(
            "DELETE FROM mfa_recovery_codes WHERE user_id = ? AND code_hash = ?",
            (user["id"], code_hash),
        )
    return True


def enable_mfa(payload: TotpCodeInput, request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT mfa_pending_secret FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if row is None or not row["mfa_pending_secret"]:
            raise HTTPException(status_code=409, detail="Anzisha usajili wa 2FA kwanza.")
        try:
            secret = decrypt_totp_secret(row["mfa_pending_secret"])
        except RuntimeError as error:
            logger.exception("Could not decrypt pending MFA secret for user %s.", user["id"])
            raise HTTPException(status_code=503, detail="Usanidi wa 2FA haupatikani.") from error
        counter = totp_counter(secret, payload.code)
        if counter is None:
            raise HTTPException(status_code=422, detail="Nambari ya authenticator si sahihi.")
        connection.execute(
            """
            UPDATE users
            SET mfa_secret = ?, mfa_pending_secret = '', mfa_enabled = 1,
                mfa_last_counter = ?
            WHERE id = ?
            """,
            (row["mfa_pending_secret"], counter, user["id"]),
        )
        recovery_codes = [secrets.token_urlsafe(9) for _ in range(10)]
        for code in recovery_codes:
            connection.execute(
                "INSERT INTO mfa_recovery_codes (user_id, code_hash) VALUES (?, ?)",
                (user["id"], hashlib.sha256(code.encode("utf-8")).hexdigest()),
            )
    record_audit("MFA_ENABLED", "account", user["id"], actor=user, request=request)
    return {"enabled": True, "recovery_codes": recovery_codes}


def disable_mfa(payload: MfaDisableInput, request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT id, password_hash, mfa_secret, mfa_last_counter FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if row is None or not verify_password(payload.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Nenosiri si sahihi.")
        if not row["mfa_secret"] or not verify_mfa_code(
            connection,
            row,
            payload.code,
        ):
            raise HTTPException(status_code=422, detail="Nambari ya authenticator si sahihi au imetumika.")
        connection.execute(
            """
            UPDATE users
            SET mfa_secret = '', mfa_pending_secret = '', mfa_enabled = 0,
                mfa_last_counter = -1
            WHERE id = ?
            """,
            (user["id"],),
        )
        connection.execute(
            "DELETE FROM mfa_recovery_codes WHERE user_id = ?",
            (user["id"],),
        )
        current_token = request.cookies.get(SESSION_COOKIE, "")
        current_hash = hashlib.sha256(current_token.encode("utf-8")).hexdigest()
        connection.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user["id"], current_hash),
        )
    record_audit("MFA_DISABLED", "account", user["id"], actor=user, request=request)
    return {"enabled": False}


def list_sessions(request: Request):
    user = get_current_user(request)
    current_token = request.cookies.get(SESSION_COOKIE, "")
    current_hash = hashlib.sha256(current_token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT token_hash, user_agent, ip_address, created_at, last_seen_at
            FROM sessions WHERE user_id = ? AND expires_at > ?
            ORDER BY last_seen_at DESC
            """,
            (user["id"], time.time()),
        ).fetchall()
    return [
        {
            "current": row["token_hash"] == current_hash,
            "user_agent": row["user_agent"],
            "ip_address": row["ip_address"],
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
        }
        for row in rows
    ]


def revoke_other_sessions(request: Request):
    user = get_current_user(request)
    current_token = request.cookies.get(SESSION_COOKIE, "")
    current_hash = hashlib.sha256(current_token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        result = connection.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user["id"], current_hash),
        )
    record_audit(
        "OTHER_SESSIONS_REVOKED",
        "account",
        user["id"],
        f"count={result.rowcount}",
        actor=user,
        request=request,
    )
    return {"revoked_count": result.rowcount}


def setup_admin(payload: AdminSetupInput, request: Request, response: Response):
    expected_token = os.getenv("ADMIN_SETUP_TOKEN", "").strip()
    if not expected_token:
        raise HTTPException(status_code=503, detail="ADMIN_SETUP_TOKEN haijawekwa.")
    if not secrets.compare_digest(payload.setup_token, expected_token):
        raise HTTPException(status_code=401, detail="Tokeni ya kuanzisha admin si sahihi.")
    full_name, email = clean_identity(payload.full_name, payload.email)
    verification_token, verification_hash = create_email_verification_token()
    with db_connection() as connection:
        if connection.execute(
            "SELECT id FROM users WHERE role = 'ADMIN' LIMIT 1"
        ).fetchone():
            raise HTTPException(status_code=409, detail="Admin mkuu tayari ameanzishwa.")
        user_id = secrets.token_hex(16)
        try:
            connection.execute(
                """
                INSERT INTO users
                    (id, full_name, email, password_hash, role, status, shop_name)
                VALUES (?, ?, ?, ?, 'ADMIN', 'ACTIVE', ?)
                """,
                (
                    user_id,
                    full_name,
                    email,
                    hash_password(payload.password),
                    "LipaRahisi",
                ),
            )
            connection.execute(
                "UPDATE users SET verification_token_hash = ? WHERE id = ?",
                (verification_hash, user_id),
            )
        except Exception as error:
            if is_integrity_error(error):
                raise HTTPException(
                    status_code=409,
                    detail="Akaunti ya admin au barua pepe tayari ipo.",
                ) from error
            raise
        connection.execute(
            "UPDATE products SET seller_id = ? WHERE seller_id IS NULL",
            (user_id,),
        )
        connection.execute(
            """
            UPDATE orders
            SET seller_id = (
                SELECT seller_id FROM products WHERE products.id = orders.product_id
            )
            WHERE seller_id IS NULL
            """
        )
        token = create_session(connection, user_id, request)
    set_session_cookie(response, request, token)
    send_app_email(
        "admin_verification",
        email,
        lambda app_url: verification_email(
            full_name,
            f"{app_url}/verify-email?token={quote(verification_token, safe='')}",
            app_url,
        ),
        str(request.base_url),
    )
    return {"user": {"id": user_id, "full_name": full_name, "email": email, "role": "ADMIN", "status": "ACTIVE", "shop_name": "LipaRahisi", "phone": ""}}


def register_seller(payload: SellerRegistrationInput, request: Request):
    full_name, email = clean_identity(payload.full_name, payload.email)
    shop_name = payload.shop_name.strip()
    if not shop_name:
        raise HTTPException(status_code=422, detail="Jina la duka linahitajika.")
    phone = normalize_phone(payload.phone)
    user_id = secrets.token_hex(16)
    try:
        with db_connection() as connection:
            connection.execute(
                """
                INSERT INTO users
                    (id, full_name, email, password_hash, role, status, shop_name, phone)
                VALUES (?, ?, ?, ?, 'SELLER', 'PENDING', ?, ?)
                """,
                (
                    user_id,
                    full_name,
                    email,
                    hash_password(payload.password),
                    shop_name,
                    phone,
                ),
            )
            connection.execute(
                "UPDATE users SET store_slug = ? WHERE id = ?",
                (make_store_slug(shop_name, user_id), user_id),
            )
            verification_token, verification_hash = create_email_verification_token()
            connection.execute(
                "UPDATE users SET verification_token_hash = ? WHERE id = ?",
                (verification_hash, user_id),
            )
    except Exception as error:
        if is_integrity_error(error):
            raise HTTPException(
                status_code=409,
                detail="Barua pepe hii tayari imesajiliwa.",
            ) from error
        raise
    send_app_email(
        "seller_verification",
        email,
        lambda app_url: verification_email(
            full_name,
            f"{app_url}/verify-email?token={quote(verification_token, safe='')}",
            app_url,
        ),
        str(request.base_url),
    )
    queue_user_notification(
        user_id,
        "KYC_PENDING",
        {"shop_name": shop_name},
        email=email,
        phone=phone,
    )
    with db_connection() as connection:
        admin = connection.execute(
            "SELECT id, email FROM users WHERE role = 'ADMIN' LIMIT 1"
        ).fetchone()
    if admin:
        queue_user_notification(
            admin["id"],
            "SELLER_REGISTERED",
            {"shop_name": shop_name},
            email=admin["email"],
        )
    return {
        "status": "PENDING",
        "message": "Usajili umehifadhiwa. Msimamizi mkuu ataidhinisha akaunti yako kabla ya kuuza.",
    }


def register_customer(payload: CustomerRegistrationInput):
    full_name, email = clean_identity(payload.full_name, payload.email)
    phone = normalize_phone(payload.phone)
    user_id = secrets.token_hex(16)
    verification_token, verification_hash = create_email_verification_token()
    try:
        with db_connection() as connection:
            connection.execute(
                """
                INSERT INTO users
                    (id, full_name, email, password_hash, role, status, phone)
                VALUES (?, ?, ?, ?, 'BUYER', 'ACTIVE', ?)
                """,
                (
                    user_id,
                    full_name,
                    email,
                    hash_password(payload.password),
                    phone,
                ),
            )
            connection.execute(
                "UPDATE users SET verification_token_hash = ? WHERE id = ?",
                (verification_hash, user_id),
            )
    except Exception as error:
        if is_integrity_error(error):
            raise HTTPException(
                status_code=409,
                detail="Barua pepe hii tayari imesajiliwa.",
            ) from error
        raise
    send_app_email(
        "customer_verification",
        email,
        lambda app_url: verification_email(
            full_name,
            f"{app_url}/verify-email?token={quote(verification_token, safe='')}",
            app_url,
        ),
    )
    return {
        "message": "Akaunti ya mnunuzi imefunguliwa. Angalia barua pepe yako kuthibitisha akaunti.",
        "user_id": user_id,
    }


def login(payload: LoginInput, request: Request, response: Response):
    email = payload.email.strip().lower()
    blocked = False
    valid_credentials = False
    account_status = ""
    result = None
    token = ""
    with db_connection() as connection:
        attempt = connection.execute(
            "SELECT failed_count, blocked_until FROM login_attempts WHERE email = ?",
            (email,),
        ).fetchone()
        if attempt and attempt["blocked_until"] > time.time():
            blocked = True
        else:
            user = connection.execute(
                "SELECT * FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            valid_credentials = bool(
                user is not None
                and verify_password(payload.password, user["password_hash"])
            )
            if valid_credentials and user["mfa_enabled"]:
                valid_credentials = verify_mfa_code(
                    connection,
                    user,
                    payload.totp_code,
                )
            if not valid_credentials:
                failed_count = (attempt["failed_count"] if attempt else 0) + 1
                blocked_until = time.time() + 15 * 60 if failed_count >= 5 else 0
                if attempt:
                    connection.execute(
                        "UPDATE login_attempts SET failed_count = ?, blocked_until = ? WHERE email = ?",
                        (failed_count, blocked_until, email),
                    )
                else:
                    connection.execute(
                        "INSERT INTO login_attempts (email, failed_count, blocked_until) VALUES (?, ?, ?)",
                        (email, failed_count, blocked_until),
                    )
            elif user["status"] in {"ACTIVE", "SUSPENDED"}:
                account_status = user["status"]
                connection.execute(
                    "DELETE FROM login_attempts WHERE email = ?",
                    (email,),
                )
                token = create_session(connection, user["id"], request)
                result = safe_user(user)
            else:
                account_status = user["status"]

    if blocked:
        record_audit("LOGIN_BLOCKED", "auth", request=request)
        raise HTTPException(
            status_code=429,
            detail="Akaunti imezuiwa kwa muda kutokana na majaribio mengi ya login.",
        )
    if not valid_credentials:
        record_audit("LOGIN_FAILED", "auth", request=request)
        raise HTTPException(status_code=401, detail="Barua pepe, nenosiri au nambari ya 2FA si sahihi.")
    if account_status not in {"ACTIVE", "SUSPENDED"}:
        raise HTTPException(
            status_code=403,
            detail="Akaunti inasubiri idhini ya msimamizi mkuu.",
        )
    set_session_cookie(response, request, token)
    record_audit(
        "LOGIN_SUCCEEDED" if account_status == "ACTIVE" else "LOGIN_RESTRICTED",
        "auth",
        actor=result,
        request=request,
    )
    return {"user": result}


def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with db_connection() as connection:
            session = connection.execute(
                """
                SELECT u.id, u.email FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        if session:
            record_audit(
                "LOGOUT",
                "auth",
                actor={"id": session["id"], "email": session["email"]},
                request=request,
            )
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")
    return {"message": "Umetoka kwenye akaunti."}


def change_password(payload: PasswordChangeInput, request: Request, response: Response):
    user = get_current_user(request)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        if row is None or not verify_password(payload.current_password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Nenosiri la sasa si sahihi.")
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(payload.new_password), user["id"]),
        )
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        token = create_session(connection, user["id"], request)
    set_session_cookie(response, request, token)
    send_app_email(
        "password_changed",
        user["email"],
        lambda app_url: password_changed_email(user["full_name"], app_url),
        str(request.base_url),
    )
    return {"message": "Nenosiri limebadilishwa."}


def request_password_reset(payload: PasswordResetRequestInput, request: Request):
    email = payload.email
    now = time.time()
    should_send = False
    with db_connection() as connection:
        user = connection.execute(
            "SELECT id, full_name, email, reset_expires FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if user is not None:
            last_expiry = float(user["reset_expires"] or 0)
            recently_requested = last_expiry > (
                now + RESET_TOKEN_TTL_SECONDS - PASSWORD_RESET_COOLDOWN_SECONDS
            )
            if not recently_requested:
                reset_token = secrets.token_urlsafe(32)
                reset_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()
                connection.execute(
                    "UPDATE users SET reset_token_hash = ?, reset_expires = ? WHERE id = ?",
                    (reset_hash, now + RESET_TOKEN_TTL_SECONDS, user["id"]),
                )
                should_send = True
    if user is not None and should_send:
        reset_url = (
            f"{{app_url}}/reset-password?token={quote(reset_token, safe='')}"
        )
        sent = send_app_email(
            "password_reset",
            user["email"],
            lambda app_url: password_reset_email(
                user["full_name"],
                reset_url.format(app_url=app_url),
                app_url,
            ),
            str(request.base_url),
        )
        if not sent:
            with db_connection() as connection:
                connection.execute(
                    """
                    UPDATE users SET reset_token_hash = '', reset_expires = 0
                    WHERE id = ? AND reset_token_hash = ?
                    """,
                    (user["id"], reset_hash),
                )
    return {"message": "Ikiwa akaunti ipo, link ya kubadilisha nenosiri imetumwa."}


def confirm_password_reset(payload: PasswordResetInput):
    reset_hash = hashlib.sha256(payload.reset_token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        user = connection.execute(
            "SELECT id, full_name, email FROM users WHERE reset_token_hash = ? AND reset_expires > ?",
            (reset_hash, time.time()),
        ).fetchone()
        if user is None:
            raise HTTPException(status_code=400, detail="Link ya reset si sahihi au imekwisha muda.")
        connection.execute(
            "UPDATE users SET password_hash = ?, reset_token_hash = '', reset_expires = 0 WHERE id = ?",
            (hash_password(payload.new_password), user["id"]),
        )
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    send_app_email(
        "password_changed",
        user["email"],
        lambda app_url: password_changed_email(user["full_name"], app_url),
    )
    return {"message": "Nenosiri limebadilishwa."}


def verify_email(token: str):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        user = connection.execute(
            "SELECT id, full_name, email FROM users WHERE verification_token_hash = ?",
            (token_hash,),
        ).fetchone()
        if user is None:
            raise HTTPException(status_code=400, detail="Link ya uthibitisho si sahihi.")
        connection.execute(
            "UPDATE users SET email_verified = 1, verification_token_hash = '' WHERE id = ?",
            (user["id"],),
        )
    send_app_email(
        "welcome",
        user["email"],
        lambda app_url: welcome_email(user["full_name"], app_url),
    )
    return {"message": "Barua pepe imethibitishwa."}


def update_storefront(payload: StorefrontInput, request: Request):
    user = require_seller(request)
    shop_name = payload.shop_name.strip()
    store_slug = make_store_slug(shop_name, user["id"])
    image_url = payload.store_image_url.strip()
    if image_url:
        parsed = urlsplit(image_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(status_code=422, detail="Picha lazima iwe na URL ya HTTP au HTTPS.")
    with db_connection() as connection:
        connection.execute(
            """
            UPDATE users
            SET shop_name = ?, store_slug = ?, store_description = ?, store_image_url = ?
            WHERE id = ? AND role = 'SELLER'
            """,
            (
                shop_name,
                store_slug,
                payload.store_description.strip(),
                image_url,
                user["id"],
            ),
        )
    record_audit(
        "STOREFRONT_UPDATED",
        "seller",
        user["id"],
        "Store profile updated",
        actor=user,
        request=request,
    )
    return {
        "shop_name": shop_name,
        "store_slug": store_slug,
        "store_description": payload.store_description.strip(),
        "store_image_url": image_url,
    }


def read_storefront(store_slug: str):
    with db_connection() as connection:
        seller = connection.execute(
            """
            SELECT id, full_name, phone, shop_name, store_slug, store_description,
                   store_image_url, profile_image_url, kyc_status
            FROM users
            WHERE store_slug = ? AND role = 'SELLER' AND status = 'ACTIVE'
            """,
            (store_slug,),
        ).fetchone()
        if seller is None:
            raise HTTPException(status_code=404, detail="Duka halikupatikana.")
        products = connection.execute(
            """
            SELECT id, name, description, image_url, category, amount,
                   stock_quantity, created_at
            FROM products
            WHERE seller_id = ? AND is_active = 1
            ORDER BY created_at DESC
            """,
            (seller["id"],),
        ).fetchall()
    return {
        "store": dict(seller),
        "products": attach_product_variants(
            attach_product_videos(attach_product_gallery(products))
        ),
    }


def list_products(
    request: Request,
    q: str = "",
    seller: str = "",
    min_amount: int = 0,
    max_amount: int = 999999999,
    category: str = "",
):
    user = optional_current_user(request)
    search = q.strip().lower()
    seller_search = seller.strip().lower()
    category_search = category.strip().lower()
    min_amount = max(0, min_amount)
    max_amount = max(0, max_amount)
    app_url = public_app_url(str(request.base_url))
    cache_key = None
    if user is None:
        key_data = json.dumps(
            [
                search,
                seller_search,
                min_amount,
                max_amount,
                category_search,
                app_url,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        cache_key = (
            PUBLIC_PRODUCT_CACHE_PREFIX
            + hashlib.sha256(key_data.encode("utf-8")).hexdigest()
        )
        cached = cached_product_list(cache_key)
        if cached is not None:
            return cached

    terms = ["p.amount >= ?", "p.amount <= ?"]
    params = [min_amount, max_amount]
    if search:
        terms.append("(LOWER(p.name) LIKE ? OR LOWER(COALESCE(u.shop_name, '')) LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if seller_search:
        terms.append("LOWER(COALESCE(u.shop_name, '')) LIKE ?")
        params.append(f"%{seller_search}%")
    if category_search:
        terms.append("LOWER(p.category) = ?")
        params.append(category_search)
    where = " AND ".join(terms)
    with db_connection() as connection:
        if user and user["role"] == "ADMIN":
            rows = connection.execute(
                """
                SELECT p.id, p.name, p.description, p.image_url, p.category,
                       p.subcategory, p.video_url, p.brand,
                       p.amount, p.cost_amount, p.is_active, p.low_stock_threshold,
                       p.stock_quantity, p.seller_id, p.created_at, u.shop_name,
                       u.store_slug, u.kyc_status
                FROM products p LEFT JOIN users u ON u.id = p.seller_id
                WHERE """ + where + """
                ORDER BY p.created_at DESC
                """,
                tuple(params),
            ).fetchall()
        elif user and user["role"] == "SELLER":
            rows = connection.execute(
                """
                SELECT p.id, p.name, p.description, p.image_url, p.category,
                       p.subcategory, p.video_url, p.brand,
                       p.amount, p.cost_amount, p.is_active, p.low_stock_threshold,
                       p.stock_quantity, p.seller_id, p.created_at, '' AS shop_name,
                       '' AS store_slug, u.kyc_status
                FROM products p LEFT JOIN users u ON u.id = p.seller_id
                WHERE p.seller_id = ? AND """ + where + """
                ORDER BY p.created_at DESC
                """,
                (user["id"], *params),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT p.id, p.name, p.description, p.image_url, p.category,
                       p.subcategory, p.video_url, p.brand,
                       p.amount, p.cost_amount, p.is_active, p.low_stock_threshold,
                       p.stock_quantity, p.seller_id, p.created_at, u.shop_name,
                       u.store_slug, u.kyc_status
                FROM products p LEFT JOIN users u ON u.id = p.seller_id
                WHERE p.is_active = 1
                  AND (p.seller_id IS NULL OR u.status = 'ACTIVE')
                  AND """ + where + """
                ORDER BY p.created_at DESC
                """,
                tuple(params),
            ).fetchall()
    products = [
        {
            **dict(row),
            "qr_url": f"{app_url}/p/{row['id']}",
            "is_active": bool(row["is_active"]),
        }
        for row in rows
    ]
    products = attach_product_variants(
        attach_product_videos(attach_product_gallery(products))
    )
    if cache_key is not None:
        cache_product_list(cache_key, products)
    return products


def record_inventory_movement(
    connection,
    product_id,
    seller_id,
    actor_id,
    quantity_delta,
    reason,
):
    if not quantity_delta:
        return
    connection.execute(
        """
        INSERT INTO inventory_movements
            (id, product_id, seller_id, actor_id, quantity_delta, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            secrets.token_hex(16),
            product_id,
            seller_id,
            actor_id,
            quantity_delta,
            reason,
        ),
    )


def list_categories():
    with db_connection() as connection:
        rows = connection.execute(
            "SELECT id, name FROM categories ORDER BY name"
        ).fetchall()
    return [dict(row) for row in rows]


def create_category(payload: CategoryInput, request: Request):
    actor = require_admin(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Jina la kundi linahitajika.")
    category_id = secrets.token_hex(16)
    try:
        with db_connection() as connection:
            connection.execute(
                "INSERT INTO categories (id, name) VALUES (?, ?)",
                (category_id, name),
            )
    except Exception as error:
        if is_integrity_error(error):
            raise HTTPException(status_code=409, detail="Kundi hili tayari lipo.") from error
        raise
    record_audit("CATEGORY_CREATED", "category", category_id, name, actor=actor, request=request)
    return {"id": category_id, "name": name}


def list_coupons(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, code, discount_type, discount_value, max_redemptions,
                   redemption_count, is_active, created_at
            FROM coupons ORDER BY created_at DESC
            """
        ).fetchall()
    return [{**dict(row), "is_active": bool(row["is_active"])} for row in rows]


def create_coupon(payload: CouponInput, request: Request):
    actor = require_admin(request)
    if payload.discount_type == "PERCENT" and payload.discount_value > 100:
        raise HTTPException(
            status_code=422,
            detail="Punguzo la asilimia haliwezi kuzidi 100.",
        )
    coupon_id = secrets.token_hex(16)
    try:
        with db_connection() as connection:
            connection.execute(
                """
                INSERT INTO coupons
                    (id, code, discount_type, discount_value, max_redemptions, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    coupon_id,
                    payload.code,
                    payload.discount_type,
                    payload.discount_value,
                    payload.max_redemptions,
                    actor["id"],
                ),
            )
    except Exception as error:
        if is_integrity_error(error):
            raise HTTPException(
                status_code=409,
                detail="Msimbo huu wa kuponi tayari upo.",
            ) from error
        raise
    record_audit(
        "COUPON_CREATED",
        "coupon",
        coupon_id,
        payload.code,
        actor=actor,
        request=request,
    )
    with db_connection() as connection:
        row = connection.execute(
            """
            SELECT id, code, discount_type, discount_value, max_redemptions,
                   redemption_count, is_active, created_at
            FROM coupons WHERE id = ?
            """,
            (coupon_id,),
        ).fetchone()
    return {**dict(row), "is_active": bool(row["is_active"])}


def set_coupon_status(coupon_id: str, payload: CouponStatusInput, request: Request):
    actor = require_admin(request)
    with db_connection() as connection:
        result = connection.execute(
            "UPDATE coupons SET is_active = ? WHERE id = ?",
            (int(payload.is_active), coupon_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Kuponi haikupatikana.")
    record_audit(
        "COUPON_STATUS_UPDATED",
        "coupon",
        coupon_id,
        "ACTIVE" if payload.is_active else "INACTIVE",
        actor=actor,
        request=request,
    )
    return {"id": coupon_id, "is_active": payload.is_active}


def coupon_buyer_key(buyer_id, buyer_email):
    if buyer_id:
        return f"account:{buyer_id}"
    return f"email:{buyer_email.strip().lower()}"


def coupon_discount_for_checkout(
    connection,
    coupon_code,
    subtotal,
    buyer_id,
    buyer_email,
    minimum_total=1,
    demo_checkout=False,
):
    code = (coupon_code or "").strip().upper()
    if not code:
        return "", 0
    if not demo_checkout or not is_demo_mode():
        raise HTTPException(
            status_code=422,
            detail="Kuponi zinapatikana kwenye majaribio pekee.",
        )
    coupon = connection.execute(
        "SELECT * FROM coupons WHERE code = ?",
        (code,),
    ).fetchone()
    if coupon is None or not coupon["is_active"]:
        raise HTTPException(status_code=404, detail="Kuponi haipo au haijawashwa.")
    if (
        coupon["max_redemptions"] is not None
        and coupon["redemption_count"] >= coupon["max_redemptions"]
    ):
        raise HTTPException(status_code=409, detail="Matumizi ya kuponi yamefika kikomo.")
    buyer_key = coupon_buyer_key(buyer_id, buyer_email)
    redeemed = connection.execute(
        """
        SELECT 1 FROM coupon_redemptions
        WHERE coupon_id = ? AND buyer_key = ?
        """,
        (coupon["id"], buyer_key),
    ).fetchone()
    if redeemed is not None:
        raise HTTPException(
            status_code=409,
            detail="Tayari umetumia kuponi hii.",
        )
    discount = (
        subtotal * coupon["discount_value"] // 100
        if coupon["discount_type"] == "PERCENT"
        else coupon["discount_value"]
    )
    discount = min(discount, max(0, subtotal - minimum_total))
    if discount <= 0:
        raise HTTPException(
            status_code=422,
            detail="Kuponi hii haiwezi kupunguza kiasi cha oda hii.",
        )
    return code, discount


def allocate_coupon_discount(discount, seller_subtotals):
    total_subtotal = sum(amount for _seller_id, amount in seller_subtotals)
    remaining = discount
    allocations = {}
    for seller_id, subtotal in seller_subtotals:
        share = min(discount * subtotal // total_subtotal, subtotal - 1, remaining)
        allocations[seller_id] = share
        remaining -= share
    for seller_id, subtotal in seller_subtotals:
        if remaining <= 0:
            break
        extra = min(subtotal - 1 - allocations[seller_id], remaining)
        allocations[seller_id] += extra
        remaining -= extra
    if remaining:
        raise HTTPException(
            status_code=422,
            detail="Kuponi hii haiwezi kupunguza kiasi cha oda hii.",
        )
    return allocations


def record_coupon_redemption(connection, orders):
    coupon_codes = {order.get("coupon_code") or "" for order in orders}
    coupon_codes.discard("")
    if not coupon_codes:
        return
    if (
        len(coupon_codes) != 1
        or not is_demo_mode()
        or any(
            order.get("provider_reference")
            or order.get("payment_method") == ABLINER_PAYMENT_METHOD
            for order in orders
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="Kuponi hii haiwezi kutumika kwenye malipo halisi.",
        )
    coupon_code = next(iter(coupon_codes))
    first_order = orders[0]
    redemption_key = first_order.get("checkout_id") or first_order["order_id"]
    coupon = connection.execute(
        "SELECT id, max_redemptions, redemption_count FROM coupons WHERE code = ?",
        (coupon_code,),
    ).fetchone()
    if coupon is None:
        raise HTTPException(status_code=409, detail="Kuponi haipo tena.")
    existing = connection.execute(
        """
        SELECT 1 FROM coupon_redemptions
        WHERE coupon_id = ? AND redemption_key = ?
        """,
        (coupon["id"], redemption_key),
    ).fetchone()
    if existing is not None:
        return
    buyer_key = coupon_buyer_key(
        first_order.get("buyer_id"),
        first_order.get("buyer_email") or "",
    )
    updated = connection.execute(
        """
        UPDATE coupons
        SET redemption_count = redemption_count + 1
        WHERE id = ?
          AND (? IS NULL OR redemption_count < ?)
        """,
        (
            coupon["id"],
            coupon["max_redemptions"],
            coupon["max_redemptions"],
        ),
    )
    if updated.rowcount == 0:
        raise HTTPException(status_code=409, detail="Matumizi ya kuponi yamefika kikomo.")
    try:
        connection.execute(
            """
            INSERT INTO coupon_redemptions (coupon_id, redemption_key, buyer_key)
            VALUES (?, ?, ?)
            """,
            (coupon["id"], redemption_key, buyer_key),
        )
    except Exception as error:
        if is_integrity_error(error):
            raise HTTPException(
                status_code=409,
                detail="Tayari umetumia kuponi hii.",
            ) from error
        raise


def low_stock_products(request: Request):
    user = get_current_user(request)
    seller_filter = "" if user["role"] == "ADMIN" else "AND p.seller_id = ?"
    parameters = () if user["role"] == "ADMIN" else (user["id"],)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT p.id, p.name, p.category, p.stock_quantity,
                   p.low_stock_threshold, p.seller_id, u.shop_name
            FROM products p
            LEFT JOIN users u ON u.id = p.seller_id
            WHERE p.is_active = 1
              AND p.stock_quantity <= p.low_stock_threshold
            """ + seller_filter + """
            ORDER BY p.stock_quantity ASC, p.name
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def product_inventory_movements(product_id: str, request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        product = connection.execute(
            "SELECT seller_id FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None or (
            user["role"] != "ADMIN" and product["seller_id"] != user["id"]
        ):
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        rows = connection.execute(
            """
            SELECT id, quantity_delta, reason, created_at
            FROM inventory_movements
            WHERE product_id = ?
            ORDER BY created_at DESC
            """,
            (product_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_to_wishlist(product_id: str, request: Request):
    user = require_buyer(request)
    with db_connection() as connection:
        product = connection.execute(
            "SELECT id FROM products WHERE id = ? AND is_active = 1",
            (product_id,),
        ).fetchone()
        if product is None:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        connection.execute(
            """
            INSERT INTO wishlists (user_id, product_id) VALUES (?, ?)
            ON CONFLICT (user_id, product_id) DO NOTHING
            """,
            (user["id"], product_id),
        )
    return {"message": "Bidhaa imeongezwa kwenye vipendwa."}


def remove_from_wishlist(product_id: str, request: Request):
    user = require_buyer(request)
    with db_connection() as connection:
        connection.execute(
            "DELETE FROM wishlists WHERE user_id = ? AND product_id = ?",
            (user["id"], product_id),
        )
    return {"message": "Bidhaa imeondolewa kwenye vipendwa."}


def list_wishlist(request: Request):
    user = require_buyer(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT p.id, p.name, p.description, p.image_url, p.category,
                   p.amount, p.stock_quantity, u.shop_name
            FROM wishlists w
            JOIN products p ON p.id = w.product_id
            LEFT JOIN users u ON u.id = p.seller_id
            WHERE w.user_id = ? AND p.is_active = 1
            ORDER BY w.created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return attach_product_videos(attach_product_gallery(rows))


def export_products(request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        if user["role"] == "ADMIN":
            rows = connection.execute(
                """
                SELECT id, name, description, image_url, category, amount,
                       cost_amount, stock_quantity, low_stock_threshold
                FROM products ORDER BY created_at DESC
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, name, description, image_url, category, amount,
                       cost_amount, stock_quantity, low_stock_threshold
                FROM products WHERE seller_id = ? ORDER BY created_at DESC
                """,
                (user["id"],),
            ).fetchall()
    output = io.StringIO()
    fields = [
        "id", "name", "description", "image_url", "category", "amount",
        "cost_amount", "stock_quantity", "low_stock_threshold",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({
            key: spreadsheet_safe_cell(value)
            for key, value in dict(row).items()
        })
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=liparahisi-products.csv"},
    )


def spreadsheet_safe_cell(value):
    if not isinstance(value, str):
        return value
    trimmed = value.lstrip(" \t\r\n")
    if value.startswith(("\t", "\r", "\n")) or (
        trimmed and trimmed[0] in "=+-@"
    ):
        return "'" + value
    return value


def import_products(payload: ProductCsvInput, request: Request):
    user = require_seller(request)
    csv_text = payload.csv_text
    if len(csv_text.encode("utf-8")) > 1_000_000:
        raise HTTPException(status_code=413, detail="Faili ya bidhaa imezidi 1 MB.")
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        required = {"name", "amount"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError("CSV lazima iwe na safu za name na amount.")
        rows = list(reader)
        if not rows or len(rows) > 500:
            raise ValueError("CSV iwe na bidhaa 1 hadi 500.")
        payloads = [ProductInput(**row) for row in rows]
    except (csv.Error, ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=f"CSV si sahihi: {error}") from error
    imported = []
    with db_connection() as connection:
        for payload in payloads:
            product_id = secrets.token_hex(16)
            connection.execute(
                """
                INSERT INTO products
                    (id, name, description, image_url, category, subcategory, brand, video_url,
                     amount, seller_id, cost_amount, stock_quantity, low_stock_threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    payload.name,
                    payload.description.strip(),
                    payload.image_url.strip(),
                    payload.category,
                    payload.subcategory,
                    payload.brand,
                    payload.video_url.strip(),
                    payload.amount,
                    user["id"],
                    payload.cost_amount,
                    payload.stock_quantity,
                    payload.low_stock_threshold,
                ),
            )
            if payload.stock_quantity:
                record_inventory_movement(
                    connection,
                    product_id,
                    user["id"],
                    user["id"],
                    payload.stock_quantity,
                    "CSV_IMPORT",
                )
            imported.append(product_id)
    return {"imported_count": len(imported), "product_ids": imported}


def create_product(payload: ProductInput, request: Request):
    user = get_current_user(request)
    if user["role"] != "SELLER":
        raise HTTPException(status_code=403, detail="Ni akaunti ya muuzaji pekee inayoweza kuunda bidhaa.")
    product_id = secrets.token_hex(16)
    stock_quantity = (
        sum(variant.stock_quantity for variant in payload.variants)
        if payload.variants
        else payload.stock_quantity
    )
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO products
                (id, name, description, image_url, category, subcategory, brand, video_url,
                 amount, seller_id, cost_amount, stock_quantity, low_stock_threshold)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                payload.name,
                payload.description.strip(),
                payload.image_url.strip(),
                payload.category,
                payload.subcategory,
                payload.brand,
                payload.video_url.strip(),
                payload.amount,
                user["id"],
                payload.cost_amount,
                stock_quantity,
                payload.low_stock_threshold,
            ),
        )
        for variant in payload.variants:
            connection.execute(
                """
                INSERT INTO product_variants (id, product_id, label, stock_quantity)
                VALUES (?, ?, ?, ?)
                """,
                (
                    secrets.token_hex(16),
                    product_id,
                    variant.label,
                    variant.stock_quantity,
                ),
            )
        if stock_quantity:
            record_inventory_movement(
                connection,
                product_id,
                user["id"],
                user["id"],
                stock_quantity,
                "INITIAL_STOCK",
            )
    record_audit("PRODUCT_CREATED", "product", product_id, payload.name, actor=user, request=request)
    app_url = public_app_url(str(request.base_url))
    return {
        "id": product_id,
        "name": payload.name,
        "description": payload.description.strip(),
        "image_url": payload.image_url.strip(),
        "category": payload.category,
        "subcategory": payload.subcategory,
        "brand": payload.brand,
        "video_url": payload.video_url.strip(),
        "amount": payload.amount,
        "cost_amount": payload.cost_amount,
        "stock_quantity": stock_quantity,
        "low_stock_threshold": payload.low_stock_threshold,
        "variants": attach_product_variants([{"id": product_id}])[0]["variants"],
        "qr_url": f"{app_url}/p/{product_id}",
    }


def update_product_variant_stock(
    product_id: str,
    variant_id: str,
    payload: ProductVariantStockInput,
    request: Request,
):
    seller = require_seller(request)
    with db_connection() as connection:
        connection.execute(
            "UPDATE products SET stock_quantity = stock_quantity WHERE id = ? AND seller_id = ?",
            (product_id, seller["id"]),
        )
        variant = connection.execute(
            """
            SELECT v.label, v.stock_quantity, p.seller_id, p.stock_quantity AS product_stock
            FROM product_variants v
            JOIN products p ON p.id = v.product_id
            WHERE v.id = ? AND v.product_id = ?
            """,
            (variant_id, product_id),
        ).fetchone()
        if variant is None or variant["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Aina ya bidhaa haikupatikana.")
        connection.execute(
            "UPDATE product_variants SET stock_quantity = ? WHERE id = ?",
            (payload.stock_quantity, variant_id),
        )
        total_stock = connection.execute(
            "SELECT COALESCE(SUM(stock_quantity), 0) AS total FROM product_variants WHERE product_id = ?",
            (product_id,),
        ).fetchone()["total"]
        stock_delta = total_stock - variant["product_stock"]
        connection.execute(
            "UPDATE products SET stock_quantity = ? WHERE id = ?",
            (total_stock, product_id),
        )
        if stock_delta:
            record_inventory_movement(
                connection,
                product_id,
                seller["id"],
                seller["id"],
                stock_delta,
                "VARIANT_STOCK_ADJUSTMENT",
            )
    invalidate_public_product_lists()
    record_audit(
        "PRODUCT_VARIANT_STOCK_UPDATED",
        "product_variant",
        variant_id,
        f"{variant['label']} stock={payload.stock_quantity}",
        actor=seller,
        request=request,
    )
    return {
        "id": variant_id,
        "product_id": product_id,
        "label": variant["label"],
        "stock_quantity": payload.stock_quantity,
    }


def add_product_variant(
    product_id: str,
    payload: ProductVariantInput,
    request: Request,
):
    seller = require_seller(request)
    with db_connection() as connection:
        connection.execute(
            "UPDATE products SET stock_quantity = stock_quantity WHERE id = ? AND seller_id = ?",
            (product_id, seller["id"]),
        )
        product = connection.execute(
            "SELECT seller_id, stock_quantity FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None or product["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        variants = product_variants_for(connection, product_id)
        if len(variants) >= 100:
            raise HTTPException(status_code=409, detail="Bidhaa haiwezi kuwa na variants zaidi ya 100.")
        if any(variant["label"].casefold() == payload.label.casefold() for variant in variants):
            raise HTTPException(status_code=409, detail="Variant yenye jina hili tayari ipo.")
        variant_id = secrets.token_hex(16)
        connection.execute(
            """
            INSERT INTO product_variants (id, product_id, label, stock_quantity)
            VALUES (?, ?, ?, ?)
            """,
            (variant_id, product_id, payload.label, payload.stock_quantity),
        )
        total_stock = sum(variant["stock_quantity"] for variant in variants)
        total_stock += payload.stock_quantity
        stock_delta = total_stock - product["stock_quantity"]
        connection.execute(
            "UPDATE products SET stock_quantity = ? WHERE id = ?",
            (total_stock, product_id),
        )
        if stock_delta:
            record_inventory_movement(
                connection,
                product_id,
                seller["id"],
                seller["id"],
                stock_delta,
                "VARIANT_ADDED",
            )
    invalidate_public_product_lists()
    record_audit(
        "PRODUCT_VARIANT_ADDED",
        "product_variant",
        variant_id,
        f"{payload.label} stock={payload.stock_quantity}",
        actor=seller,
        request=request,
    )
    return {
        "id": variant_id,
        "product_id": product_id,
        "label": payload.label,
        "stock_quantity": payload.stock_quantity,
    }


def update_product(product_id: str, payload: ProductUpdateInput, request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        product = connection.execute(
            "SELECT * FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        if user["role"] != "ADMIN" and product["seller_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        has_variants = connection.execute(
            "SELECT 1 FROM product_variants WHERE product_id = ? LIMIT 1",
            (product_id,),
        ).fetchone()
        if has_variants and payload.stock_quantity is not None:
            raise HTTPException(
                status_code=422,
                detail="Badilisha stock ya kila variant kivyake.",
            )
        name = payload.name if payload.name is not None else product["name"]
        description = payload.description if payload.description is not None else product["description"]
        image_url = payload.image_url if payload.image_url is not None else product["image_url"]
        category = payload.category if payload.category is not None else product["category"]
        subcategory = payload.subcategory if payload.subcategory is not None else product["subcategory"]
        brand = payload.brand if payload.brand is not None else product["brand"]
        video_url = payload.video_url if payload.video_url is not None else product["video_url"]
        amount = payload.amount if payload.amount is not None else product["amount"]
        cost_amount = payload.cost_amount if payload.cost_amount is not None else product["cost_amount"]
        stock_quantity = payload.stock_quantity if payload.stock_quantity is not None else product["stock_quantity"]
        low_stock_threshold = (
            payload.low_stock_threshold
            if payload.low_stock_threshold is not None
            else product["low_stock_threshold"]
        )
        connection.execute(
            """
            UPDATE products
            SET name = ?, description = ?, image_url = ?, category = ?, subcategory = ?, brand = ?, video_url = ?, amount = ?,
                cost_amount = ?, stock_quantity = ?, low_stock_threshold = ?
            WHERE id = ?
            """,
            (
                name,
                description,
                image_url,
                category,
                subcategory,
                brand,
                video_url,
                amount,
                cost_amount,
                stock_quantity,
                low_stock_threshold,
                product_id,
            ),
        )
        stock_delta = stock_quantity - product["stock_quantity"]
        if stock_delta:
            record_inventory_movement(
                connection,
                product_id,
                product["seller_id"],
                user["id"],
                stock_delta,
                "MANUAL_ADJUSTMENT",
            )
    record_audit("PRODUCT_UPDATED", "product", product_id, f"{name} stock={stock_quantity}", actor=user, request=request)
    return {
        "id": product_id,
        "name": name,
        "description": description,
        "image_url": image_url,
        "category": category,
        "subcategory": subcategory,
        "brand": brand,
        "video_url": video_url,
        "amount": amount,
        "cost_amount": cost_amount,
        "stock_quantity": stock_quantity,
        "low_stock_threshold": low_stock_threshold,
    }


def archive_product(product_id: str, request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        product = connection.execute(
            "SELECT seller_id FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None or (user["role"] != "ADMIN" and product["seller_id"] != user["id"]):
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        connection.execute("UPDATE products SET is_active = 0 WHERE id = ?", (product_id,))
    record_audit("PRODUCT_ARCHIVED", "product", product_id, actor=user, request=request)
    return {"message": "Bidhaa imeondolewa kwenye orodha ya mauzo."}


def read_product(product_id: str):
    product = get_product(product_id)
    result = {
        "id": product["id"],
        "name": product["name"],
        "description": product["description"],
        "image_url": product["image_url"],
        "category": product["category"],
        "amount": product["amount"],
        "stock_quantity": product["stock_quantity"],
        "shop_name": product["shop_name"] or product["seller_name"] or "Muuzaji",
        "store_slug": product["store_slug"] or "",
        **list_product_reviews(product["id"]),
    }
    return attach_product_variants(
        attach_product_videos(attach_product_gallery([result]))
    )[0]


@app.get("/api/sellers/{seller_id}/profile-image")
def seller_profile_image(seller_id: str):
    with db_connection() as connection:
        row = connection.execute(
            "SELECT content_type, image_data FROM account_images WHERE user_id = ?",
            (seller_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Picha ya muuzaji haijapakiwa.")
    return Response(
        content=bytes(row["image_data"]),
        media_type=row["content_type"],
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.api_route("/api/account/profile-image", methods=["GET", "POST", "DELETE"])
async def account_profile_image(request: Request, image: UploadFile = File(None)):
    if request.method == "GET":
        user = get_current_user(request)
        with db_connection() as connection:
            row = connection.execute(
                "SELECT content_type, image_data FROM account_images WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Picha ya profile haijapakiwa.")
        return Response(content=bytes(row["image_data"]), media_type=row["content_type"], headers={"Cache-Control": "private, max-age=300"})

    user = get_current_user(request)
    if request.method == "DELETE":
        with db_connection() as connection:
            connection.execute("DELETE FROM account_images WHERE user_id = ?", (user["id"],))
            connection.execute("UPDATE users SET profile_image_url = '' WHERE id = ?", (user["id"],))
        return {"profile_image_url": ""}

    if image is None:
        raise HTTPException(status_code=422, detail="Chagua picha ya profile.")
    content_type, image_data = read_uploaded_product_image(image)
    profile_image_url = "/api/account/profile-image"
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO account_images (user_id, content_type, image_data)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                content_type = excluded.content_type,
                image_data = excluded.image_data,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user["id"], content_type, image_data),
        )
        connection.execute(
            "UPDATE users SET profile_image_url = ? WHERE id = ?",
            (profile_image_url, user["id"]),
        )
    record_audit("PROFILE_IMAGE_UPDATED", "account", user["id"], content_type, actor=user, request=request)
    return {"profile_image_url": profile_image_url}


def read_uploaded_product_image(image):
    content_type = (image.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=415, detail="Tumia picha ya JPEG, PNG au WebP.")
    image_data = image.file.read(MAX_PRODUCT_IMAGE_BYTES + 1)
    if len(image_data) > MAX_PRODUCT_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Picha imezidi ukubwa wa MB 5.")
    if not image_data or not upload_media_type(content_type, image_data):
        raise HTTPException(status_code=422, detail="Faili halilingani na aina ya picha iliyochaguliwa.")
    return content_type, image_data


def upload_product_image(
    product_id: str,
    request: Request,
    image: UploadFile = File(...),
):
    seller = require_seller(request)
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_PRODUCT_IMAGE_BYTES + 65536:
        raise HTTPException(status_code=413, detail="Picha imezidi ukubwa wa MB 5.")

    content_type, image_data = read_uploaded_product_image(image)

    with db_connection() as connection:
        product = connection.execute(
            "SELECT seller_id FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None or product["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        image_url = f"/api/products/{product_id}/image"
        connection.execute(
            """
            INSERT INTO product_images (product_id, content_type, image_data)
            VALUES (?, ?, ?)
            ON CONFLICT (product_id) DO UPDATE SET
                content_type = excluded.content_type,
                image_data = excluded.image_data,
                updated_at = CURRENT_TIMESTAMP
            """,
            (product_id, content_type, image_data),
        )
        connection.execute(
            "UPDATE products SET image_url = ? WHERE id = ?",
            (image_url, product_id),
        )
    record_audit(
        "PRODUCT_IMAGE_UPLOADED",
        "product",
        product_id,
        content_type,
        actor=seller,
        request=request,
    )
    invalidate_public_product_lists()
    return {"product_id": product_id, "image_url": image_url}


@app.post("/api/documents")
async def upload_seller_document(
    request: Request,
    kind: Literal["seller_verification", "product_document"] = Form(...),
    product_id: str | None = Form(default=None),
    document_role: str = Form(default=""),
    expires_at: str = Form(default=""),
    document: UploadFile = File(...),
):
    seller = require_seller(request)
    if (kind == "product_document") != bool(product_id):
        raise HTTPException(status_code=422, detail="Chagua aina sahihi ya nyaraka na bidhaa.")
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_DOCUMENT_BYTES + 65536:
        raise HTTPException(status_code=413, detail="Nyaraka imezidi ukubwa wa MB 10.")

    content_type = (document.content_type or "").lower()
    allowed_types = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
    if content_type not in allowed_types:
        raise HTTPException(status_code=415, detail="Tumia PDF, JPEG, PNG au WebP.")
    data = document.file.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="Nyaraka imezidi ukubwa wa MB 10.")
    if not data or not upload_media_type(content_type, data):
        raise HTTPException(status_code=422, detail="Faili halilingani na aina ya nyaraka iliyochaguliwa.")

    if product_id:
        with db_connection() as connection:
            product = connection.execute(
                "SELECT seller_id FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()
        if product is None or product["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")

    document_id = secrets.token_hex(16)
    original_filename = re.split(r"[/\\]", document.filename or "nyaraka")[-1]
    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", original_filename)[:200] or "nyaraka"
    storage_data = data
    if kind == "seller_verification":
        try:
            storage_data = encrypt_kyc_document(data)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
    expires_at_value = expires_at.strip() or None
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO seller_documents
                (id, seller_id, product_id, kind, document_role, expires_at,
                 filename, content_type, document_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                seller["id"],
                product_id,
                kind,
                document_role.strip()[:100],
                expires_at_value,
                filename,
                content_type,
                storage_data,
            ),
        )
    record_audit(
        "DOCUMENT_UPLOADED",
        "document",
        document_id,
        kind,
        actor=seller,
        request=request,
    )
    return {
        "id": document_id,
        "kind": kind,
        "filename": filename,
        "content_type": content_type,
        "document_role": document_role.strip(),
        "expires_at": expires_at_value,
        "product_id": product_id,
    }


@app.post("/api/kyc")
def submit_seller_kyc(payload: KycSubmitInput, request: Request):
    seller = require_seller(request)
    if payload.seller_type == "INDIVIDUAL":
        if not payload.identity_name.strip() or not payload.identity_number.strip():
            raise HTTPException(
                status_code=422,
                detail="Individual seller anahitaji majina na namba ya utambulisho.",
            )
    else:
        if not payload.business_name.strip() or not payload.business_address.strip():
            raise HTTPException(
                status_code=422,
                detail="Business seller anahitaji jina na anwani ya biashara.",
            )
    with db_connection() as connection:
        current = connection.execute(
            "SELECT status FROM seller_kyc WHERE seller_id = ?",
            (seller["id"],),
        ).fetchone()
        if current and current["status"] == "VERIFIED":
            raise HTTPException(
                status_code=409,
                detail="Taarifa zako za KYC tayari zimethibitishwa.",
            )
        document_count = connection.execute(
            """
            SELECT COUNT(*) AS count FROM seller_documents
            WHERE seller_id = ? AND kind = 'seller_verification'
            """,
            (seller["id"],),
        ).fetchone()["count"]
        if document_count < 1:
            raise HTTPException(
                status_code=422,
                detail="Pakia angalau nyaraka moja ya uthibitisho kabla ya kutuma KYC.",
            )
        connection.execute(
            """
            INSERT INTO seller_kyc (
                seller_id, seller_type, phone, identity_name, identity_number,
                business_name, business_registration_number, tin_number,
                business_license_number, business_address, status,
                rejection_reason, submitted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_VERIFICATION', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (seller_id) DO UPDATE SET
                seller_type = excluded.seller_type,
                phone = excluded.phone,
                identity_name = excluded.identity_name,
                identity_number = excluded.identity_number,
                business_name = excluded.business_name,
                business_registration_number = excluded.business_registration_number,
                tin_number = excluded.tin_number,
                business_license_number = excluded.business_license_number,
                business_address = excluded.business_address,
                status = 'PENDING_VERIFICATION',
                rejection_reason = '',
                submitted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                seller["id"],
                payload.seller_type,
                payload.phone,
                payload.identity_name.strip(),
                payload.identity_number.strip(),
                payload.business_name.strip(),
                payload.business_registration_number.strip(),
                payload.tin_number.strip(),
                payload.business_license_number.strip(),
                payload.business_address.strip(),
            ),
        )
        connection.execute(
            "INSERT INTO kyc_history (id, seller_id, actor_id, status, reason) VALUES (?, ?, ?, 'PENDING_VERIFICATION', 'Seller submitted KYC')",
            (secrets.token_hex(16), seller["id"], seller["id"]),
        )
        connection.execute(
            "UPDATE users SET phone = ?, kyc_status = 'PENDING_VERIFICATION' WHERE id = ?",
            (payload.phone, seller["id"]),
        )
    record_audit("KYC_SUBMITTED", "seller", seller["id"], "Pending verification", actor=seller, request=request)
    return get_seller_kyc_status(request)


@app.get("/api/kyc")
def get_seller_kyc_status(request: Request):
    seller = require_seller(request)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM seller_kyc WHERE seller_id = ?",
            (seller["id"],),
        ).fetchone()
        documents = connection.execute(
            """
            SELECT id, document_role, filename, content_type, expires_at, created_at
            FROM seller_documents
            WHERE seller_id = ? AND kind = 'seller_verification'
            ORDER BY created_at DESC
            """,
            (seller["id"],),
        ).fetchall()
    if row is None:
        return {
            "status": "NOT_VERIFIED",
            "profile": None,
            "documents": [dict(item) for item in documents],
            "history": [],
        }
    profile = dict(row)
    with db_connection() as connection:
        history = connection.execute(
            """
            SELECT id, actor_id, status, reason, created_at
            FROM kyc_history WHERE seller_id = ?
            ORDER BY created_at DESC LIMIT 100
            """,
            (seller["id"],),
        ).fetchall()
    return {
        "status": profile["status"],
        "profile": profile,
        "documents": [dict(item) for item in documents],
        "history": [dict(item) for item in history],
    }


@app.get("/api/admin/kyc")
def list_seller_kyc(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT k.*, u.full_name, u.email, u.shop_name, u.store_slug
            FROM seller_kyc k
            JOIN users u ON u.id = k.seller_id
            ORDER BY k.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/admin/kyc/{seller_id}")
def review_seller_kyc(seller_id: str, payload: KycReviewInput, request: Request):
    actor = require_admin(request)
    if payload.status in {"REJECTED", "RESUBMISSION_REQUIRED"} and not payload.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="Toa sababu kwa kukataa au kuomba nyaraka nyingine.",
        )
    expires_at = payload.expires_at.strip() or None
    with db_connection() as connection:
        row = connection.execute(
            "SELECT seller_id FROM seller_kyc WHERE seller_id = ?",
            (seller_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Maombi ya KYC hayajapatikana.")
        connection.execute(
            """
            UPDATE seller_kyc
            SET status = ?, rejection_reason = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP,
                expires_at = ?, updated_at = CURRENT_TIMESTAMP
            WHERE seller_id = ?
            """,
            (payload.status, payload.reason.strip(), actor["id"], expires_at, seller_id),
        )
        connection.execute(
            "UPDATE users SET kyc_status = ? WHERE id = ? AND role = 'SELLER'",
            (payload.status, seller_id),
        )
        connection.execute(
            "INSERT INTO kyc_history (id, seller_id, actor_id, status, reason) VALUES (?, ?, ?, ?, ?)",
            (
                secrets.token_hex(16),
                seller_id,
                actor["id"],
                payload.status,
                payload.reason.strip(),
            ),
        )
    record_audit("KYC_REVIEWED", "seller", seller_id, f"{payload.status}: {payload.reason}", actor=actor, request=request)
    with db_connection() as connection:
        seller = connection.execute(
            "SELECT email, phone FROM users WHERE id = ?",
            (seller_id,),
        ).fetchone()
    if seller:
        queue_user_notification(
            seller_id,
            "KYC_STATUS_CHANGED",
            {"status": payload.status},
            email=seller["email"],
            phone=seller["phone"],
        )
    return {"seller_id": seller_id, "status": payload.status, "reason": payload.reason.strip()}


@app.get("/api/admin/kyc/{seller_id}")
def get_seller_kyc_review_data(seller_id: str, request: Request):
    require_admin(request)
    with db_connection() as connection:
        profile = connection.execute(
            """
            SELECT k.*, u.full_name, u.email, u.phone AS account_phone,
                   u.status AS account_status, u.shop_name, u.store_slug
            FROM seller_kyc k
            JOIN users u ON u.id = k.seller_id
            WHERE k.seller_id = ?
            """,
            (seller_id,),
        ).fetchone()
        if profile is None:
            raise HTTPException(status_code=404, detail="Maombi ya KYC hayajapatikana.")
        documents = connection.execute(
            """
            SELECT id, document_role, filename, content_type, expires_at, created_at
            FROM seller_documents
            WHERE seller_id = ? AND kind = 'seller_verification'
            ORDER BY created_at DESC
            """,
            (seller_id,),
        ).fetchall()
        history = connection.execute(
            """
            SELECT h.id, h.status, h.reason, h.created_at,
                   u.full_name AS actor_name
            FROM kyc_history h
            LEFT JOIN users u ON u.id = h.actor_id
            WHERE h.seller_id = ?
            ORDER BY h.created_at DESC
            """,
            (seller_id,),
        ).fetchall()
    return {
        "profile": dict(profile),
        "documents": [dict(row) for row in documents],
        "history": [dict(row) for row in history],
    }


@app.get("/api/admin/kyc/{seller_id}/documents/{document_id}")
def read_seller_kyc_document_admin(
    seller_id: str,
    document_id: str,
    request: Request,
):
    require_admin(request)
    with db_connection() as connection:
        document = connection.execute(
            """
            SELECT filename, content_type, document_data
            FROM seller_documents
            WHERE id = ? AND seller_id = ? AND kind = 'seller_verification'
            """,
            (document_id, seller_id),
        ).fetchone()
    if document is None:
        raise HTTPException(status_code=404, detail="Nyaraka ya KYC haikupatikana.")
    try:
        document_data = decrypt_kyc_document(document["document_data"])
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    record_audit(
        "KYC_DOCUMENT_VIEWED",
        "seller",
        seller_id,
        document["filename"],
        actor=get_current_user(request),
        request=request,
    )
    return Response(
        content=document_data,
        media_type=document["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{urllib.parse.quote(document["filename"])}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@app.delete("/api/kyc")
def delete_seller_kyc_submission(request: Request):
    seller = require_seller(request)
    with db_connection() as connection:
        row = connection.execute(
            "SELECT status FROM seller_kyc WHERE seller_id = ?",
            (seller["id"],),
        ).fetchone()
        if row and row["status"] == "VERIFIED":
            raise HTTPException(
                status_code=409,
                detail="Nyaraka za KYC zilizothibitishwa haziwezi kufutwa na muuzaji.",
            )
        connection.execute(
            "DELETE FROM seller_documents WHERE seller_id = ? AND kind = 'seller_verification'",
            (seller["id"],),
        )
        connection.execute(
            "DELETE FROM seller_kyc WHERE seller_id = ?",
            (seller["id"],),
        )
        connection.execute(
            "UPDATE users SET kyc_status = 'NOT_VERIFIED' WHERE id = ?",
            (seller["id"],),
        )
        connection.execute(
            "INSERT INTO kyc_history (id, seller_id, actor_id, status, reason) VALUES (?, ?, ?, 'NOT_VERIFIED', 'Seller withdrew KYC submission')",
            (secrets.token_hex(16), seller["id"], seller["id"]),
        )
    record_audit("KYC_SUBMISSION_DELETED", "seller", seller["id"], "Seller withdrew KYC submission", actor=seller, request=request)
    return {"status": "NOT_VERIFIED", "deleted": True}


@app.get("/api/kyc/requirements")
def list_kyc_requirements():
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT seller_type, requirement_key, label, is_required,
                   document_kind, expires_policy_days, is_active
            FROM kyc_requirements
            WHERE is_active = 1
            ORDER BY seller_type, label
            """
        ).fetchall()
    result = {"INDIVIDUAL": [], "BUSINESS": []}
    for row in rows:
        result[row["seller_type"]].append(dict(row))
    return result


@app.post("/api/admin/kyc/requirements")
def upsert_kyc_requirement(payload: KycRequirementInput, request: Request):
    actor = require_admin(request)
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO kyc_requirements
                (id, seller_type, requirement_key, label, is_required,
                 document_kind, expires_policy_days, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (seller_type, requirement_key) DO UPDATE SET
                label = excluded.label,
                is_required = excluded.is_required,
                document_kind = excluded.document_kind,
                expires_policy_days = excluded.expires_policy_days,
                is_active = excluded.is_active
            """,
            (
                secrets.token_hex(16),
                payload.seller_type,
                payload.requirement_key.strip(),
                payload.label.strip(),
                payload.is_required,
                payload.document_kind.strip(),
                payload.expires_policy_days,
                payload.is_active,
            ),
        )
    record_audit(
        "KYC_REQUIREMENT_UPDATED",
        "kyc",
        payload.requirement_key,
        payload.seller_type,
        actor=actor,
        request=request,
    )
    return payload.model_dump()


@app.get("/api/documents")
def seller_documents(request: Request):
    seller = require_seller(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, product_id, kind, filename, content_type, created_at
            FROM seller_documents WHERE seller_id = ?
            ORDER BY created_at DESC LIMIT 200
            """,
            (seller["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/documents/{document_id}")
def read_seller_document(document_id: str, request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        document = connection.execute(
            """
            SELECT seller_id, kind, filename, content_type, document_data
            FROM seller_documents WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
    if document is None or (
        user["role"] != "ADMIN" and document["seller_id"] != user["id"]
    ):
        raise HTTPException(status_code=404, detail="Nyaraka haikupatikana.")
    document_data = document["document_data"]
    if document["kind"] == "seller_verification":
        try:
            document_data = decrypt_kyc_document(document["document_data"])
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
    return Response(
        content=document_data,
        media_type=document["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{urllib.parse.quote(document["filename"])}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


def upload_product_gallery(
    product_id: str,
    request: Request,
    images: list[UploadFile] = File(...),
):
    seller = require_seller(request)
    if not images or len(images) > MAX_PRODUCT_GALLERY_IMAGES:
        raise HTTPException(
            status_code=422,
            detail=f"Chagua picha 1 hadi {MAX_PRODUCT_GALLERY_IMAGES} za bidhaa.",
        )
    declared_length = request.headers.get("content-length")
    max_request_bytes = len(images) * (MAX_PRODUCT_IMAGE_BYTES + 65536)
    if declared_length and declared_length.isdigit() and int(declared_length) > max_request_bytes:
        raise HTTPException(status_code=413, detail="Picha za bidhaa zimezidi ukubwa unaoruhusiwa.")
    prepared_images = [read_uploaded_product_image(image) for image in images]

    with db_connection() as connection:
        product = connection.execute(
            "SELECT seller_id, image_url FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None or product["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        existing = connection.execute(
            """
            SELECT id, sort_order FROM product_gallery_images
            WHERE product_id = ?
            ORDER BY sort_order, id
            """,
            (product_id,),
        ).fetchall()
        primary_is_gallery = any(
            product["image_url"] == f"/api/products/{product_id}/gallery/{row['id']}"
            for row in existing
        )
        current_count = len(existing) + int(bool(product["image_url"]) and not primary_is_gallery)
        if current_count + len(prepared_images) > MAX_PRODUCT_GALLERY_IMAGES:
            raise HTTPException(
                status_code=409,
                detail=f"Bidhaa inaweza kuwa na picha {MAX_PRODUCT_GALLERY_IMAGES} tu kwa jumla.",
            )
        next_order = max((row["sort_order"] for row in existing), default=-1) + 1
        new_ids = []
        for index, (content_type, image_data) in enumerate(prepared_images):
            image_id = secrets.token_hex(16)
            connection.execute(
                """
                INSERT INTO product_gallery_images
                    (id, product_id, sort_order, content_type, image_data)
                VALUES (?, ?, ?, ?, ?)
                """,
                (image_id, product_id, next_order + index, content_type, image_data),
            )
            new_ids.append(image_id)
        if not product["image_url"]:
            connection.execute(
                "UPDATE products SET image_url = ? WHERE id = ?",
                (f"/api/products/{product_id}/gallery/{new_ids[0]}", product_id),
            )
    invalidate_public_product_lists()
    return {
        "product_id": product_id,
        "image_ids": new_ids,
    }


def read_product_gallery_image(product_id: str, image_id: str):
    with db_connection() as connection:
        image = connection.execute(
            """
            SELECT content_type, image_data FROM product_gallery_images
            WHERE product_id = ? AND id = ?
            """,
            (product_id, image_id),
        ).fetchone()
    if image is None:
        raise HTTPException(status_code=404, detail="Picha ya pembe ya bidhaa haikupatikana.")
    return Response(
        content=image["image_data"],
        media_type=image["content_type"],
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


def delete_product_gallery_image(product_id: str, image_id: str, request: Request):
    seller = require_seller(request)
    image_url = f"/api/products/{product_id}/gallery/{image_id}"
    with db_connection() as connection:
        product = connection.execute(
            "SELECT seller_id, image_url FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None or product["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        deleted = connection.execute(
            "DELETE FROM product_gallery_images WHERE product_id = ? AND id = ?",
            (product_id, image_id),
        )
        if deleted.rowcount == 0:
            raise HTTPException(status_code=404, detail="Picha ya pembe ya bidhaa haikupatikana.")
        if product["image_url"] == image_url:
            replacement = connection.execute(
                """
                SELECT id FROM product_gallery_images
                WHERE product_id = ?
                ORDER BY sort_order, id
                LIMIT 1
                """,
                (product_id,),
            ).fetchone()
            replacement_url = (
                f"/api/products/{product_id}/gallery/{replacement['id']}"
                if replacement
                else ""
            )
            connection.execute(
                "UPDATE products SET image_url = ? WHERE id = ?",
                (replacement_url, product_id),
            )
    invalidate_public_product_lists()
    return {"product_id": product_id, "image_id": image_id}


def read_uploaded_product_video(video):
    content_type = (video.content_type or "").lower().split(";", 1)[0].strip()
    signatures = {
        "video/mp4": lambda data: len(data) >= 12 and data[4:8] == b"ftyp",
        "video/webm": lambda data: data.startswith(b"\x1a\x45\xdf\xa3"),
    }
    if content_type not in signatures:
        raise HTTPException(status_code=415, detail="Tumia video ya MP4 au WebM.")
    video_data = video.file.read(MAX_PRODUCT_VIDEO_BYTES + 1)
    if len(video_data) > MAX_PRODUCT_VIDEO_BYTES:
        raise HTTPException(status_code=413, detail="Video imezidi ukubwa wa MB 20.")
    if not video_data or not signatures[content_type](video_data):
        raise HTTPException(
            status_code=422,
            detail="Faili la video halilingani na aina iliyochaguliwa.",
        )
    return content_type, video_data


def upload_product_video(
    product_id: str,
    request: Request,
    video: UploadFile = File(...),
):
    seller = require_seller(request)
    declared_length = request.headers.get("content-length")
    if (
        declared_length
        and declared_length.isdigit()
        and int(declared_length) > MAX_PRODUCT_VIDEO_BYTES + 65536
    ):
        raise HTTPException(status_code=413, detail="Video imezidi ukubwa wa MB 20.")

    content_type, video_data = read_uploaded_product_video(video)
    with db_connection() as connection:
        product = connection.execute(
            "SELECT seller_id FROM products WHERE id = ? AND is_active = 1",
            (product_id,),
        ).fetchone()
        if product is None or product["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        video_count = connection.execute(
            "SELECT COUNT(*) AS count FROM product_videos WHERE product_id = ?",
            (product_id,),
        ).fetchone()["count"]
        if video_count >= MAX_PRODUCT_VIDEOS:
            raise HTTPException(
                status_code=409,
                detail=f"Bidhaa inaweza kuwa na video {MAX_PRODUCT_VIDEOS} tu.",
            )
        video_id = secrets.token_hex(16)
        connection.execute(
            """
            INSERT INTO product_videos (id, product_id, content_type, video_data)
            VALUES (?, ?, ?, ?)
            """,
            (video_id, product_id, content_type, video_data),
        )
    invalidate_public_product_lists()
    return {
        "product_id": product_id,
        "video": {
            "id": video_id,
            "url": f"/api/products/{product_id}/videos/{video_id}",
            "content_type": content_type,
        },
    }


def read_product_video(product_id: str, video_id: str, request: Request):
    with db_connection() as connection:
        video = connection.execute(
            """
            SELECT v.content_type, v.video_data
            FROM product_videos v
            JOIN products p ON p.id = v.product_id
            LEFT JOIN users u ON u.id = p.seller_id
            WHERE v.product_id = ? AND v.id = ? AND p.is_active = 1
              AND (p.seller_id IS NULL OR u.status = 'ACTIVE')
            """,
            (product_id, video_id),
        ).fetchone()
    if video is None:
        raise HTTPException(status_code=404, detail="Video haikupatikana.")

    video_data = video["video_data"]
    total_bytes = len(video_data)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    }
    range_header = request.headers.get("range")
    if not range_header:
        return Response(
            content=video_data,
            media_type=video["content_type"],
            headers=headers,
        )

    normalized_range = range_header.strip()
    match = (
        re.fullmatch(r"bytes=(\d*)-(\d*)", normalized_range)
        if len(normalized_range) <= 100
        else None
    )
    if match is None or (not match.group(1) and not match.group(2)):
        return Response(
            status_code=416,
            headers={**headers, "Content-Range": f"bytes */{total_bytes}"},
        )
    if not match.group(1):
        suffix_bytes = int(match.group(2))
        if suffix_bytes <= 0:
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{total_bytes}"},
            )
        start = max(total_bytes - suffix_bytes, 0)
        end = total_bytes - 1
    else:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else total_bytes - 1
        if start >= total_bytes or start > end:
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{total_bytes}"},
            )
        end = min(end, total_bytes - 1)

    return Response(
        content=video_data[start:end + 1],
        status_code=206,
        media_type=video["content_type"],
        headers={
            **headers,
            "Content-Range": f"bytes {start}-{end}/{total_bytes}",
        },
    )


def list_seller_documents(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT d.id, d.seller_id, u.shop_name, u.full_name AS seller_name,
                   d.product_id, d.kind, d.filename, d.content_type, d.created_at
            FROM seller_documents d
            JOIN users u ON u.id = d.seller_id
            ORDER BY d.created_at DESC
            LIMIT 500
            """
        ).fetchall()
    return [dict(row) for row in rows]


def delete_product_video(product_id: str, video_id: str, request: Request):
    seller = require_seller(request)
    with db_connection() as connection:
        product = connection.execute(
            "SELECT seller_id FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None or product["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        deleted = connection.execute(
            "DELETE FROM product_videos WHERE product_id = ? AND id = ?",
            (product_id, video_id),
        )
        if deleted.rowcount == 0:
            raise HTTPException(status_code=404, detail="Video haikupatikana.")
    invalidate_public_product_lists()
    return {"product_id": product_id, "video_id": video_id}


def read_product_image(product_id: str):
    with db_connection() as connection:
        image = connection.execute(
            "SELECT content_type, image_data FROM product_images WHERE product_id = ?",
            (product_id,),
        ).fetchone()
    if image is None:
        raise HTTPException(status_code=404, detail="Picha ya bidhaa haikupatikana.")
    return Response(
        content=image["image_data"],
        media_type=image["content_type"],
        headers={
            "Cache-Control": "public, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
        },
    )


def delete_product_image(product_id: str, request: Request):
    seller = require_seller(request)
    image_url = f"/api/products/{product_id}/image"
    with db_connection() as connection:
        product = connection.execute(
            "SELECT seller_id, image_url FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None or product["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        connection.execute(
            "DELETE FROM product_images WHERE product_id = ?",
            (product_id,),
        )
        if product["image_url"] and not product["image_url"].startswith(
            f"/api/products/{product_id}/gallery/"
        ):
            connection.execute(
                "UPDATE products SET image_url = '' WHERE id = ?",
                (product_id,),
            )
    record_audit("PRODUCT_IMAGE_DELETED", "product", product_id, actor=seller, request=request)
    invalidate_public_product_lists()
    return {"product_id": product_id, "image_url": ""}


async def create_checkout(product_id: str, payload: CheckoutInput, request: Request):
    demo_mode = is_demo_mode()
    sandbox_mode = validate_pesapal_sandbox_configuration()
    abliner_mode = payload.payment_method == ABLINER_PAYMENT_METHOD
    if abliner_mode:
        require_abliner_live()
    elif not demo_mode and not clickpesa_live_enabled():
        raise HTTPException(
            status_code=503,
            detail="Malipo ya ClickPesa hayajaanzishwa au hayajakamilisha mipangilio salama.",
        )
    if not demo_mode and not sandbox_mode and not abliner_mode and payload.payment_method not in {
        "M-Pesa",
        "Halo Pesa",
        "Tigo Pesa",
        "Airtel Money",
    }:
        raise HTTPException(
            status_code=422,
            detail="ClickPesa USSD Push inakubali malipo ya simu pekee.",
        )
    product = get_product(product_id)
    if not product["seller_id"]:
        raise HTTPException(status_code=503, detail="Bidhaa hii inahitaji kuunganishwa na akaunti ya muuzaji.")
    if product["stock_quantity"] <= 0:
        raise HTTPException(status_code=409, detail="Stock ya bidhaa imeisha.")
    if not sandbox_mode and payload.payment_method.endswith("Branch") and not payload.bank_branch:
        raise HTTPException(status_code=422, detail="Chagua tawi la benki.")

    with db_connection() as connection:
        existing = connection.execute(
            "SELECT * FROM orders WHERE request_key = ?",
            (payload.request_key,),
        ).fetchone()
        if existing:
            order = dict(existing)
            if order["product_id"] != product_id:
                raise HTTPException(status_code=409, detail="Ombi hili la malipo tayari limetumika.")
            if order["checkout_url"]:
                return {
                    "order_id": order["order_id"],
                    "checkout_url": order["checkout_url"],
                    "payment_url": stored_pesapal_payment_url(order),
                    "payment_status": order["payment_status"],
                    "demo": not bool(order["provider_reference"])
                    and order["payment_method"] != "PesaPal Sandbox",
                    "sandbox": order["payment_method"] == "PesaPal Sandbox",
                }
            return {
                "order_id": order["order_id"],
                "payment_status": order["payment_status"],
                "demo": not bool(order["provider_reference"])
                and order["payment_method"] != "PesaPal Sandbox",
                "sandbox": order["payment_method"] == "PesaPal Sandbox",
            }

    demo_order = demo_mode and not sandbox_mode and not abliner_mode
    order_id = (
        "TEST" if sandbox_mode else "DEMO" if demo_order else "ORD"
    ) + secrets.token_hex(10).upper()
    provider_reference = (
        "LR" + secrets.token_hex(9).upper()
        if sandbox_mode or not demo_order
        else ""
    )
    buyer = optional_current_user(request)
    buyer_id = buyer["id"] if buyer and buyer["role"] == "BUYER" else None
    buyer_email = checkout_buyer_email(payload, buyer)
    with db_connection() as connection:
        variant = validate_variant_selection(
            connection,
            product,
            payload.variant_id,
            1,
        )
        coupon_code, coupon_discount = coupon_discount_for_checkout(
            connection,
            payload.coupon_code,
            product["amount"],
            buyer_id,
            buyer_email,
            demo_checkout=demo_order,
        )
    amount = product["amount"] - coupon_discount
    try:
        with db_connection() as connection:
            tracking_code = generate_tracking_code(connection)
            connection.execute(
                """
                INSERT INTO orders
                    (order_id, request_key, product_id, seller_id, amount,
                     buyer_name, buyer_email, buyer_phone, buyer_id,
                     payment_method, bank_branch, delivery_address, provider_reference,
                     product_cost, expires_at, tracking_code, coupon_code, coupon_discount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    payload.request_key,
                    product_id,
                    product["seller_id"],
                    amount,
                    payload.buyer_name.strip(),
                    buyer_email,
                    payload.buyer_phone,
                    buyer_id,
                    ABLINER_PAYMENT_METHOD
                    if abliner_mode
                    else "PesaPal Sandbox" if sandbox_mode
                    else payload.payment_method if demo_mode else "ClickPesa USSD Push",
                    payload.bank_branch or "",
                    payload.delivery_address.strip(),
                    provider_reference,
                    product["cost_amount"],
                    time.time() + ORDER_TTL_SECONDS,
                    tracking_code,
                    coupon_code,
                    coupon_discount,
                ),
            )
            connection.execute(
                """
                INSERT INTO delivery_tracking_events
                    (id, order_id, status, description, actor_id)
                VALUES (?, ?, 'ORDER_PLACED', 'Order placed', ?)
                """,
                (secrets.token_hex(16), order_id, buyer_id),
            )
            connection.execute(
                """
                INSERT INTO order_items
                    (order_id, product_id, quantity, unit_amount, unit_cost,
                     variant_id, variant_label)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    product_id,
                    product["amount"],
                    product["cost_amount"],
                    variant["variant_id"],
                    variant["variant_label"],
                ),
            )
            schedule_abandoned_checkout_reminder(
                connection,
                "ORDER",
                order_id,
                buyer_email,
                payload.buyer_name,
                amount,
                product_id=product_id,
                consent=payload.abandoned_checkout_consent,
            )
    except Exception as error:
        if is_integrity_error(error):
            with db_connection() as connection:
                existing = connection.execute(
                    "SELECT * FROM orders WHERE request_key = ?",
                    (payload.request_key,),
                ).fetchone()
            if existing is not None and existing["product_id"] == product_id:
                return {
                    "order_id": existing["order_id"],
                    "payment_status": existing["payment_status"],
                    "demo": not bool(existing["provider_reference"]),
                    "sandbox": existing["payment_method"] == "PesaPal Sandbox",
                    "payment_url": stored_pesapal_payment_url(existing),
                }
            raise HTTPException(status_code=409, detail="Ombi hili la malipo tayari limetumika.") from error
        raise

    app_url = public_app_url(str(request.base_url))
    checkout_url = f"{app_url}/pay/{quote(product_id)}?order_id={quote(order_id)}"
    if sandbox_mode:
        checkout_url += "&sandbox=1"
    elif demo_order:
        checkout_url += "&demo=1"
    with db_connection() as connection:
        connection.execute(
            "UPDATE orders SET checkout_url = ? WHERE order_id = ?",
            (checkout_url, order_id),
        )
    payment_status = "PENDING"
    payment_url = None
    if sandbox_mode:
        payment_status, payment_url = await begin_pesapal_payment(
            provider_reference,
            amount,
            f"LipaRahisi: {product['name']}",
            f"{app_url}/api/callbacks/pesapal",
            checkout_url,
            payload.buyer_name.strip(),
            payload.buyer_email,
            payload.buyer_phone,
        )
    elif abliner_mode:
        payment_status = await begin_abliner_payment(
            provider_reference,
            payload.buyer_phone,
            amount,
            f"{app_url}/api/webhooks/abliner",
        )
    elif not demo_mode:
        payment_status = await begin_clickpesa_payment(
            provider_reference,
            payload.buyer_phone,
            amount,
        )
    return {
        "order_id": order_id,
        "checkout_url": checkout_url,
        "payment_url": payment_url,
        "payment_status": payment_status,
        "demo": demo_order,
        "sandbox": sandbox_mode,
        "amount": amount,
        "coupon_code": coupon_code,
        "coupon_discount": coupon_discount,
    }


async def create_cart_checkout(payload: CartCheckoutInput, request: Request):
    demo_mode = is_demo_mode()
    sandbox_mode = validate_pesapal_sandbox_configuration()
    abliner_mode = payload.payment_method == ABLINER_PAYMENT_METHOD
    if abliner_mode:
        require_abliner_live()
    elif not demo_mode and not clickpesa_live_enabled():
        raise HTTPException(
            status_code=503,
            detail="Malipo ya ClickPesa hayajaanzishwa au hayajakamilisha mipangilio salama.",
        )
    if not demo_mode and not sandbox_mode and not abliner_mode and payload.payment_method not in {
        "M-Pesa",
        "Halo Pesa",
        "Tigo Pesa",
        "Airtel Money",
    }:
        raise HTTPException(
            status_code=422,
            detail="ClickPesa USSD Push inakubali malipo ya simu pekee.",
        )
    if not sandbox_mode and payload.payment_method.endswith("Branch") and not payload.bank_branch:
        raise HTTPException(status_code=422, detail="Chagua tawi la benki.")

    merged = {}
    buyer = optional_current_user(request)
    buyer_id = buyer["id"] if buyer and buyer["role"] == "BUYER" else None
    buyer_email = checkout_buyer_email(payload, buyer)
    with db_connection() as connection:
        seen_product_ids = set()
        for item in payload.items:
            if item.product_id in seen_product_ids:
                raise HTTPException(
                    status_code=422,
                    detail="Kila bidhaa iwe mara moja tu kwenye kikapu; chagua variant moja.",
                )
            seen_product_ids.add(item.product_id)
            product = connection.execute(
                """
                SELECT p.id, p.name, p.amount, p.cost_amount, p.stock_quantity, p.seller_id,
                       u.status AS seller_status
                FROM products p LEFT JOIN users u ON u.id = p.seller_id
                WHERE p.id = ? AND p.is_active = 1
                """,
                (item.product_id,),
            ).fetchone()
            if product is None or product["seller_id"] is None or product["seller_status"] != "ACTIVE":
                raise HTTPException(status_code=404, detail="Bidhaa haipatikani au muuzaji wake hajathibitishwa.")
            if product["stock_quantity"] < item.quantity:
                raise HTTPException(
                    status_code=409,
                    detail=f"Stock ya {product['name']} haitoshi; inabaki {product['stock_quantity']}.",
                )
            variant = validate_variant_selection(
                connection,
                product,
                item.variant_id,
                item.quantity,
            )
            product = {
                **dict(product),
                **variant,
            }
            seller_id = product["seller_id"]
            merged.setdefault(seller_id, []).append(
                (product, item.quantity)
            )
        existing = connection.execute(
            "SELECT checkout_id FROM orders WHERE cart_request_key = ?",
            (payload.request_key,),
        ).fetchone()
        if existing:
            return get_cart_checkout(existing["checkout_id"], request)

        seller_subtotals = [
            (
                seller_id,
                sum(product["amount"] * quantity for product, quantity in seller_items),
            )
            for seller_id, seller_items in merged.items()
        ]
        subtotal = sum(amount for _seller_id, amount in seller_subtotals)
        coupon_code, total_coupon_discount = coupon_discount_for_checkout(
            connection,
            payload.coupon_code,
            subtotal,
            buyer_id,
            buyer_email,
            minimum_total=len(seller_subtotals),
            demo_checkout=demo_mode and not sandbox_mode and not abliner_mode,
        )
        coupon_allocations = allocate_coupon_discount(
            total_coupon_discount,
            seller_subtotals,
        ) if total_coupon_discount else {}
        checkout_id = "CART" + secrets.token_hex(16).upper()
        demo_order = demo_mode and not sandbox_mode and not abliner_mode
        provider_reference = (
            "LR" + secrets.token_hex(9).upper()
            if sandbox_mode or not demo_order
            else ""
        )
        expires_at = time.time() + ORDER_TTL_SECONDS
        order_ids = []
        for seller_index, (seller_id, seller_items) in enumerate(merged.items()):
            order_id = ("TEST" if sandbox_mode else "DEMO" if demo_order else "ORD") + secrets.token_hex(10).upper()
            tracking_code = generate_tracking_code(connection)
            seller_subtotal = sum(
                product["amount"] * quantity for product, quantity in seller_items
            )
            coupon_discount = coupon_allocations.get(seller_id, 0)
            amount = seller_subtotal - coupon_discount
            product_cost = sum(product["cost_amount"] * quantity for product, quantity in seller_items)
            fee = 0 if sandbox_mode else commission_amount(amount)
            payout = 0 if sandbox_mode else amount - fee
            profit = 0 if sandbox_mode else payout - product_cost
            first_product = seller_items[0][0]
            connection.execute(
                """
                INSERT INTO orders
                    (order_id, request_key, product_id, seller_id, amount,
                     buyer_name, buyer_email, buyer_phone, buyer_id,
                     payment_method, bank_branch, delivery_address, provider_reference,
                     product_cost, platform_fee, seller_payout, net_profit,
                     checkout_id, cart_request_key, expires_at, tracking_code,
                     coupon_code, coupon_discount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    f"{payload.request_key}{seller_index:02d}",
                    first_product["id"],
                    seller_id,
                    amount,
                    payload.buyer_name.strip(),
                    buyer_email,
                    payload.buyer_phone,
                    buyer_id,
                    ABLINER_PAYMENT_METHOD
                    if abliner_mode
                    else "PesaPal Sandbox" if sandbox_mode
                    else payload.payment_method if demo_mode else "ClickPesa USSD Push",
                    payload.bank_branch or "",
                    payload.delivery_address.strip(),
                    provider_reference if seller_index == 0 else "",
                    product_cost,
                    fee,
                    payout,
                    profit,
                    checkout_id,
                    payload.request_key,
                    expires_at,
                    tracking_code,
                    coupon_code,
                    coupon_discount,
                ),
            )
            connection.execute(
                """
                INSERT INTO delivery_tracking_events
                    (id, order_id, status, description, actor_id)
                VALUES (?, ?, 'ORDER_PLACED', 'Order placed', ?)
                """,
                (secrets.token_hex(16), order_id, buyer_id),
            )
            for product, quantity in seller_items:
                connection.execute(
                    """
                    INSERT INTO order_items
                        (order_id, product_id, quantity, unit_amount, unit_cost,
                         variant_id, variant_label)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        product["id"],
                        quantity,
                        product["amount"],
                        product["cost_amount"],
                        product["variant_id"],
                        product["variant_label"],
                    ),
                )
            order_ids.append(order_id)
        schedule_abandoned_checkout_reminder(
            connection,
            "CART",
            checkout_id,
            buyer_email,
            payload.buyer_name,
            subtotal - total_coupon_discount,
            consent=payload.abandoned_checkout_consent,
        )

    app_url = public_app_url(str(request.base_url))
    checkout_url = f"{app_url}/cart/{quote(checkout_id)}"
    if sandbox_mode:
        checkout_url += "?sandbox=1"
    elif demo_order:
        checkout_url += "?demo=1"
    with db_connection() as connection:
        connection.execute(
            "UPDATE orders SET checkout_url = ? WHERE checkout_id = ?",
            (checkout_url, checkout_id),
        )
    payment_status = "PENDING"
    payment_url = None
    total_amount = subtotal - total_coupon_discount
    if sandbox_mode:
        payment_status, payment_url = await begin_pesapal_payment(
            provider_reference,
            total_amount,
            f"LipaRahisi cart {checkout_id}",
            f"{app_url}/api/callbacks/pesapal",
            checkout_url,
            payload.buyer_name.strip(),
            payload.buyer_email,
            payload.buyer_phone,
        )
        if payment_url:
            with db_connection() as connection:
                payment_id = connection.execute(
                    "SELECT provider_payment_id FROM orders WHERE provider_reference = ?",
                    (provider_reference,),
                ).fetchone()["provider_payment_id"]
                connection.execute(
                    """
                    UPDATE orders
                    SET checkout_url = ?, provider_payment_id = ?
                    WHERE checkout_id = ? AND payment_status = 'PENDING'
                    """,
                    (payment_url, payment_id, checkout_id),
                )
    elif abliner_mode:
        payment_status = await begin_abliner_payment(
            provider_reference,
            payload.buyer_phone,
            total_amount,
            f"{app_url}/api/webhooks/abliner",
        )
    elif not demo_mode:
        payment_status = await begin_clickpesa_payment(
            provider_reference,
            payload.buyer_phone,
            total_amount,
        )
    return {
        "checkout_id": checkout_id,
        "checkout_url": checkout_url,
        "payment_url": payment_url,
        "order_ids": order_ids,
        "payment_status": payment_status,
        "demo": demo_order,
        "sandbox": sandbox_mode,
        "total_amount": total_amount,
        "coupon_code": coupon_code,
        "coupon_discount": total_coupon_discount,
    }


def get_cart_checkout(checkout_id: str, request: Request):
    expire_pending_orders()
    with db_connection() as connection:
        orders = connection.execute(
            """
            SELECT o.*, u.shop_name
            FROM orders o LEFT JOIN users u ON u.id = o.seller_id
            WHERE o.checkout_id = ?
            ORDER BY o.created_at
            """,
            (checkout_id,),
        ).fetchall()
    if not orders:
        raise HTTPException(status_code=404, detail="Kikapu cha malipo hakikupatikana.")
    splits = []
    for order in orders:
        row = dict(order)
        with db_connection() as connection:
            items = connection.execute(
                """
                SELECT oi.product_id, oi.quantity, oi.unit_amount, oi.variant_label,
                       p.name AS product_name
                FROM order_items oi JOIN products p ON p.id = oi.product_id
                WHERE oi.order_id = ?
                """,
                (row["order_id"],),
            ).fetchall()
        splits.append({
            "order_id": row["order_id"],
            "seller_id": row["seller_id"],
            "shop_name": row["shop_name"] or "Muuzaji",
            "amount": row["amount"],
            "payment_status": row["payment_status"],
            "platform_fee": row["platform_fee"],
            "seller_payout": row["seller_payout"],
            "items": [dict(item) for item in items],
        })
    result = {
        "checkout_id": checkout_id,
        "payment_status": "COMPLETED" if all(split["payment_status"] == "COMPLETED" for split in splits)
        else "FAILED" if any(split["payment_status"] == "FAILED" for split in splits)
        else "EXPIRED" if all(split["payment_status"] == "EXPIRED" for split in splits)
        else "PENDING",
        "payment_method": orders[0]["payment_method"],
        "expires_at": orders[0]["expires_at"],
        "demo": not any(order["provider_reference"] for order in orders),
        "sandbox": orders[0]["payment_method"] == "PesaPal Sandbox",
        "payment_url": stored_pesapal_payment_url(orders[0])
        if all(order["payment_status"] == "PENDING" for order in orders)
        else None,
        "commission_percent": COMMISSION_PERCENT,
        "total_amount": sum(split["amount"] for split in splits),
        "coupon_code": orders[0]["coupon_code"] or "",
        "coupon_discount": sum(order["coupon_discount"] for order in orders),
        "platform_fee": sum(split["platform_fee"] for split in splits),
        "completed_at": next((row["completed_at"] for row in orders if row["completed_at"]), None),
        "splits": splits,
    }
    buyer = optional_current_user(request)
    if (
        buyer
        and buyer["role"] == "BUYER"
        and all(order["buyer_id"] == buyer["id"] for order in orders)
    ):
        result.update({
            "buyer_name": orders[0]["buyer_name"],
            "buyer_email": orders[0]["buyer_email"],
            "buyer_phone": orders[0]["buyer_phone"],
            "bank_branch": orders[0]["bank_branch"],
        })
    return result


def read_cart_checkout(checkout_id: str, request: Request):
    return get_cart_checkout(checkout_id, request)


def complete_cart_checkout(checkout_id: str, request: Request):
    if not is_demo_mode():
        raise HTTPException(status_code=404, detail="Endpoint ya majaribio imezimwa.")
    expire_pending_orders()
    with db_connection() as connection:
        orders = connection.execute(
            "SELECT order_id, payment_status FROM orders WHERE checkout_id = ?",
            (checkout_id,),
        ).fetchall()
    if not orders:
        raise HTTPException(status_code=404, detail="Kikapu cha malipo hakikupatikana.")
    if any(order["payment_status"] == "EXPIRED" for order in orders):
        raise HTTPException(status_code=410, detail="Kikapu hiki kimekwisha muda; anzisha oda mpya.")
    complete_order_group([order["order_id"] for order in orders])
    return get_cart_checkout(checkout_id, request)


def order_status(order_id: str, request: Request):
    order = get_order(order_id)
    with db_connection() as connection:
        product = connection.execute(
            """
            SELECT p.name, oi.variant_label,
                   rr.status AS return_status, rr.reason AS return_reason,
                   cr.status AS cancellation_status
            FROM products p
            LEFT JOIN order_items oi
              ON oi.product_id = p.id AND oi.order_id = ?
            LEFT JOIN return_requests rr ON rr.order_id = ?
            LEFT JOIN cancellation_requests cr ON cr.order_id = ?
            WHERE p.id = ?
            """,
            (order_id, order_id, order_id, order["product_id"]),
        ).fetchone()
    result = {
        "order_id": order_id,
        "payment_status": order["payment_status"],
        "amount": order["amount"],
        "product_name": product["name"] if product else "Bidhaa",
        "variant_label": product["variant_label"] if product else "",
        "payment_method": order["payment_method"],
        "completed_at": order["completed_at"],
        "platform_fee": order["platform_fee"],
        "seller_payout": order["seller_payout"],
        "commission_percent": COMMISSION_PERCENT,
        "demo": not bool(order["provider_reference"]),
        "sandbox": order["payment_method"] == "PesaPal Sandbox",
        "payment_url": stored_pesapal_payment_url(order),
        "coupon_code": order["coupon_code"] or "",
        "coupon_discount": order["coupon_discount"],
    }
    buyer = optional_current_user(request)
    if buyer and buyer["role"] == "BUYER" and buyer["id"] == order["buyer_id"]:
        result.update({
            "buyer_name": order["buyer_name"],
            "bank_branch": order["bank_branch"],
            "delivery_address": order["delivery_address"],
            "delivery_status": order["delivery_status"],
            "delivery_carrier": order["delivery_carrier"],
            "tracking_code": order["tracking_code"],
            "delivery_fee": order["delivery_fee"],
            "return_status": product["return_status"] if product else None,
            "return_reason": product["return_reason"] if product else None,
            "cancellation_status": product["cancellation_status"] if product else None,
        })
    return result


def order_history(request: Request):
    buyer = require_buyer(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT o.order_id, o.product_id, o.amount, o.payment_status,
                   o.delivery_status, o.delivery_carrier, o.tracking_code,
                   o.delivery_fee, o.delivery_address,
                   o.created_at, o.completed_at, p.name AS product_name,
                   p.image_url, u.shop_name, oi.variant_label,
                   rr.status AS return_status, rr.reason AS return_reason,
                   cr.status AS cancellation_status
            FROM orders o
            JOIN products p ON p.id = o.product_id
            LEFT JOIN users u ON u.id = o.seller_id
            LEFT JOIN order_items oi
              ON oi.order_id = o.order_id AND oi.product_id = o.product_id
            LEFT JOIN return_requests rr ON rr.order_id = o.order_id
            LEFT JOIN cancellation_requests cr ON cr.order_id = o.order_id
            WHERE o.buyer_id = ? AND o.payment_method != 'PesaPal Sandbox'
            ORDER BY o.created_at DESC
            LIMIT 200
            """,
            (buyer["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


def update_delivery(order_id: str, payload: DeliveryUpdateInput, request: Request):
    actor = get_current_user(request)
    with db_connection() as connection:
        order = connection.execute(
            """
            SELECT seller_id, payment_status, delivery_status, payment_method,
                   tracking_code, delivery_agent_id, buyer_id, buyer_phone
            FROM orders WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        agent = None
        if actor["role"] == "DELIVERY_AGENT":
            agent = connection.execute(
                "SELECT id FROM delivery_agents WHERE user_id = ?",
                (actor["id"],),
            ).fetchone()
        if (
            actor["role"] != "ADMIN"
            and actor["role"] != "SELLER"
            and not (actor["role"] == "DELIVERY_AGENT" and agent and agent["id"] == order["delivery_agent_id"])
        ):
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        if actor["role"] == "SELLER" and order["seller_id"] != actor["id"]:
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        if order["payment_status"] != "COMPLETED":
            raise HTTPException(status_code=409, detail="Delivery huanza oda ikikamilika.")
        if order["payment_method"] == "PesaPal Sandbox":
            raise HTTPException(status_code=409, detail="Oda ya sandbox haiwezi kuanzisha delivery.")
        transitions = {
                "UNFULFILLED": {"PROCESSING", "ORDER_CONFIRMED"},
                "PROCESSING": {"ORDER_CONFIRMED", "SELLER_PREPARING", "READY_FOR_PICKUP", "DELIVERY_FAILED", "RETURNED"},
                "ORDER_CONFIRMED": {"SELLER_PREPARING", "READY_FOR_PICKUP", "DELIVERY_FAILED", "RETURNED"},
                "SELLER_PREPARING": {"READY_FOR_PICKUP", "DELIVERY_FAILED", "RETURNED"},
                "READY_FOR_PICKUP": {"AGENT_ASSIGNED", "PICKED_UP", "DELIVERY_FAILED", "RETURNED"},
                "AGENT_ASSIGNED": {"PICKED_UP", "DELIVERY_FAILED", "RETURNED"},
                "PICKED_UP": {"IN_TRANSIT", "NEAR_DESTINATION", "OUT_FOR_DELIVERY", "DELIVERY_FAILED", "RETURNED"},
                "IN_TRANSIT": {"NEAR_DESTINATION", "OUT_FOR_DELIVERY", "DELIVERY_FAILED", "RETURNED"},
                "NEAR_DESTINATION": {"OUT_FOR_DELIVERY", "DELIVERY_FAILED", "RETURNED"},
                "OUT_FOR_DELIVERY": {"DELIVERED", "DELIVERY_FAILED", "RETURNED"},
                "SHIPPED": {"IN_TRANSIT", "DELIVERED", "DELIVERY_FAILED", "RETURNED"},
                "DELIVERED": set(),
                "DELIVERY_FAILED": {"OUT_FOR_DELIVERY", "RETURNED"},
                "RETURNED": set(),
        }
        next_status = "IN_TRANSIT" if payload.delivery_status == "SHIPPED" else payload.delivery_status
        if next_status == "DELIVERED":
                if actor["role"] not in {"ADMIN", "SELLER"}:
                    raise HTTPException(status_code=403, detail="Uthibitisho wa delivery hautaruhusiwa kwa mtumiaji huyu.")
        if (
                next_status != order["delivery_status"]
                and next_status not in transitions.get(order["delivery_status"], set())
        ):
            raise HTTPException(status_code=409, detail="Hatua ya delivery haiwezi kurukwa au kurudishwa nyuma.")
        tracking_code = payload.tracking_code.strip() or order["tracking_code"]
        if not tracking_code:
            tracking_code = generate_tracking_code(connection)
        if payload.delivery_status == "SHIPPED":
            pending_cancellation = connection.execute(
                """
                SELECT 1 FROM cancellation_requests
                WHERE order_id = ? AND status = 'PENDING'
                """,
                (order_id,),
            ).fetchone()
            if pending_cancellation:
                raise HTTPException(
                    status_code=409,
                    detail="Tatua ombi la kughairi kabla ya kusafirisha oda.",
                )
        connection.execute(
            """
            UPDATE orders
            SET delivery_status = ?, delivery_carrier = ?, tracking_code = ?,
                delivery_fee = ?,
                delivered_at = CASE WHEN ? = 'DELIVERED' THEN CURRENT_TIMESTAMP ELSE delivered_at END
            WHERE order_id = ?
            """,
            (
                next_status,
                payload.delivery_carrier.strip(),
                tracking_code,
                payload.delivery_fee,
                next_status,
                order_id,
            ),
        )
        connection.execute(
                """
                INSERT INTO delivery_tracking_events
                    (id, order_id, status, description, actor_id, latitude, longitude)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secrets.token_hex(16),
                    order_id,
                    next_status,
                    payload.description.strip() or f"Hali imebadilishwa kwenda {next_status}",
                    actor["id"],
                    payload.latitude,
                    payload.longitude,
                ),
        )
        if next_status in {"DELIVERED", "DELIVERY_FAILED", "RETURNED"}:
            connection.execute(
                "UPDATE delivery_locations SET is_live = 0 WHERE order_id = ?",
                (order_id,),
            )
    record_audit(
        "DELIVERY_STATUS_UPDATED",
        "order",
        order_id,
        next_status,
        f"status={next_status}; carrier={payload.delivery_carrier.strip()}; fee={payload.delivery_fee}",
        actor=actor,
        request=request,
    )
    if order["buyer_id"]:
        with db_connection() as connection:
            buyer = connection.execute(
                "SELECT email, phone FROM users WHERE id = ?",
                (order["buyer_id"],),
            ).fetchone()
        if buyer:
            queue_user_notification(
                order["buyer_id"],
                "DELIVERY_UPDATED",
                {"tracking_code": tracking_code, "status": next_status},
                link=f"/track/{tracking_code}",
                email=buyer["email"],
                phone=buyer["phone"] or order["buyer_phone"],
            )
    return {
        "order_id": order_id,
        "delivery_status": next_status,
        "tracking_code": tracking_code,
        "delivery_carrier": payload.delivery_carrier.strip(),
        "delivery_fee": payload.delivery_fee,
    }


@app.get("/api/tracking/{tracking_number}")
def public_tracking(tracking_number: str):
    with db_connection() as connection:
        order = connection.execute(
            """
            SELECT o.order_id, o.tracking_code, o.delivery_status, o.delivery_address,
                   o.delivery_mode, o.carrier_name, o.carrier_tracking_number,
                   o.carrier_status, o.completed_at, o.delivered_at,
                   p.name AS product_name, p.id AS product_id,
                   u.shop_name
            FROM orders o
            JOIN products p ON p.id = o.product_id
            LEFT JOIN users u ON u.id = o.seller_id
            WHERE o.tracking_code = ?
            """,
            (tracking_number.strip(),),
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Tracking number haikupatikana.")
        events = connection.execute(
            """
            SELECT status, description, latitude, longitude, evidence_url, created_at
            FROM delivery_tracking_events
            WHERE order_id = ?
            ORDER BY created_at ASC
            """,
            (order["order_id"],),
        ).fetchall()
        latest_location = None
        if order["delivery_status"] not in {"DELIVERED", "DELIVERY_FAILED", "RETURNED"}:
            location_row = connection.execute(
                """
                SELECT latitude, longitude, accuracy_m, created_at
                FROM delivery_locations
                WHERE order_id = ? AND is_live = 1 AND party_role = 'AGENT'
                ORDER BY created_at DESC LIMIT 1
                """,
                (order["order_id"],),
            ).fetchone()
            if location_row:
                latest_location = dict(location_row)
        proof = connection.execute(
            """
            SELECT recipient_name, received_at, image_url, latitude, longitude
            FROM proof_of_delivery
            WHERE order_id = ? AND received_at IS NOT NULL
            """,
            (order["order_id"],),
        ).fetchone()
    return {
        "tracking_number": order["tracking_code"],
        "order_status": order["delivery_status"],
        "product": {"name": order["product_name"]},
        "seller": {"shop_name": order["shop_name"] or "Muuzaji"},
        "delivery_mode": order["delivery_mode"],
        "carrier": {
            "name": order["carrier_name"] or None,
            "tracking_number": order["carrier_tracking_number"] or None,
            "status": order["carrier_status"] or None,
        },
        "timeline": [dict(row) for row in events],
        "live_location": latest_location,
        "proof_of_delivery": dict(proof) if proof else None,
        "eta": None,
        "completed_at": order["delivered_at"] or order["completed_at"],
    }


@app.post("/api/orders/{order_id}/location")
def update_delivery_location(order_id: str, payload: DeliveryLocationInput, request: Request):
    actor = get_current_user(request)
    with db_connection() as connection:
        order = connection.execute(
            "SELECT seller_id, buyer_id, delivery_status, delivery_agent_id FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        if order["delivery_status"] in {"DELIVERED", "DELIVERY_FAILED", "RETURNED"}:
            raise HTTPException(status_code=409, detail="GPS sharing imezimwa kwa delivery hii.")

        if actor["role"] == "SELLER":
            if order["seller_id"] != actor["id"]:
                raise HTTPException(status_code=403, detail="Huna ruhusa ya kushiriki pickup location ya oda hii.")
            party_role = "SELLER"
            is_live = 0
        elif actor["role"] == "BUYER":
            if order["buyer_id"] != actor["id"]:
                raise HTTPException(status_code=403, detail="Huna ruhusa ya kushiriki location ya oda hii.")
            if not payload.sharing_consent:
                raise HTTPException(status_code=422, detail="Ridhaa ya kushiriki destination location inahitajika.")
            party_role = "BUYER"
            is_live = 0
        elif actor["role"] == "DELIVERY_AGENT":
            agent = connection.execute(
                "SELECT id FROM delivery_agents WHERE user_id = ?",
                (actor["id"],),
            ).fetchone()
            if agent is None or agent["id"] != order["delivery_agent_id"]:
                raise HTTPException(status_code=403, detail="Huna ruhusa ya kufuatilia oda hii.")
            if not payload.sharing_consent:
                raise HTTPException(status_code=422, detail="Ridhaa ya kushiriki live GPS inahitajika.")
            party_role = "AGENT"
            is_live = 1
        elif actor["role"] == "ADMIN":
            party_role = "ADMIN"
            is_live = 0
        else:
            raise HTTPException(status_code=403, detail="Nafasi hii hairuhusiwi kushiriki location.")
        connection.execute(
            """
            INSERT INTO delivery_locations
                (id, order_id, agent_id, party_role, party_user_id,
                 latitude, longitude, accuracy_m, is_live)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                secrets.token_hex(16),
                order_id,
                actor["id"],
                party_role,
                actor["id"],
                payload.latitude,
                payload.longitude,
                payload.accuracy_m,
                is_live,
            ),
        )
    record_audit("DELIVERY_LOCATION_UPDATED", "order", order_id, party_role, actor=actor, request=request)
    return {"order_id": order_id, "party_role": party_role, "live": bool(is_live), "updated": True}


@app.post("/api/orders/{order_id}/proof/request")
def request_delivery_otp(order_id: str, request: Request):
    actor = get_current_user(request)
    with db_connection() as connection:
        order = connection.execute(
            "SELECT seller_id, buyer_phone, delivery_status FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if order is None or (actor["role"] != "ADMIN" and order["seller_id"] != actor["id"]):
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        otp = f"{secrets.randbelow(900000) + 100000}"
        otp_hash = hashlib.sha256(otp.encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO proof_of_delivery (order_id, otp_hash, otp_expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT (order_id) DO UPDATE SET
                otp_hash = excluded.otp_hash,
                otp_expires_at = excluded.otp_expires_at,
                otp_attempts = 0
            """,
            (order_id, otp_hash, time.time() + 10 * 60),
        )
    record_audit("DELIVERY_OTP_REQUESTED", "order", order_id, "", actor=actor, request=request)
    logger.warning("Delivery OTP generated for order %s; external SMS delivery is not connected.", order_id)
    return {"expires_in_seconds": 600, "sms_sent": False}


@app.post("/api/orders/{order_id}/proof/verify")
def verify_delivery_proof(order_id: str, payload: ProofOfDeliveryInput, request: Request):
    actor = get_current_user(request)
    otp_hash = hashlib.sha256(payload.otp.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        order = connection.execute(
            "SELECT seller_id, payment_status FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if order is None or (actor["role"] != "ADMIN" and order["seller_id"] != actor["id"]):
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        proof = connection.execute(
            "SELECT otp_hash, otp_expires_at, otp_attempts FROM proof_of_delivery WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if proof is None:
            raise HTTPException(status_code=409, detail="Omba OTP ya delivery kwanza.")
        if proof["otp_attempts"] >= 5:
            raise HTTPException(status_code=429, detail="Majaribio ya OTP yamezidi kikomo.")
        if proof["otp_expires_at"] < time.time():
            raise HTTPException(status_code=410, detail="OTP imekwisha muda.")
        if not hmac.compare_digest(proof["otp_hash"], otp_hash):
            connection.execute(
                "UPDATE proof_of_delivery SET otp_attempts = otp_attempts + 1 WHERE order_id = ?",
                (order_id,),
            )
            raise HTTPException(status_code=422, detail="OTP si sahihi.")
        connection.execute(
            """
            UPDATE proof_of_delivery
            SET recipient_name = ?, received_at = CURRENT_TIMESTAMP,
                image_url = ?, latitude = ?, longitude = ?, otp_attempts = 0,
                otp_hash = ''
            WHERE order_id = ?
            """,
            (
                payload.recipient_name.strip(),
                payload.image_url.strip(),
                payload.latitude,
                payload.longitude,
                order_id,
            ),
        )
        connection.execute(
            "UPDATE orders SET delivery_status = 'DELIVERED', delivered_at = CURRENT_TIMESTAMP WHERE order_id = ?",
            (order_id,),
        )
        connection.execute(
            """
            INSERT INTO delivery_tracking_events
                (id, order_id, status, description, actor_id, latitude, longitude)
            VALUES (?, ?, 'DELIVERED', 'Proof of delivery verified', ?, ?, ?)
            """,
            (
                secrets.token_hex(16),
                order_id,
                actor["id"],
                payload.latitude,
                payload.longitude,
            ),
        )
        connection.execute(
            "UPDATE delivery_locations SET is_live = 0 WHERE order_id = ?",
            (order_id,),
        )
    record_audit("DELIVERY_PROOF_VERIFIED", "order", order_id, "", actor=actor, request=request)
    return {"order_id": order_id, "delivery_status": "DELIVERED"}


@app.post("/api/orders/{order_id}/confirm-delivery")
def confirm_delivery_by_buyer(
    order_id: str,
    payload: BuyerDeliveryConfirmationInput,
    request: Request,
):
    actor = get_current_user(request)
    otp_hash = hashlib.sha256(payload.otp.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        order = connection.execute(
            "SELECT buyer_id, delivery_status FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        if actor["role"] != "BUYER" or order["buyer_id"] != actor["id"]:
            raise HTTPException(status_code=403, detail="Mnunuzi pekee wa oda hii anaweza kuidhinisha delivery.")
        if order["delivery_status"] == "DELIVERED":
            raise HTTPException(status_code=409, detail="Delivery tayari imeidhinishwa.")
        proof = connection.execute(
            "SELECT otp_hash, otp_expires_at, otp_attempts FROM proof_of_delivery WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if proof is None:
            raise HTTPException(status_code=409, detail="OTP ya delivery haijatolewa.")
        if proof["otp_attempts"] >= 5:
            raise HTTPException(status_code=429, detail="Majaribio ya OTP yamezidi kikomo.")
        if proof["otp_expires_at"] < time.time():
            raise HTTPException(status_code=410, detail="OTP imekwisha muda.")
        if not hmac.compare_digest(proof["otp_hash"], otp_hash):
            connection.execute(
                "UPDATE proof_of_delivery SET otp_attempts = otp_attempts + 1 WHERE order_id = ?",
                (order_id,),
            )
            raise HTTPException(status_code=422, detail="OTP si sahihi.")
        connection.execute(
            """
            UPDATE proof_of_delivery
            SET recipient_name = ?, received_at = CURRENT_TIMESTAMP,
                otp_attempts = 0, otp_hash = ''
            WHERE order_id = ?
            """,
            (payload.recipient_name.strip(), order_id),
        )
        connection.execute(
            "UPDATE orders SET delivery_status = 'DELIVERED', delivered_at = CURRENT_TIMESTAMP WHERE order_id = ?",
            (order_id,),
        )
        connection.execute(
            """
            INSERT INTO delivery_tracking_events
                (id, order_id, status, description, actor_id)
            VALUES (?, ?, 'DELIVERED', 'Buyer confirmed receipt', ?)
            """,
            (secrets.token_hex(16), order_id, actor["id"]),
        )
        connection.execute(
            "UPDATE delivery_locations SET is_live = 0 WHERE order_id = ?",
            (order_id,),
        )
    record_audit("DELIVERY_CONFIRMED_BY_BUYER", "order", order_id, "", actor=actor, request=request)
    return {"order_id": order_id, "delivery_status": "DELIVERED", "confirmed_by": "BUYER"}


@app.patch("/api/orders/{order_id}/carrier")
def update_external_carrier(order_id: str, payload: ExternalCarrierUpdateInput, request: Request):
    actor = get_current_user(request)
    with db_connection() as connection:
        order = connection.execute(
            "SELECT seller_id FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if order is None or (actor["role"] != "ADMIN" and order["seller_id"] != actor["id"]):
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        connection.execute(
            """
            UPDATE orders
            SET delivery_mode = 'EXTERNAL',
                carrier_name = ?, carrier_tracking_number = ?, carrier_status = ?
            WHERE order_id = ?
            """,
            (
                payload.carrier_name.strip(),
                payload.carrier_tracking_number.strip(),
                payload.carrier_status.strip(),
                order_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO delivery_tracking_events
                (id, order_id, status, description, actor_id)
            VALUES (?, ?, 'PROCESSING', ?, ?)
            """,
            (
                secrets.token_hex(16),
                order_id,
                f"External carrier: {payload.carrier_name.strip()}",
                actor["id"],
            ),
        )
    record_audit("EXTERNAL_CARRIER_ASSIGNED", "order", order_id, payload.carrier_name, actor=actor, request=request)
    return {
        "delivery_mode": "EXTERNAL",
        "carrier_name": payload.carrier_name.strip(),
        "carrier_tracking_number": payload.carrier_tracking_number.strip(),
        "carrier_status": payload.carrier_status.strip(),
    }


@app.post("/api/admin/delivery-agents")
def create_delivery_agent(payload: DeliveryAgentCreateInput, request: Request):
    actor = require_admin(request)
    full_name, email = clean_identity(payload.full_name, payload.email)
    user_id = secrets.token_hex(16)
    agent_id = secrets.token_hex(16)
    try:
        with db_connection() as connection:
            connection.execute(
                """
                INSERT INTO users
                    (id, full_name, email, password_hash, role, status, shop_name, phone)
                VALUES (?, ?, ?, ?, 'DELIVERY_AGENT', 'ACTIVE', '', ?)
                """,
                (
                    user_id,
                    full_name,
                    email,
                    hash_password(payload.password),
                    payload.phone,
                ),
            )
            connection.execute(
                """
                INSERT INTO delivery_agents
                    (id, user_id, phone, vehicle_type, vehicle_plate, status)
                VALUES (?, ?, ?, ?, ?, 'ACTIVE')
                """,
                (
                    agent_id,
                    user_id,
                    payload.phone,
                    payload.vehicle_type.strip(),
                    payload.vehicle_plate.strip(),
                ),
            )
    except Exception as error:
        if is_integrity_error(error):
            raise HTTPException(status_code=409, detail="Barua pepe ya agent tayari imesajiliwa.") from error
        raise
    record_audit("DELIVERY_AGENT_CREATED", "agent", agent_id, full_name, actor=actor, request=request)
    return {
        "id": agent_id,
        "user_id": user_id,
        "full_name": full_name,
        "email": email,
        "phone": payload.phone,
        "vehicle_type": payload.vehicle_type.strip(),
        "vehicle_plate": payload.vehicle_plate.strip(),
        "status": "ACTIVE",
    }


@app.get("/api/admin/delivery-agents")
def list_delivery_agents(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT da.id, da.user_id, da.phone, da.vehicle_type,
                   da.vehicle_plate, da.status, da.created_at,
                   u.full_name, u.email
            FROM delivery_agents da
            JOIN users u ON u.id = da.user_id
            ORDER BY da.created_at DESC
            LIMIT 500
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/orders/{order_id}/assign-agent")
def assign_delivery_agent(
    order_id: str,
    payload: DeliveryAgentAssignmentInput,
    request: Request,
):
    actor = get_current_user(request)
    with db_connection() as connection:
        order = connection.execute(
            "SELECT seller_id FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if order is None or (actor["role"] != "ADMIN" and order["seller_id"] != actor["id"]):
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        agent = connection.execute(
            "SELECT id, user_id, phone, vehicle_type, vehicle_plate, status FROM delivery_agents WHERE id = ? OR user_id = ?",
            (payload.agent_user_id, payload.agent_user_id),
        ).fetchone()
        if agent is None:
            raise HTTPException(status_code=404, detail="Delivery agent hakupatikana.")
        if agent["status"] != "ACTIVE":
            raise HTTPException(status_code=409, detail="Delivery agent haipo active.")
        connection.execute(
            "UPDATE orders SET delivery_agent_id = ?, delivery_status = 'AGENT_ASSIGNED' WHERE order_id = ?",
            (agent["id"], order_id),
        )
        connection.execute(
            """
            INSERT INTO delivery_tracking_events
                (id, order_id, status, description, actor_id)
            VALUES (?, ?, 'AGENT_ASSIGNED', 'Delivery agent assigned', ?)
            """,
            (secrets.token_hex(16), order_id, actor["id"]),
        )
    record_audit("DELIVERY_AGENT_ASSIGNED", "order", order_id, agent["id"], actor=actor, request=request)
    return {
        "order_id": order_id,
        "delivery_status": "AGENT_ASSIGNED",
        "agent": {
            "id": agent["id"],
            "phone": agent["phone"],
            "vehicle_type": agent["vehicle_type"],
            "vehicle_plate": agent["vehicle_plate"],
        },
    }


@app.get("/api/deliveries")
def list_deliveries(request: Request):
    actor = get_current_user(request)
    with db_connection() as connection:
        if actor["role"] == "ADMIN":
            rows = connection.execute(
                """
                SELECT o.order_id, o.tracking_code, o.delivery_status, o.delivery_address,
                       o.delivery_agent_id, o.completed_at, o.created_at,
                       p.name AS product_name, u.shop_name
                FROM orders o
                JOIN products p ON p.id = o.product_id
                LEFT JOIN users u ON u.id = o.seller_id
                WHERE o.payment_status = 'COMPLETED'
                ORDER BY o.created_at DESC LIMIT 500
                """
            ).fetchall()
        elif actor["role"] == "SELLER":
            rows = connection.execute(
                """
                SELECT o.order_id, o.tracking_code, o.delivery_status, o.delivery_address,
                       o.delivery_agent_id, o.completed_at, o.created_at,
                       p.name AS product_name, u.shop_name
                FROM orders o
                JOIN products p ON p.id = o.product_id
                LEFT JOIN users u ON u.id = o.seller_id
                WHERE o.payment_status = 'COMPLETED' AND o.seller_id = ?
                ORDER BY o.created_at DESC LIMIT 500
                """,
                (actor["id"],),
            ).fetchall()
        else:
            agent = connection.execute(
                "SELECT id FROM delivery_agents WHERE user_id = ?",
                (actor["id"],),
            ).fetchone()
            if agent is None:
                raise HTTPException(status_code=403, detail="Akaunti hii si delivery agent.")
            rows = connection.execute(
                """
                SELECT o.order_id, o.tracking_code, o.delivery_status, o.delivery_address,
                       o.delivery_agent_id, o.completed_at, o.created_at,
                       p.name AS product_name, u.shop_name,
                       (SELECT dl.latitude FROM delivery_locations dl
                        WHERE dl.order_id = o.order_id AND dl.party_user_id = ?
                          AND dl.party_role = 'AGENT' AND dl.is_live = 1
                        ORDER BY dl.created_at DESC LIMIT 1) AS agent_latitude,
                       (SELECT dl.longitude FROM delivery_locations dl
                        WHERE dl.order_id = o.order_id AND dl.party_user_id = ?
                          AND dl.party_role = 'AGENT' AND dl.is_live = 1
                        ORDER BY dl.created_at DESC LIMIT 1) AS agent_longitude,
                       (SELECT dl.created_at FROM delivery_locations dl
                        WHERE dl.order_id = o.order_id AND dl.party_user_id = ?
                          AND dl.party_role = 'AGENT' AND dl.is_live = 1
                        ORDER BY dl.created_at DESC LIMIT 1) AS location_updated_at
                FROM orders o
                JOIN products p ON p.id = o.product_id
                LEFT JOIN users u ON u.id = o.seller_id
                WHERE o.payment_status = 'COMPLETED' AND o.delivery_agent_id = ?
                ORDER BY o.created_at DESC LIMIT 500
                """,
                (actor["id"], actor["id"], actor["id"], agent["id"]),
            ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/deliveries/available")
def list_available_deliveries(request: Request):
    actor = get_current_user(request)
    if actor["role"] != "DELIVERY_AGENT":
        raise HTTPException(status_code=403, detail="Orodha hii ni ya delivery agent.")
    with db_connection() as connection:
        agent = connection.execute(
            "SELECT id, status FROM delivery_agents WHERE user_id = ?",
            (actor["id"],),
        ).fetchone()
        if agent is None or agent["status"] != "ACTIVE":
            raise HTTPException(status_code=403, detail="Akaunti hii si delivery agent hai.")
        rows = connection.execute(
            """
            SELECT o.order_id, o.tracking_code, o.delivery_address, o.created_at,
                   p.name AS product_name, u.shop_name
            FROM orders o
            JOIN products p ON p.id = o.product_id
            LEFT JOIN users u ON u.id = o.seller_id
            WHERE o.payment_status = 'COMPLETED'
              AND o.delivery_status = 'READY_FOR_PICKUP'
              AND o.delivery_agent_id IS NULL
            ORDER BY o.created_at ASC
            LIMIT 100
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/deliveries/{order_id}/accept")
def accept_delivery(order_id: str, request: Request):
    actor = get_current_user(request)
    if actor["role"] != "DELIVERY_AGENT":
        raise HTTPException(status_code=403, detail="Oda hii inaweza kukubaliwa na delivery agent.")
    with db_connection() as connection:
        agent = connection.execute(
            "SELECT id, status FROM delivery_agents WHERE user_id = ?",
            (actor["id"],),
        ).fetchone()
        if agent is None or agent["status"] != "ACTIVE":
            raise HTTPException(status_code=403, detail="Akaunti hii si delivery agent hai.")
        result = connection.execute(
            """
            UPDATE orders
            SET delivery_agent_id = ?, delivery_status = 'AGENT_ASSIGNED'
            WHERE order_id = ?
              AND payment_status = 'COMPLETED'
              AND delivery_status = 'READY_FOR_PICKUP'
              AND delivery_agent_id IS NULL
            """,
            (agent["id"], order_id),
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="Oda hii haipatikani tena kwa ajili ya kugawiwa.")
        connection.execute(
            """
            INSERT INTO delivery_tracking_events
                (id, order_id, status, description, actor_id)
            VALUES (?, ?, 'AGENT_ASSIGNED', 'Delivery agent accepted the assignment', ?)
            """,
            (secrets.token_hex(16), order_id, actor["id"]),
        )
    record_audit(
        "DELIVERY_ACCEPTED",
        "order",
        order_id,
        "Delivery agent accepted an available delivery",
        actor=actor,
        request=request,
    )
    return {"order_id": order_id, "delivery_status": "AGENT_ASSIGNED"}


def request_cancellation(order_id: str, payload: CancellationInput, request: Request):
    buyer = require_buyer(request)
    with db_connection() as connection:
        connection.execute(
            "UPDATE orders SET order_id = order_id WHERE order_id = ? AND buyer_id = ?",
            (order_id, buyer["id"]),
        )
        order = connection.execute(
            """
            SELECT buyer_id, seller_id, payment_status, delivery_status,
                   cancelled_at, payment_method
            FROM orders WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()
        if order is None or order["buyer_id"] != buyer["id"]:
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        if order["payment_method"] == "PesaPal Sandbox":
            raise HTTPException(status_code=409, detail="Oda ya sandbox haiwezi kuombewa kughairiwa.")
        if (
            order["cancelled_at"]
            or order["payment_status"] not in {"PENDING", "COMPLETED"}
            or order["delivery_status"] not in {
                "UNFULFILLED", "PROCESSING", "ORDER_CONFIRMED",
                "SELLER_PREPARING", "READY_FOR_PICKUP", "AGENT_ASSIGNED",
            }
        ):
            raise HTTPException(status_code=409, detail="Oda hii haiwezi kuombewa kughairiwa.")
        return_request = connection.execute(
            "SELECT status FROM return_requests WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if return_request and return_request["status"] in {"PENDING", "APPROVED"}:
            raise HTTPException(
                status_code=409,
                detail="Oda hii ina ombi la kurejesha linalosubiri au limeidhinishwa.",
            )
        existing = connection.execute(
            "SELECT status FROM cancellation_requests WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Ombi la kughairi oda hii limeshatumwa.")
        request_id = secrets.token_hex(16)
        connection.execute(
            """
            INSERT INTO cancellation_requests (id, order_id, requester_id, reason)
            VALUES (?, ?, ?, ?)
            """,
            (request_id, order_id, buyer["id"], payload.reason.strip()),
        )
    record_audit(
        "CANCELLATION_REQUESTED",
        "order",
        order_id,
        "Buyer requested cancellation",
        actor=buyer,
        request=request,
    )
    if order["seller_id"]:
        with db_connection() as connection:
            seller = connection.execute(
                "SELECT email, phone FROM users WHERE id = ?",
                (order["seller_id"],),
            ).fetchone()
        if seller:
            queue_user_notification(
                order["seller_id"],
                "RETURN_REQUESTED",
                {"order_id": order_id},
                email=seller["email"],
                phone=seller["phone"],
            )
    return {"id": request_id, "status": "PENDING"}


def resolve_cancellation(
    cancellation_id: str,
    payload: CancellationResolutionInput,
    request: Request,
):
    actor = require_admin(request)
    with db_connection() as connection:
        cancellation = connection.execute(
            """
            SELECT c.order_id, c.status, o.payment_status, o.product_id,
                   o.seller_id, o.checkout_id, o.delivery_status
            FROM cancellation_requests c
            JOIN orders o ON o.order_id = c.order_id
            WHERE c.id = ?
            """,
            (cancellation_id,),
        ).fetchone()
        if cancellation is None:
            raise HTTPException(status_code=404, detail="Ombi halikupatikana.")
        if cancellation["status"] != "PENDING":
            raise HTTPException(status_code=409, detail="Ombi hili limeshughulikiwa tayari.")
        if payload.status == "APPROVED":
            if cancellation["delivery_status"] in {"SHIPPED", "DELIVERED"}:
                raise HTTPException(
                    status_code=409,
                    detail="Oda imeshasafirishwa; shughulikia ombi la marejesho badala ya kughairi.",
                )
            if cancellation["payment_status"] == "COMPLETED":
                if not is_demo_mode():
                    raise HTTPException(status_code=409, detail="Refund ya malipo halisi haijaunganishwa.")
                items = connection.execute(
                    """
                    SELECT product_id, quantity, variant_id
                    FROM order_items WHERE order_id = ?
                    """,
                    (cancellation["order_id"],),
                ).fetchall()
                if items:
                    for item in items:
                        if item["variant_id"]:
                            connection.execute(
                                """
                                UPDATE product_variants
                                SET stock_quantity = stock_quantity + ?
                                WHERE id = ? AND product_id = ?
                                """,
                                (
                                    item["quantity"],
                                    item["variant_id"],
                                    item["product_id"],
                                ),
                            )
                        connection.execute(
                            "UPDATE products SET stock_quantity = stock_quantity + ? WHERE id = ?",
                            (item["quantity"], item["product_id"]),
                        )
                        record_inventory_movement(
                            connection,
                            item["product_id"],
                            cancellation["seller_id"],
                            actor["id"],
                            item["quantity"],
                            "ORDER_CANCELLED",
                        )
                else:
                    connection.execute(
                        "UPDATE products SET stock_quantity = stock_quantity + 1 WHERE id = ?",
                        (cancellation["product_id"],),
                    )
                    record_inventory_movement(
                        connection,
                        cancellation["product_id"],
                        cancellation["seller_id"],
                        actor["id"],
                        1,
                        "ORDER_CANCELLED",
                    )
            connection.execute(
                """
                UPDATE orders
                SET payment_status = 'CANCELLED', cancelled_at = CURRENT_TIMESTAMP
                WHERE order_id = ?
                """,
                (cancellation["order_id"],),
            )
            cancel_checkout_reminders_for_order(connection, dict(cancellation))
        connection.execute(
            """
            UPDATE cancellation_requests
            SET status = ?, admin_note = ?, resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payload.status, payload.admin_note.strip(), cancellation_id),
        )
    record_audit(
        "CANCELLATION_RESOLVED",
        "order",
        cancellation["order_id"],
        payload.status,
        actor=actor,
        request=request,
    )
    return {"id": cancellation_id, "order_id": cancellation["order_id"], "status": payload.status}


def list_cancellations(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT c.id, c.order_id, c.reason, c.status, c.admin_note,
                   c.created_at, o.amount, o.payment_status,
                   u.full_name AS buyer_name, u.email AS buyer_email
            FROM cancellation_requests c
            JOIN orders o ON o.order_id = c.order_id
            JOIN users u ON u.id = c.requester_id
            ORDER BY c.created_at DESC
            LIMIT 500
            """
        ).fetchall()
    return [dict(row) for row in rows]


def request_return(order_id: str, payload: ReturnRequestInput, request: Request):
    buyer = require_buyer(request)
    with db_connection() as connection:
        connection.execute(
            "UPDATE orders SET order_id = order_id WHERE order_id = ? AND buyer_id = ?",
            (order_id, buyer["id"]),
        )
        order = connection.execute(
            """
            SELECT payment_status, delivery_status, delivered_at, cancelled_at
            FROM orders
            WHERE order_id = ? AND buyer_id = ?
            """,
            (order_id, buyer["id"]),
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        if (
            order["payment_status"] != "COMPLETED"
            or order["delivery_status"] != "DELIVERED"
            or order["cancelled_at"]
        ):
            raise HTTPException(
                status_code=409,
                detail="Ombi la kurejesha linapatikana baada ya oda iliyolipiwa kufikishwa.",
            )
        delivered_at = order["delivered_at"]
        if not delivered_at:
            raise HTTPException(
                status_code=409,
                detail="Muda wa delivery haujarekodiwa; wasiliana na msaada kuhusu ombi la marejesho.",
            )
        if isinstance(delivered_at, str):
            delivered_at = datetime.fromisoformat(
                delivered_at.replace("Z", "+00:00")
            )
        if delivered_at.tzinfo is None:
            delivered_at = delivered_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > delivered_at + timedelta(
            seconds=RETURN_REQUEST_WINDOW_SECONDS
        ):
            raise HTTPException(
                status_code=409,
                detail="Muda wa siku 7 wa kuomba marejesho umeisha.",
            )
        cancellation = connection.execute(
            "SELECT status FROM cancellation_requests WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if cancellation and cancellation["status"] in {"PENDING", "APPROVED"}:
            raise HTTPException(
                status_code=409,
                detail="Ombi la kughairi oda hii linasubiri au tayari limeidhinishwa.",
            )
        if connection.execute(
            "SELECT 1 FROM return_requests WHERE order_id = ?",
            (order_id,),
        ).fetchone():
            raise HTTPException(status_code=409, detail="Ombi la kurejesha oda hii limeshatumwa.")
        request_id = secrets.token_hex(16)
        try:
            connection.execute(
                """
                INSERT INTO return_requests (id, order_id, requester_id, reason)
                VALUES (?, ?, ?, ?)
                """,
                (request_id, order_id, buyer["id"], payload.reason.strip()),
            )
        except Exception as error:
            if is_integrity_error(error):
                raise HTTPException(
                    status_code=409,
                    detail="Ombi la kurejesha oda hii limeshatumwa.",
                ) from error
            raise
    record_audit(
        "RETURN_REQUESTED",
        "order",
        order_id,
        payload.reason.strip(),
        actor=buyer,
        request=request,
    )
    return {"id": request_id, "order_id": order_id, "status": "PENDING"}


def list_return_requests(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT r.id, r.order_id, r.reason, r.status, r.admin_note,
                   r.created_at, o.amount, o.payment_status,
                   o.delivery_status, u.full_name AS buyer_name,
                   u.email AS buyer_email
            FROM return_requests r
            JOIN orders o ON o.order_id = r.order_id
            JOIN users u ON u.id = r.requester_id
            ORDER BY r.created_at DESC
            LIMIT 500
            """
        ).fetchall()
    return [dict(row) for row in rows]


def resolve_return_request(
    return_id: str,
    payload: ReturnResolutionInput,
    request: Request,
):
    actor = require_admin(request)
    with db_connection() as connection:
        result = connection.execute(
            """
            UPDATE return_requests
            SET status = ?, admin_note = ?, resolved_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'PENDING'
            """,
            (payload.status, payload.admin_note.strip(), return_id),
        )
        if result.rowcount == 0:
            exists = connection.execute(
                "SELECT 1 FROM return_requests WHERE id = ?",
                (return_id,),
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="Ombi la kurejesha halikupatikana.")
            raise HTTPException(status_code=409, detail="Ombi hili limeshughulikiwa tayari.")
        order_id = connection.execute(
            "SELECT order_id FROM return_requests WHERE id = ?",
            (return_id,),
        ).fetchone()["order_id"]
    record_audit(
        "RETURN_REQUEST_RESOLVED",
        "order",
        order_id,
        f"{payload.status}; no payment was transferred",
        actor=actor,
        request=request,
    )
    return {
        "id": return_id,
        "order_id": order_id,
        "status": payload.status,
        "refund_issued": False,
    }


def create_review(order_id: str, product_id: str, payload: ReviewInput, request: Request):
    buyer = require_buyer(request)
    with db_connection() as connection:
        order = connection.execute(
            """
            SELECT payment_status, payment_method FROM orders
            WHERE order_id = ? AND buyer_id = ?
            """,
            (order_id, buyer["id"]),
        ).fetchone()
        if order is None or order["payment_status"] != "COMPLETED":
            raise HTTPException(status_code=403, detail="Unaweza kutoa maoni baada ya oda kukamilika.")
        if order["payment_method"] == "PesaPal Sandbox":
            raise HTTPException(status_code=403, detail="Oda ya sandbox haiwezi kutumika kutoa maoni.")
        item = connection.execute(
            "SELECT 1 FROM order_items WHERE order_id = ? AND product_id = ?",
            (order_id, product_id),
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Bidhaa haipo kwenye oda hii.")
        review_id = secrets.token_hex(16)
        try:
            connection.execute(
                """
                INSERT INTO reviews
                    (id, order_id, product_id, buyer_id, rating, comment)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    order_id,
                    product_id,
                    buyer["id"],
                    payload.rating,
                    payload.comment.strip(),
                ),
            )
        except Exception as error:
            if is_integrity_error(error):
                raise HTTPException(status_code=409, detail="Tayari umetoa maoni kwa bidhaa hii kwenye oda hiyo.") from error
            raise
    record_audit(
        "REVIEW_SUBMITTED",
        "review",
        review_id,
        f"rating={payload.rating}",
        actor=buyer,
        request=request,
    )
    return {"id": review_id, "status": "PENDING"}


def list_product_reviews(product_id: str):
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT r.rating, r.comment, r.created_at, u.full_name AS buyer_name
            FROM reviews r
            JOIN users u ON u.id = r.buyer_id
            WHERE r.product_id = ? AND r.status = 'APPROVED'
            ORDER BY r.created_at DESC
            """,
            (product_id,),
        ).fetchall()
    reviews = [dict(row) for row in rows]
    return {
        "average_rating": round(sum(row["rating"] for row in reviews) / len(reviews), 1)
        if reviews else None,
        "review_count": len(reviews),
        "reviews": reviews,
    }


def list_reviews(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT r.id, r.order_id, r.product_id, p.name AS product_name,
                   r.rating, r.comment, r.status, r.created_at,
                   u.full_name AS buyer_name
            FROM reviews r
            JOIN products p ON p.id = r.product_id
            JOIN users u ON u.id = r.buyer_id
            ORDER BY r.created_at DESC
            LIMIT 500
            """
        ).fetchall()
    return [dict(row) for row in rows]


def moderate_review(
    review_id: str,
    payload: ReviewModerationInput,
    request: Request,
):
    actor = require_admin(request)
    with db_connection() as connection:
        result = connection.execute(
            "UPDATE reviews SET status = ? WHERE id = ?",
            (payload.status, review_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Maoni hayakupatikana.")
    record_audit(
        "REVIEW_MODERATED",
        "review",
        review_id,
        payload.status,
        actor=actor,
        request=request,
    )
    return {"id": review_id, "status": payload.status}


def create_support_ticket(payload: SupportTicketInput, request: Request):
    email = payload.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Barua pepe si sahihi.")
    user = optional_current_user(request)
    ticket_id = secrets.token_hex(16)
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO support_tickets (id, user_id, email, subject, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                user["id"] if user else None,
                email,
                payload.subject.strip(),
                payload.message.strip(),
            ),
        )
    record_audit(
        "SUPPORT_TICKET_CREATED",
        "ticket",
        ticket_id,
        "Support request submitted",
        actor=user,
        request=request,
    )
    return {"id": ticket_id, "status": "OPEN"}


def list_support_tickets(request: Request):
    user = get_current_user(request)
    with db_connection() as connection:
        if user["role"] == "ADMIN":
            rows = connection.execute(
                """
                SELECT id, user_id, email, subject, message, status,
                       admin_response, created_at, updated_at
                FROM support_tickets ORDER BY created_at DESC LIMIT 500
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, email, subject, message, status, admin_response,
                       created_at, updated_at
                FROM support_tickets WHERE user_id = ?
                ORDER BY created_at DESC LIMIT 100
                """,
                (user["id"],),
            ).fetchall()
    return [dict(row) for row in rows]


def update_support_ticket(
    ticket_id: str,
    payload: SupportTicketUpdateInput,
    request: Request,
):
    actor = require_admin(request)
    with db_connection() as connection:
        result = connection.execute(
            """
            UPDATE support_tickets
            SET status = ?, admin_response = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payload.status, payload.admin_response.strip(), ticket_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Tiketi haikupatikana.")
        ticket = connection.execute(
            "SELECT user_id, email FROM support_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
    record_audit("SUPPORT_TICKET_UPDATED", "ticket", ticket_id, payload.status, actor=actor, request=request)
    if ticket and ticket["user_id"]:
        queue_user_notification(
            ticket["user_id"],
            "SUPPORT_UPDATE",
            {"ticket_id": ticket_id, "status": payload.status},
            email=ticket["email"],
        )
    return {"id": ticket_id, "status": payload.status}


def demo_complete(order_id: str):
    if not is_demo_mode():
        raise HTTPException(status_code=404, detail="Endpoint ya malipo ya majaribio imezimwa.")
    order = complete_demo_order(order_id)
    return {
        "order_id": order_id,
        "payment_status": order["payment_status"],
        "amount": order["amount"],
        "platform_fee": order["platform_fee"],
        "seller_payout": order["seller_payout"],
        "demo": True,
    }


def dashboard(request: Request):
    user = get_current_user(request)
    where = "" if user["role"] == "ADMIN" else "AND seller_id = ?"
    params = () if user["role"] == "ADMIN" else (user["id"],)
    product_scope = "" if user["role"] == "ADMIN" else " AND seller_id = ?"
    with db_connection() as connection:
        metrics = connection.execute(
            f"""
            SELECT COUNT(*) AS order_count,
                   COALESCE(SUM(amount), 0) AS gross_sales,
                   COALESCE(SUM(platform_fee), 0) AS platform_fees,
                   COALESCE(SUM(provider_fee), 0) AS provider_fees,
                   COALESCE(SUM(product_cost), 0) AS product_costs,
                   COALESCE(SUM(seller_payout), 0) AS seller_payout,
                   COALESCE(SUM(net_profit), 0) AS net_profit
            FROM orders
            WHERE payment_status = 'COMPLETED'
              AND payment_method != 'PesaPal Sandbox' {where}
            """,
            params,
        ).fetchone()
        product_count = connection.execute(
            "SELECT COUNT(*) AS count FROM products WHERE is_active = 1"
            + product_scope,
            params,
        ).fetchone()["count"]
        order_metrics = connection.execute(
            f"""
            SELECT COUNT(*) AS total_orders,
                   COUNT(DISTINCT buyer_id) AS customer_count,
                   COALESCE(SUM(CASE WHEN payment_status = 'COMPLETED'
                         AND delivery_status NOT IN ('DELIVERED', 'DELIVERY_FAILED', 'RETURNED', 'CANCELLED')
                       THEN 1 ELSE 0 END), 0) AS active_deliveries
            FROM orders
            WHERE payment_method != 'PesaPal Sandbox' {where}
            """,
            params,
        ).fetchone()
        product_metrics = connection.execute(
            """
            SELECT COALESCE(SUM(stock_quantity), 0) AS inventory_units,
                   COALESCE(SUM(CASE WHEN stock_quantity <= low_stock_threshold THEN 1 ELSE 0 END), 0) AS low_stock_count
            FROM products
            WHERE is_active = 1
            """ + product_scope,
            params,
        ).fetchone()
        review_where = "" if user["role"] == "ADMIN" else " AND p.seller_id = ?"
        review_metrics = connection.execute(
            """
            SELECT COUNT(*) AS review_count
            FROM reviews r
            JOIN products p ON p.id = r.product_id
            WHERE 1 = 1
            """ + review_where,
            params,
        ).fetchone()
        cancellation_metrics = connection.execute(
            """
            SELECT COUNT(*) AS pending_cancellations
            FROM cancellation_requests c
            JOIN orders o ON o.order_id = c.order_id
            WHERE c.status = 'PENDING'
            """ + ("" if user["role"] == "ADMIN" else " AND o.seller_id = ?"),
            params,
        ).fetchone()
        cutoff = (
            "CURRENT_TIMESTAMP - INTERVAL '6 days'"
            if connection.postgres
            else "datetime('now', '-6 days')"
        )
        sales_trend = connection.execute(
            f"""
            SELECT DATE(created_at) AS day, COALESCE(SUM(amount), 0) AS sales,
                   COUNT(*) AS order_count
            FROM orders
            WHERE payment_status = 'COMPLETED'
              AND payment_method != 'PesaPal Sandbox'
              AND created_at >= {cutoff} {where}
            GROUP BY DATE(created_at)
            ORDER BY DATE(created_at)
            """,
            params,
        ).fetchall()
    return {
        **dict(metrics),
        "product_count": product_count,
        **dict(order_metrics),
        **dict(product_metrics),
        **dict(review_metrics),
        **dict(cancellation_metrics),
        "sales_trend": [dict(row) for row in sales_trend],
        "commission_percent": COMMISSION_PERCENT,
        "demo": is_demo_mode(),
    }


def admin_dashboard_summary(request: Request):
    require_admin(request)
    with db_connection() as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS total_users,
                (SELECT COUNT(*) FROM users WHERE role = 'BUYER') AS buyers,
                (SELECT COUNT(*) FROM users WHERE role = 'SELLER') AS sellers,
                (SELECT COUNT(*) FROM users WHERE role = 'SELLER' AND kyc_status = 'VERIFIED') AS verified_sellers,
                (SELECT COUNT(*) FROM users WHERE role = 'SELLER'
                    AND kyc_status IN ('PENDING_VERIFICATION', 'UNDER_REVIEW', 'RESUBMISSION_REQUIRED')) AS pending_kyc,
                (SELECT COUNT(*) FROM businesses WHERE status = 'ACTIVE') AS businesses,
                (SELECT COUNT(*) FROM products) AS products,
                (SELECT COUNT(*) FROM orders) AS orders,
                (SELECT COUNT(*) FROM orders
                    WHERE payment_status = 'COMPLETED'
                      AND delivery_status NOT IN ('DELIVERED', 'DELIVERY_FAILED', 'RETURNED', 'CANCELLED')) AS active_deliveries,
                (SELECT COUNT(*) FROM security_alerts
                    WHERE status NOT IN ('CLOSED', 'FALSE_POSITIVE', 'RESOLVED')) AS fraud_alerts
            """
        ).fetchone()
        cutoff = (
            "CURRENT_TIMESTAMP - INTERVAL '1 day'"
            if connection.postgres
            else "datetime('now', '-1 day')"
        )
        security_events = connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM audit_log
            WHERE created_at >= {cutoff}
              AND (action LIKE 'LOGIN_%' OR action LIKE '%SECURITY%' OR action LIKE '%ACCESS%')
            """
        ).fetchone()["count"]
        trend_cutoff = (
            "CURRENT_TIMESTAMP - INTERVAL '6 days'"
            if connection.postgres
            else "datetime('now', '-6 days')"
        )
        sales_trend = connection.execute(
            f"""
            SELECT DATE(created_at) AS day, COALESCE(SUM(amount), 0) AS sales,
                   COUNT(*) AS order_count
            FROM orders
            WHERE payment_status = 'COMPLETED'
              AND payment_method != 'PesaPal Sandbox'
              AND created_at >= {trend_cutoff}
            GROUP BY DATE(created_at)
            ORDER BY DATE(created_at)
            """
        ).fetchall()
        delivery_statuses = connection.execute(
            """
            SELECT delivery_status AS status, COUNT(*) AS count
            FROM orders
            WHERE payment_status = 'COMPLETED'
            GROUP BY delivery_status
            ORDER BY count DESC
            """
        ).fetchall()
        activity = connection.execute(
            """
            SELECT action, actor_email, target_type, target_id, created_at
            FROM audit_log
            ORDER BY created_at DESC
            LIMIT 10
            """
        ).fetchall()
    return {
        "counts": {**dict(counts), "security_events": security_events},
        "sales_trend": [dict(row) for row in sales_trend],
        "delivery_statuses": [dict(row) for row in delivery_statuses],
        "activity": [dict(row) for row in activity],
    }


def list_payout_ledger(request: Request):
    actor = get_current_user(request)
    if actor["role"] not in {"ADMIN", "SELLER"}:
        raise HTTPException(status_code=403, detail="Taarifa za malipo zinapatikana kwa wauzaji na admin pekee.")
    seller_filter = "WHERE u.role = 'SELLER'"
    parameters = ()
    if actor["role"] == "SELLER":
        seller_filter += " AND u.id = ?"
        parameters = (actor["id"],)
    with db_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT u.id AS seller_id, u.full_name AS seller_name, u.shop_name,
                   COALESCE(earned.total, 0) AS completed_earned,
                   COALESCE(paid.total, 0) AS recorded_paid
            FROM users u
            LEFT JOIN (
                SELECT seller_id, SUM(seller_payout) AS total
                FROM orders
                WHERE payment_status = 'COMPLETED'
                GROUP BY seller_id
            ) earned ON earned.seller_id = u.id
            LEFT JOIN (
                SELECT seller_id, SUM(amount) AS total
                FROM seller_payouts
                GROUP BY seller_id
            ) paid ON paid.seller_id = u.id
            {seller_filter}
            ORDER BY u.shop_name, u.full_name
            LIMIT 500
            """,
            parameters,
        ).fetchall()
        entry_query = """
            SELECT p.id, p.seller_id, p.amount, p.payment_method,
                   p.payment_reference, p.note, p.created_at,
                   u.full_name AS seller_name, u.shop_name,
                   admin.full_name AS recorded_by_name
            FROM seller_payouts p
            JOIN users u ON u.id = p.seller_id
            LEFT JOIN users admin ON admin.id = p.recorded_by
        """
        entry_parameters = ()
        if actor["role"] == "SELLER":
            entry_query += " WHERE p.seller_id = ?"
            entry_parameters = (actor["id"],)
        entry_query += " ORDER BY p.created_at DESC LIMIT 500"
        entries = connection.execute(entry_query, entry_parameters).fetchall()

    accounts = []
    for row in rows:
        account = dict(row)
        eligible_total = max(0, account["completed_earned"])
        account["outstanding_amount"] = max(
            0,
            eligible_total - account["recorded_paid"],
        )
        account["overpaid_amount"] = max(
            0,
            account["recorded_paid"] - eligible_total,
        )
        accounts.append(account)
    return {
        "accounts": accounts,
        "entries": [dict(row) for row in entries],
        "automatic_transfers_enabled": False,
    }


def create_manual_payout(payload: PayoutEntryInput, request: Request):
    actor = require_admin(request)
    payout_id = secrets.token_hex(16)
    with db_connection() as connection:
        connection.execute(
            "UPDATE users SET id = id WHERE id = ? AND role = 'SELLER'",
            (payload.seller_id,),
        )
        seller = connection.execute(
            "SELECT id FROM users WHERE id = ? AND role = 'SELLER'",
            (payload.seller_id,),
        ).fetchone()
        if seller is None:
            raise HTTPException(status_code=404, detail="Muuzaji hakupatikana.")
        completed_earned = connection.execute(
            """
            SELECT COALESCE(SUM(seller_payout), 0) AS total
            FROM orders
            WHERE seller_id = ? AND payment_status = 'COMPLETED'
            """,
            (payload.seller_id,),
        ).fetchone()["total"]
        recorded_paid = connection.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM seller_payouts WHERE seller_id = ?",
            (payload.seller_id,),
        ).fetchone()["total"]
        outstanding = max(0, max(0, completed_earned) - recorded_paid)
        if payload.amount > outstanding:
            raise HTTPException(
                status_code=409,
                detail="Malipo yanazidi salio la muuzaji.",
            )
        try:
            connection.execute(
                """
                INSERT INTO seller_payouts
                    (id, seller_id, amount, payment_method, payment_reference, note, recorded_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payout_id,
                    payload.seller_id,
                    payload.amount,
                    payload.payment_method,
                    payload.payment_reference,
                    payload.note.strip(),
                    actor["id"],
                ),
            )
        except Exception as error:
            if is_integrity_error(error):
                raise HTTPException(
                    status_code=409,
                    detail="Rejea ya malipo hii tayari imerekodiwa kwa muuzaji.",
                ) from error
            raise
    record_audit(
        "MANUAL_PAYOUT_RECORDED",
        "seller_payout",
        payout_id,
        f"amount={payload.amount}; method={payload.payment_method}; no transfer initiated",
        actor=actor,
        request=request,
    )
    return {
        "id": payout_id,
        "seller_id": payload.seller_id,
        "amount": payload.amount,
        "payment_method": payload.payment_method,
        "payment_reference": payload.payment_reference,
        "outstanding_amount": outstanding - payload.amount,
        "transfer_initiated": False,
    }


def list_sales(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    status: str = "",
):
    user = get_current_user(request)
    base = """
        SELECT o.order_id, o.product_id, o.seller_id, o.amount, o.buyer_name,
               o.buyer_email, o.payment_method, o.payment_status, o.product_cost,
               o.provider_fee, o.platform_fee, o.seller_payout, o.net_profit,
               o.delivery_address, o.delivery_status, o.delivery_carrier,
               o.tracking_code, o.delivery_fee,
               o.completed_at, o.archived_at,
               o.created_at, p.name AS product_name,
               u.shop_name, oi.variant_label
        FROM orders o
        JOIN products p ON p.id = o.product_id
        LEFT JOIN users u ON u.id = o.seller_id
        LEFT JOIN order_items oi
          ON oi.order_id = o.order_id AND oi.product_id = o.product_id
    """
    if user["role"] == "ADMIN":
        conditions = ["o.payment_method != 'PesaPal Sandbox'"]
        parameters = []
    else:
        conditions = [
            "o.seller_id = ?",
            "o.archived_at IS NULL",
            "o.payment_method != 'PesaPal Sandbox'",
        ]
        parameters = [user["id"]]
    for label, value, operator in (
        ("start_date", start_date, ">="),
        ("end_date", end_date, "<="),
    ):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail=f"{label} lazima iwe tarehe ya YYYY-MM-DD.",
                ) from error
            conditions.append(
                f"SUBSTR(CAST(o.created_at AS TEXT), 1, 10) {operator} ?"
            )
            parameters.append(value)
    if status:
        if status not in {"PENDING", "COMPLETED", "EXPIRED", "CANCELLED"}:
            raise HTTPException(status_code=422, detail="Hali ya mauzo si sahihi.")
        conditions.append("o.payment_status = ?")
        parameters.append(status)
    if conditions:
        base += " WHERE " + " AND ".join(conditions)
    query = base + " ORDER BY o.created_at DESC"
    with db_connection() as connection:
        rows = connection.execute(query, tuple(parameters)).fetchall()
    return [dict(row) for row in rows]


def archive_sale(order_id: str, request: Request):
    user = require_seller(request)
    with db_connection() as connection:
        result = connection.execute(
            """
            UPDATE orders SET archived_at = CURRENT_TIMESTAMP
            WHERE order_id = ? AND seller_id = ? AND archived_at IS NULL
              AND payment_status = 'COMPLETED'
            """,
            (order_id, user["id"]),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Rekodi ya mauzo haikupatikana.")
    record_audit("SALE_ARCHIVED", "order", order_id, "Seller archived completed sale", actor=user, request=request)
    return {"message": "Rekodi imefichwa kwenye historia yako; admin bado anaiona."}


def list_sellers(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT u.id, u.full_name, u.email, u.status, u.shop_name, u.phone, u.kyc_status, u.created_at,
                   COUNT(o.order_id) AS order_count,
                   COALESCE(SUM(CASE WHEN o.payment_status = 'COMPLETED' THEN o.amount ELSE 0 END), 0) AS gross_sales,
                   COALESCE(SUM(CASE WHEN o.payment_status = 'COMPLETED' THEN o.platform_fee ELSE 0 END), 0) AS platform_fees
            FROM users u LEFT JOIN orders o
              ON o.seller_id = u.id AND o.payment_method != 'PesaPal Sandbox'
            WHERE u.role = 'SELLER'
            GROUP BY u.id, u.full_name, u.email, u.status, u.shop_name, u.phone, u.kyc_status, u.created_at
            ORDER BY u.created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def update_seller_status(seller_id: str, payload: SellerStatusInput, request: Request):
    actor = require_admin(request)
    status = payload.status
    with db_connection() as connection:
        result = connection.execute(
            "UPDATE users SET status = ? WHERE id = ? AND role = 'SELLER'",
            (status, seller_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Muuzaji hakupatikana.")
    record_audit("SELLER_STATUS_UPDATED", "seller", seller_id, payload.status, actor=actor, request=request)
    return {"id": seller_id, "status": status}


def list_audit(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, actor_id, actor_email, action, target_type, target_id,
                   detail, ip_address, created_at
            FROM audit_log
            ORDER BY created_at DESC
            LIMIT 500
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_system_alerts(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, alert_type, message, created_at, acknowledged_at
            FROM system_alerts
            ORDER BY created_at DESC
            LIMIT 200
            """
        ).fetchall()
    return [dict(row) for row in rows]


def acknowledge_system_alert(alert_id: str, request: Request):
    actor = require_admin(request)
    with db_connection() as connection:
        result = connection.execute(
            """
            UPDATE system_alerts SET acknowledged_at = CURRENT_TIMESTAMP
            WHERE id = ? AND acknowledged_at IS NULL
            """,
            (alert_id,),
        )
        if result.rowcount == 0:
            exists = connection.execute(
                "SELECT 1 FROM system_alerts WHERE id = ?",
                (alert_id,),
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="Arifa haikupatikana.")
    record_audit(
        "SYSTEM_ALERT_ACKNOWLEDGED",
        "system_alert",
        alert_id,
        actor=actor,
        request=request,
    )
    return {"id": alert_id, "acknowledged": True}


def export_sales(request: Request):
    require_admin(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT o.order_id, o.created_at, o.completed_at, o.payment_status,
                   o.buyer_name, o.buyer_email, o.buyer_phone, o.payment_method,
                   o.amount, o.product_cost, o.platform_fee, o.seller_payout,
                   o.net_profit, u.shop_name
            FROM orders o LEFT JOIN users u ON u.id = o.seller_id
            ORDER BY o.created_at DESC
            """
        ).fetchall()
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "order_id", "created_at", "completed_at", "payment_status",
        "buyer_name", "buyer_email", "buyer_phone", "payment_method",
        "amount", "product_cost", "platform_fee", "seller_payout",
        "net_profit", "shop_name",
    ])
    writer.writeheader()
    for row in rows:
        writer.writerow({
            key: spreadsheet_safe_cell(value)
            for key, value in dict(row).items()
        })
    record_audit("SALES_EXPORTED", "export", "sales.csv", "Admin exported sales", actor=require_admin(request), request=request)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=liparahisi-sales.csv"},
    )


if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        CachedStaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/p/{product_id}")
    def short_product_link(product_id: str):
        get_product(product_id)
        return RedirectResponse(
            url=f"/pay/{product_id}?from=qr",
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/")
    def frontend_index():
        response = FileResponse(FRONTEND_DIST / "index.html")
        response.headers["Cache-Control"] = "no-store"
        return response


    @app.get("/{path:path}")
    def frontend_fallback(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint haikupatikana.")
        frontend_root = FRONTEND_DIST.resolve()
        requested_file = (frontend_root / path).resolve()
        if requested_file.is_relative_to(frontend_root) and requested_file.is_file():
            return FileResponse(requested_file)
        response = FileResponse(FRONTEND_DIST / "index.html")
        response.headers["Cache-Control"] = "no-store"
        return response


from services.admin.main import app as admin_service_app
from services.catalog.main import app as catalog_service_app
from services.identity.main import app as identity_service_app
from services.orders.main import app as orders_service_app

IN_PROCESS_SERVICE_APPS = {
    "admin": admin_service_app,
    "catalog": catalog_service_app,
    "identity": identity_service_app,
    "orders": orders_service_app,
}

for service_name in IN_PROCESS_SERVICE_APPS:
    service_url_key = f"{service_name.upper()}_SERVICE_URL"
    service_token_key = f"{service_name.upper()}_SERVICE_TOKEN"
    if (
        not os.getenv(service_url_key, "").strip()
        and not os.getenv(service_token_key, "").strip()
    ):
        os.environ[service_token_key] = secrets.token_urlsafe(32)
