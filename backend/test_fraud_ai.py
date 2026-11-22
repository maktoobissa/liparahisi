import json
import os
import time
import unittest
from unittest.mock import patch

import httpx

from backend.fraud_ai import analyze_security_alerts


class FraudAiTests(unittest.TestCase):
    def test_ai_stays_disabled_without_api_key(self):
        alert = {
            "id": "internal-alert-id",
            "category": "BRUTE_FORCE",
            "risk_score": 65,
            "evidence": [{"failed_count": 6}],
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch(
            "backend.fraud_ai.httpx.post"
        ) as post:
            result = analyze_security_alerts([alert])

        self.assertEqual(result["status"], "DISABLED")
        self.assertEqual(result["analyzed"], 0)
        post.assert_not_called()

    def test_openai_receives_only_aggregates_and_validates_reasons(self):
        internal_id = "private-alert-reference"
        ip_address = "198.51.100.23"
        alert = {
            "id": internal_id,
            "category": "BRUTE_FORCE",
            "risk_score": 65,
            "evidence": [
                {"failed_count": 6},
                {"blocked_until": time.time() + 300},
                {"ip_address": ip_address},
            ],
        }
        model_output = {
            "assessments": [
                {
                    "ref": 0,
                    "risk_score": 72,
                    "reason_codes": ["MULTIPLE_FAILED_LOGINS", "ACTIVE_LOGIN_LOCKOUT"],
                }
            ]
        }
        api_response = httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(model_output)},
                        ],
                    }
                ],
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
            "backend.fraud_ai.httpx.post",
            return_value=api_response,
        ) as post:
            result = analyze_security_alerts([alert])

        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["analyzed"], 1)
        assessment = result["assessments"][0]
        self.assertEqual(assessment["alert_id"], internal_id)
        self.assertEqual(assessment["risk_score"], 72)
        self.assertEqual(
            assessment["reasons"],
            ["Active login lockout", "Repeated failed logins"],
        )
        call = post.call_args
        self.assertEqual(call.args[0], "https://api.openai.com/v1/responses")
        payload = call.kwargs["json"]
        self.assertFalse(payload["store"])
        serialized_input = payload["input"][1]["content"]
        self.assertNotIn(internal_id, serialized_input)
        self.assertNotIn(ip_address, serialized_input)
        self.assertNotIn("blocked_until", serialized_input)
        self.assertIn('"account_lockout_active":true', serialized_input)

    def test_ai_rejects_reasons_not_supported_by_observed_evidence(self):
        alert = {
            "id": "alert-1",
            "category": "BRUTE_FORCE",
            "risk_score": 65,
            "evidence": [{"failed_count": 6}],
        }
        api_response = httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "assessments": [
                                            {
                                                "ref": 0,
                                                "risk_score": 95,
                                                "reason_codes": ["PAID_ORDERS_UNFULFILLED"],
                                            }
                                        ]
                                    }
                                ),
                            }
                        ],
                    }
                ],
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
            "backend.fraud_ai.httpx.post",
            return_value=api_response,
        ):
            result = analyze_security_alerts([alert])

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["analyzed"], 0)
        self.assertEqual(result["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
