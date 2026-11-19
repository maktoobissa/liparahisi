import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
import csv
import io
import unicodedata
from contextlib import asynccontextmanager, contextmanager
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
import psycopg
from backend.email_delivery import (
    EmailDeliveryError,
    deliver_email,
    email_is_configured,
    email_provider,
)
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, validator


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
logger = logging.getLogger(__name__)

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "backend" / "data.sqlite3")))
FRONTEND_DIST = PROJECT_ROOT / "dist"
SESSION_COOKIE = "liparahisi_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
COMMISSION_PERCENT = 5
ORDER_TTL_SECONDS = 60 * 60 * 24
MAX_PRODUCT_IMAGE_BYTES = 5 * 1024 * 1024
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
}


class DatabaseConnection:
    def __init__(self, connection, postgres=False):
        self.connection = connection
        self.postgres = postgres

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
        self.connection.close()


def connect_db():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]
        return DatabaseConnection(
            psycopg.connect(database_url, row_factory=dict_row),
            postgres=True,
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
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL,
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
                tracking_code TEXT NOT NULL DEFAULT '',
                delivered_at {timestamp_type},
                cancelled_at {timestamp_type},
                payment_status TEXT NOT NULL DEFAULT 'PENDING',
                checkout_url TEXT,
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
            """
        )

        product_columns = {
            "seller_id": "TEXT",
            "description": "TEXT NOT NULL DEFAULT ''",
            "image_url": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT ''",
            "cost_amount": "INTEGER NOT NULL DEFAULT 0",
            "stock_quantity": "INTEGER NOT NULL DEFAULT 0",
            "low_stock_threshold": "INTEGER NOT NULL DEFAULT 5",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
        }
        user_columns = {
            "store_slug": "TEXT NOT NULL DEFAULT ''",
            "store_description": "TEXT NOT NULL DEFAULT ''",
            "store_image_url": "TEXT NOT NULL DEFAULT ''",
            "email_verified": "INTEGER NOT NULL DEFAULT 0",
            "verification_token_hash": "TEXT NOT NULL DEFAULT ''",
            "reset_token_hash": "TEXT NOT NULL DEFAULT ''",
            "reset_expires": "REAL NOT NULL DEFAULT 0",
        }
        order_columns = {
            "seller_id": "TEXT",
            "buyer_name": "TEXT NOT NULL DEFAULT ''",
            "buyer_email": "TEXT NOT NULL DEFAULT ''",
            "buyer_phone": "TEXT NOT NULL DEFAULT ''",
            "buyer_id": "TEXT",
            "payment_method": "TEXT NOT NULL DEFAULT ''",
            "bank_branch": "TEXT NOT NULL DEFAULT ''",
            "delivery_address": "TEXT NOT NULL DEFAULT ''",
            "delivery_status": "TEXT NOT NULL DEFAULT 'UNFULFILLED'",
            "tracking_code": "TEXT NOT NULL DEFAULT ''",
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
        }
        for column, declaration in product_columns.items():
            ensure_column(connection, "products", column, declaration)
        for column, declaration in user_columns.items():
            ensure_column(connection, "users", column, declaration)
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
            """
            CREATE UNIQUE INDEX IF NOT EXISTS orders_checkout_seller_idx
            ON orders(checkout_id, seller_id)
            """
        )


@asynccontextmanager
async def lifespan(_app):
    initialize_db()
    yield


app = FastAPI(title="LipaRahisi API", lifespan=lifespan)


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


class PasswordChangeInput(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class PasswordResetRequestInput(BaseModel):
    email: str = Field(min_length=5, max_length=254)


class PasswordResetInput(BaseModel):
    reset_token: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=10, max_length=128)


class SellerStatusInput(BaseModel):
    status: Literal["ACTIVE", "REJECTED", "SUSPENDED"]


class CategoryInput(BaseModel):
    name: str = Field(min_length=2, max_length=60)


class ProductInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    image_url: str = Field(default="", max_length=2048)
    category: str = Field(default="", max_length=60)
    amount: int = Field(gt=0, le=999999999)
    cost_amount: int = Field(default=0, ge=0, le=999999999)
    stock_quantity: int = Field(default=0, ge=0, le=999999999)
    low_stock_threshold: int = Field(default=5, ge=0, le=999999999)

    @validator("name")
    def strip_product_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Jina la bidhaa linahitajika.")
        return value

    @validator("category")
    def normalize_category(cls, value):
        return value.strip()

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


class ProductCsvInput(BaseModel):
    csv_text: str = Field(min_length=1, max_length=1_000_000)


class StorefrontInput(BaseModel):
    shop_name: str = Field(min_length=2, max_length=100)
    store_description: str = Field(default="", max_length=1000)
    store_image_url: str = Field(default="", max_length=2048)


class DeliveryUpdateInput(BaseModel):
    delivery_status: Literal["PROCESSING", "SHIPPED", "DELIVERED"]
    tracking_code: str = Field(default="", max_length=100)


class CancellationInput(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


class CancellationResolutionInput(BaseModel):
    status: Literal["APPROVED", "REJECTED"]
    admin_note: str = Field(default="", max_length=1000)


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

class CheckoutInput(BaseModel):
    request_key: str = Field(min_length=32, max_length=64)
    buyer_name: str = Field(min_length=2, max_length=100)
    buyer_email: str = Field(min_length=5, max_length=254)
    buyer_phone: str = Field(min_length=9, max_length=20)
    payment_method: str
    bank_branch: str | None = Field(default=None, max_length=100)
    delivery_address: str = Field(default="", max_length=500)

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


class CartItemInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=99)


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


def send_smtp_email(to_email, subject, text):
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
                response = client.post(
                    f"{notification_service_url.rstrip('/')}/internal/emails",
                    headers={"X-Internal-Token": service_token},
                    json={
                        "to_email": to_email,
                        "subject": subject,
                        "text": text,
                    },
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
        deliver_email(to_email, subject, text)
    except EmailDeliveryError as error:
        raise HTTPException(
            status_code=502 if email_is_configured() else 503,
            detail=str(error),
        ) from error


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
        "phone": user["phone"],
    }


def get_current_user(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Ingia kwenye akaunti yako.")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        user = connection.execute(
            """
            SELECT u.id, u.full_name, u.email, u.role, u.status, u.shop_name,
                   u.store_slug, u.phone
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (token_hash, time.time()),
        ).fetchone()
    if user is None:
        raise HTTPException(status_code=401, detail="Kikao kimeisha; ingia tena.")
    if user["status"] != "ACTIVE":
        raise HTTPException(status_code=403, detail="Akaunti yako haijaidhinishwa.")
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


def create_session(connection, user_id):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (token_hash, user_id, time.time() + SESSION_TTL_SECONDS),
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


def public_app_url(request_base_url=None):
    value = os.getenv("PUBLIC_APP_URL", "").strip()
    candidates = [
        "https://liparahisi.onrender.com",
        value,
        str(request_base_url).rstrip("/") if request_base_url else "",
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


def complete_demo_order(order_id):
    order = get_order(order_id)
    if not order["order_id"].startswith("DEMO"):
        raise HTTPException(status_code=400, detail="Hili si oda ya majaribio.")
    if order["payment_status"] == "EXPIRED":
        raise HTTPException(status_code=410, detail="Oda hii imekwisha muda; anzisha oda mpya.")
    if order["payment_status"] != "COMPLETED":
        fee = commission_amount(order["amount"])
        payout = order["amount"] - fee - order["provider_fee"]
        profit = payout - order["product_cost"]
        with db_connection() as connection:
            items = connection.execute(
                "SELECT product_id, quantity FROM order_items WHERE order_id = ?",
                (order_id,),
            ).fetchall()
            if items:
                for item in items:
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
            connection.execute(
                """
                UPDATE orders
                SET payment_status = 'COMPLETED',
                    platform_fee = ?, seller_payout = ?, net_profit = ?,
                    completed_at = CURRENT_TIMESTAMP
                WHERE order_id = ? AND payment_status != 'COMPLETED'
                """,
                (fee, payout, profit, order_id),
            )
    return get_order(order_id)


@app.get("/api/health")
def health():
    with db_connection() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok"}


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
        "live_payments_enabled": False,
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
    methods=["GET", "PATCH"],
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
    methods=["GET", "POST", "PUT"],
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
        user = safe_user(get_current_user(request))
    except HTTPException as error:
        if error.status_code not in {401, 403}:
            raise
    return {
        "setup_required": admin is None,
        "setup_available": bool(os.getenv("ADMIN_SETUP_TOKEN", "").strip()),
        "user": user,
    }


def setup_admin(payload: AdminSetupInput, request: Request, response: Response):
    expected_token = os.getenv("ADMIN_SETUP_TOKEN", "").strip()
    if not expected_token:
        raise HTTPException(status_code=503, detail="ADMIN_SETUP_TOKEN haijawekwa.")
    if not secrets.compare_digest(payload.setup_token, expected_token):
        raise HTTPException(status_code=401, detail="Tokeni ya kuanzisha admin si sahihi.")
    full_name, email = clean_identity(payload.full_name, payload.email)
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
        token = create_session(connection, user_id)
    set_session_cookie(response, request, token)
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
            verification_token = secrets.token_urlsafe(32)
            verification_hash = hashlib.sha256(verification_token.encode("utf-8")).hexdigest()
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
    email_delivery_notice = ""
    try:
        app_url = public_app_url(str(request.base_url))
        send_smtp_email(
            email,
            "LipaRahisi — thibitisha akaunti",
            f"Thibitisha akaunti: {app_url}/verify-email?token={quote(verification_token, safe='')}",
        )
    except HTTPException as error:
        logger.warning("Seller verification email was not sent: %s", error.detail)
        email_delivery_notice = f" Barua pepe ya uthibitisho haikutumwa: {error.detail}"
    return {
        "status": "PENDING",
        "message": (
            "Usajili umehifadhiwa. Msimamizi mkuu ataidhinisha akaunti yako kabla ya kuuza."
            + email_delivery_notice
        ),
    }


def register_customer(payload: CustomerRegistrationInput):
    full_name, email = clean_identity(payload.full_name, payload.email)
    phone = normalize_phone(payload.phone)
    user_id = secrets.token_hex(16)
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
    except Exception as error:
        if is_integrity_error(error):
            raise HTTPException(
                status_code=409,
                detail="Barua pepe hii tayari imesajiliwa.",
            ) from error
        raise
    return {
        "message": "Akaunti ya mnunuzi imefunguliwa. Sasa unaweza kuingia.",
        "user_id": user_id,
    }


def login(payload: LoginInput, request: Request, response: Response):
    email = payload.email.strip().lower()
    with db_connection() as connection:
        attempt = connection.execute(
            "SELECT failed_count, blocked_until FROM login_attempts WHERE email = ?",
            (email,),
        ).fetchone()
        if attempt and attempt["blocked_until"] > time.time():
            raise HTTPException(
                status_code=429,
                detail="Akaunti imezuiwa kwa muda kutokana na majaribio mengi ya login.",
            )
        user = connection.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if user is None or not verify_password(payload.password, user["password_hash"]):
            failed = connection.execute(
                "SELECT failed_count FROM login_attempts WHERE email = ?",
                (email,),
            ).fetchone()
            failed_count = (failed["failed_count"] if failed else 0) + 1
            blocked_until = time.time() + 15 * 60 if failed_count >= 5 else 0
            if failed:
                connection.execute(
                    "UPDATE login_attempts SET failed_count = ?, blocked_until = ? WHERE email = ?",
                    (failed_count, blocked_until, email),
                )
            else:
                connection.execute(
                    "INSERT INTO login_attempts (email, failed_count, blocked_until) VALUES (?, ?, ?)",
                    (email, failed_count, blocked_until),
                )
            raise HTTPException(status_code=401, detail="Barua pepe au nenosiri si sahihi.")
        if user["status"] != "ACTIVE":
            raise HTTPException(
                status_code=403,
                detail="Akaunti inasubiri idhini ya msimamizi mkuu.",
            )
        connection.execute(
            "DELETE FROM login_attempts WHERE email = ?",
            (email,),
        )
        token = create_session(connection, user["id"])
        result = safe_user(user)
    set_session_cookie(response, request, token)
    return {"user": result}


def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with db_connection() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
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
        token = create_session(connection, user["id"])
    set_session_cookie(response, request, token)
    return {"message": "Nenosiri limebadilishwa."}


def request_password_reset(payload: PasswordResetRequestInput, request: Request):
    email = payload.email.strip().lower()
    with db_connection() as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if user is not None:
            reset_token = secrets.token_urlsafe(32)
            reset_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()
            connection.execute(
                "UPDATE users SET reset_token_hash = ?, reset_expires = ? WHERE id = ?",
                (reset_hash, time.time() + 60 * 60, user["id"]),
            )
            app_url = public_app_url(str(request.base_url))
            send_smtp_email(
                email,
                "LipaRahisi — password reset",
                "Reset link: "
                f"{app_url}/reset-password?token={quote(reset_token, safe='')}\n"
                "Link hii inaisha baada ya saa 1.",
            )
    return {"message": "Ikiwa akaunti ipo, link ya kubadilisha nenosiri imetumwa."}


def confirm_password_reset(payload: PasswordResetInput):
    reset_hash = hashlib.sha256(payload.reset_token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE reset_token_hash = ? AND reset_expires > ?",
            (reset_hash, time.time()),
        ).fetchone()
        if user is None:
            raise HTTPException(status_code=400, detail="Link ya reset si sahihi au imekwisha muda.")
        connection.execute(
            "UPDATE users SET password_hash = ?, reset_token_hash = '', reset_expires = 0 WHERE id = ?",
            (hash_password(payload.new_password), user["id"]),
        )
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    return {"message": "Nenosiri limebadilishwa."}


def verify_email(token: str):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with db_connection() as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE verification_token_hash = ?",
            (token_hash,),
        ).fetchone()
        if user is None:
            raise HTTPException(status_code=400, detail="Link ya uthibitisho si sahihi.")
        connection.execute(
            "UPDATE users SET email_verified = 1, verification_token_hash = '' WHERE id = ?",
            (user["id"],),
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
            SELECT id, full_name, shop_name, store_slug, store_description,
                   store_image_url
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
    return {"store": dict(seller), "products": [dict(row) for row in products]}


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
    terms = ["p.amount >= ?", "p.amount <= ?"]
    params = [max(0, min_amount), max(0, max_amount)]
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
                       p.amount, p.cost_amount, p.is_active, p.low_stock_threshold,
                       p.stock_quantity, p.seller_id, p.created_at, u.shop_name
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
                       p.amount, p.cost_amount, p.is_active, p.low_stock_threshold,
                       p.stock_quantity, p.seller_id, p.created_at, '' AS shop_name
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
                       p.amount, p.cost_amount, p.is_active, p.low_stock_threshold,
                       p.stock_quantity, p.seller_id, p.created_at, u.shop_name
                FROM products p LEFT JOIN users u ON u.id = p.seller_id
                WHERE p.is_active = 1
                  AND (p.seller_id IS NULL OR u.status = 'ACTIVE')
                  AND """ + where + """
                ORDER BY p.created_at DESC
                """,
                tuple(params),
            ).fetchall()
    app_url = public_app_url(str(request.base_url))
    return [
        {
            **dict(row),
            "qr_url": f"{app_url}/p/{row['id']}",
            "is_active": bool(row["is_active"]),
        }
        for row in rows
    ]


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
    return [dict(row) for row in rows]


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
                    (id, name, description, image_url, category, amount,
                     seller_id, cost_amount, stock_quantity, low_stock_threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    payload.name,
                    payload.description.strip(),
                    payload.image_url.strip(),
                    payload.category,
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
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO products
                (id, name, description, image_url, category, amount, seller_id,
                 cost_amount, stock_quantity, low_stock_threshold)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                payload.name,
                payload.description.strip(),
                payload.image_url.strip(),
                payload.category,
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
        "amount": payload.amount,
        "cost_amount": payload.cost_amount,
        "stock_quantity": payload.stock_quantity,
        "low_stock_threshold": payload.low_stock_threshold,
        "qr_url": f"{app_url}/p/{product_id}",
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
        name = payload.name if payload.name is not None else product["name"]
        description = payload.description if payload.description is not None else product["description"]
        image_url = payload.image_url if payload.image_url is not None else product["image_url"]
        category = payload.category if payload.category is not None else product["category"]
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
            SET name = ?, description = ?, image_url = ?, category = ?, amount = ?,
                cost_amount = ?, stock_quantity = ?, low_stock_threshold = ?
            WHERE id = ?
            """,
            (
                name,
                description,
                image_url,
                category,
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
    return {"message": "Bidhaa imeondolewa kwenye orodha ya mauzo."}


def read_product(product_id: str):
    product = get_product(product_id)
    return {
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


def upload_product_image(
    product_id: str,
    request: Request,
    image: UploadFile = File(...),
):
    seller = require_seller(request)
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > MAX_PRODUCT_IMAGE_BYTES + 65536:
        raise HTTPException(status_code=413, detail="Picha imezidi ukubwa wa MB 5.")

    content_type = (image.content_type or "").lower()
    signatures = {
        "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
        "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": lambda data: len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if content_type not in signatures:
        raise HTTPException(status_code=415, detail="Tumia picha ya JPEG, PNG au WebP.")
    image_data = image.file.read(MAX_PRODUCT_IMAGE_BYTES + 1)
    if len(image_data) > MAX_PRODUCT_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Picha imezidi ukubwa wa MB 5.")
    if not image_data or not signatures[content_type](image_data):
        raise HTTPException(status_code=422, detail="Faili halilingani na aina ya picha iliyochaguliwa.")

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
    return {"product_id": product_id, "image_url": image_url}


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
            "SELECT seller_id FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        if product is None or product["seller_id"] != seller["id"]:
            raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
        connection.execute(
            "DELETE FROM product_images WHERE product_id = ?",
            (product_id,),
        )
        connection.execute(
            "UPDATE products SET image_url = '' WHERE id = ? AND image_url = ?",
            (product_id, image_url),
        )
    return {"product_id": product_id, "image_url": ""}


def create_checkout(product_id: str, payload: CheckoutInput, request: Request):
    if not is_demo_mode():
        raise HTTPException(
            status_code=503,
            detail="Malipo halisi yamezimwa hadi akaunti na mgawanyo wa malipo wa kila muuzaji viunganishwe.",
        )
    product = get_product(product_id)
    if not product["seller_id"]:
        raise HTTPException(status_code=503, detail="Bidhaa hii inahitaji kuunganishwa na akaunti ya muuzaji.")
    if product["stock_quantity"] <= 0:
        raise HTTPException(status_code=409, detail="Stock ya bidhaa imeisha.")
    if payload.payment_method.endswith("Branch") and not payload.bank_branch:
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
                return {"order_id": order["order_id"], "checkout_url": order["checkout_url"]}
            return {"order_id": order["order_id"], "payment_status": order["payment_status"], "demo": True}

    order_id = "DEMO" + secrets.token_hex(10).upper()
    buyer = optional_current_user(request)
    buyer_id = buyer["id"] if buyer and buyer["role"] == "BUYER" else None
    try:
        with db_connection() as connection:
            connection.execute(
                """
                INSERT INTO orders
                    (order_id, request_key, product_id, seller_id, amount,
                     buyer_name, buyer_email, buyer_phone, buyer_id,
                     payment_method, bank_branch, delivery_address,
                     product_cost, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    payload.request_key,
                    product_id,
                    product["seller_id"],
                    product["amount"],
                    payload.buyer_name.strip(),
                    payload.buyer_email,
                    payload.buyer_phone,
                    buyer_id,
                    payload.payment_method,
                    payload.bank_branch or "",
                    payload.delivery_address.strip(),
                    product["cost_amount"],
                    time.time() + ORDER_TTL_SECONDS,
                ),
            )
            connection.execute(
                """
                INSERT INTO order_items
                    (order_id, product_id, quantity, unit_amount, unit_cost)
                VALUES (?, ?, 1, ?, ?)
                """,
                (order_id, product_id, product["amount"], product["cost_amount"]),
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
                    "demo": True,
                }
            raise HTTPException(status_code=409, detail="Ombi hili la malipo tayari limetumika.") from error
        raise

    app_url = public_app_url(str(request.base_url))
    checkout_url = f"{app_url}/pay/{product_id}?order_id={quote(order_id)}&demo=1"
    with db_connection() as connection:
        connection.execute(
            "UPDATE orders SET checkout_url = ? WHERE order_id = ?",
            (checkout_url, order_id),
        )
    return {"order_id": order_id, "checkout_url": checkout_url, "demo": True}


def create_cart_checkout(payload: CartCheckoutInput, request: Request):
    if not is_demo_mode():
        raise HTTPException(
            status_code=503,
            detail="Malipo halisi yamezimwa hadi mgawanyo wa malipo wa wauzaji viunganishwe.",
        )
    if payload.payment_method.endswith("Branch") and not payload.bank_branch:
        raise HTTPException(status_code=422, detail="Chagua tawi la benki.")

    merged = {}
    buyer = optional_current_user(request)
    buyer_id = buyer["id"] if buyer and buyer["role"] == "BUYER" else None
    with db_connection() as connection:
        for item in payload.items:
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

        checkout_id = "CART" + secrets.token_hex(16).upper()
        expires_at = time.time() + ORDER_TTL_SECONDS
        order_ids = []
        for seller_index, (seller_id, seller_items) in enumerate(merged.items()):
            order_id = "DEMO" + secrets.token_hex(10).upper()
            amount = sum(product["amount"] * quantity for product, quantity in seller_items)
            product_cost = sum(product["cost_amount"] * quantity for product, quantity in seller_items)
            fee = commission_amount(amount)
            payout = amount - fee
            profit = payout - product_cost
            first_product = seller_items[0][0]
            connection.execute(
                """
                INSERT INTO orders
                    (order_id, request_key, product_id, seller_id, amount,
                     buyer_name, buyer_email, buyer_phone, buyer_id,
                     payment_method, bank_branch, delivery_address,
                     product_cost, platform_fee, seller_payout, net_profit,
                     checkout_id, cart_request_key, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    f"{payload.request_key}{seller_index:02d}",
                    first_product["id"],
                    seller_id,
                    amount,
                    payload.buyer_name.strip(),
                    payload.buyer_email,
                    payload.buyer_phone,
                    buyer_id,
                    payload.payment_method,
                    payload.bank_branch or "",
                    payload.delivery_address.strip(),
                    product_cost,
                    fee,
                    payout,
                    profit,
                    checkout_id,
                    payload.request_key,
                    expires_at,
                ),
            )
            for product, quantity in seller_items:
                connection.execute(
                    """
                    INSERT INTO order_items
                        (order_id, product_id, quantity, unit_amount, unit_cost)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        product["id"],
                        quantity,
                        product["amount"],
                        product["cost_amount"],
                    ),
                )
            order_ids.append(order_id)

    app_url = public_app_url(str(request.base_url))
    checkout_url = f"{app_url}/cart/{checkout_id}"
    with db_connection() as connection:
        connection.execute(
            "UPDATE orders SET checkout_url = ? WHERE checkout_id = ?",
            (checkout_url, checkout_id),
        )
    return {
        "checkout_id": checkout_id,
        "checkout_url": checkout_url,
        "order_ids": order_ids,
        "demo": True,
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
                SELECT oi.product_id, oi.quantity, oi.unit_amount,
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
        else "EXPIRED" if all(split["payment_status"] == "EXPIRED" for split in splits)
        else "PENDING",
        "payment_method": orders[0]["payment_method"],
        "expires_at": orders[0]["expires_at"],
        "demo": True,
        "commission_percent": COMMISSION_PERCENT,
        "total_amount": sum(split["amount"] for split in splits),
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
    for order in orders:
        complete_demo_order(order["order_id"])
    return get_cart_checkout(checkout_id, request)


def order_status(order_id: str, request: Request):
    order = get_order(order_id)
    if not is_demo_mode():
        raise HTTPException(status_code=503, detail="Hali ya malipo halisi haijaunganishwa.")
    with db_connection() as connection:
        product = connection.execute(
            "SELECT name FROM products WHERE id = ?",
            (order["product_id"],),
        ).fetchone()
    result = {
        "order_id": order_id,
        "payment_status": order["payment_status"],
        "amount": order["amount"],
        "product_name": product["name"] if product else "Bidhaa",
        "payment_method": order["payment_method"],
        "completed_at": order["completed_at"],
        "platform_fee": order["platform_fee"],
        "seller_payout": order["seller_payout"],
        "commission_percent": COMMISSION_PERCENT,
        "demo": True,
    }
    buyer = optional_current_user(request)
    if buyer and buyer["role"] == "BUYER" and buyer["id"] == order["buyer_id"]:
        result.update({
            "buyer_name": order["buyer_name"],
            "bank_branch": order["bank_branch"],
            "delivery_address": order["delivery_address"],
            "delivery_status": order["delivery_status"],
            "tracking_code": order["tracking_code"],
        })
    return result


def order_history(request: Request):
    buyer = require_buyer(request)
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT o.order_id, o.product_id, o.amount, o.payment_status,
                   o.delivery_status, o.tracking_code, o.delivery_address,
                   o.created_at, o.completed_at, p.name AS product_name,
                   p.image_url, u.shop_name
            FROM orders o
            JOIN products p ON p.id = o.product_id
            LEFT JOIN users u ON u.id = o.seller_id
            WHERE o.buyer_id = ?
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
            "SELECT seller_id, payment_status, delivery_status FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if order is None or (
            actor["role"] != "ADMIN" and order["seller_id"] != actor["id"]
        ):
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        if order["payment_status"] != "COMPLETED":
            raise HTTPException(status_code=409, detail="Delivery huanza oda ikikamilika.")
        transitions = {
            "UNFULFILLED": {"PROCESSING"},
            "PROCESSING": {"SHIPPED"},
            "SHIPPED": {"DELIVERED"},
            "DELIVERED": set(),
        }
        if (
            payload.delivery_status != order["delivery_status"]
            and payload.delivery_status not in transitions.get(order["delivery_status"], set())
        ):
            raise HTTPException(status_code=409, detail="Hatua ya delivery haiwezi kurukwa au kurudishwa nyuma.")
        connection.execute(
            """
            UPDATE orders
            SET delivery_status = ?, tracking_code = ?,
                delivered_at = CASE WHEN ? = 'DELIVERED' THEN CURRENT_TIMESTAMP ELSE delivered_at END
            WHERE order_id = ?
            """,
            (payload.delivery_status, payload.tracking_code.strip(), payload.delivery_status, order_id),
        )
    record_audit(
        "DELIVERY_STATUS_UPDATED",
        "order",
        order_id,
        payload.delivery_status,
        actor=actor,
        request=request,
    )
    return {
        "order_id": order_id,
        "delivery_status": payload.delivery_status,
        "tracking_code": payload.tracking_code.strip(),
    }


def request_cancellation(order_id: str, payload: CancellationInput, request: Request):
    buyer = require_buyer(request)
    with db_connection() as connection:
        order = connection.execute(
            "SELECT buyer_id, payment_status, cancelled_at FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if order is None or order["buyer_id"] != buyer["id"]:
            raise HTTPException(status_code=404, detail="Oda haikupatikana.")
        if order["cancelled_at"] or order["payment_status"] not in {"PENDING", "COMPLETED"}:
            raise HTTPException(status_code=409, detail="Oda hii haiwezi kuombewa kughairiwa.")
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
                   o.seller_id
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
            if cancellation["payment_status"] == "COMPLETED":
                if not is_demo_mode():
                    raise HTTPException(status_code=409, detail="Refund ya malipo halisi haijaunganishwa.")
                items = connection.execute(
                    "SELECT product_id, quantity FROM order_items WHERE order_id = ?",
                    (cancellation["order_id"],),
                ).fetchall()
                if items:
                    for item in items:
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


def create_review(order_id: str, product_id: str, payload: ReviewInput, request: Request):
    buyer = require_buyer(request)
    with db_connection() as connection:
        order = connection.execute(
            """
            SELECT payment_status FROM orders
            WHERE order_id = ? AND buyer_id = ?
            """,
            (order_id, buyer["id"]),
        ).fetchone()
        if order is None or order["payment_status"] != "COMPLETED":
            raise HTTPException(status_code=403, detail="Unaweza kutoa maoni baada ya oda kukamilika.")
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
    record_audit("SUPPORT_TICKET_UPDATED", "ticket", ticket_id, payload.status, actor=actor, request=request)
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
            WHERE payment_status = 'COMPLETED' {where}
            """,
            params,
        ).fetchone()
        product_count = connection.execute(
            "SELECT COUNT(*) AS count FROM products WHERE is_active = 1"
            + ("" if user["role"] == "ADMIN" else " AND seller_id = ?"),
            params,
        ).fetchone()["count"]
    return {
        **dict(metrics),
        "product_count": product_count,
        "commission_percent": COMMISSION_PERCENT,
        "demo": is_demo_mode(),
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
               o.delivery_address, o.delivery_status, o.tracking_code,
               o.completed_at, o.archived_at,
               o.created_at, p.name AS product_name,
               u.shop_name
        FROM orders o
        JOIN products p ON p.id = o.product_id
        LEFT JOIN users u ON u.id = o.seller_id
    """
    if user["role"] == "ADMIN":
        conditions = []
        parameters = []
    else:
        conditions = ["o.seller_id = ?", "o.archived_at IS NULL"]
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
            SELECT u.id, u.full_name, u.email, u.status, u.shop_name, u.phone, u.created_at,
                   COUNT(o.order_id) AS order_count,
                   COALESCE(SUM(CASE WHEN o.payment_status = 'COMPLETED' THEN o.amount ELSE 0 END), 0) AS gross_sales,
                   COALESCE(SUM(CASE WHEN o.payment_status = 'COMPLETED' THEN o.platform_fee ELSE 0 END), 0) AS platform_fees
            FROM users u LEFT JOIN orders o ON o.seller_id = u.id
            WHERE u.role = 'SELLER'
            GROUP BY u.id, u.full_name, u.email, u.status, u.shop_name, u.phone, u.created_at
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
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

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
