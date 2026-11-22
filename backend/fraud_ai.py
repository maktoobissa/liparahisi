import json
import logging
import math
import os
import time
from typing import Any

import httpx


logger = logging.getLogger(__name__)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-4o-mini"
MAX_ALERTS_PER_SCAN = 20
SAFE_NUMERIC_EVIDENCE = {
    "failed_count",
    "failed_login_count",
    "unfulfilled_completed_orders",
    "cancellation_requests",
    "completed_orders",
    "request_rate",
}
REASON_LABELS = {
    "MULTIPLE_FAILED_LOGINS": "Repeated failed logins",
    "ACTIVE_LOGIN_LOCKOUT": "Active login lockout",
    "FAILED_LOGIN_BURST": "Repeated failed logins from one IP",
    "PAID_ORDERS_UNFULFILLED": "Paid orders remain unfulfilled",
    "REPEATED_CANCELLATIONS": "Repeated cancellation requests",
    "HIGH_CANCELLATION_RATIO": "Cancellation requests are high relative to completed orders",
}
CATEGORY_REASON_CODES = {
    "BRUTE_FORCE": {"MULTIPLE_FAILED_LOGINS", "ACTIVE_LOGIN_LOCKOUT"},
    "UNUSUAL_LOGIN_ACTIVITY": {"FAILED_LOGIN_BURST"},
    "SELLER_RISK": {"PAID_ORDERS_UNFULFILLED", "REPEATED_CANCELLATIONS"},
    "REPEATED_CANCELLATIONS": {"REPEATED_CANCELLATIONS", "HIGH_CANCELLATION_RATIO"},
}


def _safe_evidence(evidence: list[dict[str, Any]]) -> dict[str, int | float | bool]:
    result: dict[str, int | float | bool] = {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if key == "blocked_until" and isinstance(value, (int, float)):
                result["account_lockout_active"] = value > time.time()
            elif (
                key in SAFE_NUMERIC_EVIDENCE
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                result[key] = value
    return result


def _available_reason_codes(category: str, evidence: dict[str, int | float | bool]) -> list[str]:
    codes = set()
    if category == "BRUTE_FORCE":
        if evidence.get("failed_count", 0) >= 5:
            codes.add("MULTIPLE_FAILED_LOGINS")
        if evidence.get("account_lockout_active") is True:
            codes.add("ACTIVE_LOGIN_LOCKOUT")
    elif category == "UNUSUAL_LOGIN_ACTIVITY":
        if evidence.get("failed_login_count", 0) >= 10:
            codes.add("FAILED_LOGIN_BURST")
    elif category == "SELLER_RISK":
        if evidence.get("unfulfilled_completed_orders", 0) >= 3:
            codes.add("PAID_ORDERS_UNFULFILLED")
        if evidence.get("cancellation_requests", 0) >= 3:
            codes.add("REPEATED_CANCELLATIONS")
    elif category == "REPEATED_CANCELLATIONS":
        if evidence.get("cancellation_requests", 0) >= 3:
            codes.add("REPEATED_CANCELLATIONS")
        if evidence.get("request_rate", 0) >= 0.5:
            codes.add("HIGH_CANCELLATION_RATIO")
    return sorted(codes)


def _output_text(response_body: dict[str, Any]) -> str | None:
    output_items = response_body.get("output", [])
    if not isinstance(output_items, list):
        return None
    for output_item in output_items:
        if not isinstance(output_item, dict):
            continue
        if output_item.get("type") != "message":
            continue
        content_items = output_item.get("content", [])
        if not isinstance(content_items, list):
            continue
        for content_item in content_items:
            if not isinstance(content_item, dict):
                continue
            if content_item.get("type") == "output_text":
                text = content_item.get("text")
                if isinstance(text, str):
                    return text
    return None


def _result(status: str, model: str | None, assessments=None, skipped=0):
    assessments = assessments or []
    return {
        "status": status,
        "model": model,
        "analyzed": len(assessments),
        "skipped": skipped,
        "assessments": assessments,
    }


def analyze_security_alerts(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _result("DISABLED", None, skipped=len(alerts))
    model = os.getenv("OPENAI_FRAUD_MODEL", DEFAULT_MODEL).strip()
    if not model or len(model) > 100 or any(char.isspace() for char in model):
        return _result("FAILED", model, skipped=len(alerts))
    if not alerts:
        return _result("SUCCEEDED", model)

    candidates = []
    for alert in alerts[:MAX_ALERTS_PER_SCAN]:
        safe_evidence = _safe_evidence(alert.get("evidence", []))
        allowed_codes = _available_reason_codes(alert["category"], safe_evidence)
        if not allowed_codes:
            continue
        candidates.append(
            {
                "alert_id": alert["id"],
                "ref": len(candidates),
                "category": alert["category"],
                "rule_risk_score": alert["risk_score"],
                "evidence": safe_evidence,
                "available_reason_codes": allowed_codes,
            }
        )

    skipped = max(0, len(alerts) - len(candidates))
    if not candidates:
        return _result("PARTIAL" if skipped else "SUCCEEDED", model, skipped=skipped)

    schema = {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "integer", "minimum": 0, "maximum": len(candidates) - 1},
                        "risk_score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "reason_codes": {
                            "type": "array",
                            "items": {"type": "string", "enum": sorted(REASON_LABELS)},
                        },
                    },
                    "required": ["ref", "risk_score", "reason_codes"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["assessments"],
        "additionalProperties": False,
    }
    request_payload = {
        "model": model,
        "store": False,
        "max_output_tokens": 1200,
        "input": [
            {
                "role": "system",
                "content": (
                    "Assess only the supplied aggregated platform signals for whether a human "
                    "should review them. They are not proof of wrongdoing. Use only evidence "
                    "fields and reason codes provided for each item. Do not infer identity, intent, "
                    "guilt, or facts not present. Return a low score and no unsupported reason "
                    "codes when evidence is weak. Never recommend or take enforcement action."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    [
                        {key: value for key, value in candidate.items() if key != "alert_id"}
                        for candidate in candidates
                    ],
                    separators=(",", ":"),
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "fraud_review_signals",
                "strict": True,
                "schema": schema,
            },
        },
    }
    try:
        response = httpx.post(
            OPENAI_RESPONSES_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=request_payload,
            timeout=20.0,
        )
        response.raise_for_status()
        response_body = response.json()
        if (
            not isinstance(response_body, dict)
            or response_body.get("status") != "completed"
        ):
            logger.warning("OpenAI fraud analysis response was not completed.")
            return _result("FAILED", model, skipped=len(alerts))
        output_text = _output_text(response_body)
        if not output_text:
            logger.warning("OpenAI fraud analysis returned no structured text.")
            return _result("FAILED", model, skipped=len(alerts))
        decoded = json.loads(output_text)
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as error:
        logger.warning(
            "OpenAI fraud analysis failed; rule alerts remain available (%s).",
            type(error).__name__,
        )
        return _result("FAILED", model, skipped=len(alerts))

    if not isinstance(decoded, dict) or not isinstance(decoded.get("assessments"), list):
        return _result("FAILED", model, skipped=len(alerts))

    assessments = []
    seen_refs = set()
    for item in decoded["assessments"]:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        score = item.get("risk_score")
        if (
            not isinstance(ref, int)
            or isinstance(ref, bool)
            or ref < 0
            or ref >= len(candidates)
            or ref in seen_refs
            or not isinstance(score, int)
            or isinstance(score, bool)
            or score < 0
            or score > 100
        ):
            continue
        candidate = candidates[ref]
        reason_codes = item.get("reason_codes")
        if not isinstance(reason_codes, list):
            continue
        valid_codes = sorted(
            {
                code
                for code in reason_codes
                if isinstance(code, str)
                and code in candidate["available_reason_codes"]
                and code in CATEGORY_REASON_CODES.get(candidate["category"], set())
            }
        )
        if not valid_codes:
            continue
        seen_refs.add(ref)
        assessments.append(
            {
                "alert_id": candidate["alert_id"],
                "risk_score": score,
                "reason_codes": valid_codes,
                "reasons": [REASON_LABELS[code] for code in valid_codes],
            }
        )

    skipped += len(candidates) - len(assessments)
    if not assessments:
        return _result("FAILED", model, skipped=skipped)
    status = "PARTIAL" if skipped else "SUCCEEDED"
    return _result(status, model, assessments=assessments, skipped=skipped)
