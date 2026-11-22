import base64
import os

import httpx


class NotificationProviderError(RuntimeError):
    pass


def env_flag(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def sms_configured():
    provider = os.getenv("SMS_PROVIDER", "").strip().lower()
    return provider == "twilio" and all(
        os.getenv(name, "").strip()
        for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
    )


def whatsapp_configured():
    return all(
        os.getenv(name, "").strip()
        for name in ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_ACCESS_TOKEN")
    )


def web_push_configured():
    return all(
        os.getenv(name, "").strip()
        for name in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_SUBJECT")
    )


def mobile_push_configured():
    return bool(os.getenv("FCM_SERVER_KEY", "").strip())


def send_sms(to_phone, text):
    if not sms_configured():
        raise NotificationProviderError("Twilio SMS haijawekwa kikamilifu.")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN").strip()
    sender = os.getenv("TWILIO_FROM_NUMBER").strip()
    credentials = base64.b64encode(
        f"{account_sid}:{auth_token}".encode("utf-8")
    ).decode("ascii")
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
                headers={"Authorization": f"Basic {credentials}"},
                data={"To": to_phone, "From": sender, "Body": text},
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("sid") or "")
    except httpx.HTTPStatusError as error:
        raise NotificationProviderError(
            f"Twilio imekataa ombi (HTTP {error.response.status_code})."
        ) from error
    except httpx.RequestError as error:
        raise NotificationProviderError("Imeshindikana kuungana na Twilio.") from error


def send_whatsapp(to_phone, text):
    if not whatsapp_configured():
        raise NotificationProviderError("WhatsApp Cloud API haijawekwa kikamilifu.")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID").strip()
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN").strip()
    api_version = os.getenv("WHATSAPP_API_VERSION", "v22.0").strip()
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to_phone,
                    "type": "text",
                    "text": {"preview_url": False, "body": text[:4096]},
                },
            )
            response.raise_for_status()
            data = response.json()
            messages = data.get("messages") or []
            return str(messages[0].get("id") or "") if messages else ""
    except httpx.HTTPStatusError as error:
        raise NotificationProviderError(
            f"WhatsApp Cloud API imekataa ombi (HTTP {error.response.status_code})."
        ) from error
    except httpx.RequestError as error:
        raise NotificationProviderError("Imeshindikana kuungana na WhatsApp.") from error


def send_mobile_push(subscription, title, text, link=None):
    server_key = os.getenv("FCM_SERVER_KEY", "").strip()
    if not server_key:
        raise NotificationProviderError("Firebase Cloud Messaging haijawekwa.")
    token = (
        subscription.get("token")
        or subscription.get("registration_token")
        or subscription.get("endpoint")
        if isinstance(subscription, dict)
        else subscription
    )
    if not token:
        raise NotificationProviderError("Device push token haipo.")
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                "https://fcm.googleapis.com/fcm/send",
                headers={"Authorization": f"key={server_key}", "Content-Type": "application/json"},
                json={
                    "to": token,
                    "notification": {"title": title, "body": text, "icon": "/icons/icon.svg"},
                    "data": {"link": link or "/"},
                },
            )
            response.raise_for_status()
            data = response.json()
            if data.get("failure"):
                raise NotificationProviderError("Firebase ilikataa device push.")
            return str(data.get("message_id") or "")
    except httpx.HTTPStatusError as error:
        raise NotificationProviderError(
            f"FCM imekataa ombi (HTTP {error.response.status_code})."
        ) from error
    except httpx.RequestError as error:
        raise NotificationProviderError("Imeshindikana kuungana na FCM.") from error


def send_web_push(subscription, title, text, link=None):
    if not web_push_configured():
        raise NotificationProviderError("Web Push VAPID haijawekwa.")
    try:
        from pywebpush import webpush, WebPushException
    except ImportError as error:
        raise NotificationProviderError("PyWebPush haijasakinishwa.") from error
    import json

    payload = json.dumps({"title": title, "body": text, "link": link or "/"})
    try:
        result = webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=os.getenv("VAPID_PRIVATE_KEY").strip(),
            vapid_claims={
                "sub": os.getenv("VAPID_SUBJECT").strip(),
            },
            ttl=60 * 60,
        )
        return str(result.status_code)
    except WebPushException as error:
        raise NotificationProviderError(f"Web Push imekataa ombi: {error}") from error
