import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from services.catalog.main import verify_gateway_token


class CatalogApiTests(unittest.TestCase):
    def test_internal_endpoint_rejects_missing_and_invalid_tokens(self):
        with patch.dict(os.environ, {"CATALOG_SERVICE_TOKEN": "secret"}):
            with self.assertRaises(HTTPException) as missing:
                verify_gateway_token(None)
            with self.assertRaises(HTTPException) as invalid:
                verify_gateway_token("wrong")

        self.assertEqual(missing.exception.status_code, 401)
        self.assertEqual(invalid.exception.status_code, 401)

    def test_internal_endpoint_fails_closed_without_configured_token(self):
        with patch.dict(os.environ, {"CATALOG_SERVICE_TOKEN": ""}):
            with self.assertRaises(HTTPException) as raised:
                verify_gateway_token("anything")

        self.assertEqual(raised.exception.status_code, 503)
