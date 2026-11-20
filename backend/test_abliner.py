import json
import unittest

import httpx

from backend.abliner import (
    AblinerClient,
    AblinerError,
    ABLINER_API_BASE_URL,
    verify_webhook_signature,
)


class AblinerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_mobile_deposit_is_signed_and_idempotent(self):
        requests = []
        reference = "LR0123456789ABCDEF"

        def handler(request):
            requests.append(request)
            return httpx.Response(
                201,
                json={
                    "status": "success",
                    "data": {
                        "id": "tx_123",
                        "type": "deposit",
                        "method": "mobile",
                        "amount": 5000,
                        "currency": "TZS",
                        "status": "pending",
                        "customer_reference": reference,
                    },
                },
            )

        client = AblinerClient(
            "test-api-key",
            "test-webhook-secret",
            transport=httpx.MockTransport(handler),
        )
        result = await client.create_mobile_deposit(
            amount=5000,
            phone="255712345678",
            reference=reference,
            callback_url="https://liparahisi.onrender.com/api/webhooks/abliner",
        )

        request = requests[0]
        self.assertEqual(request.url, f"{ABLINER_API_BASE_URL}/deposits")
        self.assertEqual(request.headers["authorization"], "Bearer test-api-key")
        self.assertEqual(request.headers["idempotency-key"], reference)
        raw_body = request.content
        self.assertTrue(
            verify_webhook_signature(
                raw_body,
                request.headers["x-abliner-timestamp"],
                request.headers["x-abliner-signature"],
                "test-webhook-secret",
            )
        )
        self.assertEqual(
            json.loads(raw_body),
            {
                "amount": 5000,
                "method": "mobile",
                "currency": "TZS",
                "phone": "255712345678",
                "reference": reference,
                "callback_url": "https://liparahisi.onrender.com/api/webhooks/abliner",
            },
        )
        self.assertEqual(result["id"], "tx_123")

    async def test_network_timeout_is_uncertain(self):
        def handler(_request):
            raise httpx.TimeoutException("simulated timeout")

        client = AblinerClient(
            "test-api-key",
            "test-webhook-secret",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(AblinerError) as error:
            await client.create_mobile_deposit(
                amount=5000,
                phone="255712345678",
                reference="LR0123456789ABCDEF",
                callback_url="https://liparahisi.onrender.com/api/webhooks/abliner",
            )

        self.assertTrue(error.exception.uncertain)


class AblinerWebhookSignatureTests(unittest.TestCase):
    def test_signature_rejects_stale_timestamp_and_tampered_body(self):
        raw_body = b'{"event":"transaction.completed"}'
        timestamp = "1700000000"

        self.assertFalse(
            verify_webhook_signature(
                raw_body,
                timestamp,
                "0" * 64,
                "test-webhook-secret",
                now=1700000301,
            )
        )
        self.assertFalse(
            verify_webhook_signature(
                b'{"event":"transaction.failed"}',
                "1700000000",
                "0" * 64,
                "test-webhook-secret",
                now=1700000000,
            )
        )


if __name__ == "__main__":
    unittest.main()
