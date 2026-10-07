import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPPORTPILOT_DISABLE_LLM"] = "1"

import agents as A  # noqa: E402
from agents import mcp_bus  # noqa: E402

SCENARIOS = [
    ("CUST1001", ["My order was delivered but I received the wrong item"]),
    ("CUST1002", ["My payment failed but money was deducted", "ORD12346"]),
    ("CUST1008", ["There's a ₹18,499 transaction I did not make. Is this fraud?"]),
    ("CUST1003", ["I can't login, it says my account is locked"]),
    (None, ["What are the delivery charges for cash on delivery?"]),
]


def outcome(turns):
    return [(r["understanding"] or {}).get("intent") if r.get("understanding") else None for r in turns], \
        [r["decision"]["decision"] if isinstance(r.get("decision"), dict) else r.get("decision") for r in turns], \
        [[(a["action"], a["status"]) for a in r["actions"]] for r in turns], \
        [(r["ticket"] or {}).get("status") for r in turns]


def test_agents_run_as_real_mcp_tool_calls():
    store = A.Store(":memory:")
    bus = mcp_bus.get_bus(store)
    assert bus.transport == "mcp", bus.error
    r = A.handle_message(store, "My order was delivered but I received the wrong item", customer_id="CUST1001", use_llm=False)
    calls = [s for s in r["trace"] if s.get("tool")]
    assert {s["transport"] for s in calls} == {"mcp"}
    assert {"understand_message", "track_conversation", "customer_context", "search_knowledge", "troubleshoot",
            "decide_escalation", "execute_actions", "create_ticket", "track_resolution", "compose_reply",
            "review_reply"} <= {s["tool"] for s in calls}
    assert all(s["args"] and s["result"] for s in calls)


def test_tools_are_listed_over_mcp():
    names = {t["name"] for t in mcp_bus.get_bus(A.Store(":memory:")).list_tools()}
    assert {"understand_message", "troubleshoot", "create_ticket", "record_csat"} <= names


@pytest.mark.parametrize("customer_id,messages", SCENARIOS)
def test_mcp_and_direct_give_the_same_outcome(customer_id, messages, monkeypatch):
    via_mcp = A.run_conversation(A.Store(":memory:"), messages, customer_id)
    monkeypatch.setenv("SUPPORTPILOT_MCP", "0")
    direct_store = A.Store(":memory:")
    via_direct = A.run_conversation(direct_store, messages, customer_id)
    assert mcp_bus.get_bus(direct_store).transport == "direct"
    assert outcome(via_mcp) == outcome(via_direct)


def test_tool_errors_surface_instead_of_silently_passing():
    store = A.Store(":memory:")
    with pytest.raises(RuntimeError):
        mcp_bus.get_bus(store).call("record_csat", dict(ticket_id="TKT-NOPE", rating=5))
