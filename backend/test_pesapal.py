import json
import unittest

import httpx

from backend.pesapal import PesapalClient, PesapalError, SANDBOX_API_BASE_URL


class PesapalClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_order_uses_sandbox_and_validates_returned_reference(self):
        requests = []
        merchant_reference = "LR0123456789ABCDEF"

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/api/Auth/RequestToken"):
                return httpx.Response(
                    200,
                    json={"token": "sandbox-token", "status": "200", "error": None},
                )
            return httpx.Response(
                200,
                json={
                    "order_tracking_id": "tracking-id",
                    "merchant_reference": merchant_reference,
                    "redirect_url": (
                        "https://cybqa.pesapal.com/pesapaliframe/"
                        "PesapalIframe3/Index/?OrderTrackingId=tracking-id"
                    ),
                    "status": "200",
                    "error": None,
                },
            )

        client = PesapalClient(
            "sandbox-key",
            "sandbox-secret",
            transport=httpx.MockTransport(handler),
        )
        result = await client.submit_order(
            merchant_reference=merchant_reference,
            amount=5000,
            description="Test order",
            callback_url="https://liparahisi.onrender.com/api/callbacks/pesapal",
            cancellation_url="https://liparahisi.onrender.com/pay/product?order_id=TEST",
            notification_id="84740ab4-3cd9-47da-8a4f-dd1db53494b5",
            buyer_name="Mteja mmoja",
            buyer_email="buyer@example.com",
            buyer_phone="0712345678",
        )

        self.assertEqual(result["order_tracking_id"], "tracking-id")
        self.assertEqual(requests[0].url, f"{SANDBOX_API_BASE_URL}/api/Auth/RequestToken")
        self.assertEqual(
            requests[0].headers["content-type"],
            "application/json",
        )
        self.assertEqual(
            json.loads(requests[0].content),
            {
                "consumer_key": "sandbox-key",
                "consumer_secret": "sandbox-secret",
            },
        )
        self.assertEqual(
            requests[1].url,
            f"{SANDBOX_API_BASE_URL}/api/Transactions/SubmitOrderRequest",
        )
        self.assertEqual(requests[1].headers["authorization"], "Bearer sandbox-token")
        submitted = json.loads(requests[1].content)
        self.assertEqual(submitted["id"], merchant_reference)
        self.assertEqual(submitted["currency"], "TZS")
        self.assertEqual(submitted["amount"], 5000)
        self.assertEqual(submitted["billing_address"]["email_address"], "buyer@example.com")
        self.assertEqual(submitted["notification_id"], "84740ab4-3cd9-47da-8a4f-dd1db53494b5")

    async def test_status_request_uses_tracking_id(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/api/Auth/RequestToken"):
                return httpx.Response(
                    200,
                    json={"token": "sandbox-token", "status": "200", "error": None},
                )
            return httpx.Response(
                200,
                json={
                    "status_code": 1,
                    "merchant_reference": "LR0123456789ABCDEF",
                    "amount": 5000,
                    "currency": "TZS",
                    "status": "200",
                    "error": None,
                },
            )

        client = PesapalClient(
            "sandbox-key",
            "sandbox-secret",
            transport=httpx.MockTransport(handler),
        )
        result = await client.get_transaction_status("tracking-id")

        self.assertEqual(result["status_code"], 1)
        self.assertEqual(
            str(requests[1].url),
            f"{SANDBOX_API_BASE_URL}/api/Transactions/GetTransactionStatus"
            "?orderTrackingId=tracking-id",
        )

    async def test_submit_order_rejects_non_sandbox_redirect(self):
        def handler(request):
            if request.url.path.endswith("/api/Auth/RequestToken"):
                return httpx.Response(
                    200,
                    json={"token": "sandbox-token", "status": "200", "error": None},
                )
            return httpx.Response(
                200,
                json={
                    "order_tracking_id": "tracking-id",
                    "merchant_reference": "LR0123456789ABCDEF",
                    "redirect_url": "https://attacker.example/pay",
                    "status": "200",
                    "error": None,
                },
            )

        client = PesapalClient(
            "sandbox-key",
            "sandbox-secret",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(PesapalError) as error:
            await client.submit_order(
                merchant_reference="LR0123456789ABCDEF",
                amount=5000,
                description="Test order",
                callback_url="https://liparahisi.onrender.com/api/callbacks/pesapal",
                cancellation_url="https://liparahisi.onrender.com/pay/product?order_id=TEST",
                notification_id="84740ab4-3cd9-47da-8a4f-dd1db53494b5",
                buyer_name="Mteja",
                buyer_email="buyer@example.com",
                buyer_phone="0712345678",
            )

        self.assertTrue(error.exception.uncertain)


if __name__ == "__main__":
    unittest.main()
