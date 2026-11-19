import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.identity.main import app


class IdentityServiceTests(unittest.TestCase):
    def test_auth_routes_require_gateway_token(self):
        with patch.dict("os.environ", {"IDENTITY_SERVICE_TOKEN": "identity-secret"}):
            with TestClient(app) as client:
                status = client.get("/status")
                self.assertEqual(status.status_code, 401)
