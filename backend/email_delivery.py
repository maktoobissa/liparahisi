import os
import smtplib
from email.message import EmailMessage

import httpx


RESEND_API_URL = "https://api.resend.com/emails"


class EmailDeliveryError(RuntimeError):
    pass


def email_provider():
    return os.getenv("EMAIL_PROVIDER", "smtp").strip().lower() or "smtp"


def email_is_configured():
    provider = email_provider()
    if provider == "resend":
        return all(
            os.getenv(key, "").strip()
            for key in ("RESEND_API_KEY", "EMAIL_FROM")
        )
    if provider == "smtp":
        username = os.getenv("SMTP_USERNAME", "").strip()
        sender = os.getenv("SMTP_FROM", "").strip() or username
        return all(
            (
                os.getenv("SMTP_HOST", "").strip(),
                username,
                os.getenv("SMTP_PASSWORD", "").strip(),
                sender,
            )
        )
    return False


def deliver_email(to_email, subject, text, html=None):
    provider = email_provider()
    if provider == "resend":
        api_key = os.getenv("RESEND_API_KEY", "").strip()
        sender = os.getenv("EMAIL_FROM", "").strip()
        if not api_key or not sender:
            raise EmailDeliveryError(
                "Resend haijawekwa. Weka RESEND_API_KEY na EMAIL_FROM."
            )
        try:
            with httpx.Client(timeout=20.0) as client:
                payload = {
                    "from": sender,
                    "to": [to_email],
                    "subject": subject,
                    "text": text,
                }
                if html:
                    payload["html"] = html
                response = client.post(
                    RESEND_API_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
                response.raise_for_status()
        except httpx.TimeoutException as error:
            raise EmailDeliveryError("Resend haijajibu kwa wakati.") from error
        except httpx.HTTPStatusError as error:
            raise EmailDeliveryError(
                f"Resend imekataa ombi (HTTP {error.response.status_code})."
            ) from error
        except httpx.RequestError as error:
            raise EmailDeliveryError("Imeshindikana kuungana na Resend.") from error
        return

    if provider != "smtp":
        raise EmailDeliveryError("EMAIL_PROVIDER lazima iwe smtp au resend.")

    host = os.getenv("SMTP_HOST", "").strip()
    try:
        port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    except ValueError as error:
        raise EmailDeliveryError("SMTP_PORT si namba sahihi.") from error
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM", username).strip()
    if not host or not username or not password or not sender:
        raise EmailDeliveryError("SMTP haijawekwa kikamilifu.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to_email
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    use_ssl = (
        os.getenv("SMTP_USE_SSL", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    try:
        server = (
            smtplib.SMTP_SSL(host, port, timeout=20)
            if use_ssl
            else smtplib.SMTP(host, port, timeout=20)
        )
        with server:
            if not use_ssl:
                server.starttls()
            server.login(username, password)
            server.send_message(message)
    except (smtplib.SMTPException, TimeoutError, OSError) as error:
        raise EmailDeliveryError("Imeshindikana kutuma barua pepe kupitia SMTP.") from error
