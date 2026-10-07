"""Agent 1: Conversation Understanding — intent, category, priority, sentiment and entities.

Deterministic phrase/keyword scoring with an explainable confidence. When the
rules are unsure (confidence < 0.6) and a Groq key is configured, the LLM may
break the tie, but only by choosing from the closed intent list.
"""
from __future__ import annotations

import math
import re

from .shared_agent import CONVERSATIONAL, INTENTS, bump_priority, groq_json, llm_enabled, normalize_text

ENTITY_PATTERNS = {
    "order_id": r"\bORD[-\s]?(\d{4,8})\b",
    "ticket_id": r"\bTKT[-\s#]?(\d{3,6})\b",
    "transaction_id": r"\bTXN[-\s]?(\d{4,10})\b",
    "customer_id": r"\bCUST[-\s]?(\d{3,6})\b",
    "service_id": r"\bSRV[-\s]?(\d{3,6})\b",
    "subscription_id": r"\bSUB[-\s]?(\d{3,6})\b",
}
PREFIX = {"order_id": "ORD", "ticket_id": "TKT-", "transaction_id": "TXN", "customer_id": "CUST", "service_id": "SRV",
          "subscription_id": "SUB"}
EMAIL_RE = r"[\w.+-]+@[\w-]+\.[\w.-]+"
PHONE_RE = r"(?<!\d)(?:\+?91[\s-]?)?([6-9]\d{9})(?!\d)"
AMOUNT_RE = r"(?:₹|\brs\.?|\binr)\s?([\d,]+(?:\.\d+)?)"
PLAN_RE = r"\b(premium|plus|basic)\b"

NEGATIVE = {"frustrated": 1.0, "frustrating": 1.0, "angry": 1.2, "annoyed": 1.0, "upset": 1.0, "worst": 1.4, "terrible": 1.3,
            "horrible": 1.3, "useless": 1.3, "ridiculous": 1.2, "pathetic": 1.4, "disappointed": 1.0, "unacceptable": 1.3,
            "fed up": 1.4, "waste": 1.0, "cheated": 1.4, "disgusting": 1.4, "furious": 1.5, "hate": 1.2, "bad": 0.6,
            "poor": 0.6, "never again": 1.4, "third time": 1.0, "again": 0.5, "still": 0.4, "nobody": 0.8, "no one": 0.8,
            "wtf": 1.5, "nonsense": 1.2, "fraud": 0.8, "scam": 1.0, "deducted": 0.5, "failed": 0.5, "wrong": 0.5,
            "broken": 0.6, "damaged": 0.6, "lost": 0.5, "not working": 0.5, "cant": 0.3, "unable": 0.3, "locked": 0.3,
            "delayed": 0.5, "late": 0.4, "worried": 0.8, "stressed": 0.9, "urgent": 0.4, "immediately": 0.4}
POSITIVE = {"thanks": 1.0, "thank you": 1.2, "great": 1.0, "good": 0.6, "awesome": 1.2, "love": 1.0, "happy": 1.0,
            "resolved": 0.8, "perfect": 1.2, "excellent": 1.2, "appreciate": 1.0, "helpful": 1.0, "nice": 0.6, "quick": 0.5}
URGENCY = ["urgent", "asap", "immediately", "right now", "emergency", "at the earliest", "today itself"]


def _hits(text, terms):
    return [t for t in terms if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text)]


def extract_entities(text):
    raw = str(text or "")
    ents = {}
    for name, pattern in ENTITY_PATTERNS.items():
        m = re.search(pattern, raw, re.I)
        if m:
            ents[name] = f"{PREFIX[name]}{m.group(1)}"
    m = re.search(EMAIL_RE, raw)
    if m:
        ents["email"] = m.group(0).lower()
    m = re.search(PHONE_RE, raw)
    if m:
        ents["phone"] = m.group(1)
    m = re.search(AMOUNT_RE, raw, re.I)
    if m:
        ents["amount"] = float(m.group(1).replace(",", ""))
    m = re.search(PLAN_RE, raw, re.I)
    if m:
        ents["plan"] = m.group(1).title()
    return ents


def analyze_sentiment(text):
    t = normalize_text(text)
    neg = sum(NEGATIVE[w] for w in _hits(t, NEGATIVE))
    pos = sum(POSITIVE[w] for w in _hits(t, POSITIVE))
    letters = [c for c in str(text) if c.isalpha()]
    caps = sum(c.isupper() for c in letters) / len(letters) if len(letters) > 12 else 0
    excl = str(text).count("!")
    neg += (1.0 if caps > 0.6 else 0) + min(excl, 4) * 0.3 * (1 if neg else 0)
    raw = pos - neg
    score = round(math.tanh(raw / 2), 2)
    if score <= -0.6:
        emotion = "angry"
    elif score <= -0.15:
        emotion = "frustrated"
    elif score >= 0.3:
        emotion = "happy"
    else:
        emotion = "calm"
    sentiment = "negative" if score <= -0.15 else ("positive" if score >= 0.3 else "neutral")
    return dict(sentiment=sentiment, emotion=emotion, sentiment_score=score)


def _conversational(t):
    if re.fullmatch(r"(hi+|hello+|hey+|hii+|namaste|good (morning|afternoon|evening))[\s!.,]*(there|team)?[\s!.]*", t):
        return "greeting"
    if re.fullmatch(r"(ok(ay)?[, ]*)?(thanks?|thank you|thx|ty|great|perfect|awesome|cool|that works|got it)( (so much|a lot|again))?[\s!.]*", t):
        return "thanks"
    if re.fullmatch(r"(yes|yeah|yep|sure|ok|okay|please do|go ahead|confirm(ed)?)[\s!.]*", t):
        return "affirm"
    if re.fullmatch(r"(no|nope|nah|not now|no thanks|thats all|nothing else)[\s!.]*", t):
        return "deny"
    return None


def score_intents(text):
    t = normalize_text(text)
    scores, matched = {}, {}
    for intent, spec in INTENTS.items():
        ph = _hits(t, spec.get("phrases", []))
        kw = _hits(t, spec.get("keywords", []))
        s = 3 * len(ph) + len(kw)
        if s:
            scores[intent], matched[intent] = s, ph + kw
    return scores, matched


def understand(text, use_llm=True):
    """Return the structured understanding of one customer message."""
    t = normalize_text(text)
    entities = extract_entities(text)
    senti = analyze_sentiment(text)
    small = _conversational(t)
    scores, matched = score_intents(text)
    if small and not scores:
        return dict(intent=small, category="Conversation", label=small.title(), priority="low", confidence=0.95,
                    entities=entities, secondary_intents=[], matched_keywords=[], source="rules", priority_reasons=[], **senti)

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], list(INTENTS).index(kv[0])))
    question = re.match(r"^(what|how|can|do|does|is|are|which|when|why|where can)\b", t) is not None
    if ranked and ranked[0][1] < 3 and (ranked[0][1] < 2 or question):
        # keyword-only hits on a question ("what are the delivery charges?") are FAQ material, not a workflow
        ranked = []
    if ranked:
        intent, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
        confidence = 1 - math.exp(-top / 3)
        if second:
            confidence *= 1 - 0.35 * second / top
        confidence = round(min(max(confidence, 0.3), 0.98), 2)
    else:
        intent, confidence = "general_inquiry", 0.35
    source = "rules"

    if confidence < 0.6 and use_llm and llm_enabled():
        guess = groq_json(
            "Classify the customer support message into exactly one intent from this list: "
            f"{', '.join(i for i in INTENTS)}.\nMessage: {text!r}\n"
            'Answer as JSON: {"intent": "...", "confidence": 0.0-1.0}',
            system_msg="You are an intent classifier for a customer support desk. Reply with JSON only.")
        if guess and guess.get("intent") in INTENTS:
            llm_conf = float(guess.get("confidence", 0.6) or 0.6)
            if guess["intent"] != intent or llm_conf > confidence:
                intent, confidence, source = guess["intent"], round(max(confidence, min(0.85, 0.85 * llm_conf)), 2), "llm"

    spec = INTENTS[intent]
    priority, reasons = spec["priority"], []
    if _hits(t, URGENCY):
        priority = bump_priority(priority, cap="critical" if spec["category"] == "Security" else "high")
        reasons.append("urgency words")
    if senti["emotion"] == "angry":
        priority = bump_priority(priority, cap="critical" if spec["category"] == "Security" else "high")
        reasons.append("angry customer")
    if entities.get("amount", 0) >= 20000:
        priority = bump_priority(priority, cap="critical" if spec["category"] == "Security" else "high")
        reasons.append("high amount mentioned")

    return dict(intent=intent, category=spec["category"], label=spec["label"], priority=priority, confidence=confidence,
                entities=entities, secondary_intents=[i for i, _ in ranked[1:3]], matched_keywords=matched.get(intent, []),
                source=source, priority_reasons=reasons, **senti)


def is_conversational(intent):
    return intent in CONVERSATIONAL
