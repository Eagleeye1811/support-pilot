"""Response composition — sentiment-aware, personalised and grounded only in executed facts.

The reply is assembled deterministically from the investigation, the
*successful* action results and the escalation decision. Tone adapts to the
customer's emotion (empathy first for angry/frustrated customers). If a Groq
key is configured the LLM may polish wording, but the polished text is
rejected unless every id, amount and date from the facts survives unchanged.
"""
from __future__ import annotations

import re

from .shared_agent import INTENTS, fmt_ts, groq_chat, llm_enabled
from .satisfaction_agent import CSAT_PROMPT

SLOT_QUESTIONS = {
    "order_id": "Could you share your **Order ID** (for example ORD12345)? You'll find it in My Orders or the order email.",
    "customer_identity": "Could you share the **email address or phone number** registered with your account so I can look you up?",
    "service_id": "Could you share your NovaFiber **service ID** (starts with SRV, printed on your bill)?",
}
EMPATHY = {
    "angry": "I'm really sorry about this{name} — I understand how frustrating it is, and I'm treating it as a priority.",
    "frustrated": "I'm sorry for the trouble{name}. Let me sort this out for you right away.",
    "calm": "Thanks for reaching out{name}.",
    "happy": "Happy to help{name}!",
}
SAFETY = {
    "fraud_report": "Please don't share OTPs, PINs or full card numbers with anyone — we will never ask for them. "
                    "You can also report it on the national cybercrime helpline **1930**.",
    "data_breach": "Please change passwords anywhere you reused this one, and watch for phishing messages pretending to be us.",
}


def _empathy(emotion, name, premium):
    line = EMPATHY.get(emotion, EMPATHY["calm"]).format(name=f", {name}" if name else "")
    if premium and emotion in ("angry", "frustrated"):
        line += " As a Premium member your case gets priority handling."
    return line


def compose(decision, understanding, investigation, actions, ctx, ticket=None, issue=None, articles=None, channel="web"):
    name = (ctx or {}).get("first_name") if channel != "email" else None  # email greeting already names them
    premium = (ctx or {}).get("tier") == "Premium"
    emotion = understanding.get("emotion", "calm")
    intent = (issue or {}).get("intent", understanding["intent"])
    parts = []

    if decision["decision"] == "clarify":
        slot = (investigation.get("missing_slots") or ["order_id"])[0]
        lead = _empathy(emotion, name, premium) if emotion != "calm" else (f"Sure, {name}." if name else "Sure, I can help with that.")
        notes = [s["detail"] for s in investigation.get("steps", []) if s["check"] in ("Verify order ownership", "Collect details")
                 and ("not found" in s["detail"] or "not linked" in s["detail"])]
        parts = [lead]
        if notes:
            parts.append(notes[0].split(" — ")[0] + ".")
        parts.append(SLOT_QUESTIONS.get(slot, f"Could you share your {slot.replace('_', ' ')}?"))
        return " ".join(parts)

    parts.append(_empathy(emotion, name, premium))
    ok = [a for a in actions if a["status"] in ("success", "skipped") and a["action"] != "notify_customer" and a.get("customer_message")]
    findings = [s for s in investigation.get("steps", []) if s["status"] in ("fail", "warn")][:2]

    if decision["decision"] == "auto_resolve":
        if intent == "general_inquiry" and articles:
            parts.append(articles[0]["content"])
        elif findings:
            parts.append("Here's what I found: " + " ".join(f"{f['detail'].rstrip('.')}." for f in findings))
        elif investigation.get("diagnosis"):
            parts.append(investigation["diagnosis"])
        if ok:
            parts.append("Here's what I've done:\n" + "\n".join(f"- {a['customer_message']}" for a in ok))
        if ticket:
            parts.append(f"Reference: **{ticket['id']}**.")
        parts.append("Is there anything else I can help you with?")
        return "\n\n".join(parts)

    # escalation
    if findings:
        parts.append("Here's what I found: " + " ".join(f"{f['detail'].rstrip('.')}." for f in findings))
    elif intent == "human_agent":
        parts.append("Of course — I'm connecting you with a member of our team.")
    elif any("threshold" in r for r in decision.get("reasons", [])):
        parts.append("I've verified your details, and to make sure this is handled exactly right, a specialist will confirm the "
                     "final step personally.")
    if ok:
        parts.append("I've already taken these steps:\n" + "\n".join(f"- {a['customer_message']}" for a in ok))
    failed = [a for a in actions if a["status"] in ("failed", "blocked") and not a.get("fallback_recovered")]
    if failed:
        parts.append(f"I tried to fix it automatically, but {failed[0]['label'].lower()} didn't go through, so I'm handing it to a specialist.")
    if ticket:
        sla = ticket.get("sla", {})
        parts.append(f"I've escalated this to our **{ticket['team']}** team as ticket **{ticket['id']}** "
                     f"(priority {ticket['priority'].upper()}). {ticket['assignee']} has the full investigation and "
                     f"conversation, so you won't need to repeat anything. Expect a response by **{fmt_ts(sla.get('response_due'))}**.")
    if intent in SAFETY:
        parts.append(SAFETY[intent])
    return "\n\n".join(parts)


def csat_request():
    return CSAT_PROMPT


_FACT_RE = re.compile(r"(TKT-\d+|ORD\d+|RF\d+|TXN\d+|RL-[\w-]+|SR-\d+|VIS-\d+|SORRY\d+-\d+|₹[\d,]+|\d{1,2} \w{3} \d{4}, \d{2}:\d{2} IST)")


def polish(text, emotion="calm"):
    """Optional LLM rewrite; returns the original unless every fact token is preserved."""
    if not llm_enabled():
        return text, False
    out = groq_chat(
        "Rewrite this customer-support reply to sound warm, concise and human. Keep markdown bullet points. "
        "Keep EVERY id, amount, date and time exactly as written. Do not add any new promise or fact.\n\n"
        f"Customer emotion: {emotion}\n\nReply:\n{text}",
        system_msg="You polish customer support replies without changing facts.", max_tokens=500)
    if not out:
        return text, False
    if not set(_FACT_RE.findall(text)).issubset(set(_FACT_RE.findall(out))):
        return text, False
    return out, True


def label(intent):
    return INTENTS.get(intent, {}).get("label", intent)
