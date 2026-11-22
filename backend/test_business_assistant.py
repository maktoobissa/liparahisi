import json
import os
import unittest
from unittest.mock import MagicMock, patch

from backend.business_assistant import (
    ai_recommendations,
    classify_question,
    render_answer,
)


class BusinessAssistantTests(unittest.TestCase):
    def test_questions_are_classified_and_answers_use_only_supplied_facts(self):
        facts = {
            "period": {"start": "2026-03-01", "end": "2026-03-08"},
            "sales": {"orders": 2, "amount": 125000, "estimated_profit": 35000},
            "top_products": [],
            "low_stock": [],
            "slow_products": [],
            "branches": [],
        }

        self.assertEqual(classify_question("Mauzo yangu ya mwezi huu yakoje?"), "sales")
        self.assertIn("125,000", render_answer("sales", facts, "en"))
        self.assertIn("35 000", render_answer("profit", facts, "fr"))
        self.assertIn("dépenses", render_answer("expenses", facts, "fr"))

    def test_ai_provider_receives_aggregates_not_product_names(self):
        facts = {
            "sales": {"orders": 4, "amount": 9000},
            "low_stock": [{"name": "Private product name", "stock": 1}],
            "branches": [],
        }
        response = MagicMock()
        response.json.return_value = {
            "output": [{
                "type": "message",
                "content": [{
                    "type": "output_text",
                    "text": json.dumps({"recommendations": ["REVIEW_REPLENISHMENT"]}),
                }],
            }],
        }
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "BUSINESS_AI_PROVIDER": "openai",
        }):
            with patch("backend.business_assistant.httpx.post", return_value=response) as post:
                result = ai_recommendations("low_stock", facts)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(result["codes"], ["REVIEW_REPLENISHMENT"])
        self.assertNotIn("Private product name", json.dumps(payload))
        self.assertEqual(payload["input"][1]["content"], json.dumps({
            "intent": "low_stock",
            "completed_orders": 4,
            "low_stock_count": 1,
            "branch_count": 0,
            "allowed_recommendations": ["REVIEW_REPLENISHMENT"],
        }, separators=(",", ":")))

    def test_disabled_provider_returns_rules_and_unknown_provider_fails_explicitly(self):
        facts = {"low_stock": [{"stock": 1}], "branches": []}
        with patch.dict(os.environ, {
            "BUSINESS_AI_PROVIDER": "disabled",
            "OPENAI_API_KEY": "test-key",
        }):
            self.assertEqual(
                ai_recommendations("low_stock", facts),
                {"status": "NOT_CONFIGURED", "codes": ["REVIEW_REPLENISHMENT"]},
            )
        with patch.dict(os.environ, {"BUSINESS_AI_PROVIDER": "unknown"}):
            with self.assertRaises(RuntimeError):
                ai_recommendations("low_stock", facts)


if __name__ == "__main__":
    unittest.main()
