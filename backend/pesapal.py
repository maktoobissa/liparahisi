import asyncio
import time
from uuid import UUID
from urllib.parse import urlsplit

import httpx


SANDBOX_API_BASE_URL = "https://cybqa.pesapal.com/pesapalv3"


class PesapalError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class PesapalClient:
    def __init__(self, consumer_key, consumer_secret, transport=None):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.transport = transport
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def _client(self):
        return httpx.AsyncClient(
            base_url=SANDBOX_API_BASE_URL,
            timeout=25.0,
            transport=self.transport,
        )

    async def _authorization_token(self):
        async with self._token_lock:
            if self._token and self._token_expires_at > time.monotonic():
                return self._token
            try:
                async with self._client() as client:
                    response = await client.post(
                        "/api/Auth/RequestToken",
                        headers={"Accept": "application/json"},
                        json={
                            "consumer_key": self.consumer_key,
                            "consumer_secret": self.consumer_secret,
                        },
                    )
                    response.raise_for_status()
                    result = response.json()
            except httpx.TimeoutException as error:
                raise PesapalError("PesaPal haijajibu wakati wa uthibitishaji.") from error
            except httpx.RequestError as error:
                raise PesapalError("Imeshindikana kuungana na PesaPal.") from error
            except httpx.HTTPStatusError as error:
                raise PesapalError(
                    f"PesaPal imekataa uthibitishaji (HTTP {error.response.status_code})."
                ) from error
            except ValueError as error:
                raise PesapalError("PesaPal imerudisha majibu yasiyosomeka.") from error

            token = result.get("token") if isinstance(result, dict) else None
            if (
                not isinstance(token, str)
                or not token
                or result.get("error")
                or str(result.get("status", "200")) != "200"
            ):
                raise PesapalError("PesaPal imeshindwa kuthibitisha akaunti ya sandbox.")
            self._token = token
            self._token_expires_at = time.monotonic() + 4 * 60
            return self._token

    async def _request(self, method, path, *, payload=None, params=None, payment_request=False):
        token = await self._authorization_token()
        try:
            async with self._client() as client:
                response = await client.request(
                    method,
                    path,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                    json=payload,
                    params=params,
                )
                response.raise_for_status()
                result = response.json()
        except httpx.TimeoutException as error:
            raise PesapalError(
                "PesaPal haijajibu kwa wakati.",
                uncertain=payment_request,
            ) from error
        except httpx.RequestError as error:
            raise PesapalError(
                "Imeshindikana kuungana na PesaPal.",
                uncertain=payment_request,
            ) from error
        except httpx.HTTPStatusError as error:
            raise PesapalError(
                f"PesaPal imekataa ombi (HTTP {error.response.status_code}).",
                uncertain=payment_request and error.response.status_code >= 500,
            ) from error
        except ValueError as error:
            raise PesapalError(
                "PesaPal imerudisha majibu yasiyosomeka.",
                uncertain=payment_request,
            ) from error

        if (
            not isinstance(result, dict)
            or result.get("error")
            or str(result.get("status", "200")) != "200"
        ):
            raise PesapalError(
                "PesaPal imeshindwa kushughulikia ombi.",
                uncertain=payment_request,
            )
        return result

    async def submit_order(
        self,
        *,
        merchant_reference,
        amount,
        description,
        callback_url,
        cancellation_url,
        notification_id,
        buyer_name,
        buyer_email,
        buyer_phone,
    ):
        try:
            UUID(notification_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise PesapalError("PESAPAL_NOTIFICATION_ID si UUID sahihi.") from error

        names = buyer_name.split()
        result = await self._request(
            "POST",
            "/api/Transactions/SubmitOrderRequest",
            payload={
                "id": merchant_reference,
                "currency": "TZS",
                "amount": amount,
                "description": description[:100],
                "redirect_mode": "TOP_WINDOW",
                "callback_url": callback_url,
                "cancellation_url": cancellation_url,
                "notification_id": notification_id,
                "billing_address": {
                    "email_address": buyer_email,
                    "phone_number": buyer_phone,
                    "country_code": "TZ",
                    "first_name": names[0] if names else "Mteja",
                    "last_name": " ".join(names[1:]) or (names[0] if names else "Mteja"),
                },
            },
            payment_request=True,
        )
        tracking_id = result.get("order_tracking_id")
        returned_reference = result.get("merchant_reference")
        redirect_url = result.get("redirect_url")
        parsed_redirect = urlsplit(redirect_url) if isinstance(redirect_url, str) else None
        if (
            not isinstance(tracking_id, str)
            or not tracking_id
            or returned_reference != merchant_reference
            or parsed_redirect is None
            or parsed_redirect.scheme != "https"
            or parsed_redirect.hostname != "cybqa.pesapal.com"
        ):
            raise PesapalError(
                "PesaPal imerudisha taarifa zisizolingana na ombi la sandbox.",
                uncertain=True,
            )
        return {
            "order_tracking_id": tracking_id,
            "merchant_reference": returned_reference,
            "redirect_url": redirect_url,
        }

    async def get_transaction_status(self, tracking_id):
        return await self._request(
            "GET",
            "/api/Transactions/GetTransactionStatus",
            params={"orderTrackingId": tracking_id},
        )
