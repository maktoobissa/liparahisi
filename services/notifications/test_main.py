import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from services.notifications.main import internal_status, require_internal_token, send_email
from backend.notification_providers import send_sms, send_whatsapp


class NotificationServiceTests(unittest.TestCase):
    def test_internal_endpoint_rejects_missing_and_invalid_tokens(self):
        with patch.dict(os.environ, {"NOTIFICATION_SERVICE_TOKEN": "secret"}):
            with self.assertRaises(HTTPException) as missing:
                require_internal_token(None)
            with self.assertRaises(HTTPException) as invalid:
                require_internal_token("wrong")

        self.assertEqual(missing.exception.status_code, 401)
        self.assertEqual(invalid.exception.status_code, 401)

    def test_internal_endpoint_fails_closed_without_configured_token(self):
        with patch.dict(os.environ, {"NOTIFICATION_SERVICE_TOKEN": ""}):
            with self.assertRaises(HTTPException) as raised:
                require_internal_token("anything")

        self.assertEqual(raised.exception.status_code, 503)

    def test_smtp_uses_starttls_and_clears_no_credentials_into_message(self):
        server = MagicMock()
        server.__enter__.return_value = server
        with patch.dict(
            os.environ,
            {
                "EMAIL_PROVIDER": "smtp",
                "SMTP_HOST": "smtp.example.com",
                "SMTP_PORT": "587",
                "SMTP_USERNAME": "sender@example.com",
                "SMTP_PASSWORD": "app-password",
                "SMTP_FROM": "sender@example.com",
                "SMTP_USE_SSL": "false",
            },
        ):
            with patch(
                "backend.email_delivery.smtplib.SMTP",
                return_value=server,
            ) as smtp:
                send_email({
                    "recipient": "buyer@example.com",
                    "subject": "Reset password",
                    "body": "Reset link",
                })

        smtp.assert_called_once_with("smtp.example.com", 587, timeout=20)
        server.starttls.assert_called_once_with()
        server.login.assert_called_once_with("sender@example.com", "app-password")
        message = server.send_message.call_args.args[0]
        self.assertEqual(message["To"], "buyer@example.com")
        self.assertEqual(message.get_content().strip(), "Reset link")

    def test_internal_status_reports_resend_configuration(self):
        with patch.dict(
            os.environ,
            {
                "NOTIFICATION_SERVICE_TOKEN": "secret",
                "EMAIL_PROVIDER": "resend",
                "RESEND_API_KEY": "test-key",
                "EMAIL_FROM": "mail@example.com",
            },
        ):
            status = internal_status("secret")

        self.assertTrue(status["email_configured"])
        self.assertFalse(status["smtp_configured"])
        self.assertEqual(status["email_provider"], "resend")

    def test_outbox_sends_using_resend_http_api(self):
        client = MagicMock()
        client.__enter__.return_value = client
        html = "<p>Reset link</p>"
        with patch.dict(
            os.environ,
            {
                "EMAIL_PROVIDER": "resend",
                "RESEND_API_KEY": "test-key",
                "EMAIL_FROM": "mail@example.com",
            },
        ):
            with patch("backend.email_delivery.httpx.Client") as client_factory:
                client_factory.return_value = client
                send_email({
                    "recipient": "buyer@example.com",
                    "subject": "Reset password",
                    "body": "Reset link",
                    "html_body": html,
                })

        client.post.assert_called_once_with(
            "https://api.resend.com/emails",
            headers={"Authorization": "Bearer test-key"},
            json={
                "from": "mail@example.com",
                "to": ["buyer@example.com"],
                "subject": "Reset password",
                "text": "Reset link",
                "html": html,
            },
        )

    def test_sms_provider_uses_twilio_sandbox_credentials(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.post.return_value.raise_for_status.return_value = None
        client.post.return_value.json.return_value = {"sid": "SM_TEST"}
        with patch.dict(
            os.environ,
            {
                "SMS_PROVIDER": "twilio",
                "TWILIO_ACCOUNT_SID": "AC_TEST",
                "TWILIO_AUTH_TOKEN": "TEST_TOKEN",
                "TWILIO_FROM_NUMBER": "+255700000000",
            },
        ):
            with patch("backend.notification_providers.httpx.Client") as client_factory:
                client_factory.return_value = client
                result = send_sms("+255711111111", "LipaRahisi test")

        self.assertEqual(result, "SM_TEST")
        self.assertIn("/Accounts/AC_TEST/Messages.json", client.post.call_args.args[0])

    def test_whatsapp_provider_uses_official_cloud_api(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.post.return_value.raise_for_status.return_value = None
        client.post.return_value.json.return_value = {"messages": [{"id": "wamid.TEST"}]}
        with patch.dict(
            os.environ,
            {
                "WHATSAPP_PHONE_NUMBER_ID": "123456",
                "WHATSAPP_ACCESS_TOKEN": "TEST_TOKEN",
                "WHATSAPP_API_VERSION": "v22.0",
            },
        ):
            with patch("backend.notification_providers.httpx.Client") as client_factory:
                client_factory.return_value = client
                result = send_whatsapp("+255711111111", "LipaRahisi test")

        self.assertEqual(result, "wamid.TEST")
        self.assertIn("graph.facebook.com/v22.0/123456/messages", client.post.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
