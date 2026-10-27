"""Help centre: articles must say exactly what the system does, be findable, and be safely editable."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPPORTPILOT_DISABLE_LLM"] = "1"

import agents as A  # noqa: E402
from agents import knowledge_agent as K  # noqa: E402
from agents import troubleshooting_agent as T  # noqa: E402
from agents.shared_agent import AUTO_REFUND_LIMIT, HIGH_VALUE_CLAIM, SLA_RESPONSE, inr  # noqa: E402
from integrations import demostore as D  # noqa: E402

KB = {a["id"]: a for a in K.load_help_center()}
CATEGORY_NAMES = {"Electronics": "Electronics", "Fashion": "Fashion", "Home": "Home & Kitchen", "Books": "Books",
                  "Beauty": "Beauty & Personal care", "Grocery": "Grocery"}


def body(kid):
    return KB[kid]["content"]


def human_minutes(m):
    return f"{m // 60} hour{'s' if m >= 120 else ''}" if m >= 60 else f"{m} minutes"


# ---- the articles state the rules the code enforces ----------------------------------------------------------
def test_every_article_is_well_formed():
    assert len(KB) >= 40
    for a in KB.values():
        assert a["title"] and a["content"] and a["owner"] and a["last_reviewed"]
        assert a["audience"] in ("customer", "internal")
        assert all(i in A.INTENTS for i in a["intents"]), a["id"]
        if a["type"] == "troubleshooting":
            assert a["audience"] == "internal" and a["steps"]


def test_return_windows_match_the_code():
    for cat, name in CATEGORY_NAMES.items():
        row = f"| {name} | {T.RETURN_WINDOW[cat]} days | {T.return_window(cat, 'Premium')} days |"
        assert row in body("KB-001"), row
    assert f"within **{T.SELLER_ERROR_WINDOW} days of delivery**" in body("KB-001")


def test_refund_rules_and_timelines_match_the_code():
    assert inr(AUTO_REFUND_LIMIT) in body("KB-002")
    t = body("KB-023")
    for method, kind in (("UPI", "fast"), ("NovaPay wallet", "fast"), ("Credit / debit card", "standard"), ("Net banking", "standard")):
        assert f"| {method} | within {D.REFUND_DAYS[kind]} days |" in t


def test_claims_replacement_and_delay_rules_match_the_code():
    for kid in ("KB-031", "KB-032", "KB-006", "KB-007"):
        assert inr(HIGH_VALUE_CLAIM) in body(kid), kid
    assert f"within {D.REPLACEMENT_DAYS['default']} days" in body("KB-031") and "next day for Nova Premium" in body("KB-031")
    late = body("KB-029")
    assert inr(T.DELAY_COUPON["default"]) in late and inr(T.DELAY_COUPON["Premium"]) in late
    assert f"valid for {D.COUPON_VALID_DAYS} days" in late and f"**{D.COUPON_VALID_DAYS} days**" in body("KB-028")


def test_account_and_membership_rules_match_the_code():
    assert f"**{T.LOCK_THRESHOLD} failed password attempts**" in body("KB-013")
    assert f"**{D.RESET_LINK_MINUTES} minutes**" in body("KB-012")
    plans = body("KB-014")
    assert f"| Price | {inr(D.PLAN_PRICES['Plus'])} / month | {inr(D.PLAN_PRICES['Premium'])} / month |" in plans


def test_response_time_table_matches_the_sla_policy():
    t = body("KB-043")
    for prio, label in (("critical", "Critical (fraud, security)"), ("high", "High"), ("medium", "Medium"), ("low", "Low")):
        cells = " | ".join(human_minutes(SLA_RESPONSE[tier][prio]) for tier in ("Standard", "Plus", "Premium"))
        assert f"| {label} | {cells} |" in t, label


def test_kb_ids_cited_by_the_troubleshooting_agent_exist():
    import re
    src = open(T.__file__, encoding="utf-8").read()
    for kid in set(re.findall(r"KB-\d{3}", src)):
        assert kid in KB, kid


# ---- retrieval ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("question, expected", [
    ("can I exchange shoes for another size", "KB-033"), ("what is the return window for electronics", "KB-001"),
    ("is cash on delivery available", "KB-025"), ("how long does a refund to my card take", "KB-023"),
    ("what payment methods do you accept", "KB-034"), ("when is support available", "KB-024"),
    ("how do I change my wifi password", "KB-042"), ("do you offer EMI", "KB-035"),
    ("how do I download my invoice", "KB-036"), ("can I change my delivery address", "KB-026"),
    ("how do I delete my account", "KB-022"), ("what are premium membership benefits", "KB-014"),
])
def test_faq_questions_find_the_right_article(question, expected):
    top = [a["id"] for a in A.search_kb(question, "general_inquiry", None, 3)]
    assert expected in top, (question, top)


def test_internal_playbooks_never_reach_customers():
    store = A.Store(":memory:")
    r = A.handle_message(store, "what is the workflow for a failed payment with bank debit?", None, None, use_llm=False)
    for a in K.load_help_center():
        if a["audience"] == "internal":
            assert a["content"][:60] not in r["reply"]


def test_premium_members_get_the_30_day_return_window():
    assert T.return_window("Electronics", "Premium") == 30 and T.return_window("Electronics", "Plus") == 10
    assert T.return_window("Grocery", "Premium") == 3


# ---- storage: imported into the database, editable, versioned ----------------------------------------------------
def test_kb_is_imported_into_the_database_and_edits_are_versioned():
    store = A.Store(":memory:")
    kb = A.load_kb(store)
    assert {a["id"] for a in kb} >= set(KB) and store.get("meta", "kb_import")["source"] == "docs/help-center"
    assert store.get("kb_articles", "KB-025")["source_file"].endswith("delivery-charges-and-cod.md")
    with pytest.raises(ValueError):
        K.save_article(store, "KB-025", " ", content="x")
    new = K.save_article(store, "KB-025", "Kavya", content="Delivery is free on every order this week.\n\nMore text.")
    assert new["version"] == 2 and new["summary"] == "Delivery is free on every order this week."
    assert K.history(store, "KB-025")[0]["version"] == 1
    assert any(a["id"] == "KB-025" for a in A.search_kb("free delivery this week", store=store))
    K.reimport(store, "Kavya")
    assert store.get("kb_articles", "KB-025")["version"] == 1 and len(K.history(store, "KB-025")) == 2
