import hashlib
import hmac
import json
import unittest

import httpx

from backend.clickpesa import (
    ClickPesaClient,
    ClickPesaError,
    create_payload_checksum,
    verify_webhook_checksum,
)


class ClickPesaChecksumTests(unittest.TestCase):
    def test_checksum_is_order_independent_and_excludes_signature_fields(self):
        first = {
            "event": "PAYMENT RECEIVED",
            "data": {"orderReference": "LR123", "collectedAmount": "5000"},
            "checksumMethod": "HMAC-SHA256",
        }
        second = {
            "checksum": "ignored",
            "data": {"collectedAmount": "5000", "orderReference": "LR123"},
            "event": "PAYMENT RECEIVED",
        }

        self.assertEqual(
            create_payload_checksum("checksum-secret", first),
            create_payload_checksum("checksum-secret", second),
        )

    def test_verifies_webhook_checksum_without_accepting_tampered_payload(self):
        payload = {
            "event": "PAYMENT RECEIVED",
            "data": {"orderReference": "LR123", "collectedAmount": "5000"},
        }
        payload["checksum"] = create_payload_checksum("checksum-secret", payload)

        self.assertTrue(verify_webhook_checksum(payload, "checksum-secret"))
        payload["data"]["collectedAmount"] = "50000"
        self.assertFalse(verify_webhook_checksum(payload, "checksum-secret"))
        self.assertFalse(verify_webhook_checksum(payload, ""))


class ClickPesaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_previews_and_initiates_push_with_cached_auth_token(self):
        requests = []

        def handle_request(request):
            requests.append(request)
            if request.url.path.endswith("/generate-token"):
                return httpx.Response(200, json={"success": True, "token": "Bearer test-token"})
            if request.url.path.endswith("/preview-ussd-push-request"):
                return httpx.Response(
                    200,
                    json={"activeMethods": [{"name": "M-PESA", "status": "AVAILABLE"}]},
                )
            if request.url.path.endswith("/initiate-ussd-push-request"):
                return httpx.Response(
                    200,
                    json={
                        "id": "PAY123",
                        "status": "PROCESSING",
                        "orderReference": "LR1234567890123456",
                    },
                )
            return httpx.Response(404)

        client = ClickPesaClient(
            "test-client",
            "test-api-key",
            "test-checksum",
            transport=httpx.MockTransport(handle_request),
        )
        result = await client.initiate_ussd_push(
            "255712345678",
            5000,
            "LR1234567890123456",
        )

        self.assertEqual(result["status"], "PROCESSING")
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0].headers["client-id"], "test-client")
        self.assertEqual(requests[1].headers["Authorization"], "Bearer test-token")
        sent_payload = json.loads(requests[2].content)
        unsigned_payload = {
            key: value
            for key, value in sent_payload.items()
            if key != "checksum"
        }
        expected = hmac.new(
            b"test-checksum",
            json.dumps(unsigned_payload, separators=(",", ":"), sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(sent_payload["checksum"], expected)

    async def test_does_not_initiate_when_no_payment_method_is_available(self):
        requests = []

        def handle_request(request):
            requests.append(request.url.path)
            if request.url.path.endswith("/generate-token"):
                return httpx.Response(200, json={"success": True, "token": "Bearer test-token"})
            return httpx.Response(
                200,
                json={"activeMethods": [{"name": "M-PESA", "status": "UNAVAILABLE"}]},
            )

        client = ClickPesaClient(
            "test-client",
            "test-api-key",
            "test-checksum",
            transport=httpx.MockTransport(handle_request),
        )

        with self.assertRaises(ClickPesaError):
            await client.initiate_ussd_push(
                "255712345678",
                5000,
                "LR1234567890123456",
            )
        self.assertFalse(any(path.endswith("/initiate-ussd-push-request") for path in requests))


if __name__ == "__main__":
    unittest.main()
