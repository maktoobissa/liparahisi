import asyncio
import hashlib
import hmac
import json
import time

import httpx


API_BASE_URL = "https://api.clickpesa.com/third-parties"


class ClickPesaError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def _canonicalize(value):
    if isinstance(value, dict):
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    return value


def create_payload_checksum(checksum_key, payload):
    unsigned_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"checksum", "checksumMethod"}
    }
    encoded = json.dumps(
        _canonicalize(unsigned_payload),
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        checksum_key.encode("utf-8"),
        encoded,
        hashlib.sha256,
    ).hexdigest()


def verify_webhook_checksum(payload, checksum_key):
    if not isinstance(payload, dict) or not checksum_key:
        return False
    supplied_checksum = payload.get("checksum")
    if not isinstance(supplied_checksum, str):
        return False
    expected_checksum = create_payload_checksum(checksum_key, payload)
    return hmac.compare_digest(supplied_checksum.lower(), expected_checksum)


class ClickPesaClient:
    def __init__(self, client_id, api_key, checksum_key, transport=None):
        self.client_id = client_id
        self.api_key = api_key
        self.checksum_key = checksum_key
        self.transport = transport
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def _client(self):
        return httpx.AsyncClient(
            base_url=API_BASE_URL,
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
                        "/generate-token",
                        headers={
                            "client-id": self.client_id,
                            "api-key": self.api_key,
                        },
                    )
                    response.raise_for_status()
                    result = response.json()
            except httpx.TimeoutException as exc:
                raise ClickPesaError(
                    "ClickPesa haijajibu wakati wa uthibitishaji.",
                ) from exc
            except httpx.RequestError as exc:
                raise ClickPesaError(
                    "Imeshindikana kuungana na ClickPesa.",
                ) from exc
            except httpx.HTTPStatusError as exc:
                raise ClickPesaError(
                    f"ClickPesa imekataa uthibitishaji (HTTP {exc.response.status_code}).",
                ) from exc
            except ValueError as exc:
                raise ClickPesaError(
                    "ClickPesa imerudisha majibu yasiyosomeka.",
                ) from exc

            token = result.get("token") if isinstance(result, dict) else None
            if not isinstance(token, str) or not token.startswith("Bearer "):
                raise ClickPesaError(
                    "ClickPesa imerudisha token isiyo sahihi.",
                )
            self._token = token
            self._token_expires_at = time.monotonic() + 55 * 60
            return self._token

    async def _request(self, method, path, payload=None, payment_request=False):
        token = await self._authorization_token()
        headers = {"Authorization": token}
        request_payload = payload
        if payload is not None:
            request_payload = dict(payload)
            request_payload["checksum"] = create_payload_checksum(
                self.checksum_key,
                request_payload,
            )
        try:
            async with self._client() as client:
                response = await client.request(
                    method,
                    path,
                    headers=headers,
                    json=request_payload,
                )
                response.raise_for_status()
                result = response.json()
        except httpx.TimeoutException as exc:
            raise ClickPesaError(
                "ClickPesa haijajibu kwa wakati; hali ya ombi la malipo haijulikani.",
                uncertain=payment_request,
            ) from exc
        except httpx.RequestError as exc:
            raise ClickPesaError(
                "Imeshindikana kuungana na ClickPesa.",
                uncertain=payment_request,
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ClickPesaError(
                f"ClickPesa imekataa ombi (HTTP {exc.response.status_code}).",
                uncertain=payment_request and exc.response.status_code >= 500,
            ) from exc
        except ValueError as exc:
            raise ClickPesaError(
                "ClickPesa imerudisha majibu yasiyosomeka.",
                uncertain=payment_request,
            ) from exc

        if not isinstance(result, dict):
            raise ClickPesaError(
                "ClickPesa imerudisha majibu yasiyo sahihi.",
                uncertain=payment_request,
            )
        return result

    async def initiate_ussd_push(self, phone_number, amount, order_reference):
        preview = await self._request(
            "POST",
            "/payments/preview-ussd-push-request",
            {
                "amount": str(amount),
                "currency": "TZS",
                "orderReference": order_reference,
                "phoneNumber": phone_number,
            },
        )
        methods = preview.get("activeMethods")
        if not isinstance(methods, list) or not any(
            isinstance(method, dict) and method.get("status") == "AVAILABLE"
            for method in methods
        ):
            raise ClickPesaError(
                "Hakuna njia ya malipo ya simu inayopatikana kwa ombi hili.",
            )

        result = await self._request(
            "POST",
            "/payments/initiate-ussd-push-request",
            {
                "amount": str(amount),
                "currency": "TZS",
                "orderReference": order_reference,
                "phoneNumber": phone_number,
            },
            payment_request=True,
        )
        returned_reference = result.get("orderReference")
        if returned_reference != order_reference:
            raise ClickPesaError(
                "ClickPesa imerudisha rejea ya oda isiyo sahihi; "
                "hali ya malipo inahitaji kuhakikiwa.",
                uncertain=True,
            )
        if result.get("status") not in {"PROCESSING", "PENDING", "SUCCESS", "SETTLED", "FAILED"}:
            raise ClickPesaError(
                "ClickPesa imerudisha hali ya malipo isiyotambulika.",
                uncertain=True,
            )
        return result
