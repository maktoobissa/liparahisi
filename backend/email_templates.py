from dataclasses import dataclass
from html import escape


@dataclass(frozen=True)
class EmailContent:
    subject: str
    text: str
    html: str


def _render_email(
    subject,
    full_name,
    heading,
    paragraphs,
    frontend_url,
    button_label=None,
    button_url=None,
    details=(),
):
    name = full_name.strip() or "mteja"
    safe_name = escape(name)
    safe_frontend_url = escape(frontend_url.rstrip("/"), quote=True)
    logo_url = escape(f"{frontend_url.rstrip('/')}/icons/icon.svg", quote=True)
    text_parts = [f"Habari {name},", heading, *paragraphs]
    if details:
        text_parts.extend(f"{label}: {value}" for label, value in details)
    if button_label and button_url:
        text_parts.extend((button_label, button_url))
    text_parts.append("LipaRahisi | Malipo na biashara kwa urahisi.")
    text = "\n\n".join(text_parts)

    paragraph_html = "".join(
        f'<p style="margin:0 0 16px;color:#334155;line-height:1.6">{escape(paragraph)}</p>'
        for paragraph in paragraphs
    )
    details_html = ""
    if details:
        rows = "".join(
            "<tr>"
            f'<td style="padding:9px 12px;color:#64748b;border-bottom:1px solid #e2e8f0">{escape(str(label))}</td>'
            f'<td style="padding:9px 12px;color:#0f172a;text-align:right;border-bottom:1px solid #e2e8f0">{escape(str(value))}</td>'
            "</tr>"
            for label, value in details
        )
        details_html = (
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            f'style="margin:20px 0;border-collapse:collapse">{rows}</table>'
        )
    button_html = ""
    if button_label and button_url:
        safe_button_url = escape(button_url, quote=True)
        button_html = (
            '<p style="margin:24px 0">'
            f'<a href="{safe_button_url}" style="display:inline-block;background:#16835d;'
            'border-radius:8px;color:#ffffff;font-weight:700;padding:13px 20px;'
            f'text-decoration:none">{escape(button_label)}</a></p>'
            f'<p style="margin:0 0 16px;color:#64748b;font-size:13px;line-height:1.5">'
            f'Kama kitufe hakifanyi kazi, tumia kiungo hiki: '
            f'<a href="{safe_button_url}" style="color:#126a4c;word-break:break-all">{safe_button_url}</a></p>'
        )
    html = (
        '<!doctype html><html lang="sw"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(subject)}</title></head>'
        '<body style="margin:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="background:#f1f5f9;padding:28px 12px"><tr><td align="center">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="max-width:600px;background:#ffffff;border-radius:14px;overflow:hidden">'
        '<tr><td style="padding:24px 28px;background:#f0fdf4;border-bottom:1px solid #dcfce7">'
        f'<img src="{logo_url}" width="40" height="40" alt="LipaRahisi" '
        'style="display:inline-block;vertical-align:middle;margin-right:10px">'
        '<span style="display:inline-block;vertical-align:middle;color:#126a4c;'
        'font-size:20px;font-weight:700">LipaRahisi</span></td></tr>'
        '<tr><td style="padding:30px 28px">'
        f'<p style="margin:0 0 10px;color:#334155">Habari {safe_name},</p>'
        f'<h1 style="margin:0 0 18px;color:#0f172a;font-size:23px;line-height:1.3">{escape(heading)}</h1>'
        f'{paragraph_html}{details_html}{button_html}'
        '<p style="margin:24px 0 0;color:#64748b;font-size:13px;line-height:1.5">'
        'Hujatambua ombi hili? Unaweza kupuuza ujumbe huu au kuwasiliana na timu ya LipaRahisi.</p>'
        '</td></tr>'
        '<tr><td style="padding:18px 28px;background:#f8fafc;color:#64748b;'
        'font-size:12px;line-height:1.5;text-align:center">'
        f'<a href="{safe_frontend_url}" style="color:#126a4c;text-decoration:none">LipaRahisi</a>'
        ' · Malipo na biashara kwa urahisi.</td></tr>'
        '</table></td></tr></table></body></html>'
    )
    return EmailContent(subject=subject, text=text, html=html)


def verification_email(full_name, verification_url, frontend_url):
    return _render_email(
        "Thibitisha akaunti yako ya LipaRahisi",
        full_name,
        "Thibitisha barua pepe yako",
        ["Tumeunda akaunti yako ya LipaRahisi. Thibitisha anwani hii ya barua pepe ili kukamilisha usajili."],
        frontend_url,
        "Thibitisha barua pepe",
        verification_url,
    )


def welcome_email(full_name, frontend_url):
    return _render_email(
        "Karibu LipaRahisi",
        full_name,
        "Karibu LipaRahisi!",
        ["Akaunti yako imethibitishwa. Sasa unaweza kutumia huduma za LipaRahisi."],
        frontend_url,
        "Fungua LipaRahisi",
        frontend_url.rstrip("/") + "/",
    )


def password_reset_email(full_name, reset_url, frontend_url):
    return _render_email(
        "Badilisha nenosiri la LipaRahisi",
        full_name,
        "Ombi la kubadilisha nenosiri",
        [
            "Tumepokea ombi la kuweka nenosiri jipya la akaunti yako.",
            "Kiungo hiki kitatumika mara moja tu na kitaisha baada ya saa 1. Ikiwa hukuomba mabadiliko haya, puuza ujumbe huu.",
        ],
        frontend_url,
        "Weka nenosiri jipya",
        reset_url,
    )


def password_changed_email(full_name, frontend_url):
    return _render_email(
        "Nenosiri la LipaRahisi limebadilishwa",
        full_name,
        "Nenosiri limebadilishwa",
        [
            "Nenosiri la akaunti yako ya LipaRahisi limebadilishwa.",
            "Ikiwa hukufanya mabadiliko haya, wasiliana na timu ya LipaRahisi mara moja.",
        ],
        frontend_url,
        "Fungua LipaRahisi",
        frontend_url.rstrip("/") + "/",
    )


def payment_email(full_name, orders, frontend_url, failed=False, demo=False):
    order_ids = ", ".join(order["order_id"] for order in orders)
    product_names = ", ".join(
        dict.fromkeys(order.get("product_name") or "Bidhaa" for order in orders)
    )
    total = sum(int(order["amount"]) for order in orders)
    payment_method = ", ".join(
        dict.fromkeys(order.get("payment_method") or "Haijaainishwa" for order in orders)
    )
    completed_at = next(
        (
            order.get("completed_at") or order.get("created_at")
            for order in orders
            if order.get("completed_at") or order.get("created_at")
        ),
        "Haijaainishwa",
    )
    status = "Haikukamilika" if failed else "Imekamilika"
    details = [
        ("Hali ya malipo", status),
        ("Namba ya oda", order_ids),
        ("Bidhaa", product_names),
        ("Jumla", f"TSh {total:,}"),
        ("Njia ya malipo", payment_method),
        ("Tarehe", completed_at),
    ]
    paragraphs = (
        ["Malipo yako hayakukamilika. Unaweza kujaribu tena kutoka LipaRahisi."]
        if failed
        else ["Malipo yako yamepokelewa. Taarifa hii pia ni risiti ya malipo yako."]
    )
    if demo:
        paragraphs.append("Hili lilikuwa jaribio la demo; hakuna fedha halisi zilizohamishwa.")
    return _render_email(
        "Malipo hayakukamilika — LipaRahisi" if failed else "Risiti ya malipo — LipaRahisi",
        full_name,
        "Malipo hayakukamilika" if failed else "Risiti ya malipo",
        paragraphs,
        frontend_url,
        "Fungua LipaRahisi",
        frontend_url.rstrip("/") + "/",
        details,
    )
