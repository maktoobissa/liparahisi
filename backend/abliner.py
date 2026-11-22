import hashlib
import hmac
import json
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx


ABLINER_API_BASE_URL = "https://abliner.net/api/v1"


class AblinerError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def verify_webhook_signature(raw_body, timestamp, signature, secret, now=None):
    if not secret or not isinstance(raw_body, bytes):
        return False
    if not isinstance(timestamp, str) or not re.fullmatch(r"\d{1,12}", timestamp):
        return False
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
        return False
    try:
        timestamp_value = int(timestamp)
    except ValueError:
        return False
    current_time = time.time() if now is None else now
    if abs(current_time - timestamp_value) > 300:
        return False

    message = timestamp.encode("ascii") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return hmac.compare_digest(expected, bytes.fromhex(signature))


class AblinerClient:
    def __init__(self, api_key, webhook_secret, transport=None):
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.transport = transport

    def _client(self):
        return httpx.AsyncClient(
            base_url=ABLINER_API_BASE_URL,
            timeout=20.0,
            transport=self.transport,
        )

    async def create_mobile_deposit(
        self,
        *,
        amount,
        phone,
        reference,
        callback_url,
    ):
        if not isinstance(amount, int) or amount < 500 or amount > 3_000_000:
            raise AblinerError("Kiasi cha malipo ya Abliner kiko nje ya viwango.")
        if not isinstance(phone, str) or not re.fullmatch(r"255\d{9}", phone):
            raise AblinerError("Abliner inahitaji namba ya simu ya Tanzania yenye 255.")
        if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", reference):
            raise AblinerError("Rejea ya malipo ya Abliner si sahihi.")
        parsed_callback = urlsplit(callback_url) if isinstance(callback_url, str) else None
        if (
            parsed_callback is None
            or parsed_callback.scheme != "https"
            or not parsed_callback.hostname
        ):
            raise AblinerError("Abliner inahitaji callback URL salama ya HTTPS.")

        payload = {
            "amount": amount,
            "method": "mobile",
            "currency": "TZS",
            "phone": phone,
            "reference": reference,
            "callback_url": callback_url,
        }
        raw_body = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        message = timestamp.encode("ascii") + b"." + raw_body
        signature = hmac.new(
            self.webhook_secret.encode("utf-8"),
            message,
            hashlib.sha256,
        ).hexdigest()

        try:
            async with self._client() as client:
                response = await client.post(
                    "/deposits",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": reference,
                        "x-abliner-timestamp": timestamp,
                        "x-abliner-signature": signature,
                    },
                    content=raw_body,
                )
                response.raise_for_status()
                result = response.json()
        except httpx.TimeoutException as error:
            raise AblinerError(
                "Abliner haijajibu kwa wakati; angalia hali ya malipo kabla ya kujaribu tena.",
                uncertain=True,
            ) from error
        except httpx.RequestError as error:
            raise AblinerError(
                "Imeshindikana kuwasiliana na Abliner; angalia hali ya malipo kabla ya kujaribu tena.",
                uncertain=True,
            ) from error
        except httpx.HTTPStatusError as error:
            raise AblinerError(
                f"Abliner imekataa ombi (HTTP {error.response.status_code}).",
                uncertain=error.response.status_code >= 500,
            ) from error
        except ValueError as error:
            raise AblinerError(
                "Abliner imerudisha jibu lisilosomeka; angalia hali ya malipo kabla ya kujaribu tena.",
                uncertain=True,
            ) from error

        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict) or result.get("status") != "success":
            raise AblinerError("Abliner haikukubali ombi la malipo.")
        try:
            returned_amount = Decimal(str(data.get("amount")))
        except (InvalidOperation, ValueError):
            returned_amount = Decimal("NaN")
        if (
            data.get("type") != "deposit"
            or data.get("status") != "pending"
            or data.get("currency") != "TZS"
            or data.get("customer_reference") != reference
            or not returned_amount.is_finite()
            or returned_amount != amount
            or not isinstance(data.get("id"), str)
            or not data["id"]
        ):
            raise AblinerError(
                "Abliner imerudisha taarifa zisizolingana; angalia hali ya malipo kabla ya kujaribu tena.",
                uncertain=True,
            )
        return data
