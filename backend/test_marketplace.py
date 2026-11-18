import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main


class MarketplaceFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = main.DATABASE_PATH
        main.DATABASE_PATH = Path(self.temp_dir.name) / "marketplace.sqlite3"
        self.environment = patch.dict(
            os.environ,
            {
                "ADMIN_SETUP_TOKEN": "test-admin-setup-token-long-enough",
                "DATABASE_URL": "",
                "DEMO_MODE": "true",
            },
            clear=False,
        )
        self.environment.start()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        main.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

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
            product = seller_client.post(
                "/api/products",
                json={"name": "Kikombe cha kahawa", "amount": 5000, "cost_amount": 2500},
            )
            self.assertEqual(product.status_code, 201)
            product_id = product.json()["id"]

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
                self.assertEqual(receipt["bank_branch"], "Mwanza")
                completed = buyer_client.post(f"/api/orders/{order_id}/demo-complete")
                self.assertEqual(completed.status_code, 200)
                self.assertEqual(completed.json()["platform_fee"], 250)
            dashboard = seller_client.get("/api/dashboard").json()
            self.assertEqual(dashboard["gross_sales"], 5000)
            self.assertEqual(dashboard["platform_fees"], 250)
            self.assertEqual(dashboard["net_profit"], 2250)

            archived = seller_client.post(f"/api/sales/{order_id}/archive")
            self.assertEqual(archived.status_code, 200)
            self.assertEqual(seller_client.get("/api/sales").json(), [])

        admin_sales = self.client.get("/api/sales").json()
        archived_sale = next(row for row in admin_sales if row["order_id"] == order_id)
        self.assertEqual(archived_sale["platform_fee"], 250)
        self.assertIsNotNone(archived_sale["archived_at"])

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


if __name__ == "__main__":
    unittest.main()
