import base64
import hashlib
import hmac
import unittest

from backend.selcom import SelcomError, build_auth_headers, decode_selcom_url


class SelcomSigningTests(unittest.TestCase):
    def test_signed_fields_follow_payload_order(self):
        data = {"order_id": "LR123", "amount": 4500}
        timestamp = "2026-11-18T01:00:00+03:00"
        headers = build_auth_headers(
            data,
            "sandbox-key",
            "sandbox-secret",
            timestamp,
        )
        signing_string = (
            f"timestamp={timestamp}&order_id=LR123&amount=4500"
        )
        digest = hmac.new(
            b"sandbox-secret",
            signing_string.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        self.assertEqual(headers["Signed-Fields"], "order_id,amount")
        self.assertEqual(headers["Timestamp"], timestamp)
        self.assertEqual(
            headers["Digest"],
            base64.b64encode(digest).decode("ascii"),
        )
        self.assertEqual(
            headers["Authorization"],
            "SELCOM " + base64.b64encode(b"sandbox-key").decode("ascii"),
        )

    def test_decodes_https_checkout_link(self):
        encoded = base64.b64encode(
            b"https://checkout.selcommobile.com/pay/abc"
        ).decode("ascii")
        self.assertEqual(
            decode_selcom_url(encoded),
            "https://checkout.selcommobile.com/pay/abc",
        )

    def test_rejects_non_https_checkout_link(self):
        encoded = base64.b64encode(b"http://example.com/pay").decode("ascii")
        with self.assertRaises(SelcomError):
            decode_selcom_url(encoded)


if __name__ == "__main__":
    unittest.main()
