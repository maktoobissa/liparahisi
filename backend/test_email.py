import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from backend.main import integration_status, send_smtp_email
from backend.email_delivery import RESEND_API_URL


class SmtpEmailTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_PORT": "587",
                "SMTP_USERNAME": "sender@example.com",
                "SMTP_PASSWORD": "app-password",
                "SMTP_FROM": "sender@example.com",
                "SMTP_USE_SSL": "false",
                "EMAIL_PROVIDER": "smtp",
                "EMAIL_FROM": "",
                "RESEND_API_KEY": "",
                "NOTIFICATION_SERVICE_URL": "",
                "NOTIFICATION_SERVICE_TOKEN": "",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_sends_using_starttls_on_submission_port(self):
        server = MagicMock()
        server.__enter__.return_value = server
        with patch("backend.email_delivery.smtplib.SMTP", return_value=server) as smtp:
            with patch("backend.email_delivery.smtplib.SMTP_SSL") as smtp_ssl:
                send_smtp_email("buyer@example.com", "Subject", "Body")

        smtp.assert_called_once_with("smtp.example.com", 587, timeout=20)
        smtp_ssl.assert_not_called()
        server.starttls.assert_called_once_with()
        server.login.assert_called_once_with("sender@example.com", "app-password")
        message = server.send_message.call_args.args[0]
        self.assertEqual(message["To"], "buyer@example.com")
        self.assertEqual(message.get_content().strip(), "Body")

    def test_queues_through_notification_service_when_configured(self):
        client = MagicMock()
        client.__enter__.return_value = client
        with patch.dict(
            os.environ,
            {
                "NOTIFICATION_SERVICE_URL": "http://notifications:8001",
                "NOTIFICATION_SERVICE_TOKEN": "service-token",
            },
        ):
            with patch("backend.main.httpx.Client") as client_factory:
                client_factory.return_value = client
                with patch("backend.email_delivery.smtplib.SMTP") as smtp:
                    send_smtp_email("buyer@example.com", "Subject", "Body")

        client.post.assert_called_once_with(
            "http://notifications:8001/internal/emails",
            headers={"X-Internal-Token": "service-token"},
            json={
                "to_email": "buyer@example.com",
                "subject": "Subject",
                "text": "Body",
            },
        )
        smtp.assert_not_called()

    def test_sends_using_implicit_ssl_without_starttls(self):
        server = MagicMock()
        server.__enter__.return_value = server
        with patch.dict(
            os.environ,
            {"SMTP_PORT": "465", "SMTP_USE_SSL": "true"},
        ):
            with patch("backend.email_delivery.smtplib.SMTP") as smtp:
                with patch(
                    "backend.email_delivery.smtplib.SMTP_SSL",
                    return_value=server,
                ) as smtp_ssl:
                    send_smtp_email("buyer@example.com", "Subject", "Body")

        smtp.assert_not_called()
        smtp_ssl.assert_called_once_with("smtp.example.com", 465, timeout=20)
        server.starttls.assert_not_called()
        server.login.assert_called_once_with("sender@example.com", "app-password")

    def test_connection_errors_return_gateway_error(self):
        with patch(
            "backend.email_delivery.smtplib.SMTP",
            side_effect=OSError("connection refused"),
        ):
            with self.assertRaises(HTTPException) as raised:
                send_smtp_email("buyer@example.com", "Subject", "Body")

        self.assertEqual(raised.exception.status_code, 502)

    def test_sends_through_resend_over_https(self):
        client = MagicMock()
        client.__enter__.return_value = client
        with patch.dict(
            os.environ,
            {
                "EMAIL_PROVIDER": "resend",
                "RESEND_API_KEY": "test-resend-key",
                "EMAIL_FROM": "LipaRahisi <mail@example.com>",
            },
        ):
            with patch("backend.email_delivery.httpx.Client") as client_factory:
                client_factory.return_value = client
                send_smtp_email("buyer@example.com", "Subject", "Body")

        client.post.assert_called_once_with(
            RESEND_API_URL,
            headers={"Authorization": "Bearer test-resend-key"},
            json={
                "from": "LipaRahisi <mail@example.com>",
                "to": ["buyer@example.com"],
                "subject": "Subject",
                "text": "Body",
            },
        )

    def test_resend_fails_clearly_without_configuration(self):
        with patch.dict(
            os.environ,
            {"EMAIL_PROVIDER": "resend", "RESEND_API_KEY": "", "EMAIL_FROM": ""},
        ):
            with self.assertRaises(HTTPException) as raised:
                send_smtp_email("buyer@example.com", "Subject", "Body")

        self.assertEqual(raised.exception.status_code, 503)

    def test_integration_status_reports_resend_without_smtp(self):
        with patch.dict(
            os.environ,
            {
                "EMAIL_PROVIDER": "resend",
                "RESEND_API_KEY": "test-resend-key",
                "EMAIL_FROM": "mail@example.com",
                "NOTIFICATION_SERVICE_URL": "",
                "SELCOM_LIVE_ENABLED": "true",
            },
        ):
            status = integration_status()

        self.assertTrue(status["email_configured"])
        self.assertFalse(status["smtp_configured"])
        self.assertFalse(status["live_payments_enabled"])
        self.assertEqual(status["email_provider"], "resend")
