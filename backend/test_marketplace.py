import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from cryptography.fernet import Fernet
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
                "PESAPAL_SANDBOX_ENABLED": "false",
                "PESAPAL_CONSUMER_KEY": "",
                "PESAPAL_CONSUMER_SECRET": "",
                "PESAPAL_NOTIFICATION_ID": "",
                "EMAIL_PROVIDER": "resend",
                "RESEND_API_KEY": "",
                "EMAIL_FROM": "",
                "NOTIFICATION_SERVICE_URL": "",
                "NOTIFICATION_SERVICE_TOKEN": "",
                "FRONTEND_URL": "",
                "PUBLIC_APP_URL": "",
                "MFA_ENCRYPTION_KEY": "",
                "OPENAI_API_KEY": "",
                "BUSINESS_AI_PROVIDER": "openai",
                "BACKUP_S3_BUCKET": "",
                "AWS_ACCESS_KEY_ID": "",
                "AWS_SECRET_ACCESS_KEY": "",
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

    def test_tracking_numbers_are_unique_and_initialization_is_backward_compatible(self):
        main.initialize_db()
        with main.db_connection() as connection:
            first = main.generate_tracking_code(connection)
            connection.execute(
                "INSERT INTO products (id, name, amount) VALUES (?, ?, ?)",
                ("MISSINGPRODUCT", "Tracking test product", 1000),
            )
            connection.execute(
                "INSERT INTO orders (order_id, request_key, product_id, amount, tracking_code) "
                "VALUES (?, ?, ?, ?, ?)",
                ("TRACKTEST1", "TRACKREQUEST1", "MISSINGPRODUCT", 1000, first),
            )
            second = main.generate_tracking_code(connection)
        self.assertRegex(first, r"^LR-TZ-\d{4}-\d{6}$")
        self.assertRegex(second, r"^LR-TZ-\d{4}-\d{6}$")
        self.assertNotEqual(first, second)

    def test_admin_dashboard_aggregates_real_metrics_and_restricts_access(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200, setup.text)
        login = self.client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-long"},
        )
        self.assertEqual(login.status_code, 200, login.text)

        with main.db_connection() as connection:
            connection.execute(
                """
                INSERT INTO users
                    (id, full_name, email, password_hash, role, status, kyc_status)
                VALUES
                    ('dashboard-buyer', 'Dashboard Buyer', 'dashboard-buyer@example.com', ?, 'BUYER', 'ACTIVE', 'NOT_VERIFIED'),
                    ('dashboard-seller', 'Dashboard Seller', 'dashboard-seller@example.com', ?, 'SELLER', 'ACTIVE', 'VERIFIED')
                """,
                (main.hash_password("buyer-password-long"), main.hash_password("seller-password-long")),
            )
            connection.execute(
                """
                INSERT INTO businesses (id, owner_id, name, business_type)
                VALUES ('dashboard-business', 'dashboard-seller', 'Dashboard Shop', 'Retail')
                """
            )
            connection.execute(
                """
                INSERT INTO products (id, name, amount, seller_id, is_active, stock_quantity, low_stock_threshold)
                VALUES ('dashboard-product', 'Dashboard Product', 2500, 'dashboard-seller', 1, 2, 5)
                """
            )
            connection.execute(
                """
                INSERT INTO orders
                    (order_id, request_key, product_id, seller_id, buyer_id, amount,
                     payment_method, payment_status, delivery_status)
                VALUES
                    ('DASHBOARDORDER1', 'dashboard-request-1', 'dashboard-product',
                     'dashboard-seller', 'dashboard-buyer', 2500, 'M-Pesa',
                     'COMPLETED', 'READY_FOR_PICKUP')
                """
            )
            connection.execute(
                """
                INSERT INTO security_alerts
                    (id, subject_type, subject_id, category, risk_score, status)
                VALUES ('dashboard-alert', 'account', 'dashboard-seller', 'ACCOUNT_RISK', 60, 'OPEN')
                """
            )
            connection.execute(
                """
                INSERT INTO audit_log
                    (id, actor_email, action, target_type, target_id)
                VALUES ('dashboard-audit', 'dashboard-buyer@example.com', 'LOGIN_FAILED', 'account', 'dashboard-buyer')
                """
            )

        dashboard_response = self.client.get("/api/admin/dashboard")
        self.assertEqual(dashboard_response.status_code, 200, dashboard_response.text)
        counts = dashboard_response.json()["counts"]
        self.assertEqual(counts["total_users"], 3)
        self.assertEqual(counts["buyers"], 1)
        self.assertEqual(counts["sellers"], 1)
        self.assertEqual(counts["verified_sellers"], 1)
        self.assertEqual(counts["businesses"], 1)
        self.assertEqual(counts["products"], 1)
        self.assertEqual(counts["orders"], 1)
        self.assertEqual(counts["active_deliveries"], 1)
        self.assertEqual(counts["fraud_alerts"], 1)
        self.assertEqual(counts["security_events"], 2)
        self.assertEqual(len(dashboard_response.json()["sales_trend"]), 1)

        seller_client = TestClient(main.app)
        with seller_client:
            seller_login = seller_client.post(
                "/api/auth/login",
                json={
                    "email": "dashboard-seller@example.com",
                    "password": "seller-password-long",
                },
            )
            self.assertEqual(seller_login.status_code, 200, seller_login.text)
            seller_stats = seller_client.get("/api/dashboard")
            self.assertEqual(seller_stats.status_code, 200, seller_stats.text)
            self.assertEqual(seller_stats.json()["total_orders"], 1)
            self.assertEqual(seller_stats.json()["customer_count"], 1)
            self.assertEqual(seller_stats.json()["active_deliveries"], 1)
            self.assertEqual(seller_stats.json()["low_stock_count"], 1)
            self.assertEqual(seller_stats.json()["inventory_units"], 2)
            self.assertEqual(seller_client.get("/api/admin/dashboard").status_code, 403)

        buyer_client = TestClient(main.app)
        with buyer_client:
            buyer_login = buyer_client.post(
                "/api/auth/login",
                json={
                    "email": "dashboard-buyer@example.com",
                    "password": "buyer-password-long",
                },
            )
            self.assertEqual(buyer_login.status_code, 200, buyer_login.text)
            denied = buyer_client.get("/api/admin/dashboard")
            self.assertEqual(denied.status_code, 403)

    def test_delivery_agent_can_claim_ready_order_once(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200, setup.text)
        admin_login = self.client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-long"},
        )
        self.assertEqual(admin_login.status_code, 200, admin_login.text)
        agent_response = self.client.post(
            "/api/admin/delivery-agents",
            json={
                "full_name": "Delivery Agent",
                "email": "delivery-agent@example.com",
                "password": "agent-password-long",
                "phone": "0712345678",
                "vehicle_type": "Motorcycle",
            },
        )
        self.assertEqual(agent_response.status_code, 200, agent_response.text)

        with main.db_connection() as connection:
            connection.execute(
                "INSERT INTO users (id, full_name, email, password_hash, role, status) "
                "VALUES ('ready-order-seller', 'Seller', 'ready-seller@example.com', ?, 'SELLER', 'ACTIVE')",
                (main.hash_password("seller-password-long"),),
            )
            connection.execute(
                "INSERT INTO products (id, name, amount, seller_id) VALUES (?, ?, ?, ?)",
                ("ready-order-product", "Ready product", 1000, "ready-order-seller"),
            )
            connection.execute(
                """
                INSERT INTO orders
                    (order_id, request_key, product_id, seller_id, amount, payment_status, delivery_status)
                VALUES
                    ('READYDELIVERY01', 'ready-delivery-request', 'ready-order-product',
                     'ready-order-seller', 1000, 'COMPLETED', 'READY_FOR_PICKUP')
                """
            )

        agent_client = TestClient(main.app)
        with agent_client:
            login = agent_client.post(
                "/api/auth/login",
                json={
                    "email": "delivery-agent@example.com",
                    "password": "agent-password-long",
                },
            )
            self.assertEqual(login.status_code, 200, login.text)
            available = agent_client.get("/api/deliveries/available")
            self.assertEqual(available.status_code, 200, available.text)
            self.assertEqual([item["order_id"] for item in available.json()], ["READYDELIVERY01"])
            accepted = agent_client.post("/api/deliveries/READYDELIVERY01/accept")
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json()["delivery_status"], "AGENT_ASSIGNED")
            conflict = agent_client.post("/api/deliveries/READYDELIVERY01/accept")
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(agent_client.get("/api/deliveries/available").json(), [])
            assigned = agent_client.get("/api/deliveries").json()
            self.assertEqual([item["order_id"] for item in assigned], ["READYDELIVERY01"])

    def test_security_scan_alerts_are_explainable_deduplicated_and_never_suspend(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200, setup.text)
        login = self.client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-long"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        buyer_id = "fraud-scan-buyer"
        with main.db_connection() as connection:
            connection.execute(
                """
                INSERT INTO users (id, full_name, email, password_hash, role, status)
                VALUES (?, ?, ?, ?, 'BUYER', 'ACTIVE')
                """,
                (buyer_id, "Scan Buyer", "scan-buyer@example.com", "unused"),
            )
            seller_id = "fraud-scan-seller"
            product_id = "fraud-scan-product"
            connection.execute(
                """
                INSERT INTO users (id, full_name, email, password_hash, role, status)
                VALUES (?, ?, ?, ?, 'SELLER', 'ACTIVE')
                """,
                (seller_id, "Scan Seller", "scan-seller@example.com", "unused"),
            )
            connection.execute(
                "INSERT INTO products (id, name, amount, seller_id) VALUES (?, ?, ?, ?)",
                (product_id, "Scan product", 1000, seller_id),
            )
            for index in range(3):
                order_id = f"FRAUDSCANORDER{index}"
                connection.execute(
                    """
                    INSERT INTO orders
                        (order_id, request_key, product_id, seller_id, buyer_id, amount,
                         payment_status, delivery_status)
                    VALUES (?, ?, ?, ?, ?, 1000, 'COMPLETED', 'UNFULFILLED')
                    """,
                    (order_id, f"fraud-scan-request-{index}", product_id, seller_id, buyer_id),
                )
                connection.execute(
                    """
                    INSERT INTO cancellation_requests
                        (id, order_id, requester_id, reason)
                    VALUES (?, ?, ?, ?)
                    """,
                    (f"FRAUDSCANCANCEL{index}", order_id, buyer_id, "Test cancellation reason"),
                )
            connection.execute(
                "INSERT INTO login_attempts (email, failed_count, blocked_until) VALUES (?, ?, ?)",
                ("scan-buyer@example.com", 6, time.time() + 300),
            )

        first_scan = self.client.post("/api/admin/security/scan")
        second_scan = self.client.post("/api/admin/security/scan")
        self.assertEqual(first_scan.status_code, 200, first_scan.text)
        self.assertEqual(second_scan.status_code, 200, second_scan.text)
        self.assertEqual(first_scan.json()["ai_analysis"]["status"], "DISABLED")
        alerts = self.client.get(
            "/api/admin/security/alerts?category=BRUTE_FORCE&severity=MEDIUM"
        )
        self.assertEqual(alerts.status_code, 200, alerts.text)
        self.assertEqual(len(alerts.json()), 1)
        alert = alerts.json()[0]
        self.assertEqual(alert["detection_model"], "LOCAL_RULES_V1")
        self.assertEqual(alert["evidence"][0]["failed_count"], 6)
        seller_alerts = self.client.get("/api/admin/security/alerts?category=SELLER_RISK")
        buyer_alerts = self.client.get(
            "/api/admin/security/alerts?category=REPEATED_CANCELLATIONS"
        )
        self.assertEqual(seller_alerts.status_code, 200, seller_alerts.text)
        self.assertEqual(buyer_alerts.status_code, 200, buyer_alerts.text)
        self.assertEqual(seller_alerts.json()[0]["subject_id"], seller_id)
        self.assertEqual(buyer_alerts.json()[0]["subject_id"], buyer_id)
        self.assertTrue(all("not proof of fraud" in reason for reason in seller_alerts.json()[0]["reasons"]))
        with main.db_connection() as connection:
            buyer = connection.execute(
                "SELECT status FROM users WHERE id = ?",
                (buyer_id,),
            ).fetchone()
            alert_count = connection.execute(
                "SELECT COUNT(*) AS count FROM security_alerts WHERE category = 'BRUTE_FORCE'"
            ).fetchone()["count"]
        self.assertEqual(buyer["status"], "ACTIVE")
        self.assertEqual(alert_count, 1)

    def test_ai_assessment_is_stored_separately_from_rule_decisions(self):
        alert_id = "fraud-ai-assessment-alert"
        with main.db_connection() as connection:
            connection.execute(
                """
                INSERT INTO security_alerts
                    (id, subject_type, subject_id, category, risk_score, severity,
                     status, reasons_json, evidence_json, detection_model)
                VALUES (?, 'BUYER', 'buyer-id', 'REPEATED_CANCELLATIONS', 45, 'LOW',
                        'OPEN', '[]', '[]', 'LOCAL_RULES_V1')
                """,
                (alert_id,),
            )
        with patch(
            "backend.main.analyze_security_alerts",
            return_value={
                "status": "SUCCEEDED",
                "model": "gpt-4o-mini",
                "analyzed": 1,
                "skipped": 0,
                "assessments": [
                    {
                        "alert_id": alert_id,
                        "risk_score": 98,
                        "reasons": ["Repeated cancellation requests"],
                    }
                ],
            },
        ):
            result = main.enrich_security_alerts_with_ai([alert_id])

        self.assertEqual(result["status"], "SUCCEEDED")
        with main.db_connection() as connection:
            alert = connection.execute(
                """
                SELECT risk_score, severity, ai_used, ai_risk_score, ai_reasons_json, ai_model
                FROM security_alerts WHERE id = ?
                """,
                (alert_id,),
            ).fetchone()
        self.assertEqual(alert["risk_score"], 45)
        self.assertEqual(alert["severity"], "LOW")
        self.assertEqual(alert["ai_used"], 1)
        self.assertEqual(alert["ai_risk_score"], 98)
        self.assertEqual(json.loads(alert["ai_reasons_json"]), ["Repeated cancellation requests"])
        self.assertEqual(alert["ai_model"], "gpt-4o-mini")

    def test_security_admin_actions_appeals_and_access_control(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200, setup.text)
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "admin-password-long"},
            ).status_code,
            200,
        )
        buyer_id = "fraud-appeal-buyer"
        alert_id = "fraud-appeal-alert"
        with main.db_connection() as connection:
            connection.execute(
                """
                INSERT INTO users (id, full_name, email, password_hash, role, status)
                VALUES (?, ?, ?, ?, 'BUYER', 'ACTIVE')
                """,
                (buyer_id, "Appeal Buyer", "appeal-buyer@example.com", main.hash_password("buyer-password-long")),
            )
            connection.execute(
                """
                INSERT INTO security_alerts
                    (id, subject_type, subject_id, category, risk_score, severity,
                     status, reasons_json, evidence_json, detection_model)
                VALUES (?, 'BUYER', ?, 'REPEATED_CANCELLATIONS', 70, 'HIGH',
                        'OPEN', '[]', '[]', 'LOCAL_RULES_V1')
                """,
                (alert_id, buyer_id),
            )

        self.assertEqual(
            self.client.patch(
                f"/api/admin/security/alerts/{alert_id}",
                json={"action": "SUSPEND", "note": "Manual review confirmed severe abuse."},
            ).status_code,
            200,
        )
        with main.db_connection() as connection:
            suspended_status = connection.execute(
                "SELECT status FROM users WHERE id = ?",
                (buyer_id,),
            ).fetchone()["status"]
        self.assertEqual(suspended_status, "SUSPENDED")

        buyer_client = TestClient(main.app)
        buyer_login = buyer_client.post(
            "/api/auth/login",
            json={"email": "appeal-buyer@example.com", "password": "buyer-password-long"},
        )
        self.assertEqual(buyer_login.status_code, 200, buyer_login.text)
        self.assertEqual(buyer_login.json()["user"]["status"], "SUSPENDED")
        self.assertEqual(
            buyer_client.get("/api/auth/status").json()["user"]["status"],
            "SUSPENDED",
        )
        self.assertEqual(
            buyer_client.get("/api/admin/security/alerts").status_code,
            403,
        )
        submitted = buyer_client.post(
            f"/api/security/alerts/{alert_id}/appeal",
            json={"message": "I believe this alert is based on incorrect order information."},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        appeal_id = submitted.json()["id"]
        buyer_alerts = buyer_client.get("/api/security/alerts")
        self.assertEqual(buyer_alerts.status_code, 200, buyer_alerts.text)
        self.assertEqual(buyer_alerts.json()[0]["appeal_status"], "PENDING")

        appeals = self.client.get("/api/admin/security/appeals")
        self.assertEqual(appeals.status_code, 200, appeals.text)
        self.assertEqual(appeals.json()[0]["id"], appeal_id)
        decision = self.client.patch(
            f"/api/admin/security/appeals/{appeal_id}",
            json={"status": "ACCEPTED", "note": "Evidence will be reviewed again."},
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertEqual(decision.json()["alert_status"], "SUSPENDED")
        self.assertEqual(
            buyer_client.get("/api/security/alerts").json()[0]["appeal_status"],
            "ACCEPTED",
        )
        restored = self.client.patch(
            f"/api/admin/security/alerts/{alert_id}",
            json={"action": "RESTORE", "note": "Appeal accepted; access restored."},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        with main.db_connection() as connection:
            restored_status = connection.execute(
                "SELECT status FROM users WHERE id = ?",
                (buyer_id,),
            ).fetchone()["status"]
            connection.execute(
                """
                INSERT INTO notification_preferences (user_id, security_alerts_enabled)
                VALUES (?, 0)
                """,
                (buyer_id,),
            )
        with patch("backend.main.send_channel_notification") as send_notification:
            warned = self.client.patch(
                f"/api/admin/security/alerts/{alert_id}",
                json={"action": "WARN", "note": "Please review the account activity."},
            )
            self.assertEqual(warned.status_code, 200, warned.text)
            self.assertFalse(warned.json()["notification_queued"])
            send_notification.assert_not_called()
        self.assertEqual(restored_status, "ACTIVE")
        duplicate_decision = self.client.patch(
            f"/api/admin/security/appeals/{appeal_id}",
            json={"status": "REJECTED", "note": "This decision must not be repeated."},
        )
        self.assertEqual(duplicate_decision.status_code, 409)
        with main.db_connection() as connection:
            final_appeal_status = connection.execute(
                "SELECT status FROM security_appeals WHERE id = ?",
                (appeal_id,),
            ).fetchone()["status"]
            actions = connection.execute(
                "SELECT action FROM security_alert_actions WHERE alert_id = ? ORDER BY created_at",
                (alert_id,),
            ).fetchall()
        self.assertEqual(final_appeal_status, "ACCEPTED")
        self.assertIn("APPEAL_ACCEPTED", [row["action"] for row in actions])
        buyer_client.close()

    def test_admin_system_alerts_record_email_failures_and_are_private(self):
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

        with patch.object(
            main,
            "send_smtp_email",
            side_effect=main.HTTPException(status_code=502, detail="Resend rejected"),
        ):
            self.assertFalse(
                main.send_notification_email(
                    "payment_receipt",
                    "buyer@example.com",
                    main.EmailContent(
                        subject="Receipt",
                        text="Receipt text",
                        html="<p>Receipt</p>",
                    ),
                )
            )

        alerts = self.client.get("/api/admin/system-alerts")
        self.assertEqual(alerts.status_code, 200, alerts.text)
        email_alert = next(
            item for item in alerts.json()
            if item["alert_type"] == "EMAIL_DELIVERY_FAILED"
        )
        self.assertEqual(email_alert["message"], "Barua pepe ya mfumo imeshindwa kutumwa.")
        acknowledged = self.client.patch(
            f"/api/admin/system-alerts/{email_alert['id']}"
        )
        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
        self.assertTrue(acknowledged.json()["acknowledged"])

        buyer_registration = self.client.post(
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
            login = buyer_client.post(
                "/api/auth/login",
                json={
                    "email": "buyer@example.com",
                    "password": "buyer-password-long",
                },
            )
            self.assertEqual(login.status_code, 200)
            self.assertEqual(
                buyer_client.get("/api/admin/system-alerts").status_code,
                403,
            )

    def test_abandoned_checkout_reminders_require_consent_and_can_unsubscribe(self):
        with main.db_connection() as connection:
            connection.execute(
                """
                INSERT INTO users
                    (id, full_name, email, password_hash, role, status, shop_name)
                VALUES ('reminder-seller', 'Reminder Seller', 'reminder-seller@example.com',
                        'unused', 'SELLER', 'ACTIVE', 'Reminder Shop')
                """
            )
            connection.execute(
                """
                INSERT INTO products (id, name, amount, seller_id, stock_quantity)
                VALUES ('reminder-product', 'Sample Product', 5000,
                        'reminder-seller', 10)
                """
            )

        payload = {
            "buyer_name": "Reminder Buyer",
            "buyer_email": "buyer@example.com",
            "buyer_phone": "0712345678",
            "payment_method": "M-Pesa",
        }
        no_consent = self.client.post(
            "/api/products/reminder-product/checkout",
            json={**payload, "request_key": "n" * 32},
        )
        self.assertEqual(no_consent.status_code, 200, no_consent.text)
        with main.db_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM abandoned_checkout_reminders"
                ).fetchone()["count"],
                0,
            )

        consented = self.client.post(
            "/api/products/reminder-product/checkout",
            json={
                **payload,
                "request_key": "a" * 32,
                "abandoned_checkout_consent": True,
            },
        )
        self.assertEqual(consented.status_code, 200, consented.text)
        pending = self.client.post(
            "/api/products/reminder-product/checkout",
            json={
                **payload,
                "request_key": "b" * 32,
                "abandoned_checkout_consent": True,
            },
        )
        self.assertEqual(pending.status_code, 200, pending.text)
        completed = self.client.post(
            "/api/products/reminder-product/checkout",
            json={
                **payload,
                "request_key": "d" * 32,
                "abandoned_checkout_consent": True,
            },
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        completed_order = self.client.post(
            f"/api/orders/{completed.json()['order_id']}/demo-complete"
        )
        self.assertEqual(completed_order.status_code, 200, completed_order.text)
        self.email_sender.reset_mock()
        cart = self.client.post(
            "/api/cart/checkout",
            json={
                **payload,
                "request_key": "c" * 32,
                "abandoned_checkout_consent": True,
                "items": [{"product_id": "reminder-product", "quantity": 1}],
            },
        )
        self.assertEqual(cart.status_code, 200, cart.text)

        with main.db_connection() as connection:
            reminders = connection.execute(
                """
                SELECT checkout_type, checkout_key, status, send_at
                FROM abandoned_checkout_reminders
                ORDER BY checkout_type, checkout_key
                """
            ).fetchall()
            self.assertEqual(len(reminders), 4)
            self.assertEqual(
                sum(row["checkout_type"] == "CART" for row in reminders),
                1,
            )
            consented_reminder = connection.execute(
                """
                SELECT send_at FROM abandoned_checkout_reminders
                WHERE checkout_type = 'ORDER' AND checkout_key = ?
                """,
                (consented.json()["order_id"],),
            ).fetchone()
            self.assertAlmostEqual(
                consented_reminder["send_at"] - time.time(),
                main.ORDER_TTL_SECONDS,
                delta=10,
            )
            completed_reminder = connection.execute(
                """
                SELECT status FROM abandoned_checkout_reminders
                WHERE checkout_type = 'ORDER' AND checkout_key = ?
                """,
                (completed.json()["order_id"],),
            ).fetchone()
            self.assertEqual(completed_reminder["status"], "CANCELLED")
            connection.execute(
                """
                UPDATE orders SET expires_at = 0
                WHERE order_id = ?
                """,
                (consented.json()["order_id"],),
            )
            connection.execute(
                """
                UPDATE abandoned_checkout_reminders
                SET send_at = 0, next_attempt_at = 0
                WHERE checkout_type = 'ORDER' AND checkout_key = ?
                """,
                (consented.json()["order_id"],),
            )

        with patch.dict(
            os.environ,
            {"PUBLIC_APP_URL": "https://liparahisi.example"},
        ):
            self.assertEqual(main.process_due_abandoned_checkout_reminders(), 1)
        self.assertEqual(self.email_sender.call_count, 1)
        reminder_text = self.email_sender.call_args.args[2]
        token_match = re.search(
            r"/api/email/reminders/unsubscribe\?token=([A-Za-z0-9_-]+)",
            reminder_text,
        )
        self.assertIsNotNone(token_match)
        token = token_match.group(1)
        confirmation = self.client.get(
            "/api/email/reminders/unsubscribe",
            params={"token": token},
        )
        self.assertEqual(confirmation.status_code, 200)
        with main.db_connection() as connection:
            self.assertIsNone(
                connection.execute(
                    """
                    SELECT opted_out_at FROM email_reminder_preferences
                    WHERE email = ?
                    """,
                    ("buyer@example.com",),
                ).fetchone()["opted_out_at"]
            )

        unsubscribe = self.client.post(
            "/api/email/reminders/unsubscribe",
            params={"token": token},
        )
        self.assertEqual(unsubscribe.status_code, 200)
        with main.db_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT status FROM abandoned_checkout_reminders
                    WHERE checkout_type = 'ORDER' AND checkout_key = ?
                    """,
                    (pending.json()["order_id"],),
                ).fetchone()["status"],
                "UNSUBSCRIBED",
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT status FROM abandoned_checkout_reminders
                    WHERE checkout_type = 'CART' AND checkout_key = ?
                    """,
                    (cart.json()["checkout_id"],),
                ).fetchone()["status"],
                "UNSUBSCRIBED",
            )
        self.assertEqual(main.process_due_abandoned_checkout_reminders(), 0)
        self.assertEqual(self.email_sender.call_count, 1)

    def test_product_variants_track_checkout_and_cancellation_stock(self):
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
                "full_name": "Variant Seller",
                "email": "variant-seller@example.com",
                "password": "seller-password-long",
                "shop_name": "Duka la Variant",
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
                    json={
                        "email": "variant-seller@example.com",
                        "password": "seller-password-long",
                    },
                ).status_code,
                200,
            )
            created = seller_client.post(
                "/api/products",
                json={
                    "name": "Shati",
                    "amount": 1000,
                    "stock_quantity": 0,
                    "variants": [
                        {"label": "Nyekundu / M", "stock_quantity": 2},
                        {"label": "Bluu / L", "stock_quantity": 2},
                    ],
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            product_id = created.json()["id"]
            variants_by_label = {
                variant["label"]: variant
                for variant in created.json()["variants"]
            }
            red_variant = variants_by_label["Nyekundu / M"]
            blue_variant = variants_by_label["Bluu / L"]
            self.assertEqual(created.json()["stock_quantity"], 4)

            green_variant = seller_client.post(
                f"/api/products/{product_id}/variants",
                json={"label": "Kijani / M", "stock_quantity": 1},
            )
            self.assertEqual(green_variant.status_code, 201, green_variant.text)
            duplicate_variant = seller_client.post(
                f"/api/products/{product_id}/variants",
                json={"label": "kijani / m", "stock_quantity": 1},
            )
            self.assertEqual(duplicate_variant.status_code, 409)
            updated_variant = seller_client.patch(
                f"/api/products/{product_id}/variants/{green_variant.json()['id']}",
                json={"stock_quantity": 3},
            )
            self.assertEqual(updated_variant.status_code, 200, updated_variant.text)
            product = seller_client.get(f"/api/products/{product_id}").json()
            self.assertEqual(product["stock_quantity"], 7)
            self.assertEqual(
                seller_client.put(
                    f"/api/products/{product_id}",
                    json={"stock_quantity": 5},
                ).status_code,
                422,
            )

        buyer_registration = self.client.post(
            "/api/auth/customer/register",
            json={
                "full_name": "Variant Buyer",
                "email": "variant-buyer@example.com",
                "password": "buyer-password-long",
            },
        )
        self.assertEqual(buyer_registration.status_code, 201)
        buyer_client = TestClient(main.app)
        with buyer_client:
            self.assertEqual(
                buyer_client.post(
                    "/api/auth/login",
                    json={
                        "email": "variant-buyer@example.com",
                        "password": "buyer-password-long",
                    },
                ).status_code,
                200,
            )
            checkout_payload = {
                "buyer_name": "Variant Buyer",
                "buyer_email": "variant-buyer@example.com",
                "buyer_phone": "0712345678",
                "payment_method": "M-Pesa",
            }
            missing_variant = buyer_client.post(
                f"/api/products/{product_id}/checkout",
                json={**checkout_payload, "request_key": "u" * 32},
            )
            self.assertEqual(missing_variant.status_code, 422)
            single_checkout = buyer_client.post(
                f"/api/products/{product_id}/checkout",
                json={
                    **checkout_payload,
                    "request_key": "v" * 32,
                    "variant_id": red_variant["id"],
                },
            )
            self.assertEqual(single_checkout.status_code, 200, single_checkout.text)
            order_id = single_checkout.json()["order_id"]
            completed = buyer_client.post(f"/api/orders/{order_id}/demo-complete")
            self.assertEqual(completed.status_code, 200, completed.text)
            order_status = buyer_client.get(f"/api/orders/{order_id}").json()
            self.assertEqual(order_status["variant_label"], "Nyekundu / M")
            product = buyer_client.get(f"/api/products/{product_id}").json()
            stock_by_label = {variant["label"]: variant["stock_quantity"] for variant in product["variants"]}
            self.assertEqual(stock_by_label["Nyekundu / M"], 1)
            self.assertEqual(product["stock_quantity"], 6)

            out_of_stock_variant = buyer_client.post(
                "/api/cart/checkout",
                json={
                    **checkout_payload,
                    "request_key": "x" * 32,
                    "items": [{
                        "product_id": product_id,
                        "quantity": 3,
                        "variant_id": blue_variant["id"],
                    }],
                },
            )
            self.assertEqual(out_of_stock_variant.status_code, 409)

            cart_checkout = buyer_client.post(
                "/api/cart/checkout",
                json={
                    **checkout_payload,
                    "request_key": "w" * 32,
                    "items": [{
                        "product_id": product_id,
                        "quantity": 2,
                        "variant_id": blue_variant["id"],
                    }],
                },
            )
            self.assertEqual(cart_checkout.status_code, 200, cart_checkout.text)
            cart_id = cart_checkout.json()["checkout_id"]
            cart_complete = buyer_client.post(
                f"/api/checkouts/{cart_id}/demo-complete"
            )
            self.assertEqual(cart_complete.status_code, 200, cart_complete.text)
            split = cart_complete.json()["splits"][0]
            self.assertEqual(split["items"][0]["variant_label"], "Bluu / L")
            self.assertEqual(split["items"][0]["quantity"], 2)

            cancellation = buyer_client.post(
                f"/api/orders/{split['order_id']}/cancellation",
                json={"reason": "Nimebadilisha uamuzi kuhusu oda."},
            )
            self.assertEqual(cancellation.status_code, 200, cancellation.text)
            admin_cancellation = next(
                row
                for row in self.client.get("/api/admin/cancellations").json()
                if row["id"] == cancellation.json()["id"]
            )
            resolution = self.client.patch(
                f"/api/admin/cancellations/{admin_cancellation['id']}",
                json={"status": "APPROVED", "admin_note": "Imeidhinishwa demo."},
            )
            self.assertEqual(resolution.status_code, 200, resolution.text)
            restored_product = buyer_client.get(f"/api/products/{product_id}").json()
            restored_stock = {
                variant["label"]: variant["stock_quantity"]
                for variant in restored_product["variants"]
            }
            self.assertEqual(restored_stock["Bluu / L"], 2)
            self.assertEqual(restored_product["stock_quantity"], 6)

    def test_product_media_upload_validation_and_seller_ownership(self):
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
            video_data = b"\x00\x00\x00\x18ftypisom" + b"x" * 32

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

            uploaded_videos = []
            for index in range(main.MAX_PRODUCT_VIDEOS):
                upload_video = seller_client.post(
                    f"/api/products/{product_id}/videos",
                    files={
                        "video": (
                            f"product-{index}.mp4",
                            video_data,
                            "video/mp4",
                        )
                    },
                )
                self.assertEqual(upload_video.status_code, 200, upload_video.text)
                uploaded_videos.append(upload_video.json()["video"])
            video_limit = seller_client.post(
                f"/api/products/{product_id}/videos",
                files={"video": ("extra.mp4", video_data, "video/mp4")},
            )
            self.assertEqual(video_limit.status_code, 409)

            public_product = public_client.get(
                f"/api/products/{product_id}"
            ).json()
            self.assertEqual(len(public_product["videos"]), main.MAX_PRODUCT_VIDEOS)
            public_products = public_client.get("/api/products").json()
            listed_product = next(
                item for item in public_products if item["id"] == product_id
            )
            self.assertEqual(len(listed_product["videos"]), main.MAX_PRODUCT_VIDEOS)
            public_video = public_client.get(
                uploaded_videos[0]["url"],
                headers={"Range": "bytes=4-11"},
            )
            self.assertEqual(public_video.status_code, 206)
            self.assertEqual(public_video.content, video_data[4:12])
            self.assertEqual(public_video.headers["content-range"], f"bytes 4-11/{len(video_data)}")
            self.assertEqual(public_video.headers["accept-ranges"], "bytes")
            invalid_range = public_client.get(
                uploaded_videos[0]["url"],
                headers={"Range": f"bytes={'9' * 101}-"},
            )
            self.assertEqual(invalid_range.status_code, 416)
            self.assertEqual(
                invalid_range.headers["content-range"],
                f"bytes */{len(video_data)}",
            )

            invalid_video_signature = seller_client.post(
                f"/api/products/{product_id}/videos",
                files={"video": ("invalid.mp4", b"not an mp4", "video/mp4")},
            )
            self.assertEqual(invalid_video_signature.status_code, 422)
            invalid_video_type = seller_client.post(
                f"/api/products/{product_id}/videos",
                files={"video": ("invalid.txt", b"file", "text/plain")},
            )
            self.assertEqual(invalid_video_type.status_code, 415)
            oversized_video = seller_client.post(
                f"/api/products/{product_id}/videos",
                files={
                    "video": (
                        "large.mp4",
                        b"\x00\x00\x00\x18ftypisom"
                        + b"x" * main.MAX_PRODUCT_VIDEO_BYTES,
                        "video/mp4",
                    )
                },
            )
            self.assertEqual(oversized_video.status_code, 413)

            deleted_video = seller_client.delete(
                f"/api/products/{product_id}/videos/{uploaded_videos[0]['id']}"
            )
            self.assertEqual(deleted_video.status_code, 200)
            remaining_videos = public_client.get(
                f"/api/products/{product_id}"
            ).json()["videos"]
            self.assertEqual(len(remaining_videos), main.MAX_PRODUCT_VIDEOS - 1)

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
            denied_video_upload = other_client.post(
                f"/api/products/{product_id}/videos",
                files={"video": ("product.mp4", video_data, "video/mp4")},
            )
            self.assertEqual(denied_video_upload.status_code, 404)
            denied_video_delete = other_client.delete(
                f"/api/products/{product_id}/videos/{uploaded_videos[1]['id']}"
            )
            self.assertEqual(denied_video_delete.status_code, 404)

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
        coupon = self.client.post(
            "/api/admin/coupons",
            json={
                "code": "ABLINER10",
                "discount_type": "PERCENT",
                "discount_value": 10,
            },
        )
        self.assertEqual(coupon.status_code, 201, coupon.text)

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
                "DEMO_MODE": "true",
                "ABLINER_LIVE_ENABLED": "true",
                "ABLINER_API_KEY": "test-api-key",
                "ABLINER_WEBHOOK_SECRET": "test-webhook-secret",
                "PUBLIC_APP_URL": "https://liparahisi.onrender.com",
            },
        ), patch("backend.main.get_abliner_client", return_value=fake_client):
            live_coupon_checkout = buyer_client.post(
                f"/api/products/{product_id}/checkout",
                json={
                    "request_key": "f" * 32,
                    "buyer_name": "Buyer Abliner",
                    "buyer_email": "buyer-abliner@example.com",
                    "buyer_phone": "0712345678",
                    "payment_method": "Abliner Mobile Money",
                    "coupon_code": "ABLINER10",
                },
            )
            self.assertEqual(live_coupon_checkout.status_code, 422)
            self.assertIsNone(fake_client.request)
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

    def test_login_failures_are_persisted_and_block_after_five_attempts(self):
        for _ in range(5):
            response = self.client.post(
                "/api/auth/login",
                json={"email": "missing@example.com", "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)
        with main.db_connection() as connection:
            attempts = connection.execute(
                "SELECT failed_count, blocked_until FROM login_attempts WHERE email = ?",
                ("missing@example.com",),
            ).fetchone()
        self.assertEqual(attempts["failed_count"], 5)
        self.assertGreater(attempts["blocked_until"], time.time())
        blocked = self.client.post(
            "/api/auth/login",
            json={"email": "missing@example.com", "password": "wrong-password"},
        )
        self.assertEqual(blocked.status_code, 429)

    def test_totp_mfa_requires_a_fresh_code_and_supports_one_time_recovery(self):
        with patch.dict(os.environ, {"MFA_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
            setup = self.client.post(
                "/api/auth/setup",
                json={
                    "setup_token": "test-admin-setup-token-long-enough",
                    "full_name": "Main Admin",
                    "email": "admin@example.com",
                    "password": "admin-password-long",
                },
            )
            self.assertEqual(setup.status_code, 200, setup.text)
            enrollment = self.client.post("/api/auth/mfa/setup")
            self.assertEqual(enrollment.status_code, 200, enrollment.text)
            secret = enrollment.json()["secret"]

            def code_for(counter):
                key = base64.b32decode(secret + "=" * (-len(secret) % 8))
                digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
                offset = digest[-1] & 0x0F
                value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
                return f"{value:06d}"

            current_counter = int(time.time() // 30)
            enabled = self.client.post(
                "/api/auth/mfa/enable",
                json={"code": code_for(current_counter)},
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
            recovery_codes = enabled.json()["recovery_codes"]
            self.assertEqual(len(recovery_codes), 10)

            self.client.post("/api/auth/logout")
            missing_code = self.client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "admin-password-long"},
            )
            self.assertEqual(missing_code.status_code, 401)
            fresh_code = code_for(current_counter + 1)
            login = self.client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "admin-password-long",
                    "totp_code": fresh_code,
                },
            )
            self.assertEqual(login.status_code, 200, login.text)
            replay = self.client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "admin-password-long",
                    "totp_code": fresh_code,
                },
            )
            self.assertEqual(replay.status_code, 401)
            self.client.post("/api/auth/logout")

            recovery_login = self.client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "admin-password-long",
                    "totp_code": recovery_codes[0],
                },
            )
            self.assertEqual(recovery_login.status_code, 200, recovery_login.text)
            self.client.post("/api/auth/logout")
            reused_recovery = self.client.post(
                "/api/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "admin-password-long",
                    "totp_code": recovery_codes[0],
                },
            )
            self.assertEqual(reused_recovery.status_code, 401)

    def test_account_can_revoke_sessions_on_other_devices(self):
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
        other_client = TestClient(main.app)
        with other_client:
            login = other_client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "admin-password-long"},
            )
            self.assertEqual(login.status_code, 200)
            self.assertEqual(len(self.client.get("/api/auth/sessions").json()), 2)
            revoked = self.client.post("/api/auth/sessions/revoke-others")
            self.assertEqual(revoked.status_code, 200)
            self.assertEqual(revoked.json()["revoked_count"], 1)
            self.assertIsNone(other_client.get("/api/auth/status").json()["user"])

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
        self.assertFalse(status["pesapal_sandbox_enabled"])
        self.assertFalse(status["pesapal_live_enabled"])
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
        self.assertFalse(enabled_status["pesapal_live_enabled"])

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

    def test_complete_seller_kyc_workflow(self):
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
                "full_name": "Individual Seller",
                "email": "kyc-seller@example.com",
                "password": "seller-password-long",
                "shop_name": "Duka KYC",
                "phone": "0712345678",
            },
        )
        self.assertEqual(registration.status_code, 201)
        seller = next(
            item for item in self.client.get("/api/admin/sellers").json()
            if item["email"] == "kyc-seller@example.com"
        )
        self.assertEqual(
            self.client.patch(
                f"/api/admin/sellers/{seller['id']}",
                json={"status": "ACTIVE"},
            ).status_code,
            200,
        )
        kyc_key = Fernet.generate_key().decode("ascii")
        with patch.dict(os.environ, {"SELLER_KYC_ENCRYPTION_KEY": kyc_key}):
            seller_client = TestClient(main.app)
            with seller_client:
                self.assertEqual(
                    seller_client.post(
                        "/api/auth/login",
                        json={
                            "email": "kyc-seller@example.com",
                            "password": "seller-password-long",
                        },
                    ).status_code,
                    200,
                )
                pdf = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
                upload = seller_client.post(
                    "/api/documents",
                    data={
                        "kind": "seller_verification",
                        "document_role": "identity_document",
                        "expires_at": "2030-12-31",
                    },
                    files={"document": ("identity.pdf", pdf, "application/pdf")},
                )
                self.assertEqual(upload.status_code, 200, upload.text)
                submit = seller_client.post(
                    "/api/auth/kyc",
                    json={
                        "seller_type": "INDIVIDUAL",
                        "phone": "0712345678",
                        "identity_name": "Individual Seller",
                        "identity_number": "19900101000000000100",
                    },
                )
                self.assertEqual(submit.status_code, 200, submit.text)
                self.assertEqual(submit.json()["status"], "PENDING_VERIFICATION")

                under_review = self.client.patch(
                    f"/api/admin/kyc/{seller['id']}",
                    json={"status": "UNDER_REVIEW", "reason": "", "expires_at": ""},
                )
                self.assertEqual(under_review.status_code, 200)
                details = self.client.get(f"/api/admin/kyc/{seller['id']}")
                self.assertEqual(details.status_code, 200)
                self.assertEqual(len(details.json()["documents"]), 1)

                document_id = details.json()["documents"][0]["id"]
                viewed = self.client.get(
                    f"/api/admin/kyc/{seller['id']}/documents/{document_id}"
                )
                self.assertEqual(viewed.status_code, 200)
                self.assertEqual(viewed.content, pdf)

                buyer = TestClient(main.app)
                denied = buyer.get(
                    f"/api/admin/kyc/{seller['id']}/documents/{document_id}"
                )
                self.assertIn(denied.status_code, {401, 403, 404})

                resubmit = self.client.patch(
                    f"/api/admin/kyc/{seller['id']}",
                    json={
                        "status": "RESUBMISSION_REQUIRED",
                        "reason": "Pakia toleo lililoeleweka.",
                        "expires_at": "",
                    },
                )
                self.assertEqual(resubmit.status_code, 200)
                resubmitted = seller_client.post(
                    "/api/auth/kyc",
                    json={
                        "seller_type": "INDIVIDUAL",
                        "phone": "0712345678",
                        "identity_name": "Individual Seller",
                        "identity_number": "19900101000000000100",
                    },
                )
                self.assertEqual(resubmitted.status_code, 200)

                verified = self.client.patch(
                    f"/api/admin/kyc/{seller['id']}",
                    json={"status": "VERIFIED", "reason": "", "expires_at": "2030-12-31"},
                )
                self.assertEqual(verified.status_code, 200)
                seller_status = seller_client.get("/api/auth/kyc")
                self.assertEqual(seller_status.status_code, 200)
                self.assertEqual(seller_status.json()["status"], "VERIFIED")

                withdrawal = seller_client.delete("/api/auth/kyc")
                self.assertEqual(withdrawal.status_code, 409)

    def test_multi_business_branch_tenant_isolation(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "tenant-admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200)

        for email, shop in (
            ("tenant-one@example.com", "Biashara Moja"),
            ("tenant-two@example.com", "Biashara Pili"),
        ):
            registration = self.client.post(
                "/api/auth/register",
                json={
                    "full_name": shop,
                    "email": email,
                    "password": "seller-password-long",
                    "shop_name": shop,
                    "phone": "0712345678",
                },
            )
            self.assertEqual(registration.status_code, 201)
            seller = next(
                item for item in self.client.get("/api/admin/sellers").json()
                if item["email"] == email
            )
            self.assertEqual(
                self.client.patch(
                    f"/api/admin/sellers/{seller['id']}",
                    json={"status": "ACTIVE"},
                ).status_code,
                200,
            )

        def seller_client(email):
            client = TestClient(main.app)
            client.__enter__()
            self.assertEqual(
                client.post(
                    "/api/auth/login",
                    json={"email": email, "password": "seller-password-long"},
                ).status_code,
                200,
            )
            return client

        first = seller_client("tenant-one@example.com")
        second = seller_client("tenant-two@example.com")
        try:
            first_business = first.post(
                "/api/businesses",
                json={
                    "name": "Supermarket Moja",
                    "business_type": "Supermarket",
                    "address": "Dar es Salaam",
                    "phone": "0712345678",
                    "email": "tenant-one@example.com",
                },
            )
            self.assertEqual(first_business.status_code, 200, first_business.text)
            business_id = first_business.json()["id"]
            second_business = second.post(
                "/api/businesses",
                json={
                    "name": "Supermarket Pili",
                    "business_type": "Supermarket",
                    "address": "Dodoma",
                    "phone": "0712345678",
                    "email": "tenant-two@example.com",
                },
            )
            self.assertEqual(second_business.status_code, 200)

            denied = second.post(
                f"/api/businesses/{business_id}/branches",
                json={"name": "Tawi la Mpasuko", "address": "Arusha"},
            )
            self.assertIn(denied.status_code, {403, 404})

            branch = first.post(
                f"/api/businesses/{business_id}/branches",
                json={"name": "Tawi la Dodoma", "address": "Dodoma"},
            )
            self.assertEqual(branch.status_code, 200, branch.text)

            owned = first.get(f"/api/businesses/{business_id}/summary")
            self.assertEqual(owned.status_code, 200)
            self.assertGreaterEqual(len(owned.json()["branches"]), 2)

            cross_tenant = second.get(f"/api/businesses/{business_id}/summary")
            self.assertIn(cross_tenant.status_code, {403, 404})
        finally:
            first.__exit__(None, None, None)
            second.__exit__(None, None, None)

    def test_language_preference_is_persisted_across_login(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "language-admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200, setup.text)
        login = self.client.post(
            "/api/auth/login",
            json={"email": "language-admin@example.com", "password": "admin-password-long"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(self.client.get("/api/auth/language").json()["language"], "sw")

        updated = self.client.put("/api/auth/language", json={"language": "fr"})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["language"], "fr")
        self.assertEqual(self.client.put("/api/auth/language", json={"language": "de"}).status_code, 422)
        with main.db_connection() as connection:
            admin = connection.execute(
                "SELECT id, email FROM users WHERE email = ?",
                ("language-admin@example.com",),
            ).fetchone()
        with patch("backend.main.send_channel_notification", return_value=True) as send_notification:
            self.assertEqual(
                main.queue_user_notification(
                    admin["id"],
                    "ORDER_CONFIRMED",
                    {"order_id": "LR-123"},
                    email=admin["email"],
                ),
                ["EMAIL"],
            )
        self.assertEqual(send_notification.call_args.args[-1], "fr")
        with main.db_connection() as connection:
            history = connection.execute(
                """
                SELECT title, body FROM notification_history
                WHERE user_id = ? AND event = 'ORDER_CONFIRMED'
                ORDER BY rowid DESC LIMIT 1
                """,
                (admin["id"],),
            ).fetchone()
        self.assertEqual(history["title"], "Commande confirmée")
        self.assertEqual(history["body"], "La commande LR-123 a été confirmée.")
        self.client.post("/api/auth/logout")

        with TestClient(main.app) as other_device:
            login = other_device.post(
                "/api/auth/login",
                json={"email": "language-admin@example.com", "password": "admin-password-long"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            self.assertEqual(other_device.get("/api/auth/language").json()["language"], "fr")

    def test_business_assistant_uses_verified_sales_and_isolates_conversations(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "assistant-admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200, setup.text)
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"email": "assistant-admin@example.com", "password": "admin-password-long"},
            ).status_code,
            200,
        )

        for email, shop in (
            ("assistant-owner@example.com", "Assistant Owner"),
            ("assistant-outsider@example.com", "Assistant Outsider"),
        ):
            registration = self.client.post(
                "/api/auth/register",
                json={
                    "full_name": shop,
                    "email": email,
                    "password": "seller-password-long",
                    "shop_name": shop,
                    "phone": "0712345678",
                },
            )
            self.assertEqual(registration.status_code, 201, registration.text)
            seller = next(
                item for item in self.client.get("/api/admin/sellers").json()
                if item["email"] == email
            )
            self.assertEqual(
                self.client.patch(
                    f"/api/admin/sellers/{seller['id']}",
                    json={"status": "ACTIVE"},
                ).status_code,
                200,
            )

        owner_client = TestClient(main.app)
        outsider_client = TestClient(main.app)
        owner_client.__enter__()
        outsider_client.__enter__()
        try:
            for client, email in (
                (owner_client, "assistant-owner@example.com"),
                (outsider_client, "assistant-outsider@example.com"),
            ):
                login = client.post(
                    "/api/auth/login",
                    json={"email": email, "password": "seller-password-long"},
                )
                self.assertEqual(login.status_code, 200, login.text)

            business = owner_client.post(
                "/api/businesses",
                json={
                    "name": "Assistant Business",
                    "business_type": "Retail",
                    "address": "Dar es Salaam",
                    "phone": "0712345678",
                    "email": "assistant-owner@example.com",
                },
            )
            self.assertEqual(business.status_code, 200, business.text)
            business_id = business.json()["id"]
            self.assertEqual(
                owner_client.put("/api/auth/language", json={"language": "fr"}).status_code,
                200,
            )
            with main.db_connection() as connection:
                branch = connection.execute(
                    "SELECT id FROM branches WHERE business_id = ? ORDER BY created_at LIMIT 1",
                    (business_id,),
                ).fetchone()
                self.assertIsNotNone(branch)
                branch_id = branch["id"]
                owner = connection.execute(
                    "SELECT id FROM users WHERE email = ?",
                    ("assistant-owner@example.com",),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO products
                        (id, name, amount, seller_id, cost_amount, stock_quantity,
                         low_stock_threshold, business_id, branch_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "ASSISTANTPRODUCT",
                        "Verified assistant product",
                        1000,
                        owner["id"],
                        600,
                        2,
                        5,
                        business_id,
                        branch_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO orders
                        (order_id, request_key, product_id, seller_id, amount,
                         payment_method, provider_reference, payment_status,
                         product_cost, net_profit, business_id, branch_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "ASSISTANTORDER",
                        "ASSISTANTREQUEST",
                        "ASSISTANTPRODUCT",
                        owner["id"],
                        1000,
                        "ClickPesa USSD Push",
                        "verified-provider-reference",
                        "COMPLETED",
                        600,
                        400,
                        business_id,
                        branch_id,
                        main.date.today().isoformat() + "T12:00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO order_items (order_id, product_id, quantity, unit_amount, unit_cost)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("ASSISTANTORDER", "ASSISTANTPRODUCT", 1, 1000, 600),
                )
                connection.execute(
                    """
                    INSERT INTO orders
                        (order_id, request_key, product_id, seller_id, amount,
                         payment_method, provider_reference, payment_status,
                         product_cost, net_profit, business_id, branch_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "ASSISTANTDEMO",
                        "ASSISTANTDEMOREQUEST",
                        "ASSISTANTPRODUCT",
                        owner["id"],
                        50000,
                        "M-Pesa",
                        "",
                        "COMPLETED",
                        0,
                        50000,
                        business_id,
                        branch_id,
                        main.date.today().isoformat() + "T12:00:00",
                    ),
                )

            answer = owner_client.post(
                f"/api/businesses/{business_id}/assistant/messages",
                json={"question": "Quelles sont mes ventes ce mois-ci ?"},
            )
            self.assertEqual(answer.status_code, 200, answer.text)
            result = answer.json()
            self.assertTrue(result["user_message_id"])
            self.assertTrue(result["assistant_message_id"])
            self.assertNotEqual(result["user_message_id"], result["assistant_message_id"])
            self.assertIn("TZS 1 000 de ventes", result["answer"])
            self.assertIn("400", result["answer"])
            self.assertNotIn("51 000", result["answer"])
            self.assertEqual(result["sources"], ["orders"])
            self.assertEqual(result["ai_status"], "NOT_CONFIGURED")
            self.assertEqual(result["recommendation_codes"], ["REVIEW_REPLENISHMENT"])
            self.assertEqual(set(result["period"]), {"start", "end"})

            history = owner_client.get(
                f"/api/businesses/{business_id}/assistant/conversations/{result['conversation_id']}"
            )
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(len(history.json()["messages"]), 2)
            denied = outsider_client.get(
                f"/api/businesses/{business_id}/assistant/conversations/{result['conversation_id']}"
            )
            self.assertIn(denied.status_code, {403, 404})
        finally:
            owner_client.__exit__(None, None, None)
            outsider_client.__exit__(None, None, None)

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

            document_data = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
            document_upload = seller_client.post(
                "/api/documents",
                data={"kind": "product_document", "product_id": product_id},
                files={"document": ("specification.pdf", document_data, "application/pdf")},
            )
            self.assertEqual(document_upload.status_code, 200, document_upload.text)
            document_id = document_upload.json()["id"]
            private_document = seller_client.get(f"/api/documents/{document_id}")
            self.assertEqual(private_document.status_code, 200)
            self.assertEqual(private_document.content, document_data)
            self.assertEqual(private_document.headers["cache-control"], "private, no-store")
            invalid_document = seller_client.post(
                "/api/documents",
                data={"kind": "seller_verification"},
                files={"document": ("fake.pdf", b"not a pdf", "application/pdf")},
            )
            self.assertEqual(invalid_document.status_code, 422)

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
            denied_document = other_client.get(f"/api/documents/{document_id}")
            self.assertEqual(denied_document.status_code, 404)
        admin_documents = self.client.get("/api/admin/documents")
        self.assertEqual(admin_documents.status_code, 200, admin_documents.text)
        self.assertEqual(admin_documents.json()[0]["id"], document_id)

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

    def test_pesapal_sandbox_verifies_payments_without_real_sale_side_effects(self):
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
                "phone": "0712345678",
            },
        )
        self.assertEqual(registration.status_code, 201)
        seller = self.client.get("/api/admin/sellers").json()[0]
        self.client.patch(
            f"/api/admin/sellers/{seller['id']}",
            json={"status": "ACTIVE"},
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
                    "name": "Kikombe cha majaribio",
                    "amount": 5000,
                    "cost_amount": 2500,
                    "stock_quantity": 10,
                },
            )
            self.assertEqual(product.status_code, 201)
            product_id = product.json()["id"]

            class FakePesapalClient:
                def __init__(self):
                    self.reference = ""
                    self.tracking_id = ""
                    self.amount = 5000
                    self.order_number = 0

                async def submit_order(self, **payload):
                    self.order_number += 1
                    self.reference = payload["merchant_reference"]
                    self.tracking_id = f"sandbox-tracking-{self.order_number}"
                    return {
                        "order_tracking_id": self.tracking_id,
                        "merchant_reference": self.reference,
                        "redirect_url": (
                            "https://cybqa.pesapal.com/pesapaliframe/"
                            f"PesapalIframe3/Index/?OrderTrackingId={self.tracking_id}"
                        ),
                    }

                async def get_transaction_status(self, tracking_id):
                    if tracking_id != self.tracking_id:
                        raise AssertionError("Unexpected PesaPal tracking ID.")
                    return {
                        "status_code": 1,
                        "merchant_reference": self.reference,
                        "amount": self.amount,
                        "currency": "TZS",
                    }

            pesapal_client = FakePesapalClient()
            buyer_client = TestClient(main.app)
            with buyer_client, patch.dict(
                os.environ,
                {
                    "PESAPAL_SANDBOX_ENABLED": "true",
                    "PESAPAL_CONSUMER_KEY": "sandbox-key",
                    "PESAPAL_CONSUMER_SECRET": "sandbox-secret",
                    "PESAPAL_NOTIFICATION_ID": "84740ab4-3cd9-47da-8a4f-dd1db53494b5",
                },
            ), patch("backend.main.get_pesapal_client", return_value=pesapal_client):
                self.email_sender.reset_mock()
                before_stock = main.get_product(product_id)["stock_quantity"]
                checkout = buyer_client.post(
                    f"/api/products/{product_id}/checkout",
                    json={
                        "request_key": "f" * 32,
                        "buyer_name": "Buyer One",
                        "buyer_email": "buyer@example.com",
                        "buyer_phone": "0712345678",
                        "payment_method": "M-Pesa",
                    },
                )
                self.assertEqual(checkout.status_code, 200, checkout.text)
                self.assertTrue(checkout.json()["sandbox"])
                self.assertFalse(checkout.json()["demo"])
                self.assertTrue(checkout.json()["payment_url"].startswith(
                    "https://cybqa.pesapal.com/"
                ))
                order_id = checkout.json()["order_id"]

                callback = buyer_client.get(
                    "/api/callbacks/pesapal",
                    params={
                        "OrderTrackingId": pesapal_client.tracking_id,
                        "OrderMerchantReference": pesapal_client.reference,
                    },
                    follow_redirects=False,
                )
                self.assertEqual(callback.status_code, 307)
                self.assertIn(f"/pay/{product_id}?order_id={order_id}", callback.headers["location"])
                repeated_ipn = buyer_client.get(
                    "/api/webhooks/pesapal",
                    params={
                        "OrderTrackingId": pesapal_client.tracking_id,
                        "OrderMerchantReference": pesapal_client.reference,
                    },
                )
                self.assertEqual(repeated_ipn.status_code, 200, repeated_ipn.text)
                self.assertEqual(repeated_ipn.json()["status"], 200)

                receipt = buyer_client.get(f"/api/orders/{order_id}").json()
                self.assertEqual(receipt["payment_status"], "COMPLETED")
                self.assertTrue(receipt["sandbox"])
                self.assertEqual(receipt["platform_fee"], 0)
                self.assertEqual(receipt["seller_payout"], 0)
                self.assertEqual(main.get_product(product_id)["stock_quantity"], before_stock)
                self.assertEqual(self.email_sender.call_count, 0)
                self.assertEqual(seller_client.get("/api/sales").json(), [])
                self.assertEqual(seller_client.get("/api/dashboard").json()["gross_sales"], 0)
                seller_metrics = next(
                    item
                    for item in self.client.get("/api/admin/sellers").json()
                    if item["id"] == seller["id"]
                )
                self.assertEqual(seller_metrics["gross_sales"], 0)

                pesapal_client.amount = 4999
                rejected = buyer_client.post(
                    f"/api/products/{product_id}/checkout",
                    json={
                        "request_key": "g" * 32,
                        "buyer_name": "Buyer One",
                        "buyer_email": "buyer@example.com",
                        "buyer_phone": "0712345678",
                        "payment_method": "M-Pesa",
                    },
                )
                self.assertEqual(rejected.status_code, 200, rejected.text)
                rejected_ipn = buyer_client.get(
                    "/api/webhooks/pesapal",
                    params={
                        "OrderTrackingId": pesapal_client.tracking_id,
                        "OrderMerchantReference": pesapal_client.reference,
                    },
                )
                self.assertEqual(rejected_ipn.status_code, 500)
                rejected_order = buyer_client.get(
                    f"/api/orders/{rejected.json()['order_id']}"
                ).json()
                self.assertEqual(rejected_order["payment_status"], "PENDING")
                self.assertEqual(main.get_product(product_id)["stock_quantity"], before_stock)

                pesapal_client.amount = 10000
                cart = buyer_client.post(
                    "/api/cart/checkout",
                    json={
                        "request_key": "h" * 32,
                        "buyer_name": "Buyer One",
                        "buyer_email": "buyer@example.com",
                        "buyer_phone": "0712345678",
                        "payment_method": "M-Pesa",
                        "items": [{"product_id": product_id, "quantity": 2}],
                    },
                )
                self.assertEqual(cart.status_code, 200, cart.text)
                self.assertTrue(cart.json()["sandbox"])
                self.assertTrue(cart.json()["payment_url"].startswith(
                    "https://cybqa.pesapal.com/"
                ))
                cart_id = cart.json()["checkout_id"]
                cart_callback = buyer_client.get(
                    "/api/callbacks/pesapal",
                    params={
                        "OrderTrackingId": pesapal_client.tracking_id,
                        "OrderMerchantReference": pesapal_client.reference,
                    },
                    follow_redirects=False,
                )
                self.assertEqual(cart_callback.status_code, 307)
                self.assertIn(f"/cart/{cart_id}", cart_callback.headers["location"])
                cart_ipn = buyer_client.post(
                    "/api/webhooks/pesapal",
                    json={
                        "OrderTrackingId": pesapal_client.tracking_id,
                        "OrderMerchantReference": pesapal_client.reference,
                    },
                )
                self.assertEqual(cart_ipn.status_code, 200, cart_ipn.text)
                self.assertEqual(
                    buyer_client.get(f"/api/checkouts/{cart_id}").json()["payment_status"],
                    "COMPLETED",
                )
                self.assertEqual(main.get_product(product_id)["stock_quantity"], before_stock)
                self.assertEqual(self.email_sender.call_count, 0)
                self.assertEqual(seller_client.get("/api/sales").json(), [])
                self.assertEqual(seller_client.get("/api/dashboard").json()["gross_sales"], 0)


if __name__ == "__main__":
    unittest.main()
