from string import Formatter


_TEMPLATES = {
    "NEW_ORDER": {
        "sw": ("Oda mpya", "Umepewa oda mpya {order_id}."),
        "en": ("New order", "You have been assigned a new order {order_id}."),
        "fr": ("Nouvelle commande", "Une nouvelle commande {order_id} vous a été attribuée."),
    },
    "ORDER_CONFIRMED": {
        "sw": ("Oda imehakikiwa", "Oda {order_id} imehakikiwa."),
        "en": ("Order confirmed", "Order {order_id} has been confirmed."),
        "fr": ("Commande confirmée", "La commande {order_id} a été confirmée."),
    },
    "ORDER_STATUS_CHANGED": {
        "sw": ("Hali ya oda imesasishwa", "Oda {order_id}: {status}."),
        "en": ("Order status updated", "Order {order_id}: {status}."),
        "fr": ("Statut de commande mis à jour", "Commande {order_id} : {status}."),
    },
    "DELIVERY_UPDATED": {
        "sw": ("Taarifa ya delivery", "Mzigo {tracking_code}: {status}."),
        "en": ("Delivery update", "Parcel {tracking_code}: {status}."),
        "fr": ("Mise à jour de livraison", "Colis {tracking_code} : {status}."),
    },
    "SELLER_REGISTERED": {
        "sw": ("Muuzaji mpya", "{shop_name} amesajili akaunti na anahitaji ukaguzi."),
        "en": ("New seller", "{shop_name} registered and needs review."),
        "fr": ("Nouveau vendeur", "{shop_name} est inscrit et doit être vérifié."),
    },
    "KYC_PENDING": {
        "sw": ("KYC imewasilishwa", "Maombi ya KYC yako yamepokelewa."),
        "en": ("KYC submitted", "Your KYC application has been received."),
        "fr": ("Dossier KYC envoyé", "Votre dossier KYC a bien été reçu."),
    },
    "KYC_STATUS_CHANGED": {
        "sw": ("Hali ya KYC imesasishwa", "Hali ya KYC yako: {status}."),
        "en": ("KYC status updated", "Your KYC status: {status}."),
        "fr": ("Statut KYC mis à jour", "Votre statut KYC : {status}."),
    },
    "LOW_STOCK": {
        "sw": ("Stock iko chini", "{product_name} imefikia kiwango cha chini."),
        "en": ("Low stock", "{product_name} has reached its low-stock threshold."),
        "fr": ("Stock faible", "{product_name} a atteint son seuil de stock bas."),
    },
    "SECURITY_ALERT": {
        "sw": ("Tahadhari ya usalama", "{detail}"),
        "en": ("Security alert", "{detail}"),
        "fr": ("Alerte de sécurité", "{detail}"),
    },
    "SUSPICIOUS_ACTIVITY": {
        "sw": ("Shughuli ya kutiliwa shaka", "{detail}"),
        "en": ("Suspicious activity", "{detail}"),
        "fr": ("Activité suspecte", "{detail}"),
    },
    "RETURN_REQUESTED": {
        "sw": ("Ombi la kurejesha", "Oda {order_id} imeombwa kurejeshwa."),
        "en": ("Return requested", "A return has been requested for order {order_id}."),
        "fr": ("Retour demandé", "Un retour a été demandé pour la commande {order_id}."),
    },
    "SUPPORT_UPDATE": {
        "sw": ("Ombi la msaada limesasishwa", "Ombi {ticket_id}: {status}."),
        "en": ("Support request updated", "Request {ticket_id}: {status}."),
        "fr": ("Demande d’assistance mise à jour", "Demande {ticket_id} : {status}."),
    },
}

_STATUS_TRANSLATIONS = {
    "sw": {
        "PENDING": "Inasubiri",
        "COMPLETED": "Imekamilika",
        "CANCELLED": "Imeghairiwa",
        "EXPIRED": "Muda umeisha",
        "ORDER_CONFIRMED": "Oda imethibitishwa",
        "PREPARING": "Inaandaliwa",
        "READY_FOR_PICKUP": "Tayari kuchukuliwa",
        "AGENT_ASSIGNED": "Msafirishaji amepangiwa",
        "PICKED_UP": "Imechukuliwa",
        "IN_TRANSIT": "Njiani",
        "NEAR_DESTINATION": "Karibu na unakoenda",
        "OUT_FOR_DELIVERY": "Inapelekwa",
        "DELIVERED": "Imewasilishwa",
        "DELIVERY_FAILED": "Imeshindwa kuwasilishwa",
        "RETURNED": "Imerejeshwa",
        "PENDING_VERIFICATION": "Inasubiri uthibitisho",
        "UNDER_REVIEW": "Inakaguliwa",
        "VERIFIED": "Imethibitishwa",
        "REJECTED": "Imekataliwa",
        "RESUBMISSION_REQUIRED": "Tuma tena taarifa",
        "OPEN": "Wazi",
        "IN_PROGRESS": "Inaendelea",
        "RESOLVED": "Imetatuliwa",
    },
    "en": {
        "PENDING": "Pending",
        "COMPLETED": "Completed",
        "CANCELLED": "Cancelled",
        "EXPIRED": "Expired",
        "ORDER_CONFIRMED": "Order confirmed",
        "PREPARING": "Being prepared",
        "READY_FOR_PICKUP": "Ready for pickup",
        "AGENT_ASSIGNED": "Delivery agent assigned",
        "PICKED_UP": "Picked up",
        "IN_TRANSIT": "In transit",
        "NEAR_DESTINATION": "Near destination",
        "OUT_FOR_DELIVERY": "Out for delivery",
        "DELIVERED": "Delivered",
        "DELIVERY_FAILED": "Delivery failed",
        "RETURNED": "Returned",
        "PENDING_VERIFICATION": "Pending verification",
        "UNDER_REVIEW": "Under review",
        "VERIFIED": "Verified",
        "REJECTED": "Rejected",
        "RESUBMISSION_REQUIRED": "Resubmission required",
        "OPEN": "Open",
        "IN_PROGRESS": "In progress",
        "RESOLVED": "Resolved",
    },
    "fr": {
        "PENDING": "En attente",
        "COMPLETED": "Terminée",
        "CANCELLED": "Annulée",
        "EXPIRED": "Expirée",
        "ORDER_CONFIRMED": "Commande confirmée",
        "PREPARING": "En préparation",
        "READY_FOR_PICKUP": "Prête à être récupérée",
        "AGENT_ASSIGNED": "Livreur attribué",
        "PICKED_UP": "Récupérée",
        "IN_TRANSIT": "En transit",
        "NEAR_DESTINATION": "Près de la destination",
        "OUT_FOR_DELIVERY": "En cours de livraison",
        "DELIVERED": "Livrée",
        "DELIVERY_FAILED": "Échec de la livraison",
        "RETURNED": "Retournée",
        "PENDING_VERIFICATION": "Vérification en attente",
        "UNDER_REVIEW": "En cours d’examen",
        "VERIFIED": "Vérifiée",
        "REJECTED": "Refusée",
        "RESUBMISSION_REQUIRED": "Nouvel envoi requis",
        "OPEN": "Ouvert",
        "IN_PROGRESS": "En cours",
        "RESOLVED": "Résolu",
    },
}


def notification_template(event, context=None, language="sw"):
    context = context or {}
    localized = _TEMPLATES.get(event) or {
        "sw": ("Taarifa ya LipaRahisi", "Kuna sasisho jipya kwenye akaunti yako."),
        "en": ("LipaRahisi update", "There is a new update on your account."),
        "fr": ("Mise à jour LipaRahisi", "Une nouvelle mise à jour est disponible sur votre compte."),
    }
    language = language if language in {"sw", "en", "fr"} else "sw"
    title, text = localized[language]
    fields = {name for _, name, _, _ in Formatter().parse(text) if name}
    safe_context = {key: value for key, value in context.items() if key in fields}
    status = safe_context.get("status")
    if isinstance(status, str):
        safe_context["status"] = _STATUS_TRANSLATIONS[language].get(status, status)
    try:
        text = text.format(**safe_context)
    except (KeyError, ValueError):
        fallback = {
            "sw": "Kuna sasisho jipya kwenye akaunti yako.",
            "en": "There is a new update on your account.",
            "fr": "Une nouvelle mise à jour est disponible sur votre compte.",
        }
        text = fallback[language]
    return {"title": title, "text": text, "html": ""}
