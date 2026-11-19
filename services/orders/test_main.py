import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.orders.main import app


class OrdersServiceTests(unittest.TestCase):
    def test_order_routes_require_gateway_token(self):
        with patch.dict("os.environ", {"ORDERS_SERVICE_TOKEN": "orders-secret"}):
            with TestClient(app) as client:
                response = client.get("/dashboard")
                self.assertEqual(response.status_code, 401)
