import base64
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, validator

from backend.selcom import SelcomClient, SelcomError, decode_selcom_url


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "backend" / "data.sqlite3")))
FRONTEND_DIST = PROJECT_ROOT / "dist"


def connect_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def db_connection():
    connection = connect_db()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_db():
    with db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                request_key TEXT NOT NULL UNIQUE,
                product_id TEXT NOT NULL REFERENCES products(id),
                amount INTEGER NOT NULL CHECK (amount > 0),
                payment_status TEXT NOT NULL DEFAULT 'PENDING',
                checkout_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS merchant_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                password TEXT NOT NULL,
                phone TEXT
            );
            """
        )
        default_password = os.getenv("MERCHANT_ADMIN_TOKEN", "").strip()
        connection.execute(
            """
            INSERT OR IGNORE INTO merchant_settings (id, password, phone)
            VALUES (1, ?, NULL)
            """,
            (default_password,),
        )


@asynccontextmanager
async def lifespan(_app):
    initialize_db()
    yield


app = FastAPI(title="LipaRahisi API", lifespan=lifespan)


class ProductInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    amount: int = Field(gt=0, le=999999999)

    @validator("name")
    def strip_product_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Jina la bidhaa linahitajika.")
        return value


class CheckoutInput(BaseModel):
    request_key: str = Field(min_length=32, max_length=64)
    buyer_name: str = Field(min_length=2, max_length=100)
    buyer_email: str = Field(min_length=5, max_length=254)
    buyer_phone: str = Field(min_length=9, max_length=20)

    @validator("buyer_name")
    def strip_buyer_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Jina la mnunuzi linahitajika.")
        return value

    @validator("buyer_email")
    def validate_email(cls, value):
        value = value.strip()
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


class MerchantSettingsInput(BaseModel):
    new_password: str | None = Field(default=None, min_length=8, max_length=128)
    phone: str | None = Field(default=None, min_length=9, max_length=20)

    @validator("phone")
    def normalize_phone(cls, value):
        if value is None:
            return None
        digits = re.sub(r"\D", "", value)
        if len(digits) == 10 and digits.startswith("0"):
            digits = "255" + digits[1:]
        if not re.fullmatch(r"255\d{9}", digits):
            raise ValueError("Weka namba sahihi ya simu ya Tanzania.")
        return digits


def required_setting(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(
            status_code=503,
            detail=f"Mpangilio wa malipo haujakamilika: {name}.",
        )
    return value


def public_app_url(request_base_url=None):
    value = os.getenv("PUBLIC_APP_URL", "").strip()
    if not value and request_base_url:
        value = str(request_base_url).rstrip("/")
    if not value and is_demo_mode():
        value = "http://127.0.0.1:8000"
    if not value:
        raise HTTPException(
            status_code=503,
            detail="Mpangilio PUBLIC_APP_URL haujakamilika.",
        )
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=503,
            detail="PUBLIC_APP_URL lazima iwe origin kamili ya http au https bila path.",
        )
    return value


def get_selcom_client():
    try:
        return SelcomClient(
            required_setting("SELCOM_BASE_URL"),
            required_setting("SELCOM_API_KEY"),
            required_setting("SELCOM_API_SECRET"),
        )
    except SelcomError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def is_demo_mode():
    return os.getenv("DEMO_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}


def require_merchant_token(supplied_token):
    with db_connection() as connection:
        setting = connection.execute(
            "SELECT password FROM merchant_settings WHERE id = 1"
        ).fetchone()
    expected_token = setting["password"] if setting else os.getenv("MERCHANT_ADMIN_TOKEN", "").strip()
    if not expected_token and is_demo_mode():
        expected_token = "demo-merchant-token-change-me"
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="MERCHANT_ADMIN_TOKEN haujakamilika.",
        )
    if len(expected_token) < 32 and not is_demo_mode():
        raise HTTPException(
            status_code=503,
            detail="MERCHANT_ADMIN_TOKEN lazima iwe na angalau herufi 32.",
        )
    if not supplied_token or not secrets.compare_digest(
        supplied_token, expected_token
    ):
        raise HTTPException(status_code=401, detail="Tokeni ya muuzaji si sahihi.")


def get_product(product_id):
    with db_connection() as connection:
        product = connection.execute(
            "SELECT id, name, amount FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
    if product is None:
        raise HTTPException(status_code=404, detail="Bidhaa haikupatikana.")
    return dict(product)


def get_order(order_id):
    with db_connection() as connection:
        order = connection.execute(
            "SELECT * FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if order is None:
        raise HTTPException(status_code=404, detail="Oda haikupatikana.")
    return dict(order)


def b64_url(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def selcom_success(response):
    if response.get("resultcode") != "000" or response.get("result") != "SUCCESS":
        message = response.get("message")
        raise SelcomError(
            f"Selcom haikukubali ombi la malipo: {message or 'tafadhali jaribu tena.'}"
        )


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.put("/api/merchant/settings")
def update_merchant_settings(
    payload: MerchantSettingsInput,
    x_merchant_token: str | None = Header(default=None),
):
    require_merchant_token(x_merchant_token)
    with db_connection() as connection:
        setting = connection.execute(
            "SELECT password, phone FROM merchant_settings WHERE id = 1"
        ).fetchone()
        old_password = setting["password"] if setting else ""
        old_phone = setting["phone"] if setting else None
        connection.execute(
            """
            UPDATE merchant_settings
            SET password = ?, phone = ?
            WHERE id = 1
            """,
            (
                payload.new_password or old_password,
                payload.phone or old_phone,
            ),
        )
    return {
        "phone": payload.phone or old_phone,
        "message": "Mipangilio ya muuzaji imehifadhiwa.",
    }


@app.post("/api/products", status_code=201)
def create_product(
    payload: ProductInput,
    request: Request,
    x_merchant_token: str | None = Header(default=None),
):
    require_merchant_token(x_merchant_token)
    app_url = public_app_url(str(request.base_url))
    product_id = secrets.token_hex(16)
    with db_connection() as connection:
        connection.execute(
            "INSERT INTO products (id, name, amount) VALUES (?, ?, ?)",
            (product_id, payload.name, payload.amount),
        )
    return {
        "id": product_id,
        "name": payload.name,
        "amount": payload.amount,
        "qr_url": f"{app_url}/p/{product_id}",
    }


@app.get("/api/products/{product_id}")
def read_product(product_id: str):
    return get_product(product_id)


@app.post("/api/products/{product_id}/checkout")
async def create_checkout(
    product_id: str,
    payload: CheckoutInput,
    request: Request,
):
    product = get_product(product_id)
    with db_connection() as connection:
        existing = connection.execute(
            "SELECT * FROM orders WHERE request_key = ?",
            (payload.request_key,),
        ).fetchone()
        if existing:
            order = dict(existing)
            if order["product_id"] != product_id:
                raise HTTPException(
                    status_code=409,
                    detail="Ombi hili la malipo tayari limetumika.",
                )
            if order["checkout_url"]:
                return {
                    "order_id": order["order_id"],
                    "checkout_url": order["checkout_url"],
                }
            return {
                "order_id": order["order_id"],
                "payment_status": order["payment_status"],
            }

    if is_demo_mode():
            order_id = "DEMO" + secrets.token_hex(10).upper()
            with db_connection() as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO orders
                            (order_id, request_key, product_id, amount, payment_status)
                        VALUES (?, ?, ?, ?, 'PENDING')
                        """,
                        (
                            order_id,
                            payload.request_key,
                            product_id,
                            product["amount"],
                        ),
                    )
                except sqlite3.IntegrityError:
                    existing = connection.execute(
                        "SELECT * FROM orders WHERE request_key = ?",
                        (payload.request_key,),
                    ).fetchone()
                    if existing is None:
                        raise HTTPException(
                            status_code=409,
                            detail="Ombi hili la malipo halikujengwa.",
                        )
                    return {
                        "order_id": existing["order_id"],
                        "payment_status": existing["payment_status"],
                        "demo": True,
                    }
            app_url = public_app_url(str(request.base_url))
            demo_url = f"{app_url}/pay/{product_id}?order_id={quote(order_id)}&demo=1"
            with db_connection() as connection:
                connection.execute(
                    "UPDATE orders SET checkout_url = ? WHERE order_id = ?",
                    (demo_url, order_id),
                )
            return {
                "order_id": order_id,
                "checkout_url": demo_url,
                "demo": True,
            }

    selcom_client = get_selcom_client()
    vendor = required_setting("SELCOM_VENDOR")
    app_url = public_app_url(str(request.base_url))
    order_id = "LR" + secrets.token_hex(10).upper()
    try:
        with db_connection() as connection:
            connection.execute(
                """
                INSERT INTO orders
                    (order_id, request_key, product_id, amount, payment_status)
                VALUES (?, ?, ?, ?, 'PENDING')
                """,
                (
                    order_id,
                    payload.request_key,
                    product_id,
                    product["amount"],
                ),
            )
    except sqlite3.IntegrityError:
        with db_connection() as connection:
            existing = connection.execute(
                "SELECT * FROM orders WHERE request_key = ?",
                (payload.request_key,),
            ).fetchone()
        if existing is None or existing["product_id"] != product_id:
            raise HTTPException(
                status_code=409,
                detail="Ombi hili la malipo tayari limetumika.",
            )
        order = dict(existing)
        if order["checkout_url"]:
            return {
                "order_id": order["order_id"],
                "checkout_url": order["checkout_url"],
            }
        return {
            "order_id": order["order_id"],
            "payment_status": order["payment_status"],
        }

    return_url = (
        f"{app_url}/pay/{product_id}?order_id={quote(order_id)}"
    )
    cancel_url = (
        f"{app_url}/pay/{product_id}?cancelled=1&order_id={quote(order_id)}"
    )
    order_data = {
        "vendor": vendor,
        "order_id": order_id,
        "buyer_email": payload.buyer_email,
        "buyer_name": payload.buyer_name,
        "buyer_phone": payload.buyer_phone,
        "amount": product["amount"],
        "currency": "TZS",
        "redirect_url": b64_url(return_url),
        "cancel_url": b64_url(cancel_url),
        "buyer_remarks": product["name"],
        "merchant_remarks": "LipaRahisi",
        "no_of_items": 1,
    }

    try:
        response = await selcom_client.request(
            "POST",
            "/v1/checkout/create-order-minimal",
            order_data,
        )
        selcom_success(response)
        data = response.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise SelcomError("Selcom haikurudisha taarifa za checkout.")
        checkout_url = decode_selcom_url(data[0].get("payment_gateway_url", ""))
    except SelcomError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": str(exc), "order_id": order_id},
        ) from exc

    with db_connection() as connection:
        connection.execute(
            "UPDATE orders SET checkout_url = ? WHERE order_id = ?",
            (checkout_url, order_id),
        )
    return {"order_id": order_id, "checkout_url": checkout_url}


@app.get("/api/orders/{order_id}")
async def order_status(order_id: str):
    order = get_order(order_id)
    if is_demo_mode():
        return {
            "order_id": order_id,
            "payment_status": order["payment_status"],
            "amount": order["amount"],
            "demo": True,
        }
    try:
        response = await get_selcom_client().request(
            "GET",
            "/v1/checkout/order-status",
            {"order_id": order_id},
        )
        selcom_success(response)
    except SelcomError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    data = response.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise HTTPException(
            status_code=502,
            detail="Selcom haikurudisha hali ya oda.",
        )
    result = data[0]
    status = result.get("payment_status")
    if not isinstance(status, str) or not status:
        raise HTTPException(
            status_code=502,
            detail="Selcom haikurudisha hali sahihi ya malipo.",
        )
    if str(result.get("amount", "")) != str(order["amount"]):
        raise HTTPException(
            status_code=502,
            detail="Kiasi kilichorudishwa na Selcom hakilingani na oda.",
        )
    if result.get("order_id") != order_id:
        raise HTTPException(
            status_code=502,
            detail="Namba ya oda kutoka Selcom haifanani.",
        )

    with db_connection() as connection:
        connection.execute(
            "UPDATE orders SET payment_status = ? WHERE order_id = ?",
            (status, order_id),
        )
    return {
        "order_id": order_id,
        "payment_status": status,
        "amount": order["amount"],
    }


@app.post("/api/orders/{order_id}/demo-complete")
def demo_complete(order_id: str):
    if not is_demo_mode():
        raise HTTPException(
            status_code=404,
            detail="Endpoint ya demo haijajwekwa wakati mfumo wa malipo halisi unapofanya kazi.",
        )
    order = get_order(order_id)
    if not order["order_id"].startswith("DEMO"):
        raise HTTPException(status_code=400, detail="Hili si oda ya demo.")
    with db_connection() as connection:
        connection.execute(
            "UPDATE orders SET payment_status = 'COMPLETED' WHERE order_id = ?",
            (order_id,),
        )
    return {
        "order_id": order_id,
        "payment_status": "COMPLETED",
        "amount": order["amount"],
        "demo": True,
    }


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
