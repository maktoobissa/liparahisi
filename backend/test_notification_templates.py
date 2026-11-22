import unittest

from backend.notification_templates import _TEMPLATES, notification_template


class NotificationTemplateTests(unittest.TestCase):
    def test_templates_render_in_all_supported_languages(self):
        rendered = {
            language: notification_template(
                "ORDER_CONFIRMED",
                {"order_id": "LR-123"},
                language,
            )
            for language in ("sw", "en", "fr")
        }

        self.assertEqual(rendered["sw"]["text"], "Oda LR-123 imehakikiwa.")
        self.assertEqual(rendered["en"]["text"], "Order LR-123 has been confirmed.")
        self.assertEqual(rendered["fr"]["text"], "La commande LR-123 a été confirmée.")

    def test_every_event_has_templates_for_all_supported_languages(self):
        for event, templates in _TEMPLATES.items():
            with self.subTest(event=event):
                self.assertEqual(set(templates), {"sw", "en", "fr"})
                for language in ("sw", "en", "fr"):
                    rendered = notification_template(event, language=language)
                    self.assertTrue(rendered["title"])
                    self.assertTrue(rendered["text"])

    def test_missing_template_values_and_unsupported_language_are_safe(self):
        self.assertEqual(
            notification_template("NEW_ORDER", {}, "en")["text"],
            "There is a new update on your account.",
        )
        self.assertEqual(
            notification_template("UNLISTED_EVENT", language="fr")["title"],
            "Mise à jour LipaRahisi",
        )
        self.assertEqual(
            notification_template("UNLISTED_EVENT", language="unsupported")["title"],
            "Taarifa ya LipaRahisi",
        )

    def test_known_status_codes_are_localized_without_translating_free_text(self):
        rendered = notification_template(
            "DELIVERY_UPDATED",
            {"tracking_code": "LR-TZ-2026-123456", "status": "OUT_FOR_DELIVERY"},
            "fr",
        )
        self.assertEqual(
            rendered["text"],
            "Colis LR-TZ-2026-123456 : En cours de livraison.",
        )
        unknown = notification_template(
            "ORDER_STATUS_CHANGED",
            {"order_id": "LR-1", "status": "seller provided status"},
            "en",
        )
        self.assertIn("seller provided status", unknown["text"])


if __name__ == "__main__":
    unittest.main()
