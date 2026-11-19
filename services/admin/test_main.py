import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend import main
from services.admin.main import app as admin_app
from services.identity.main import app as identity_app


class InProcessIdentityClient:
    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=identity_app),
            base_url="http://identity",
        )
        return await self.client.__aenter__()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self.client.__aexit__(exc_type, exc_value, traceback)


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = main.DATABASE_PATH
        main.DATABASE_PATH = Path(self.temp_dir.name) / "admin.sqlite3"
        self.environment = patch.dict(
            os.environ,
            {
                "ADMIN_SETUP_TOKEN": "test-admin-setup-token-long-enough",
                "ADMIN_SERVICE_TOKEN": "test-gateway-service-token",
                "ADMIN_SERVICE_URL": "",
                "IDENTITY_SERVICE_TOKEN": "test-identity-service-token",
                "IDENTITY_SERVICE_URL": "http://identity",
                "DATABASE_URL": "",
                "DEMO_MODE": "true",
            },
            clear=False,
        )
        self.environment.start()
        self.identity_client_patch = patch(
            "backend.main.create_identity_service_client",
            InProcessIdentityClient,
        )
        self.identity_client_patch.start()
        self.core_context = TestClient(main.app)
        self.core_client = self.core_context.__enter__()
        setup = self.core_client.post(
            "/api/auth/setup",
            json={
                "setup_token": "test-admin-setup-token-long-enough",
                "full_name": "Main Admin",
                "email": "admin@example.com",
                "password": "admin-password-long",
            },
        )
        self.assertEqual(setup.status_code, 200)
        self.admin_session = self.core_client.cookies.get(main.SESSION_COOKIE)
        self.admin_context = TestClient(admin_app)
        self.admin_client = self.admin_context.__enter__()

    def tearDown(self):
        self.admin_context.__exit__(None, None, None)
        self.core_context.__exit__(None, None, None)
        self.identity_client_patch.stop()
        self.environment.stop()
        main.DATABASE_PATH = self.original_database_path
        self.temp_dir.cleanup()

    def test_admin_service_requires_gateway_token_and_admin_session(self):
        self.assertEqual(
            self.admin_client.get("/sellers").status_code,
            401,
        )
        headers = {"X-Admin-Service-Token": "test-gateway-service-token"}
        self.assertEqual(
            self.admin_client.get("/sellers", headers=headers).status_code,
            401,
        )
        sellers = self.admin_client.get(
            "/sellers",
            headers=headers,
            cookies={main.SESSION_COOKIE: self.admin_session},
        )
        self.assertEqual(sellers.status_code, 200)
        self.assertEqual(sellers.json(), [])

    def test_public_api_does_not_serve_admin_routes_without_gateway_config(self):
        with patch.dict(os.environ, {"ADMIN_SERVICE_TOKEN": ""}):
            response = self.core_client.get("/api/admin/sellers")
        self.assertEqual(response.status_code, 503)
