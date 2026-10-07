import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPPORTPILOT_DISABLE_LLM"] = "1"

import agents as A  # noqa: E402


@pytest.fixture()
def store():
    return A.Store(":memory:")


def talk(store, messages, customer_id=None, channel="web"):
    return A.run_conversation(store, messages, customer_id, channel)


# ---- 1 understanding ----------------------------------------------------------------------
def test_understanding_matches_problem_statement_example():
    u = A.understand("My payment failed but money was deducted.")
    assert u["intent"] == "payment_failure"
    assert u["category"] == "Billing"
    assert u["priority"] == "high"
    assert u["sentiment"] == "negative"


def test_entities_and_angry_priority_bump():
    u = A.understand("WORST service ever!!! order ORD12345 still not delivered, I'm furious")
    assert u["entities"]["order_id"] == "ORD12345"
    assert u["emotion"] == "angry"
    assert "angry customer" in u["priority_reasons"]


def test_faq_question_is_not_forced_into_a_workflow():
    assert A.understand("What are the delivery charges for COD?")["intent"] == "general_inquiry"


# ---- end-to-end auto resolution ---------------------------------------------------------------
def test_wrong_item_is_resolved_without_a_human(store):
    r = talk(store, ["My order was delivered but I received the wrong item"], "CUST1001")[0]
    assert r["decision"]["decision"] == "auto_resolve"
    assert {a["action"] for a in r["actions"]} == {"create_replacement", "generate_return_label"}
    assert r["ticket"]["status"] == "Resolved" and not r["ticket"]["escalated"]
    assert r["review"]["approved"]
    assert store.get("orders", "ORD12345")["replacement_order"]


def test_multi_turn_payment_failure_fills_order_id_slot(store):
    t1, t2 = talk(store, ["My payment failed but money was deducted", "ORD12346"], "CUST1002")
    assert t1["decision"]["decision"] == "clarify" and "Order ID" in t1["reply"]
    assert t2["issue"]["intent"] == "payment_failure" and t2["mode"] == "slot_fill"
    assert t2["actions"][0]["action"] == "initiate_refund" and t2["actions"][0]["status"] == "success"


def test_bare_number_fills_order_slot(store):
    _, t2 = talk(store, ["My payment failed", "12346"], "CUST1002")
    assert t2["issue"]["slots"]["order_id"] == "ORD12346"


def test_guest_is_identified_by_email_mid_conversation(store):
    turns = talk(store, ["my payment failed but amount debited", "it's priya.nair@example.com", "ORD12346"])
    assert turns[-1]["context"]["customer_id"] == "CUST1002"
    assert turns[-1]["decision"]["decision"] == "auto_resolve"


def test_locked_account_is_unlocked(store):
    r = talk(store, ["I can't login, my account is locked"], "CUST1003")[0]
    assert [a["action"] for a in r["actions"]] == ["unlock_account", "reset_password"]
    assert store.get("accounts", "CUST1003")["status"] == "active"


def test_hardware_fault_falls_back_to_technician_visit(store):
    r = talk(store, ["Internet keeps disconnecting and speed is slow"], "CUST1007")[0]
    assert [(a["action"], a["status"]) for a in r["actions"]] == [("remote_line_reset", "failed"),
                                                                   ("schedule_technician_visit", "success")]
    assert r["decision"]["decision"] == "auto_resolve"


# ---- escalation ----------------------------------------------------------------------------------
def test_failed_sync_escalates_with_complete_ticket(store):
    r = talk(store, ["I paid for Premium yesterday but my subscription is still not activated"], "CUST1004")[0]
    assert r["decision"]["decision"] == "escalate"
    t = store.get("tickets", r["ticket"]["id"])
    assert t["team"] == "Engineering" and t["status"] == "Assigned"
    assert any("Payment successful" in line for line in t["investigation"])
    assert any("Retry Subscription Sync" in line for line in t["actions_taken"])
    assert t["human_status"] == "Needs Engineering Review"
    assert t["conversation_summary"] and t["recommended_next_step"]


@pytest.mark.parametrize("text,cid,team", [
    ("Someone made a fraud transaction on my card", "CUST1008", "Trust & Safety"),
    ("I think my personal data was leaked in a data breach", "CUST1001", "Security Incident Response"),
    ("I will send a legal notice and go to consumer court", "CUST1002", "Legal & Privacy"),
])
def test_mandatory_escalations(store, text, cid, team):
    r = talk(store, [text], cid)[0]
    assert r["decision"]["decision"] == "escalate"
    assert r["ticket"]["team"] == team


def test_fraud_places_protective_hold_before_escalating(store):
    r = talk(store, ["There is an unauthorized transaction I did not make"], "CUST1008")[0]
    assert r["actions"][0]["action"] == "freeze_account"
    assert store.get("accounts", "CUST1008")["status"] == "security_hold"


def test_high_value_damage_needs_approval(store):
    r = talk(store, ["My laptop arrived damaged, screen is cracked"], "CUST1012")[0]
    assert r["decision"]["decision"] == "escalate"
    assert all(a["action"] != "create_replacement" for a in r["actions"])


def test_low_confidence_escalates():
    u = dict(intent="general_inquiry", confidence=0.35, emotion="calm", priority="low")
    inv = dict(confidence=0.4, resolvable=True, missing_slots=[], steps=[], diagnosis="")
    d = A.decide_escalation(u, inv, None, dict(intent="general_inquiry", turns=1, priority="low"))
    assert d["decision"] == "escalate" and d["confidence"] < 0.7


def test_angry_customer_gets_stricter_threshold():
    inv = dict(confidence=0.75, resolvable=True, missing_slots=[], steps=[], diagnosis="")
    issue = dict(intent="delivery_delay", turns=1, priority="medium")
    calm = A.decide_escalation(dict(confidence=0.75, emotion="calm", priority="medium"), inv, None, issue)
    angry = A.decide_escalation(dict(confidence=0.75, emotion="angry", priority="medium"), inv, None, issue)
    assert angry["threshold"] > calm["threshold"]
    assert calm["decision"] == "auto_resolve" and angry["decision"] == "escalate"


def test_human_request_escalates(store):
    r = talk(store, ["I want to talk to a human agent"], "CUST1009")[0]
    assert r["decision"]["decision"] == "escalate"


# ---- guardrails ------------------------------------------------------------------------------------
def test_ai_refund_limit_is_enforced(store):
    ctx = A.customer_context(store, "CUST1012")
    res = A.execute_action(store, "initiate_refund", {"order_id": "ORD12353"}, ctx)
    assert res[0]["status"] == "blocked"


def test_actions_are_idempotent(store):
    ctx = A.customer_context(store, "CUST1009")
    first = A.execute_action(store, "cancel_order", {"order_id": "ORD12352"}, ctx)[0]
    second = A.execute_action(store, "cancel_order", {"order_id": "ORD12352"}, ctx)[0]
    assert first["status"] == "success" and second["status"] == "skipped"


def test_orders_of_other_customers_are_not_disclosed(store):
    r = talk(store, ["Where is my order ORD12349?"], "CUST1001")[0]
    assert r["decision"]["decision"] == "clarify"
    assert "HM-044" not in r["reply"] and "Air Fryer" not in r["reply"]


def test_unknown_action_is_blocked(store):
    assert A.execute_action(store, "delete_database", {}, None)[0]["status"] == "blocked"


# ---- tracking, SLA, CSAT, KB, root cause -------------------------------------------------------------
def test_lifecycle_transitions_notify_customer(store):
    r = talk(store, ["I paid for Premium yesterday but my subscription is still not activated"], "CUST1004")[0]
    tid = r["ticket"]["id"]
    A.transition(store, tid, "Pending Customer", actor="Kunal Shah", note="Please log out and in again.")
    A.transition(store, tid, "Resolved", actor="Kunal Shah", note="Entitlement re-synced.")
    A.transition(store, tid, "Closed", actor="Kunal Shah")
    kinds = [n["kind"] for n in store.list("notifications", ticket_id=tid)]
    assert {"status:Assigned", "status:Pending Customer", "status:Resolved", "status:Closed"} <= set(kinds)
    with pytest.raises(ValueError):
        A.transition(store, tid, "Assigned")


def test_sla_breach_is_auto_escalated(store):
    r = talk(store, ["I paid for Premium yesterday but my subscription is still not activated"], "CUST1004")[0]
    t = store.get("tickets", r["ticket"]["id"])
    later = A.shared_agent.parse_ts(t["created_at"]) + dt.timedelta(minutes=16)  # Premium high: 15-minute response
    flagged = {f["ticket_id"]: f for f in A.sla_scan(store, ref=later)}
    assert flagged[t["id"]]["state"] == "breached" and flagged[t["id"]]["escalated_now"]
    assert store.get("tickets", t["id"])["assignee"].startswith("Sunita Rao")


def test_csat_closes_ticket_and_feeds_thresholds(store):
    t1, t2 = talk(store, ["My order was delivered but I received the wrong item", "5 great service"], "CUST1001")
    assert t2["csat"]["rating"] == 5
    assert store.get("tickets", t1["ticket"]["id"])["status"] == "Closed"
    assert A.learned_thresholds(store)["refund_request"]["threshold"] > 0.7  # seeded low AI CSAT on refunds


def test_kb_expansion_creates_article_only_for_new_knowledge(store):
    r = talk(store, ["I paid for Premium yesterday but my subscription is still not activated"], "CUST1004")[0]
    t = store.get("tickets", r["ticket"]["id"])
    created = A.expand_kb(store, t, "Engineering replayed the entitlement queue after ENT-503 errors cleared; "
                                    "customers must log out and log back in to see Premium benefits.")
    assert created["created"]
    assert any(a["id"] == created["article"]["id"] for a in A.search_kb("log out entitlement queue replay", store=store))


def test_root_cause_detects_gateway_spike(store):
    inc = [i for i in A.detect_incidents(store) if i["intent"] == "payment_failure"]
    assert inc and inc[0]["dominant_value"] == "PayFast" and "gateway" in inc[0]["hypothesis"]


@pytest.mark.parametrize("channel", ["whatsapp", "email", "telegram", "app", "web"])
def test_omnichannel_payloads_identify_customer(store, channel):
    r = A.handle_message(store, channel=channel, payload=A.SAMPLE_PAYLOADS[channel], use_llm=False)
    assert r["context"] is not None
    assert r["review"]["approved"]
