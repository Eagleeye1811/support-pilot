"""Email Agent — sends real emails over Gmail SMTP (App Password) and logs every attempt.

Settings (env / .env / Streamlit secrets): SMTP_USER (your Gmail address) and SMTP_PASSWORD
(a 16-character Google App Password). Optional: SMTP_HOST (smtp.gmail.com), SMTP_PORT (465).

Safety: mail goes only to an address the customer gave us in this conversation — never to
the seeded demo customer addresses — and at most RATE_LIMIT emails per address per hour.
Every attempt (sent / failed / not_configured / rate_limited / blocked) is stored in the
``emails`` collection, so the website can show exactly what the agent did.
"""
import html
import re
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage

from .media import product_image_bytes
from .shared_agent import COMPANY, PRODUCT, get_secret, iso, now, parse_ts

RATE_LIMIT = 5
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
STATUS_BANNER = {
    "Resolved": ("✅ Resolved", "#1a8f5f", "#e2f5ec"),
    "Closed": ("✅ Closed", "#1a8f5f", "#e2f5ec"),
    "Assigned": ("🧑‍💼 With our specialist team", "#c25e00", "#fdeedd"),
    "Opened": ("📬 Received", "#2a78d6", "#e3eefb"),
    "Pending Customer": ("💬 Waiting for your reply", "#2a78d6", "#e3eefb"),
    "Reopened": ("🔁 Reopened", "#c25e00", "#fdeedd"),
}


def smtp_settings():
    user, password = get_secret("SMTP_USER"), get_secret("SMTP_PASSWORD")
    if not user or not password:
        return None
    return dict(user=user, password=password.replace(" ", ""), host=get_secret("SMTP_HOST", "smtp.gmail.com"),
                port=int(get_secret("SMTP_PORT", "465")))


def email_configured():
    return smtp_settings() is not None


def is_valid(address):
    return bool(address and EMAIL_RE.match(address.strip()))


def mask(address):
    if not address or "@" not in address:
        return address or ""
    name, domain = address.split("@", 1)
    return f"{name[:1]}***@{domain}"


# ---- rendering ---------------------------------------------------------------------------------
def _paragraphs(text):
    """The chat reply (light markdown) → email HTML: **bold**, bullet lines and paragraphs."""
    out, bullets = [], []
    for line in text.splitlines():
        line = line.strip()
        if "rate this support experience" in line.lower():
            continue  # the chat CSAT prompt does not belong in an email
        m = re.match(r"^[-•*]\s+(.*)", line)
        if m:
            bullets.append(m.group(1))
            continue
        if bullets:
            out.append("<ul style='margin:6px 0 12px;padding-left:20px'>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets = []
        if line:
            out.append(f"<p style='margin:0 0 12px'>{line}</p>")
    if bullets:
        out.append("<ul style='margin:6px 0 12px;padding-left:20px'>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
    body = "\n".join(out)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)


def render(kind, ticket=None, reply="", message="", customer_name=None, media=()):
    """(subject, text, html) for a ticket confirmation or a status update. Images are referenced as cid:<sku>."""
    t = ticket or {}
    status = t.get("status", "Opened")
    label, color, tint = STATUS_BANNER.get(status, (status, "#2a78d6", "#e3eefb"))
    hello = f"Hi {html.escape(customer_name)}," if customer_name else "Hi there,"
    if kind == "update":
        subject = f"[{t.get('id', 'Ticket')}] Update: {status}"
        text_body = message
        main = f"<p style='margin:0 0 12px'>{html.escape(message)}</p>"
    else:
        subject = f"[{t.get('id', 'Ticket')}] {t.get('title', 'Your support request')} — {label.split(' ', 1)[-1]}"
        text_body = re.sub(r"\*\*(.+?)\*\*", r"\1", reply)
        main = _paragraphs(html.escape(reply).replace("&#x27;", "'"))
    imgs = "".join(
        f"<td style='padding:6px;text-align:center;width:50%'><img src='cid:{html.escape(m['sku'])}' width='220' "
        f"style='border-radius:12px;display:block;margin:0 auto' alt='{html.escape(m.get('name', ''))}'>"
        f"<div style='font-size:13px;color:#4b5563;margin-top:6px'><b>{html.escape(m.get('role', ''))}</b><br>"
        f"{html.escape(m.get('name', ''))}</div></td>" for m in media)
    due = (t.get("sla") or {}).get("resolution_due")
    rows = [("Ticket", t.get("id")), ("Status", status), ("Team", t.get("team")), ("Priority", t.get("priority")),
            ("Target resolution", due[:16].replace("T", " ") + " UTC" if due and status not in ("Resolved", "Closed") else None)]
    facts = "".join(f"<tr><td style='padding:4px 12px 4px 0;color:#6b7280'>{k}</td><td style='padding:4px 0'><b>{html.escape(str(v))}</b></td></tr>"
                    for k, v in rows if v)
    html_body = f"""<!doctype html><html><body style="margin:0;background:#f3f4f6;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1f2937">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:16px;overflow:hidden">
<tr><td style="background:#1d4ed8;padding:20px 28px;color:#ffffff;font-size:20px;font-weight:700">{PRODUCT}
<span style="font-weight:400;opacity:.8;font-size:14px"> · {COMPANY} Customer Support</span></td></tr>
<tr><td style="padding:20px 28px 0"><div style="display:inline-block;background:{tint};color:{color};border-radius:999px;padding:6px 14px;font-weight:700;font-size:14px">{label}</div></td></tr>
<tr><td style="padding:16px 28px 4px;font-size:15px;line-height:1.55"><p style="margin:0 0 12px">{hello}</p>{main}</td></tr>
{f'<tr><td style="padding:0 22px"><table role="presentation" width="100%"><tr>{imgs}</tr></table></td></tr>' if imgs else ''}
<tr><td style="padding:12px 28px 20px"><table role="presentation" style="background:#f9fafb;border-radius:12px;padding:12px 16px;font-size:14px;width:100%">{facts}</table></td></tr>
<tr><td style="padding:0 28px 24px;font-size:12px;color:#6b7280">This email was sent by the SupportPilot Email Agent for {t.get('id', 'your request')}.
Reply on the same channel you contacted us on to continue the conversation.</td></tr>
</table></td></tr></table></body></html>"""
    return subject, text_body + f"\n\n— {PRODUCT} · {COMPANY} Customer Support", html_body


# ---- sending -------------------------------------------------------------------------------------
def _recent_sent(store, to):
    since = now() - timedelta(hours=1)
    return [e for e in store.list("emails", to=to) if e["status"] == "sent" and parse_ts(e["created_at"]) >= since]


def _log(store, **fields):
    seq = store.next_seq("email", 1)
    rec = dict(id=f"EML-{seq:05d}", created_at=iso(), **fields)
    store.put("emails", rec["id"], rec)
    return rec


def send_email(store, to, kind="ticket", ticket_id=None, reply="", message="", images=None, customer_name=None):
    """Email Agent: send a real email (ticket confirmation or status update) to the address the customer gave us.

    `images` is a list of {sku, name, role} (see media.ticket_images); they are embedded inline.
    """
    to = (to or "").strip().lower()
    ticket = store.get("tickets", ticket_id) if ticket_id else None
    picks = [p for p in (images or []) if product_image_bytes(p["sku"])]
    subject, text_body, html_body = render(kind, ticket, reply, message, customer_name, picks)
    base = dict(to=to, to_masked=mask(to), kind=kind, ticket_id=ticket_id, subject=subject, html=html_body, text=text_body,
                media=[p["sku"] for p in picks])
    if not is_valid(to):
        return _log(store, status="blocked", error="Not a valid email address", **base)
    if any((c.get("email") or "").lower() == to for c in store.list("customers")):
        return _log(store, status="blocked", error="Seeded demo address — only addresses given by the customer are emailed", **base)
    if len(_recent_sent(store, to)) >= RATE_LIMIT:
        return _log(store, status="rate_limited", error=f"More than {RATE_LIMIT} emails to this address in the last hour", **base)
    cfg = smtp_settings()
    if not cfg:
        return _log(store, status="not_configured", error="Set SMTP_USER and SMTP_PASSWORD to send real email", **base)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"{PRODUCT} Support <{cfg['user']}>", to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    html_part = msg.get_payload()[1]
    for p in picks:
        html_part.add_related(product_image_bytes(p["sku"]), "image", "png", cid=f"<{p['sku']}>", filename=f"{p['sku']}.png")
    try:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ssl.create_default_context(), timeout=20) as smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except Exception as exc:
        return _log(store, status="failed", error=f"{type(exc).__name__}: {exc}"[:300], **base)
    return _log(store, status="sent", **base)
