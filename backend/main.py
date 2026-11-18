import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import csv
import io
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, validator


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "backend" / "data.sqlite3")))
FRONTEND_DIST = PROJECT_ROOT / "dist"
SESSION_COOKIE = "liparahisi_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
COMMISSION_PERCENT = 5
ORDER_TTL_SECONDS = 60 * 60 * 24
PAYMENT_METHODS = {
    "M-Pesa",
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
                phone TEXT NOT NULL DEFAULT '',
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
                amount INTEGER NOT NULL CHECK (amount > 0),
                seller_id TEXT REFERENCES users(id),
                cost_amount INTEGER NOT NULL DEFAULT 0,
                stock_quantity INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                payment_method TEXT NOT NULL DEFAULT '',
                bank_branch TEXT NOT NULL DEFAULT '',
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
            """
        )

        product_columns = {
            "seller_id": "TEXT",
            "cost_amount": "INTEGER NOT NULL DEFAULT 0",
            "stock_quantity": "INTEGER NOT NULL DEFAULT 0",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
        }
        order_columns = {
            "seller_id": "TEXT",
            "buyer_name": "TEXT NOT NULL DEFAULT ''",
            "buyer_email": "TEXT NOT NULL DEFAULT ''",
            "buyer_phone": "TEXT NOT NULL DEFAULT ''",
            "payment_method": "TEXT NOT NULL DEFAULT ''",
            "bank_branch": "TEXT NOT NULL DEFAULT ''",
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
        for column, declaration in order_columns.items():
            ensure_column(connection, "orders", column, declaration)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS products_seller_idx ON products(seller_id)"
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


class LoginInput(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeInput(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class SellerStatusInput(BaseModel):
    status: Literal["ACTIVE", "REJECTED", "SUSPENDED"]


class ProductInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    amount: int = Field(gt=0, le=999999999)
    cost_amount: int = Field(default=0, ge=0, le=999999999)
    stock_quantity: int = Field(default=0, ge=0, le=999999999)

    @validator("name")
    def strip_product_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Jina la bidhaa linahitajika.")
        return value


class ProductUpdateInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    amount: int | None = Field(default=None, gt=0, le=999999999)
    cost_amount: int | None = Field(default=None, ge=0, le=999999999)
    stock_quantity: int | None = Field(default=None, ge=0, le=999999999)

    @validator("name")
    def strip_product_name(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Jina la bidhaa linahitajika.")
        return value


class CheckoutInput(BaseModel):
    request_key: str = Field(min_length=32, max_length=64)
    buyer_name: str = Field(min_length=2, max_length=100)
    buyer_email: str = Field(min_length=5, max_length=254)
    buyer_phone: str = Field(min_length=9, max_length=20)
    payment_method: str
    bank_branch: str | None = Field(default=None, max_length=100)

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
    return {
        "id": user["id"],
        "full_name": user["full_name"],
        "email": user["email"],
        "role": user["role"],
        "status": user["status"],
        "shop_name": user["shop_name"],
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
            SELECT u.id, u.full_name, u.email, u.role, u.status, u.shop_name, u.phone
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
                   u.shop_name, u.full_name AS seller_name
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


@app.get("/api/auth/status")
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


@app.post("/api/auth/setup")
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


@app.post("/api/auth/register", status_code=201)
def register_seller(payload: SellerRegistrationInput):
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
    except Exception as error:
        if is_integrity_error(error):
            raise HTTPException(
                status_code=409,
                detail="Barua pepe hii tayari imesajiliwa.",
            ) from error
        raise
    return {
        "status": "PENDING",
        "message": "Usajili umepokelewa. Msimamizi mkuu ataidhinisha akaunti yako kabla ya kuuza.",
    }


@app.post("/api/auth/login")
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


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with db_connection() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")
    return {"message": "Umetoka kwenye akaunti."}


@app.put("/api/auth/password")
def change_password(payload: PasswordChangeInput, request: Request):
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
    return {"message": "Nenosiri limebadilishwa."}


@app.get("/api/products")
def list_products(request: Request, q: str = "", seller: str = "", min_amount: int = 0, max_amount: int = 999999999):
    user = get_current_user(request)
    search = q.strip().lower()
    seller_search = seller.strip().lower()
    terms = ["p.amount >= ?", "p.amount <= ?"]
    params = [max(0, min_amount), max(0, max_amount)]
    if search:
        terms.append("(LOWER(p.name) LIKE ? OR LOWER(COALESCE(u.shop_name, '')) LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if seller_search:
        terms.append("LOWER(COALESCE(u.shop_name, '')) LIKE ?")
        params.append(f"%{seller_search}%")
    where = " AND ".join(terms)
    with db_connection() as connection:
        if user["role"] == "ADMIN":
            rows = connection.execute(
                """
                SELECT p.id, p.name, p.amount, p.cost_amount, p.is_active,
                       p.stock_quantity, p.seller_id, p.created_at, u.shop_name
                FROM products p LEFT JOIN users u ON u.id = p.seller_id
                WHERE """ + where + """
                ORDER BY p.created_at DESC
                """,
                tuple(params),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT p.id, p.name, p.amount, p.cost_amount, p.is_active,
                       p.stock_quantity, p.seller_id, p.created_at, '' AS shop_name
                FROM products p LEFT JOIN users u ON u.id = p.seller_id
                WHERE p.seller_id = ? AND """ + where + """
                ORDER BY p.created_at DESC
                """,
                (user["id"], *params),
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


@app.post("/api/products", status_code=201)
def create_product(payload: ProductInput, request: Request):
    user = get_current_user(request)
    if user["role"] != "SELLER":
        raise HTTPException(status_code=403, detail="Ni akaunti ya muuzaji pekee inayoweza kuunda bidhaa.")
    product_id = secrets.token_hex(16)
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO products
                (id, name, amount, seller_id, cost_amount, stock_quantity)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                payload.name,
                payload.amount,
                user["id"],
                payload.cost_amount,
                payload.stock_quantity,
            ),
        )
    record_audit("PRODUCT_CREATED", "product", product_id, payload.name, actor=user, request=request)
    app_url = public_app_url(str(request.base_url))
    return {
        "id": product_id,
        "name": payload.name,
        "amount": payload.amount,
        "cost_amount": payload.cost_amount,
        "stock_quantity": payload.stock_quantity,
        "qr_url": f"{app_url}/p/{product_id}",
    }


@app.put("/api/products/{product_id}")
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
        amount = payload.amount if payload.amount is not None else product["amount"]
        cost_amount = payload.cost_amount if payload.cost_amount is not None else product["cost_amount"]
        stock_quantity = payload.stock_quantity if payload.stock_quantity is not None else product["stock_quantity"]
        connection.execute(
            "UPDATE products SET name = ?, amount = ?, cost_amount = ?, stock_quantity = ? WHERE id = ?",
            (name, amount, cost_amount, stock_quantity, product_id),
        )
    record_audit("PRODUCT_UPDATED", "product", product_id, f"{name} stock={stock_quantity}", actor=user, request=request)
    return {
        "id": product_id,
        "name": name,
        "amount": amount,
        "cost_amount": cost_amount,
        "stock_quantity": stock_quantity,
    }


@app.delete("/api/products/{product_id}")
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


@app.get("/api/products/{product_id}")
def read_product(product_id: str):
    product = get_product(product_id)
    return {
        "id": product["id"],
        "name": product["name"],
        "amount": product["amount"],
        "stock_quantity": product["stock_quantity"],
        "shop_name": product["shop_name"] or product["seller_name"] or "Muuzaji",
    }


@app.post("/api/products/{product_id}/checkout")
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
    try:
        with db_connection() as connection:
            connection.execute(
                """
                INSERT INTO orders
                    (order_id, request_key, product_id, seller_id, amount,
                     buyer_name, buyer_email, buyer_phone, payment_method, bank_branch,
                     product_cost, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    payload.payment_method,
                    payload.bank_branch or "",
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


@app.post("/api/cart/checkout")
def create_cart_checkout(payload: CartCheckoutInput, request: Request):
    if not is_demo_mode():
        raise HTTPException(
            status_code=503,
            detail="Malipo halisi yamezimwa hadi mgawanyo wa malipo wa wauzaji viunganishwe.",
        )
    if payload.payment_method.endswith("Branch") and not payload.bank_branch:
        raise HTTPException(status_code=422, detail="Chagua tawi la benki.")

    merged = {}
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
            return get_cart_checkout(existing["checkout_id"])

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
                     buyer_name, buyer_email, buyer_phone, payment_method, bank_branch,
                     product_cost, platform_fee, seller_payout, net_profit,
                     checkout_id, cart_request_key, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    payload.payment_method,
                    payload.bank_branch or "",
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


def get_cart_checkout(checkout_id):
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
    return {
        "checkout_id": checkout_id,
        "payment_status": "COMPLETED" if all(split["payment_status"] == "COMPLETED" for split in splits)
        else "EXPIRED" if all(split["payment_status"] == "EXPIRED" for split in splits)
        else "PENDING",
        "buyer_name": orders[0]["buyer_name"],
        "buyer_email": orders[0]["buyer_email"],
        "buyer_phone": orders[0]["buyer_phone"],
        "payment_method": orders[0]["payment_method"],
        "bank_branch": orders[0]["bank_branch"],
        "expires_at": orders[0]["expires_at"],
        "demo": True,
        "total_amount": sum(split["amount"] for split in splits),
        "platform_fee": sum(split["platform_fee"] for split in splits),
        "completed_at": next((row["completed_at"] for row in orders if row["completed_at"]), None),
        "splits": splits,
    }


@app.get("/api/checkouts/{checkout_id}")
def read_cart_checkout(checkout_id: str):
    return get_cart_checkout(checkout_id)


@app.post("/api/checkouts/{checkout_id}/demo-complete")
def complete_cart_checkout(checkout_id: str):
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
    return get_cart_checkout(checkout_id)


@app.get("/api/orders/{order_id}")
def order_status(order_id: str):
    order = get_order(order_id)
    if not is_demo_mode():
        raise HTTPException(status_code=503, detail="Hali ya malipo halisi haijaunganishwa.")
    with db_connection() as connection:
        product = connection.execute(
            "SELECT name FROM products WHERE id = ?",
            (order["product_id"],),
        ).fetchone()
    return {
        "order_id": order_id,
        "payment_status": order["payment_status"],
        "amount": order["amount"],
        "product_name": product["name"] if product else "Bidhaa",
        "buyer_name": order["buyer_name"],
        "payment_method": order["payment_method"],
        "bank_branch": order["bank_branch"],
        "completed_at": order["completed_at"],
        "platform_fee": order["platform_fee"],
        "seller_payout": order["seller_payout"],
        "demo": True,
    }


@app.post("/api/orders/{order_id}/demo-complete")
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


@app.get("/api/dashboard")
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


@app.get("/api/sales")
def list_sales(request: Request):
    user = get_current_user(request)
    base = """
        SELECT o.order_id, o.product_id, o.seller_id, o.amount, o.buyer_name,
               o.buyer_email, o.payment_method, o.payment_status, o.product_cost,
               o.provider_fee, o.platform_fee, o.seller_payout, o.net_profit,
               o.completed_at, o.archived_at, o.created_at, p.name AS product_name,
               u.shop_name
        FROM orders o
        JOIN products p ON p.id = o.product_id
        LEFT JOIN users u ON u.id = o.seller_id
    """
    if user["role"] == "ADMIN":
        query = base + " ORDER BY o.created_at DESC"
        parameters = ()
    else:
        query = base + " WHERE o.seller_id = ? AND o.archived_at IS NULL ORDER BY o.created_at DESC"
        parameters = (user["id"],)
    with db_connection() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/sales/{order_id}/archive")
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


@app.get("/api/admin/sellers")
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


@app.patch("/api/admin/sellers/{seller_id}")
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


@app.get("/api/admin/audit")
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


@app.get("/api/admin/export/sales")
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
        writer.writerow(dict(row))
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
        response = FileResponse(FRONTEND_DIST / "index.html")
        response.headers["Cache-Control"] = "no-store"
        return response
