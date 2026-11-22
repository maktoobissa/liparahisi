import json
import logging
import os
import unicodedata
from typing import Any

import httpx


logger = logging.getLogger(__name__)
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-4o-mini"

_INTENT_PATTERNS = {
    "expenses": ("expense", "gharama", "matumizi", "depense", "dépense", "cout", "coût"),
    "low_stock": ("low stock", "stock ndogo", "kuisha", "reorder", "replenish", "réappro"),
    "slow_products": ("slow", "hazitembei", "hazijauza", "no sales", "moins vendu", "ne se vend"),
    "branches": ("branch", "tawi", "matawi", "agence", "succursale"),
    "profit": ("profit", "faida", "benefit", "bénéfice"),
    "top_products": ("product", "bidhaa", "produit", "sold", "uza", "vendeur", "vendu"),
    "sales": ("sales", "mauzo", "vente", "ventes", "revenue", "mapato"),
}

_TEXT = {
    "sw": {
        "no_data": "Hakuna data ya kutosha kujibu swali hili.",
        "unsupported": "Naweza kuchambua mauzo, faida inayokadiriwa, bidhaa zilizouza zaidi, stock ndogo, bidhaa zisizouza hivi karibuni au utendaji wa matawi.",
        "sales": "Kuanzia {start} hadi {end}, oda {orders} zilizokamilika zilileta mauzo ya TZS {sales}. Faida halisi inayokadiriwa ni TZS {profit}.",
        "profit": "Faida halisi inayokadiriwa kuanzia {start} hadi {end} ni TZS {profit}, kutokana na oda {orders} zilizokamilika.",
        "top_products": "Bidhaa zilizouza zaidi kuanzia {start} hadi {end}: {items}.",
        "low_stock": "Bidhaa zifuatazo ziko chini au sawa na kiwango chako cha tahadhari ya stock: {items}.",
        "slow_products": "Bidhaa zifuatazo hazina mauzo yaliyokamilika ndani ya siku 30: {items}.",
        "branches": "Utendaji wa matawi kuanzia {start} hadi {end}: {items}.",
        "expenses": "Mfumo hauna rekodi za matumizi au historia ya gharama kwa biashara hii, hivyo siwezi kuthibitisha gharama zilizoongezeka.",
        "no_sales": "Hakuna oda zilizokamilika katika kipindi kilichochaguliwa.",
        "no_low_stock": "Hakuna bidhaa hai zilizo chini ya kiwango chako cha stock.",
        "no_slow_products": "Bidhaa zote hai zilikuwa na angalau mauzo moja katika siku 30 zilizopita.",
        "no_branches": "Hakuna matawi yenye data ya mauzo katika kipindi hiki.",
        "reorder": "Pendekezo: kagua upatikanaji wa bidhaa hizi na panga replenishment kulingana na mahitaji yako.",
        "compare": "Pendekezo: linganisha mauzo ya matawi katika kipindi sawa kabla ya kubadili ugawaji wa stock.",
        "ai_unavailable": "Ushauri wa AI haujasanidiwa; muhtasari huu umetengenezwa moja kwa moja kutoka kwenye takwimu zilizothibitishwa.",
    },
    "en": {
        "no_data": "There is not enough data to answer this question.",
        "unsupported": "I can analyze sales, estimated profit, best-selling products, low stock, products without recent sales, or branch performance.",
        "sales": "From {start} to {end}, {orders} completed orders generated TZS {sales} in sales. Estimated net profit is TZS {profit}.",
        "profit": "Estimated net profit from {start} to {end} is TZS {profit}, based on {orders} completed orders.",
        "top_products": "Best-selling products from {start} to {end}: {items}.",
        "low_stock": "These products are at or below their configured stock thresholds: {items}.",
        "slow_products": "These products had no completed sales in the last 30 days: {items}.",
        "branches": "Branch performance from {start} to {end}: {items}.",
        "expenses": "This system has no expense records or cost history for this business, so I cannot verify which costs increased.",
        "no_sales": "No completed orders were found in the selected period.",
        "no_low_stock": "No active products are at or below their stock thresholds.",
        "no_slow_products": "Every active product had at least one sale in the last 30 days.",
        "no_branches": "No branches had sales data during this period.",
        "reorder": "Suggestion: review availability for these items and plan replenishment based on your business needs.",
        "compare": "Suggestion: compare branch sales for the same period before changing stock allocation.",
        "ai_unavailable": "AI recommendations are not configured; this summary was generated directly from verified metrics.",
    },
    "fr": {
        "no_data": "Les données disponibles ne suffisent pas pour répondre à cette question.",
        "unsupported": "Je peux analyser les ventes, le bénéfice estimé, les meilleures ventes, le stock faible, les produits sans vente récente ou la performance des agences.",
        "sales": "Du {start} au {end}, {orders} commandes terminées ont généré TZS {sales} de ventes. Le bénéfice net estimé est de TZS {profit}.",
        "profit": "Le bénéfice net estimé du {start} au {end} est de TZS {profit}, sur la base de {orders} commandes terminées.",
        "top_products": "Meilleures ventes par produit du {start} au {end} : {items}.",
        "low_stock": "Ces produits sont au niveau ou sous leur seuil de stock configuré : {items}.",
        "slow_products": "Ces produits n’ont enregistré aucune vente terminée au cours des 30 derniers jours : {items}.",
        "branches": "Performance des agences du {start} au {end} : {items}.",
        "expenses": "Le système ne contient ni dépenses ni historique des coûts pour cette entreprise ; je ne peux donc pas vérifier les coûts en hausse.",
        "no_sales": "Aucune commande terminée n’a été trouvée sur la période sélectionnée.",
        "no_low_stock": "Aucun produit actif n’est au niveau ou sous son seuil de stock.",
        "no_slow_products": "Tous les produits actifs ont enregistré au moins une vente au cours des 30 derniers jours.",
        "no_branches": "Aucune agence ne présente de données de vente sur cette période.",
        "reorder": "Suggestion : vérifiez la disponibilité de ces articles et planifiez le réapprovisionnement selon vos besoins.",
        "compare": "Suggestion : comparez les ventes des agences sur la même période avant de modifier la répartition des stocks.",
        "ai_unavailable": "Les recommandations IA ne sont pas configurées ; ce résumé provient directement des indicateurs vérifiés.",
    },
}


def _normalized_question(question: str) -> str:
    normalized = unicodedata.normalize("NFKD", question.casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def classify_question(question: str) -> str | None:
    normalized = _normalized_question(question)
    if "best" in normalized or "zaidi" in normalized or "meilleur" in normalized:
        if any(word in normalized for word in ("product", "bidhaa", "produit", "sold", "uza", "vente")):
            return "top_products"
    for intent, phrases in _INTENT_PATTERNS.items():
        if any(_normalized_question(phrase) in normalized for phrase in phrases):
            return intent
    return None


def _format_amount(value: int | float, language: str) -> str:
    amount = int(round(float(value or 0)))
    formatted = f"{amount:,}"
    return formatted.replace(",", "\u202f") if language == "fr" else formatted


def render_answer(intent: str | None, facts: dict[str, Any], language: str) -> str:
    language = language if language in _TEXT else "sw"
    text = _TEXT[language]
    if intent is None:
        return text["unsupported"]
    if intent == "expenses":
        return text["expenses"]

    start = facts["period"]["start"]
    end = facts["period"]["end"]
    if intent in {"sales", "profit"}:
        if facts["sales"]["orders"] == 0:
            return text["no_sales"]
        return text[intent].format(
            start=start,
            end=end,
            orders=facts["sales"]["orders"],
            sales=_format_amount(facts["sales"]["amount"], language),
            profit=_format_amount(facts["sales"]["estimated_profit"], language),
        )
    if intent == "top_products":
        items = facts["top_products"]
        if not items:
            return text["no_sales"]
        summary = "; ".join(
            f'{item["name"]} ({item["quantity"]}): TZS {_format_amount(item["sales"], language)}'
            for item in items
        )
        return text["top_products"].format(start=start, end=end, items=summary)
    if intent == "low_stock":
        items = facts["low_stock"]
        if not items:
            return text["no_low_stock"]
        summary = "; ".join(
            f'{item["name"]} ({item["stock"]}/{item["threshold"]})'
            for item in items
        )
        return text["low_stock"].format(items=summary)
    if intent == "slow_products":
        items = facts["slow_products"]
        if not items:
            return text["no_slow_products"]
        return text["slow_products"].format(
            items="; ".join(item["name"] for item in items)
        )
    if intent == "branches":
        items = facts["branches"]
        if not items:
            return text["no_branches"]
        summary = "; ".join(
            f'{item["name"]}: TZS {_format_amount(item["sales"], language)} '
            f'({item["orders"]} orders)'
            for item in items
        )
        return text["branches"].format(start=start, end=end, items=summary)
    return text["no_data"]


def _response_text(response_body: dict[str, Any]) -> str | None:
    for item in response_body.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                return content.get("text") if isinstance(content.get("text"), str) else None
    return None


def _rule_recommendations(facts: dict[str, Any]) -> dict[str, Any]:
    codes = []
    if facts.get("low_stock"):
        codes.append("REVIEW_REPLENISHMENT")
    if len(facts.get("branches", [])) > 1:
        codes.append("COMPARE_BRANCHES")
    return {"status": "NOT_CONFIGURED", "codes": codes}


def _openai_recommendations(intent: str | None, facts: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _rule_recommendations(facts)
    if intent is None:
        return {"status": "SUCCEEDED", "codes": []}

    allowed_codes = []
    if facts.get("low_stock"):
        allowed_codes.append("REVIEW_REPLENISHMENT")
    if len(facts.get("branches", [])) > 1:
        allowed_codes.append("COMPARE_BRANCHES")
    if not allowed_codes:
        return {"status": "SUCCEEDED", "codes": []}

    schema = {
        "type": "object",
        "properties": {
            "recommendations": {
                "type": "array",
                "items": {"type": "string", "enum": allowed_codes},
                "maxItems": len(allowed_codes),
            }
        },
        "required": ["recommendations"],
        "additionalProperties": False,
    }
    aggregate = {
        "intent": intent,
        "completed_orders": facts.get("sales", {}).get("orders", 0),
        "low_stock_count": len(facts.get("low_stock", [])),
        "branch_count": len(facts.get("branches", [])),
        "allowed_recommendations": allowed_codes,
    }
    payload = {
        "model": os.getenv("OPENAI_BUSINESS_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        "store": False,
        "max_output_tokens": 200,
        "input": [
            {
                "role": "system",
                "content": (
                    "Choose only recommendations supported by the supplied aggregate counts. "
                    "Do not infer causes, invent facts, provide financial instructions, or modify "
                    "business data. Return only the required JSON."
                ),
            },
            {"role": "user", "content": json.dumps(aggregate, separators=(",", ":"))},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "business_recommendations",
                "strict": True,
                "schema": schema,
            }
        },
    }
    try:
        response = httpx.post(
            OPENAI_RESPONSES_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=15.0,
        )
        response.raise_for_status()
        response_json = response.json()
        output = _response_text(response_json)
        parsed = json.loads(output) if output else {}
        recommendations = parsed.get("recommendations", [])
        if not isinstance(recommendations, list):
            raise ValueError("Invalid business recommendation response.")
        safe_codes = [code for code in recommendations if code in allowed_codes]
        return {"status": "SUCCEEDED", "codes": safe_codes}
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError, json.JSONDecodeError) as error:
        logger.warning("Business assistant recommendation request failed (%s)", type(error).__name__)
        raise RuntimeError("AI recommendation provider is unavailable.") from error


def ai_recommendations(intent: str | None, facts: dict[str, Any]) -> dict[str, Any]:
    provider_name = os.getenv("BUSINESS_AI_PROVIDER", "openai").strip().lower()
    if provider_name in {"", "none", "disabled"}:
        return _rule_recommendations(facts)
    providers = {"openai": _openai_recommendations}
    provider = providers.get(provider_name)
    if provider is None:
        raise RuntimeError("Business assistant provider is not supported.")
    return provider(intent, facts)


def recommendation_text(code: str, language: str) -> str | None:
    language = language if language in _TEXT else "sw"
    key = {
        "REVIEW_REPLENISHMENT": "reorder",
        "COMPARE_BRANCHES": "compare",
    }.get(code)
    if not key:
        return None
    return _TEXT[language][key]


def ai_unavailable_text(language: str) -> str:
    return _TEXT.get(language, _TEXT["sw"])["ai_unavailable"]
