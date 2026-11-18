import base64
import binascii
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx


class SelcomError(Exception):
    pass


def build_auth_headers(data, api_key, api_secret, timestamp=None):
    timestamp = timestamp or datetime.now(
        timezone(timedelta(hours=3))
    ).isoformat(timespec="seconds")
    signed_fields = list(data)
    signing_string = "timestamp=" + timestamp
    for field in signed_fields:
        signing_string += f"&{field}={data[field]}"

    digest = hmac.new(
        api_secret.encode("utf-8"),
        signing_string.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    authorization = base64.b64encode(api_key.encode("ascii")).decode("ascii")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"SELCOM {authorization}",
        "Digest-Method": "HS256",
        "Digest": base64.b64encode(digest).decode("ascii"),
        "Timestamp": timestamp,
        "Signed-Fields": ",".join(signed_fields),
    }


def decode_selcom_url(value):
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        parsed = urlsplit(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise SelcomError("Selcom imerudisha link ya malipo isiyo sahihi.") from exc

    if parsed.scheme != "https" or not parsed.netloc:
        raise SelcomError("Selcom imerudisha link isiyo salama ya malipo.")
    return decoded


class SelcomClient:
    def __init__(self, base_url, api_key, api_secret):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise SelcomError("SELCOM_BASE_URL lazima iwe HTTPS.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret

    async def request(self, method, path, data):
        headers = build_auth_headers(data, self.api_key, self.api_secret)
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=data if method == "POST" else None,
                    params=data if method == "GET" else None,
                )
                response.raise_for_status()
                result = response.json()
        except httpx.TimeoutException as exc:
            raise SelcomError(
                "Selcom haijajibu kwa wakati. Hali ya oda haijulikani; "
                "kagua hali kabla ya kuanzisha malipo mapya."
            ) from exc
        except httpx.RequestError as exc:
            raise SelcomError("Imeshindikana kuungana na Selcom.") from exc
        except httpx.HTTPStatusError as exc:
            raise SelcomError(
                f"Selcom imekataa ombi (HTTP {exc.response.status_code})."
            ) from exc
        except ValueError as exc:
            raise SelcomError("Selcom imerudisha majibu yasiyosomeka.") from exc

        if not isinstance(result, dict):
            raise SelcomError("Selcom imerudisha majibu yasiyo sahihi.")
        return result
