import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import main
from backend.clickpesa import create_payload_checksum
from services.admin.main import app as admin_app
from services.catalog.main import app as catalog_app
from services.identity.main import app as identity_app
from services.orders.main import app as orders_app


class InProcessAdminClient:
    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=admin_app),
            base_url="http://admin",
        )
        return await self.client.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self.client.__aexit__(exc_type, exc_value, traceback)


class InProcessCatalogClient:
    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=catalog_app),
            base_url="http://catalog",
        )
        return await self.client.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self.client.__aexit__(exc_type, exc_value, traceback)


class InProcessIdentityClient:
    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=identity_app),
            base_url="http://identity",
        )
        return await self.client.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self.client.__aexit__(exc_type, exc_value, traceback)


class InProcessOrdersClient:
    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=orders_app),
            base_url="http://orders",
        )
        return await self.client.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self.client.__aexit__(exc_type, exc_value, traceback)


class MarketplaceFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = main.DATABASE_PATH
        main.DATABASE_PATH = Path(self.temp_dir.name) / "marketplace.sqlite3"
        self.environment = patch.dict(
            os.environ,
            {
                "ADMIN_SETUP_TOKEN": "test-admin-setup-token-long-enough",
                "ADMIN_SERVICE_TOKEN": "test-admin-service-token",
                "ADMIN_SERVICE_URL": "http://admin",
                "CATALOG_SERVICE_TOKEN": "test-catalog-service-token",
                "CATALOG_SERVICE_URL": "http://catalog",
                "IDENTITY_SERVICE_TOKEN": "test-identity-service-token",
                "IDENTITY_SERVICE_URL": "http://identity",
                "ORDERS_SERVICE_TOKEN": "test-orders-service-token",
                "ORDERS_SERVICE_URL": "http://orders",
                "DATABASE_URL": "",
                "REDIS_HOST": "",
                "REDIS_PORT": "",
                "DEMO_MODE": "true",
                "EMAIL_PROVIDER": "resend",
                "RESEND_API_KEY": "",
                "EMAIL_FROM": "",
                "NOTIFICATION_SERVICE_URL": "",
                "NOTIFICATION_SERVICE_TOKEN": "",
                "FRONTEND_URL": "",
                "PUBLIC_APP_URL": "",
            },
            clear=False,
        )
        self.environment.start()
        self.email_sender_patch = patch("backend.main.send_smtp_email")
        self.email_sender = self.email_sender_patch.start()
        self.admin_client_patch = patch(
            "backend.main.create_admin_service_client",
            InProcessAdminClient,
        )
        self.admin_client_patch.start()
        self.catalog_client_patch = patch(
            "backend.main.create_catalog_service_client",
            InProcessCatalogClient,
        )
        self.catalog_client_patch.start()
        self.identity_client_patch = patch(
            "backend.main.create_identity_service_client",
            InProcessIdentityClient,
        )
        self.identity_client_patch.start()
        self.orders_client_patch = patch(
            "backend.main.create_orders_service_client",
            InProcessOrdersClient,
        )
        self.orders_client_patch.start()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.orders_client_patch.stop()
        self.identity_client_patch.stop()
        self.admin_client_patch.stop()
        self.catalog_client_patch.stop()
        self.email_sender_patch.stop()
        self.environment.stop()
        main.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_frontend_public_files_are_served(self):
        for path in ("manifest.webmanifest", "sw.js"):
            expected = (main.FRONTEND_DIST / path).read_bytes()
            response = self.client.get(f"/{path}")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, expected)

    def test_static_assets_get_long_lived_cache_headers(self):
        with tempfile.TemporaryDirectory() as asset_dir:
            Path(asset_dir, "app-123abc.js").write_text("app")
            asset_app = FastAPI()
            asset_app.mount(
                "/assets",
                main.CachedStaticFiles(directory=asset_dir),
                name="assets",
            )
            with TestClient(asset_app) as asset_client:
                response = asset_client.get("/assets/app-123abc.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=31536000, immutable",
        )

    def test_postgres_connections_are_returned_to_pool(self):
        raw_connection = MagicMock()
        pool = MagicMock()
        pool.getconn.return_value = raw_connection

        with patch.dict(os.environ, {"DATABASE_URL": "postgres://db"}):
            with patch.object(main, "get_database_pool", return_value=pool) as get_pool:
                connection = main.connect_db()

        self.assertTrue(connection.postgres)
        get_pool.assert_called_once_with("postgresql://db")
        pool.getconn.assert_called_once_with(
            timeout=main.DATABASE_POOL_TIMEOUT_SECONDS
        )
        with self.assertRaises(RuntimeError):
            with connection:
                raise RuntimeError("test transaction failure")

        raw_connection.rollback.assert_called_once_with()
        pool.putconn.assert_called_once_with(raw_connection)

    def test_public_product_lists_are_cached_in_redis(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}
                self.get_calls = 0
                self.set_calls = 0

            def get(self, key):
                self.get_calls += 1
                return self.values.get(key)

            def setex(self, key, ttl, value):
                self.set_calls += 1
                self.values[key] = value

        redis_client = FakeRedis()
        with patch.object(main, "get_redis_client", return_value=redis_client):
            first = self.client.get("/api/products")
            second = self.client.get("/api/products")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(redis_client.get_calls, 2)
        self.assertEqual(redis_client.set_calls, 1)

    def test_redis_cache_failure_does_not_break_product_list(self):
        class FailingRedis:
            def get(self, _key):
                raise main.RedisError("cache unavailable")

            def setex(self, _key, _ttl, _value):
                raise main.RedisError("cache unavailable")

        with patch.object(main, "get_redis_client", return_value=FailingRedis()):
            response = self.client.get("/api/products")

        self.assertEqual(response.status_code, 200)

    def test_partial_redis_configuration_is_rejected(self):
        with patch.dict(
            os.environ,
            {"REDIS_HOST": "cache.internal", "REDIS_PORT": ""},
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "REDIS_HOST and REDIS_PORT must be set together",
            ):
                main.get_redis_client()

    def test_single_service_gateway_uses_in_process_apis(self):
        service_patches = (
            self.admin_client_patch,
            self.catalog_client_patch,
            self.identity_client_patch,
            self.orders_client_patch,
        )
        for service_patch in service_patches:
            service_patch.stop()

        try:
            with patch.dict(
                os.environ,
                {
                    "ADMIN_SERVICE_URL": "",
                    "CATALOG_SERVICE_URL": "",
                    "IDENTITY_SERVICE_URL": "",
                    "ORDERS_SERVICE_URL": "",
                },
            ):
                with TestClient(main.app) as client:
                    status = client.get("/api/auth/status")
                    products = client.get("/api/products?category=Vifaa")
                    categories = client.get("/api/categories")
                    admin = client.get("/api/admin/sellers")
                    orders = client.get("/api/orders/history")
                    setup = client.post(
                        "/api/auth/setup",
                        json={
                            "setup_token": "test-admin-setup-token-long-enough",
                            "full_name": "Main Admin",
                            "email": "admin@example.com",
                            "password": "admin-password-long",
                        },
                    )
                    login = client.post(
                        "/api/auth/login",
                        json={
                            "email": "admin@example.com",
                            "password": "admin-password-long",
                        },
                    )
                    admin_after_login = client.get("/api/admin/sellers")

            self.assertEqual(status.status_code, 200)
            self.assertTrue(status.json()["setup_required"])
            self.assertEqual(products.status_code, 200)
            self.assertEqual(categories.status_code, 200)
            self.assertEqual(admin.status_code, 401)
            self.assertEqual(orders.status_code, 401)
            self.assertEqual(setup.status_code, 200, setup.text)
            self.assertEqual(login.status_code, 200)
            self.assertEqual(admin_after_login.status_code, 200)
        finally:
            for service_patch in service_patches:
                service_patch.start()

    def test_password_changes_and_resets_revoke_sessions(self):
        registration = self.client.post(
            "/api/auth/customer/register",
            json={
                "full_name": "Buyer One",
                "email": "buyer@example.com",
                "password": "buyer-password-long",
            },
        )
        self.assertEqual(registration.status_code, 201)
        self.assertEqual(self.email_sender.call_count, 1)
        verification_message = self.email_sender.call_args.args[2]
        verification_match = re.search(
            r"/verify-email\?token=([A-Za-z0-9_-]+)",
            verification_message,
        )
        self.assertIsNotNone(verification_match)
        verified = self.client.get(
            "/api/auth/verify-email",
            params={"token": verification_match.group(1)},
        )
        self.assertEqual(verified.status_code, 200)
        self.assertEqual(self.email_sender.call_count, 2)

        current_client = TestClient(main.app)
        other_client = TestClient(main.app)
        with current_client, other_client:
            for client in (current_client, other_client):
                self.assertEqual(
                    client.post(
                        "/api/auth/login",
                        json={
                            "email": "buyer@example.com",
                            "password": "buyer-password-long",
                        },
                    ).status_code,
                    200,
                )

            old_token = current_client.cookies.get(main.SESSION_COOKIE)
            changed = current_client.put(
                "/api/auth/password",
                json={
                    "current_password": "buyer-password-long",
                    "new_password": "buyer-new-password-long",
                },
            )
            self.assertEqual(changed.status_code, 200)
            self.assertIn("nenosiri", self.email_sender.call_args.args[1].lower())
            self.assertNotIn(
                "buyer-new-password-long",
                self.email_sender.call_args.args[2],
            )
            self.assertNotEqual(
                old_token,
                current_client.cookies.get(main.SESSION_COOKIE),
            )
            self.assertIsNotNone(
                current_client.get("/api/auth/status").json()["user"]
            )
            self.assertIsNone(other_client.get("/api/auth/status").json()["user"])

            reset_token = "test-reset-token-" + ("x" * 32)
            reset_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()
            with main.db_connection() as connection:
                connection.execute(
                    """
                    UPDATE users
                    SET reset_token_hash = ?, reset_expires = ?
                    WHERE email = ?
                    """,
                    (reset_hash, time.time() + 3600, "buyer@example.com"),
                )

            reset = current_client.post(
                "/api/auth/password-reset/confirm",
                json={
                    "reset_token": reset_token,
                    "new_password": "buyer-reset-password-long",
                },
            )
            self.assertEqual(reset.status_code, 200)
            self.assertEqual(self.email_sender.call_count, 4)
            self.assertIsNone(current_client.get("/api/auth/status").json()["user"])

    def test_csv_formula_cells_are_neutralized(self):
        dangerous_values = (
            "=1+1",
            "+SUM(A1:A2)",
            "-1+2",
            "@SUM(A1:A2)",
            " \t=1+1",
            "\t=1+1",
            "\r=1+1",
            "\n=1+1",
        )
        for value in dangerous_values:
            with self.subTest(value=value):
                self.assertEqual(main.spreadsheet_safe_cell(value), "'" + value)

        self.assertEqual(main.spreadsheet_safe_cell(-125), -125)
        self.assertEqual(main.spreadsheet_safe_cell("Kifaa salama"), "Kifaa salama")

    def test_integration_status_and_password_reset(self):
        status = self.client.get("/api/config/integrations").json()
        self.assertFalse(status["live_payments_enabled"])
        self.assertFalse(status["clickpesa_live_enabled"])
        self.assertFalse(status["smtp_configured"])
        with patch.dict(
            os.environ,
            {
                "DEMO_MODE": "false",
                "CLICKPESA_CLIENT_ID": "clickpesa-client",
                "CLICKPESA_API_KEY": "clickpesa-key",
                "CLICKPESA_CHECKSUM_KEY": "clickpesa-checksum",
                "CLICKPESA_LIVE_ENABLED": "true",
                "CLICKPESA_MANUAL_PAYOUTS_CONFIRMED": "true",
            },
        ):
            enabled_status = self.client.get("/api/config/integrations").json()
        self.assertTrue(enabled_status["live_payments_enabled"])
        self.assertTrue(enabled_status["clickpesa_live_enabled"])

        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200)
        self.email_sender.reset_mock()
        reset_request = self.client.post(
            "/api/auth/password-reset/request",
            json={"email": "admin@example.com"},
        )
        unknown_reset_request = self.client.post(
            "/api/auth/password-reset/request",
            json={"email": "missing@example.com"},
        )
        self.assertEqual(reset_request.status_code, 200)
        self.assertEqual(unknown_reset_request.status_code, 200)
        self.assertEqual(
            reset_request.json()["message"],
            unknown_reset_request.json()["message"],
        )
        self.assertEqual(self.email_sender.call_count, 1)

    def test_password_reset_throttle_failure_and_expired_token(self):
        registration = self.client.post(
            "/api/auth/customer/register",
            json={
                "full_name": "Reset Buyer",
                "email": "reset-buyer@example.com",
                "password": "reset-buyer-password-long",
            },
        )
        self.assertEqual(registration.status_code, 201)
        self.email_sender.reset_mock()

        request_payload = {"email": "reset-buyer@example.com"}
        first_request = self.client.post(
            "/api/auth/password-reset/request",
            json=request_payload,
        )
        repeat_request = self.client.post(
            "/api/auth/password-reset/request",
            json=request_payload,
        )
        self.assertEqual(first_request.status_code, 200)
        self.assertEqual(
            first_request.json()["message"],
            repeat_request.json()["message"],
        )
        self.assertEqual(self.email_sender.call_count, 1)
        reset_text = self.email_sender.call_args.args[2]
        token_match = re.search(r"/reset-password\?token=([A-Za-z0-9_-]+)", reset_text)
        self.assertIsNotNone(token_match)

        with main.db_connection() as connection:
            connection.execute(
                "UPDATE users SET reset_expires = ? WHERE email = ?",
                (time.time() - 1, "reset-buyer@example.com"),
            )
        expired = self.client.post(
            "/api/auth/password-reset/confirm",
            json={
                "reset_token": token_match.group(1),
                "new_password": "reset-buyer-new-password",
            },
        )
        self.assertEqual(expired.status_code, 400)

        with main.db_connection() as connection:
            connection.execute(
                """
                UPDATE users
                SET reset_token_hash = '', reset_expires = 0
                WHERE email = ?
                """,
                ("reset-buyer@example.com",),
            )
        self.email_sender.side_effect = main.HTTPException(
            status_code=502,
            detail="Provider response containing private data",
        )
        failed_delivery = self.client.post(
            "/api/auth/password-reset/request",
            json=request_payload,
        )
        self.assertEqual(failed_delivery.status_code, 200)
        self.assertEqual(
            first_request.json()["message"],
            failed_delivery.json()["message"],
        )
        self.assertNotIn("private data", failed_delivery.text)

        self.email_sender.side_effect = None
        self.email_sender.reset_mock()
        retry_after_failure = self.client.post(
            "/api/auth/password-reset/request",
            json=request_payload,
        )
        self.assertEqual(retry_after_failure.status_code, 200)
        self.assertEqual(self.email_sender.call_count, 1)

    def test_seller_approval_sales_commission_and_archive_isolation(self):
        status = self.client.get("/api/auth/status").json()
        self.assertTrue(status["setup_required"])

        invalid_setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "wrong-token-value",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(invalid_setup.status_code, 401)

        admin_setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(admin_setup.status_code, 200)
        self.assertEqual(admin_setup.json()["user"]["role"], "ADMIN")
        self.assertEqual(
            self.client.post(
                "/api/auth/setup",
                json={
                    "setup_token": "test-admin-setup-token-long-enough",
                    "full_name": "Second Admin",
                    "email": "admin2@example.com",
                    "password": "another-password-long",
                },
            ).status_code,
            409,
        )

        registration = self.client.post(
            "/api/auth/register",
            json={
                "full_name": "Seller One",
                "email": "seller@example.com",
                "password": "seller-password-long",
                "shop_name": "Duka la Kwanza",
                "phone": "0712345678",
            },
        )
        self.assertEqual(registration.status_code, 201)
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"email": "seller@example.com", "password": "seller-password-long"},
            ).status_code,
            403,
        )

        sellers = self.client.get("/api/admin/sellers").json()
        seller_id = sellers[0]["id"]
        approval = self.client.patch(
            f"/api/admin/sellers/{seller_id}",
            json={"status": "ACTIVE"},
        )
        self.assertEqual(approval.status_code, 200)

        seller_client = TestClient(main.app)
        with seller_client:
            login = seller_client.post(
                "/api/auth/login",
                json={"email": "seller@example.com", "password": "seller-password-long"},
            )
            self.assertEqual(login.status_code, 200)
            with patch.dict(
                os.environ,
                {"PUBLIC_APP_URL": "https://liparahisi.onrender.com"},
            ):
                product = seller_client.post(
                    "/api/products",
                    json={"name": "Kikombe cha kahawa", "amount": 5000, "cost_amount": 2500, "stock_quantity": 100},
                )
            self.assertEqual(product.status_code, 201)
            product_data = product.json()
            product_id = product_data["id"]
            self.assertEqual(
                product_data["qr_url"],
                f"https://liparahisi.onrender.com/p/{product_id}",
            )
            public_product = seller_client.get(f"/api/products/{product_id}").json()
            self.assertEqual(public_product["name"], "Kikombe cha kahawa")
            self.assertEqual(public_product["amount"], 5000)

            other_seller_id = self.client.post(
                "/api/auth/register",
                json={
                    "full_name": "Seller Two",
                    "email": "seller2@example.com",
                    "password": "seller2-password-long",
                    "shop_name": "Duka la Pili",
                },
            )
            self.assertEqual(other_seller_id.status_code, 201)
            second_seller = next(
                seller
                for seller in self.client.get("/api/admin/sellers").json()
                if seller["email"] == "seller2@example.com"
            )
            self.client.patch(
                f"/api/admin/sellers/{second_seller['id']}",
                json={"status": "ACTIVE"},
            )
            other_client = TestClient(main.app)
            with other_client:
                other_client.post(
                    "/api/auth/login",
                    json={"email": "seller2@example.com", "password": "seller2-password-long"},
                )
                denied = other_client.put(
                    f"/api/products/{product_id}",
                    json={"amount": 1},
                )
                self.assertEqual(denied.status_code, 404)

            buyer_client = TestClient(main.app)
            with buyer_client:
                cart_checkout = buyer_client.post(
                    "/api/cart/checkout",
                    json={
                        "request_key": "c" * 32,
                        "buyer_name": "Buyer Cart",
                        "buyer_email": "buyer-cart@example.com",
                        "buyer_phone": "0712345678",
                        "payment_method": "M-Pesa",
                        "items": [{"product_id": product_id, "quantity": 2}],
                    },
                )
                self.assertEqual(cart_checkout.status_code, 200)
                cart = cart_checkout.json()
                self.assertEqual(len(cart["order_ids"]), 1)
                cart_status = buyer_client.get(
                    f"/api/checkouts/{cart['checkout_id']}"
                ).json()
                self.assertEqual(cart_status["total_amount"], 10000)
                self.assertEqual(len(cart_status["splits"]), 1)
                self.email_sender.reset_mock()
                cart_completed = buyer_client.post(
                    f"/api/checkouts/{cart['checkout_id']}/demo-complete"
                )
                self.assertEqual(cart_completed.status_code, 200)
                self.assertEqual(cart_completed.json()["payment_status"], "COMPLETED")
                self.assertEqual(self.email_sender.call_count, 2)
                cart_split_order_id = cart_completed.json()["splits"][0]["order_id"]
                repeated_cart_completion = buyer_client.post(
                    f"/api/checkouts/{cart['checkout_id']}/demo-complete"
                )
                self.assertEqual(repeated_cart_completion.status_code, 200)
                self.assertEqual(self.email_sender.call_count, 2)
                checkout_payload = {
                    "request_key": "a" * 32,
                    "buyer_name": "Buyer One",
                    "buyer_email": "buyer@example.com",
                    "buyer_phone": "0712345678",
                    "payment_method": "CRDB Branch",
                    "bank_branch": "Mwanza",
                }
                with patch.dict(os.environ, {"DEMO_MODE": "false"}):
                    refused_live_checkout = buyer_client.post(
                        f"/api/products/{product_id}/checkout",
                        json={**checkout_payload, "request_key": "b" * 32},
                    )
                self.assertEqual(refused_live_checkout.status_code, 503)
                checkout = buyer_client.post(
                    f"/api/products/{product_id}/checkout",
                    json=checkout_payload,
                )
                self.assertEqual(checkout.status_code, 200)
                order_id = checkout.json()["order_id"]
                receipt = buyer_client.get(f"/api/orders/{order_id}").json()
                self.assertEqual(receipt["payment_status"], "PENDING")
                self.assertNotIn("bank_branch", receipt)
                self.email_sender.reset_mock()
                completed = buyer_client.post(f"/api/orders/{order_id}/demo-complete")
                self.assertEqual(completed.status_code, 200)
                self.assertEqual(completed.json()["platform_fee"], 250)
                self.assertEqual(self.email_sender.call_count, 2)

                class FakeClickPesaClient:
                    async def initiate_ussd_push(self, phone_number, amount, order_reference):
                        if phone_number != "255712345678" or amount != 5000:
                            raise AssertionError("Unexpected ClickPesa payment arguments.")
                        return {
                            "id": "clickpesa-payment-id",
                            "status": "PROCESSING",
                            "orderReference": order_reference,
                        }

                with patch.dict(
                    os.environ,
                    {
                        "DEMO_MODE": "false",
                        "CLICKPESA_CLIENT_ID": "clickpesa-client",
                        "CLICKPESA_API_KEY": "clickpesa-key",
                        "CLICKPESA_CHECKSUM_KEY": "clickpesa-checksum",
                        "CLICKPESA_LIVE_ENABLED": "true",
                        "CLICKPESA_MANUAL_PAYOUTS_CONFIRMED": "true",
                    },
                ), patch(
                    "backend.main.get_clickpesa_client",
                    return_value=FakeClickPesaClient(),
                ):
                    live_checkout = buyer_client.post(
                        f"/api/products/{product_id}/checkout",
                        json={
                            **checkout_payload,
                            "request_key": "d" * 32,
                            "payment_method": "M-Pesa",
                        },
                    )
                self.assertEqual(live_checkout.status_code, 200, live_checkout.text)
                self.assertFalse(live_checkout.json()["demo"])
                self.assertEqual(live_checkout.json()["payment_status"], "PENDING")
                live_order_id = live_checkout.json()["order_id"]
                with main.db_connection() as connection:
                    live_order = connection.execute(
                        "SELECT provider_reference, checkout_url FROM orders WHERE order_id = ?",
                        (live_order_id,),
                    ).fetchone()
                    stock_before_callback = connection.execute(
                        "SELECT stock_quantity FROM products WHERE id = ?",
                        (product_id,),
                    ).fetchone()["stock_quantity"]
                self.assertTrue(live_order["provider_reference"].startswith("LR"))
                self.assertNotIn("&demo=1", live_order["checkout_url"])

                callback_payload = {
                    "event": "PAYMENT RECEIVED",
                    "data": {
                        "id": "clickpesa-payment-id",
                        "status": "SUCCESS",
                        "orderReference": live_order["provider_reference"],
                        "collectedAmount": "5000",
                        "collectedCurrency": "TZS",
                        "paymentReference": "clickpesa-reference",
                        "clientId": "clickpesa-client",
                    },
                }
                callback_payload["checksum"] = create_payload_checksum(
                    "clickpesa-checksum",
                    callback_payload,
                )
                self.email_sender.reset_mock()
                with patch.dict(
                    os.environ,
                    {
                        "CLICKPESA_CLIENT_ID": "clickpesa-client",
                        "CLICKPESA_CHECKSUM_KEY": "clickpesa-checksum",
                    },
                ):
                    callback = buyer_client.post(
                        "/api/webhooks/clickpesa",
                        json=callback_payload,
                    )
                    self.assertEqual(callback.status_code, 200, callback.text)
                    repeated_callback = buyer_client.post(
                        "/api/webhooks/clickpesa",
                        json=callback_payload,
                    )
                    self.assertEqual(repeated_callback.status_code, 200, repeated_callback.text)
                self.assertEqual(self.email_sender.call_count, 2)
                sent_emails = {
                    call.args[0]: call.args[1]
                    for call in self.email_sender.call_args_list
                }
                self.assertIn("buyer@example.com", sent_emails)
                self.assertIn("seller@example.com", sent_emails)
                self.assertIn("Risiti", sent_emails["buyer@example.com"])
                self.assertIn("Malipo ya mteja", sent_emails["seller@example.com"])
                live_receipt = buyer_client.get(
                    f"/api/orders/{live_order_id}"
                ).json()
                self.assertEqual(live_receipt["payment_status"], "COMPLETED")
                with main.db_connection() as connection:
                    stock_after_callback = connection.execute(
                        "SELECT stock_quantity FROM products WHERE id = ?",
                        (product_id,),
                    ).fetchone()["stock_quantity"]
                self.assertEqual(stock_after_callback, stock_before_callback - 1)

                with patch.dict(
                    os.environ,
                    {
                        "DEMO_MODE": "false",
                        "CLICKPESA_CLIENT_ID": "clickpesa-client",
                        "CLICKPESA_API_KEY": "clickpesa-key",
                        "CLICKPESA_CHECKSUM_KEY": "clickpesa-checksum",
                        "CLICKPESA_LIVE_ENABLED": "true",
                        "CLICKPESA_MANUAL_PAYOUTS_CONFIRMED": "true",
                    },
                ), patch(
                    "backend.main.get_clickpesa_client",
                    return_value=FakeClickPesaClient(),
                ):
                    failed_checkout = buyer_client.post(
                        f"/api/products/{product_id}/checkout",
                        json={
                            **checkout_payload,
                            "request_key": "e" * 32,
                            "payment_method": "M-Pesa",
                        },
                    )
                self.assertEqual(failed_checkout.status_code, 200, failed_checkout.text)
                with main.db_connection() as connection:
                    failed_order = connection.execute(
                        "SELECT provider_reference FROM orders WHERE order_id = ?",
                        (failed_checkout.json()["order_id"],),
                    ).fetchone()
                self.email_sender.reset_mock()
                main.update_clickpesa_reference_status(
                    failed_order["provider_reference"],
                    "FAILED",
                )
                main.update_clickpesa_reference_status(
                    failed_order["provider_reference"],
                    "FAILED",
                )
                self.assertEqual(self.email_sender.call_count, 1)
                self.assertIn("hayakukamilika", self.email_sender.call_args.args[1].lower())
                with main.db_connection() as connection:
                    connection.execute(
                        "DELETE FROM orders WHERE order_id = ?",
                        (failed_checkout.json()["order_id"],),
                    )

                invalid_callback_payload = dict(callback_payload)
                invalid_callback_payload["data"] = {
                    **callback_payload["data"],
                    "collectedAmount": "4999",
                }
                invalid_callback_payload["checksum"] = create_payload_checksum(
                    "clickpesa-checksum",
                    invalid_callback_payload,
                )
                with patch.dict(
                    os.environ,
                    {
                        "CLICKPESA_CLIENT_ID": "clickpesa-client",
                        "CLICKPESA_CHECKSUM_KEY": "clickpesa-checksum",
                    },
                ):
                    invalid_callback = buyer_client.post(
                        "/api/webhooks/clickpesa",
                        json=invalid_callback_payload,
                    )
                self.assertEqual(invalid_callback.status_code, 422)
            dashboard = seller_client.get("/api/dashboard").json()
            self.assertEqual(dashboard["gross_sales"], 20000)
            self.assertEqual(dashboard["platform_fees"], 1000)
            self.assertEqual(dashboard["net_profit"], 9000)

            archived = seller_client.post(f"/api/sales/{order_id}/archive")
            self.assertEqual(archived.status_code, 200)
            cart_archived = seller_client.post(
                f"/api/sales/{cart_split_order_id}/archive"
            )
            self.assertEqual(cart_archived.status_code, 200)
            live_archived = seller_client.post(
                f"/api/sales/{live_order_id}/archive"
            )
            self.assertEqual(live_archived.status_code, 200)
            self.assertEqual(seller_client.get("/api/sales").json(), [])

        admin_sales = self.client.get("/api/sales").json()
        archived_sale = next(row for row in admin_sales if row["order_id"] == order_id)
        self.assertEqual(archived_sale["platform_fee"], 250)
        self.assertIsNotNone(archived_sale["archived_at"])

    def test_public_qr_url_rejects_lan_address(self):
        with patch.dict(
            os.environ,
            {"PUBLIC_APP_URL": "http://192.168.1.11:8000"},
        ):
            self.assertEqual(
                main.public_app_url("http://192.168.1.11:8000"),
                "https://liparahisi.onrender.com",
            )

        with patch.dict(
            os.environ,
            {"PUBLIC_APP_URL": "https://liparahisi.onrender.com"},
        ):
            self.assertEqual(
                main.public_app_url("http://192.168.1.11:8000"),
                "https://liparahisi.onrender.com",
            )

    def test_buyer_catalog_storefront_orders_and_support_features(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200)
        category = self.client.post("/api/categories", json={"name": "Vifaa"})
        self.assertEqual(category.status_code, 200)

        registration = self.client.post(
            "/api/auth/register",
            json={
                "full_name": "Seller One",
                "email": "seller@example.com",
                "password": "seller-password-long",
                "shop_name": "Duka la Kwanza",
            },
        )
        self.assertEqual(registration.status_code, 201)
        seller = self.client.get("/api/admin/sellers").json()[0]
        self.assertEqual(
            self.client.patch(
                f"/api/admin/sellers/{seller['id']}",
                json={"status": "ACTIVE"},
            ).status_code,
            200,
        )
        seller_client = TestClient(main.app)
        with seller_client:
            login = seller_client.post(
                "/api/auth/login",
                json={"email": "seller@example.com", "password": "seller-password-long"},
            )
            self.assertEqual(login.status_code, 200)
            product = seller_client.post(
                "/api/products",
                json={
                    "name": "Kifaa cha majaribio",
                    "description": "Maelezo ya bidhaa",
                    "image_url": "https://images.example.com/item.png",
                    "category": "Vifaa",
                    "amount": 3000,
                    "stock_quantity": 6,
                    "low_stock_threshold": 6,
                },
            )
            self.assertEqual(product.status_code, 201)
            product_id = product.json()["id"]
            store = seller_client.put(
                "/api/storefront",
                json={
                    "shop_name": "Duka la Kwanza",
                    "store_description": "Bidhaa nzuri",
                    "store_image_url": "",
                },
            )
            self.assertEqual(store.status_code, 200)
            storefront = seller_client.get(
                f"/api/storefronts/{store.json()['store_slug']}"
            )
            self.assertEqual(storefront.status_code, 200)
            self.assertEqual(storefront.json()["products"][0]["id"], product_id)
            self.assertIn("name,description", seller_client.get("/api/products/export").text)
            self.assertEqual(
                seller_client.post(
                    "/api/products/import",
                    json={"csv_text": "name,amount,category\nCSV item,500,Vifaa\n"},
                ).json()["imported_count"],
                1,
            )
            low_stock_ids = {
                item["id"] for item in seller_client.get("/api/inventory/low-stock").json()
            }
            self.assertIn(product_id, low_stock_ids)

        guest_client = TestClient(main.app)
        public_products = guest_client.get("/api/products?category=Vifaa")
        self.assertEqual(public_products.status_code, 200)
        self.assertIn(product_id, {item["id"] for item in public_products.json()})

        buyer_registration = guest_client.post(
            "/api/auth/customer/register",
            json={
                "full_name": "Buyer One",
                "email": "buyer@example.com",
                "password": "buyer-password-long",
            },
        )
        self.assertEqual(buyer_registration.status_code, 201)
        buyer_client = TestClient(main.app)
        with buyer_client:
            self.assertEqual(
                buyer_client.post(
                    "/api/auth/login",
                    json={"email": "buyer@example.com", "password": "buyer-password-long"},
                ).status_code,
                200,
            )
            checkout = buyer_client.post(
                f"/api/products/{product_id}/checkout",
                json={
                    "request_key": "d" * 32,
                    "buyer_name": "Buyer One",
                    "buyer_email": "buyer@example.com",
                    "buyer_phone": "0712345678",
                    "payment_method": "M-Pesa",
                    "delivery_address": "Dar es Salaam",
                },
            )
            self.assertEqual(checkout.status_code, 200)
            order_id = checkout.json()["order_id"]
            self.assertEqual(
                buyer_client.post(f"/api/orders/{order_id}/demo-complete").status_code,
                200,
            )
            buyer_order_status = buyer_client.get(f"/api/orders/{order_id}").json()
            self.assertEqual(buyer_order_status["buyer_name"], "Buyer One")
            self.assertEqual(buyer_order_status["delivery_address"], "Dar es Salaam")
            guest_order_status = guest_client.get(f"/api/orders/{order_id}").json()
            for field in ("buyer_name", "delivery_address", "tracking_code"):
                self.assertNotIn(field, guest_order_status)
            history = buyer_client.get("/api/orders/history")
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json()[0]["order_id"], order_id)
            self.assertEqual(
                buyer_client.post(
                    f"/api/orders/{order_id}/products/{product_id}/review",
                    json={"rating": 5, "comment": "Nzuri"},
                ).status_code,
                200,
            )
            self.assertEqual(
                buyer_client.post(
                    "/api/support-tickets",
                    json={
                        "email": "buyer@example.com",
                        "subject": "Swali kuhusu oda",
                        "message": "Nahitaji msaada kuhusu ufuatiliaji wa oda yangu.",
                    },
                ).status_code,
                201,
            )

        with seller_client:
            delivery = seller_client.put(
                f"/api/orders/{order_id}/delivery",
                json={"delivery_status": "PROCESSING", "tracking_code": ""},
            )
            self.assertEqual(delivery.status_code, 200)
            invalid_delivery = seller_client.put(
                f"/api/orders/{order_id}/delivery",
                json={"delivery_status": "DELIVERED", "tracking_code": "TRACK-1"},
            )
            self.assertEqual(invalid_delivery.status_code, 409)
            sales = seller_client.get("/api/sales?status=COMPLETED")
            self.assertEqual(sales.status_code, 200)
            self.assertEqual(sales.json()[0]["delivery_status"], "PROCESSING")
            self.assertEqual(sales.json()[0]["delivery_address"], "Dar es Salaam")
            dated_sales = seller_client.get(
                "/api/sales?start_date=2020-01-01&end_date=2099-12-31"
            )
            self.assertEqual(dated_sales.status_code, 200)
            self.assertEqual(dated_sales.json()[0]["order_id"], order_id)
            invalid_date = seller_client.get(
                "/api/sales?start_date=not-a-date"
            )
            self.assertEqual(invalid_date.status_code, 422)

        cart_response = buyer_client.post(
            "/api/cart/checkout",
            json={
                "request_key": "e" * 32,
                "buyer_name": "Buyer One",
                "buyer_email": "buyer@example.com",
                "buyer_phone": "0712345678",
                "payment_method": "Halo Pesa",
                "items": [{"product_id": product_id, "quantity": 3}],
            },
        )
        self.assertEqual(cart_response.status_code, 200)
        cart_status = buyer_client.get(
            f"/api/checkouts/{cart_response.json()['checkout_id']}"
        ).json()
        self.assertEqual(cart_status["total_amount"], 9000)
        self.assertEqual(cart_status["platform_fee"], 450)
        self.assertEqual(cart_status["commission_percent"], 5)
        self.assertEqual(cart_status["payment_method"], "Halo Pesa")
        self.assertEqual(cart_status["buyer_email"], "buyer@example.com")
        guest_cart_status = guest_client.get(
            f"/api/checkouts/{cart_response.json()['checkout_id']}"
        ).json()
        for field in ("buyer_name", "buyer_email", "buyer_phone", "bank_branch"):
            self.assertNotIn(field, guest_cart_status)
        cart_completed = buyer_client.post(
            f"/api/checkouts/{cart_response.json()['checkout_id']}/demo-complete"
        )
        self.assertEqual(cart_completed.status_code, 200)
        self.assertEqual(cart_completed.json()["splits"][0]["seller_payout"], 8550)

        cancellation = buyer_client.post(
            f"/api/orders/{order_id}/cancellation",
            json={"reason": "Nimebadilisha uamuzi kuhusu oda."},
        )
        self.assertEqual(cancellation.status_code, 200)
        cancellation_row = self.client.get("/api/admin/cancellations").json()[0]
        self.assertEqual(
            self.client.patch(
                f"/api/admin/cancellations/{cancellation_row['id']}",
                json={"status": "APPROVED", "admin_note": "Imeidhinishwa demo."},
            ).status_code,
            200,
        )
        self.assertEqual(
            buyer_client.get(f"/api/orders/{order_id}").json()["payment_status"],
            "CANCELLED",
        )
        pending_reviews = self.client.get("/api/admin/reviews")
        self.assertEqual(pending_reviews.status_code, 200)
        review_id = pending_reviews.json()[0]["id"]
        self.assertEqual(
            self.client.patch(
                f"/api/admin/reviews/{review_id}",
                json={"status": "APPROVED"},
            ).status_code,
            200,
        )
        product_details = guest_client.get(f"/api/products/{product_id}").json()
        self.assertEqual(product_details["average_rating"], 5)

        admin_tickets = self.client.get("/api/admin/support-tickets")
        self.assertEqual(admin_tickets.status_code, 200)
        ticket_id = admin_tickets.json()[0]["id"]
        self.assertEqual(
            self.client.patch(
                f"/api/admin/support-tickets/{ticket_id}",
                json={"status": "RESOLVED", "admin_response": "Tumeangalia."},
            ).status_code,
            200,
        )

    def test_product_image_upload_validation_and_seller_ownership(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200)
        registration = self.client.post(
            "/api/auth/register",
            json={
                "full_name": "Seller One",
                "email": "seller@example.com",
                "password": "seller-password-long",
                "shop_name": "Duka la Kwanza",
            },
        )
        self.assertEqual(registration.status_code, 201)
        seller = self.client.get("/api/admin/sellers").json()[0]
        self.assertEqual(
            self.client.patch(
                f"/api/admin/sellers/{seller['id']}",
                json={"status": "ACTIVE"},
            ).status_code,
            200,
        )

        seller_client = TestClient(main.app)
        with seller_client:
            self.assertEqual(
                seller_client.post(
                    "/api/auth/login",
                    json={"email": "seller@example.com", "password": "seller-password-long"},
                ).status_code,
                200,
            )
            product = seller_client.post(
                "/api/products",
                json={"name": "Bidhaa yenye picha", "amount": 1000, "stock_quantity": 1},
            )
            self.assertEqual(product.status_code, 201)
            product_id = product.json()["id"]
            image_data = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
            )

            upload = seller_client.post(
                f"/api/products/{product_id}/image",
                files={"image": ("product.png", image_data, "image/png")},
            )
            self.assertEqual(upload.status_code, 200)
            self.assertEqual(
                seller_client.get(f"/api/products/{product_id}").json()["image_url"],
                f"/api/products/{product_id}/image",
            )

            public_client = TestClient(main.app)
            public_image = public_client.get(f"/api/products/{product_id}/image")
            self.assertEqual(public_image.status_code, 200)
            self.assertEqual(public_image.headers["content-type"], "image/png")
            self.assertEqual(public_image.content, image_data)

            invalid_signature = seller_client.post(
                f"/api/products/{product_id}/image",
                files={"image": ("invalid.png", b"not a png", "image/png")},
            )
            self.assertEqual(invalid_signature.status_code, 422)
            invalid_type = seller_client.post(
                f"/api/products/{product_id}/image",
                files={"image": ("invalid.txt", b"file", "text/plain")},
            )
            self.assertEqual(invalid_type.status_code, 415)
            oversized = seller_client.post(
                f"/api/products/{product_id}/image",
                files={
                    "image": (
                        "large.png",
                        b"\x89PNG\r\n\x1a\n" + b"x" * main.MAX_PRODUCT_IMAGE_BYTES,
                        "image/png",
                    )
                },
            )
            self.assertEqual(oversized.status_code, 413)

        second_registration = self.client.post(
            "/api/auth/register",
            json={
                "full_name": "Seller Two",
                "email": "seller2@example.com",
                "password": "seller2-password-long",
                "shop_name": "Duka la Pili",
            },
        )
        self.assertEqual(second_registration.status_code, 201)
        second_seller = next(
            item
            for item in self.client.get("/api/admin/sellers").json()
            if item["email"] == "seller2@example.com"
        )
        self.assertEqual(
            self.client.patch(
                f"/api/admin/sellers/{second_seller['id']}",
                json={"status": "ACTIVE"},
            ).status_code,
            200,
        )
        other_client = TestClient(main.app)
        with other_client:
            self.assertEqual(
                other_client.post(
                    "/api/auth/login",
                    json={"email": "seller2@example.com", "password": "seller2-password-long"},
                ).status_code,
                200,
            )
            denied_upload = other_client.post(
                f"/api/products/{product_id}/image",
                files={"image": ("product.png", image_data, "image/png")},
            )
            self.assertEqual(denied_upload.status_code, 404)
            denied_delete = other_client.delete(f"/api/products/{product_id}/image")
            self.assertEqual(denied_delete.status_code, 404)

    def test_existing_sqlite_products_and_orders_are_migrated(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE products (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE orders (
                    order_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    product_id TEXT NOT NULL REFERENCES products(id),
                    amount INTEGER NOT NULL,
                    payment_status TEXT NOT NULL DEFAULT 'PENDING',
                    checkout_url TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE merchant_settings (
                    id INTEGER PRIMARY KEY,
                    password TEXT NOT NULL,
                    phone TEXT
                );
                INSERT INTO products (id, name, amount) VALUES ('old-product', 'Bidhaa ya zamani', 1200);
                INSERT INTO orders (order_id, request_key, product_id, amount)
                    VALUES ('OLD-ORDER', 'old-request-key', 'old-product', 1200);
                """
            )
        finally:
            connection.close()

        main.DATABASE_PATH = legacy_path
        main.initialize_db()
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200)
        products = self.client.get("/api/products").json()
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["name"], "Bidhaa ya zamani")
        sales = self.client.get("/api/sales").json()
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]["order_id"], "OLD-ORDER")
        self.assertEqual(sales[0]["platform_fee"], 0)

    def test_abliner_checkout_and_signed_webhook_are_idempotent(self):
        with patch.dict(
            os.environ,
            {
                "ABLINER_LIVE_ENABLED": "false",
                "ABLINER_API_KEY": "",
                "ABLINER_WEBHOOK_SECRET": "",
            },
        ):
            status = self.client.get("/api/config/integrations").json()
        self.assertFalse(status["abliner_live_enabled"])

        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200)

        seller_client = TestClient(main.app)
        with seller_client:
            registration = seller_client.post(
                "/api/auth/register",
                json={
                    "full_name": "Seller Abliner",
                    "email": "abliner-seller@example.com",
                    "password": "seller-password-long",
                    "shop_name": "Duka Abliner",
                    "phone": "0712345678",
                },
            )
            self.assertEqual(registration.status_code, 201)
            seller = self.client.get("/api/admin/sellers").json()[0]
            approval = self.client.patch(
                f"/api/admin/sellers/{seller['id']}",
                json={"status": "ACTIVE"},
            )
            self.assertEqual(approval.status_code, 200)
            login = seller_client.post(
                "/api/auth/login",
                json={
                    "email": "abliner-seller@example.com",
                    "password": "seller-password-long",
                },
            )
            self.assertEqual(login.status_code, 200)
            product = seller_client.post(
                "/api/products",
                json={
                    "name": "Bidhaa ya Abliner",
                    "amount": 5000,
                    "cost_amount": 2500,
                    "stock_quantity": 5,
                },
            )
            self.assertEqual(product.status_code, 201, product.text)
            product_id = product.json()["id"]

        class FakeAblinerClient:
            def __init__(self):
                self.request = None

            async def create_mobile_deposit(self, **kwargs):
                self.request = kwargs
                return {
                    "id": "abliner-tx-1",
                    "type": "deposit",
                    "status": "pending",
                    "amount": kwargs["amount"],
                    "currency": "TZS",
                    "customer_reference": kwargs["reference"],
                }

        fake_client = FakeAblinerClient()
        buyer_client = TestClient(main.app)
        with buyer_client, patch.dict(
            os.environ,
            {
                "ABLINER_LIVE_ENABLED": "true",
                "ABLINER_API_KEY": "test-api-key",
                "ABLINER_WEBHOOK_SECRET": "test-webhook-secret",
                "PUBLIC_APP_URL": "https://liparahisi.onrender.com",
            },
        ), patch("backend.main.get_abliner_client", return_value=fake_client):
            checkout = buyer_client.post(
                f"/api/products/{product_id}/checkout",
                json={
                    "request_key": "f" * 32,
                    "buyer_name": "Buyer Abliner",
                    "buyer_email": "buyer-abliner@example.com",
                    "buyer_phone": "0712345678",
                    "payment_method": "Abliner Mobile Money",
                },
            )
            self.assertEqual(checkout.status_code, 200, checkout.text)
            self.assertFalse(checkout.json()["demo"])
            self.assertEqual(checkout.json()["payment_status"], "PENDING")
            order_id = checkout.json()["order_id"]
            order = buyer_client.get(f"/api/orders/{order_id}").json()
            self.assertEqual(order["payment_method"], "Abliner Mobile Money")
            self.assertEqual(order["payment_status"], "PENDING")
            self.assertEqual(fake_client.request["phone"], "255712345678")
            self.assertEqual(
                fake_client.request["callback_url"],
                "https://liparahisi.onrender.com/api/webhooks/abliner",
            )

            with main.db_connection() as connection:
                provider_reference = connection.execute(
                    "SELECT provider_reference FROM orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()["provider_reference"]
                stock_before = connection.execute(
                    "SELECT stock_quantity FROM products WHERE id = ?",
                    (product_id,),
                ).fetchone()["stock_quantity"]

            payload = {
                "id": "evt_abliner_1",
                "event": "transaction.completed",
                "data": {
                    "id": "abliner-tx-1",
                    "type": "deposit",
                    "amount": 5000,
                    "currency": "TZS",
                    "status": "completed",
                    "customer_reference": provider_reference,
                },
            }
            raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            timestamp = str(int(time.time()))
            signature = hmac.new(
                b"test-webhook-secret",
                timestamp.encode("ascii") + b"." + raw_body,
                hashlib.sha256,
            ).hexdigest()
            headers = {
                "content-type": "application/json",
                "x-webhook-timestamp": timestamp,
                "x-webhook-signature": signature,
            }
            invalid_callback = buyer_client.post(
                "/api/webhooks/abliner",
                content=raw_body,
                headers={**headers, "x-webhook-signature": "0" * 64},
            )
            self.assertEqual(invalid_callback.status_code, 401)

            with patch("backend.main.send_payment_notification") as payment_notification:
                callback = buyer_client.post(
                    "/api/webhooks/abliner",
                    content=raw_body,
                    headers=headers,
                )
                self.assertEqual(callback.status_code, 200, callback.text)
                repeated_callback = buyer_client.post(
                    "/api/webhooks/abliner",
                    content=raw_body,
                    headers=headers,
                )
                self.assertEqual(repeated_callback.status_code, 200, repeated_callback.text)
                self.assertEqual(payment_notification.call_count, 1)

            completed = buyer_client.get(f"/api/orders/{order_id}").json()
            self.assertEqual(completed["payment_status"], "COMPLETED")
            with main.db_connection() as connection:
                stock_after = connection.execute(
                    "SELECT stock_quantity FROM products WHERE id = ?",
                    (product_id,),
                ).fetchone()["stock_quantity"]
            self.assertEqual(stock_after, stock_before - 1)


if __name__ == "__main__":
    unittest.main()
