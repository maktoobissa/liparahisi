import base64
import hashlib
import hmac
import os
import struct
import time

from cryptography.fernet import Fernet, InvalidToken


def _fernet():
    key = os.getenv("MFA_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("MFA_ENCRYPTION_KEY haijawekwa.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError("MFA_ENCRYPTION_KEY si sahihi.") from error


def encrypt_totp_secret(secret):
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_totp_secret(encrypted_secret):
    try:
        return _fernet().decrypt(encrypted_secret.encode("ascii")).decode("ascii")
    except (InvalidToken, UnicodeEncodeError) as error:
        raise RuntimeError("Imeshindikana kusoma siri ya 2FA.") from error


def _kyc_fernet():
    key = os.getenv("SELLER_KYC_ENCRYPTION_KEY", "").strip()
    if not key:
        key = os.getenv("MFA_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("SELLER_KYC_ENCRYPTION_KEY haijawekwa.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError("SELLER_KYC_ENCRYPTION_KEY si sahihi.") from error


def encrypt_kyc_document(data):
    return _kyc_fernet().encrypt(data)


def decrypt_kyc_document(encrypted_data):
    try:
        return _kyc_fernet().decrypt(encrypted_data)
    except (InvalidToken, UnicodeEncodeError) as error:
        raise RuntimeError("Imeshindikana kusoma nyaraka ya KYC.") from error


def new_totp_secret():
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def totp_counter(secret, code, now=None):
    if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        return None
    timestamp = int(time.time() if now is None else now)
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    for counter in range(max(0, timestamp // 30 - 1), timestamp // 30 + 2):
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        if hmac.compare_digest(f"{value:06d}", code):
            return counter
    return None


def upload_media_type(content_type, data):
    signatures = {
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": (
            len(data) >= 12
            and data.startswith(b"RIFF")
            and data[8:12] == b"WEBP"
        ),
        "application/pdf": data.startswith(b"%PDF-"),
    }
    if content_type not in signatures:
        return False
    return signatures[content_type]
