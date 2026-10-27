"""Phase 1 platform tests: adapters, workspace boundary, policy + approvals, LLM merge/fallback, runs, API."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPPORTPILOT_DISABLE_LLM"] = "1"

import agents as A  # noqa: E402
from agents import approvals, llm, response_agent, runlog  # noqa: E402
from agents.policy import Policy  # noqa: E402
from agents.tenancy import Registry  # noqa: E402
from integrations import ActionError, DemoStoreAdapter, RestAdapter  # noqa: E402


@pytest.fixture()
def store():
    return A.Store(":memory:")


def ws_with(store, **policy):
    return A.Workspace(store, policy=Policy.from_dict(policy))


# ---- workspace boundary ---------------------------------------------------------------------------------
def test_business_data_is_read_only_for_agents(store):
    ws = A.as_workspace(store)
    with pytest.raises(PermissionError):
        ws.put("orders", "ORD12345", {})
    with pytest.raises(PermissionError):
        ws.update("payments", "x", status="refunded")
    ws.put("tickets", "T1", {"id": "T1"})  # support data is writable
    assert ws.get("tickets", "T1")


def test_reads_are_recorded_as_tool_calls(store):
    ws = A.as_workspace(store)
    with runlog.recording() as rec:
        ws.get("orders", "ORD12345")
        ws.list("orders", customer_id="CUST1001")
        ws.get("orders", "ORD99999")
    assert [c["tool"] for c in rec.tool_calls] == ["DemoStore.orders.get", "DemoStore.orders.find", "DemoStore.orders.get"]
    assert rec.tool_calls[0]["result"]["found"] and rec.tool_calls[2]["status"] == "not_found"
    assert all(c["simulated"] for c in rec.tool_calls)


def test_entities_not_exposed_by_the_adapter_are_denied(store):
    class NoPayments(DemoStoreAdapter):
        def entities(self):
            return tuple(e for e in super().entities() if e != "payments")
    ws = A.Workspace(store, NoPayments(store))
    with runlog.recording() as rec:
        assert ws.list("payments") == []
    assert rec.tool_calls[0]["status"] == "denied"


# ---- REST adapter: a "real" company API, simulated over HTTP-shaped calls --------------------------------
class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeCompanyAPI:
    """Pretends to be a company's REST API, backed by the DemoStore simulator; records every request."""

    def __init__(self, store, down=False):
        self.backend, self.down, self.requests = DemoStoreAdapter(store), down, []

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        self.requests.append((method, url, headers, params, json))
        if self.down:
            raise ConnectionError("refused")
        assert headers == {"Authorization": "Bearer company-token"}
        path = url.split("https://api.acme.test/", 1)[1].split("/")
        if method == "POST" and path[0] == "actions":
            try:
                data, msg = self.backend.perform(path[1], json["params"], {"customer_id": json["customer_id"]})
                return FakeResponse(200, {"data": data, "message": msg})
            except ActionError as exc:
                return FakeResponse(422, {"error": str(exc.args[0])})
        if len(path) == 2:
            rec = self.backend.get(path[0], path[1])
            return FakeResponse(200, rec) if rec else FakeResponse(404, {})
        return FakeResponse(200, {"items": self.backend.find(path[0], **(params or {}))})


def rest_workspace(down=False):
    company = A.Store(":memory:")          # the company's own systems
    support = A.Store(":memory:", seed=False)  # SupportPilot's store: no business data at all
    api = FakeCompanyAPI(company, down)
    adapter = RestAdapter("https://api.acme.test", token="company-token", session=api, name="AcmeAPI",
                          actions=["create_replacement", "generate_return_label", "initiate_refund", "cancel_order"])
    return A.Workspace(support, adapter, dict(id="acme", name="Acme")), api, company


def test_full_conversation_through_a_rest_company_api():
    ws, api, company = rest_workspace()
    r = A.handle_message(ws, "My order was delivered but I received the wrong item", None, "CUST1001", use_llm=False)
    assert r["decision"]["decision"] == "auto_resolve"
    assert {a["action"]: a["status"] for a in r["actions"]} == {"create_replacement": "success", "generate_return_label": "success"}
    posts = [u for m, u, *_ in api.requests if m == "POST"]
    assert posts == ["https://api.acme.test/actions/create_replacement", "https://api.acme.test/actions/generate_return_label"]
    assert company.get("orders", "ORD12345")["replacement_order"]      # changed in the company system...
    assert ws.platform.list("orders") == []                             # ...never copied into SupportPilot
    assert ws.get("tickets", r["ticket"]["id"])["status"] == "Resolved"
    run = ws.get("workflow_runs", r["run_id"])
    assert any(c["tool"] == "AcmeAPI.actions.create_replacement" and not c["simulated"] for c in run["tool_calls"])


def test_rest_adapter_refusal_and_unsupported_actions():
    ws, api, _ = rest_workspace()
    ctx = A.customer_context(ws, "CUST1009")
    rec = A.execute_action(ws, "expedite_shipment", {"order_id": "ORD12351"}, ctx)[0]
    assert rec["status"] == "blocked" and "does not support" in rec["message"]


def test_company_system_down_degrades_honestly():
    ws, api, _ = rest_workspace(down=True)
    r = A.handle_message(ws, "My order was delivered but I received the wrong item", None, "CUST1001", use_llm=False)
    assert r["degraded"] and "Nothing has been changed" in r["reply"]
    assert ws.get("workflow_runs", r["run_id"])["status"] == "degraded"
    assert ws.list("actions") == []


# ---- policy & approvals ------------------------------------------------------------------------------------
def test_ownership_is_checked_before_the_company_is_called(store):
    ctx = A.customer_context(store, "CUST1001")
    rec = A.execute_action(store, "cancel_order", {"order_id": "ORD12352"}, ctx)[0]  # Meera's order
    assert rec["status"] == "blocked" and rec["authorization"]["rule"] == "ownership"
    assert store.get("orders", "ORD12352")["status"] != "cancelled"


def test_ai_actions_need_an_identified_customer(store):
    rec = A.execute_action(store, "cancel_order", {"order_id": "ORD12352"}, None)[0]
    assert rec["status"] == "blocked" and rec["authorization"]["rule"] == "identity"


def test_disabled_action_is_refused(store):
    ws = ws_with(store, action_modes={"cancel_order": "disabled"})
    rec = A.execute_action(ws, "cancel_order", {"order_id": "ORD12352"}, A.customer_context(ws, "CUST1009"))[0]
    assert rec["status"] == "blocked" and "disabled" in rec["message"]


def test_tenant_approval_mode_turns_auto_resolution_into_an_approval(store):
    ws = ws_with(store, action_modes={"create_replacement": "approval"})
    r = A.handle_message(ws, "My order was delivered but I received the wrong item", None, "CUST1001", use_llm=False)
    status = {a["action"]: a["status"] for a in r["actions"]}
    assert status["create_replacement"] == "pending_approval"
    assert r["decision"]["decision"] == "escalate" and r["ticket"]["approvals"]
    assert not store.get("orders", "ORD12345").get("replacement_order")
    assert "approval" in r["reply"] and r["review"]["approved"]


def test_approve_runs_the_action_and_resolves_the_ticket(store):
    r = A.handle_message(store, "I want a refund for my laptop, order ORD12353", None, "CUST1012", use_llm=False)
    ap = approvals.pending(store)[0]
    assert ap["ticket_id"] == r["ticket"]["id"] and ap["action"] == "initiate_refund"
    assert not store.list("refunds", order_id="ORD12353")
    done = approvals.decide(store, ap["id"], True, "Aditi Rao", "QC passed")
    assert done["status"] == "approved"
    refund = store.list("refunds", order_id="ORD12353")[0]
    assert refund["amount"] == 54999
    t = store.get("tickets", r["ticket"]["id"])
    assert t["status"] == "Resolved" and t["resolved_by"] == "Human:Aditi Rao"
    assert any(n["ticket_id"] == t["id"] and "Resolved" in n["kind"] for n in store.list("notifications"))
    with pytest.raises(ValueError):
        approvals.decide(store, ap["id"], True, "Aditi Rao")
    events = [a["event"] for a in A.audit.recent(store)]
    assert "approval.approved" in events and "action.initiate_refund" in events


def test_reject_changes_nothing(store):
    A.handle_message(store, "I want a refund for my laptop, order ORD12353", None, "CUST1012", use_llm=False)
    ap = approvals.pending(store)[0]
    assert approvals.decide(store, ap["id"], False, "Aditi Rao", "used item")["status"] == "rejected"
    assert not store.list("refunds", order_id="ORD12353")


def test_external_ai_clients_cannot_run_sensitive_actions_alone(store):
    ctx = A.customer_context(store, "CUST1009")
    rec = A.execute_action(store, "cancel_order", {"order_id": "ORD12352"}, ctx, actor="AI:mcp")[0]
    assert rec["status"] == "pending_approval" and rec["authorization"]["rule"] == "external AI"
    assert store.get("orders", "ORD12352")["status"] != "cancelled"


# ---- LLM: merge, validation, fallback ----------------------------------------------------------------------
class FakeLLM:
    def __init__(self, reading=None, reply=None, fail=None):
        self.reading, self.reply, self.fail, self.calls = reading, reply, fail, []

    def __call__(self, prompt, system_msg=None, max_tokens=400, json_mode=False, purpose="chat", temperature=0.2):
        self.calls.append(purpose)
        if self.fail:
            res = llm.LLMResult(False, error=self.fail, model="fake", purpose=purpose)
        elif purpose == "understand":
            res = llm.LLMResult(True, text=json.dumps(self.reading), model="fake", purpose=purpose)
        elif purpose == "respond" and self.reply:
            res = llm.LLMResult(True, text=self.reply(prompt.split("DRAFT:\n", 1)[1]), model="fake", purpose=purpose)
        else:
            res = llm.LLMResult(True, text=prompt.split("DRAFT:\n", 1)[-1], model="fake", purpose=purpose)
        runlog.record_llm(dict(ok=res.ok, error=res.error, purpose=purpose, model="fake", ms=1))
        return res


@pytest.fixture()
def fake_llm(monkeypatch):
    def install(**kw):
        fake = FakeLLM(**kw)
        monkeypatch.setattr(llm, "enabled", lambda: True)
        monkeypatch.setattr(llm, "chat", fake)
        return fake
    return install


def reading(intent, conf=0.9, **kw):
    return dict(dict(intent=intent, confidence=conf, emotion="calm", wants_human=False, order_id=None, summary="s"), **kw)


def test_rules_and_llm_agreeing_raise_confidence(fake_llm):
    fake_llm(reading=reading("wrong_item", 0.9))
    u = A.understand("I received the wrong item")
    assert u["source"] == "rules+llm" and u["confidence"] >= 0.9 and u["llm"]["used"]


def test_llm_understands_what_rules_miss_and_ids_must_be_real(fake_llm):
    fake_llm(reading=reading("wrong_item", 0.9, order_id="12345"))
    u = A.understand("the parcel had someone else's shoes in it, not my headphones (order 12345)")
    assert u["intent"] == "wrong_item" and u["source"] == "llm"
    assert u["entities"]["order_id"] == "ORD12345"
    fake_llm(reading=reading("wrong_item", 0.9, order_id="99999"))  # not in the message → dropped
    assert "order_id" not in A.understand("the parcel had someone else's shoes in it")["entities"]


def test_invalid_llm_intent_falls_back_to_rules(fake_llm):
    fake_llm(reading=reading("issue_refund_now"))
    u = A.understand("My payment failed but money was deducted")
    assert u["intent"] == "payment_failure" and u["source"] == "rules" and u["llm"]["status"] == "fallback"


def test_security_intent_from_either_reader_wins(fake_llm):
    fake_llm(reading=reading("fraud_report", 0.85))
    assert A.understand("I was charged twice for my order")["intent"] == "fraud_report"


def test_llm_failure_is_reported_and_rules_answer(store, fake_llm):
    fake_llm(fail="timeout")
    r = A.handle_message(store, "My order was delivered but I received the wrong item", None, "CUST1001")
    assert r["decision"]["decision"] == "auto_resolve" and r["understanding"]["source"] == "rules"
    assert r["llm"]["mode"] == "fallback" and "timeout" in r["llm"]["reason"]
    assert store.get("workflow_runs", r["run_id"])["llm"]["mode"] == "fallback"


def test_llm_only_reading_needs_customer_confirmation_before_actions(store, fake_llm):
    fake_llm(reading=reading("wrong_item", 0.9))
    text = "the parcel that came today has someone else's shoes in it, not my headphones"
    r1 = A.handle_message(store, text, None, "CUST1001")
    assert r1["decision"]["decision"] == "confirm" and r1["actions"] == [] and "yes / no" in r1["reply"]
    assert not store.get("orders", "ORD12345").get("replacement_order")
    r2 = A.handle_message(store, "yes please", r1["conversation_id"], "CUST1001")
    assert r2["mode"] == "confirmed" and r2["decision"]["decision"] == "auto_resolve"
    assert store.get("orders", "ORD12345").get("replacement_order")
    assert r2["review"]["approved"]


def test_customer_can_decline_proposed_actions(store, fake_llm):
    fake_llm(reading=reading("wrong_item", 0.9))
    r1 = A.handle_message(store, "the parcel that came today has someone else's shoes in it", None, "CUST1001")
    r2 = A.handle_message(store, "no", r1["conversation_id"], "CUST1001")
    assert "haven't changed anything" in r2["reply"] and r2["actions"] == []
    assert not store.get("orders", "ORD12345").get("replacement_order")


def test_llm_reply_with_invented_facts_is_rejected(store, fake_llm):
    fake_llm(reading=reading("wrong_item"), reply=lambda draft: draft + "\nAlso, here's ₹500 credit, code GIFT500.")
    r = A.handle_message(store, "My order was delivered but I received the wrong item", None, "CUST1001")
    assert "GIFT500" not in r["reply"] and "₹500" not in r["reply"]
    assert r["llm"]["mode"] == "partial"


def test_llm_reply_that_keeps_the_facts_is_used(store, fake_llm):
    fake_llm(reading=reading("wrong_item"), reply=lambda draft: "So sorry about the mix-up!\n\n" + draft)
    r = A.handle_message(store, "My order was delivered but I received the wrong item", None, "CUST1001")
    assert r["reply"].startswith("So sorry about the mix-up!") and r["llm"]["mode"] == "llm"


def test_validation_rules():
    draft = "Refund RF7001 of ₹2,499 is initiated — expected by 31 Oct 2026. Reference TKT-1001."
    assert response_agent.validate(draft, "Good news: refund RF7001 (₹2,499) is on its way, expected by 31 Oct 2026. Ref TKT-1001.") == ""
    assert "dropped" in response_agent.validate(draft, "Refund RF7001 of ₹2,500 is initiated. Reference TKT-1001.")
    assert "number" in response_agent.validate(draft, draft + " It takes 48 hours.")


def test_gateway_reports_missing_key_timeout_and_retired_model(monkeypatch):
    monkeypatch.delenv("SUPPORTPILOT_DISABLE_LLM", raising=False)
    monkeypatch.setattr(llm, "api_key", lambda: None)
    assert llm.chat("hi").error == "not configured" and llm.status()["mode"] == "fallback"

    import requests
    monkeypatch.setattr(llm, "api_key", lambda: "test-key")
    monkeypatch.setattr(llm, "configured_model", lambda: "retired-model")
    monkeypatch.setattr(llm, "_unavailable", set())

    def timeout(*a, **k):
        raise requests.exceptions.ReadTimeout("slow")
    monkeypatch.setattr(requests, "post", timeout)
    assert llm.chat("hi").error == "timeout"

    def by_model(url, json=None, **k):
        if json["model"] == "retired-model":
            return FakeResponse(404, {"error": {"message": "The model does not exist"}})
        return FakeResponse(200, {"choices": [{"message": {"content": "hello"}}]})
    monkeypatch.setattr(requests, "post", by_model)
    res = llm.chat("hi")
    assert res.ok and res.model != "retired-model" and "unavailable" in llm.status()["model_note"]


# ---- runs & input validation --------------------------------------------------------------------------------
def test_every_turn_is_persisted_as_a_workflow_run(store):
    r = A.handle_message(store, "My order was delivered but I received the wrong item", None, "CUST1001", use_llm=False)
    run = store.get("workflow_runs", r["run_id"])
    assert run["status"] == "completed" and run["decision"] == "auto_resolve" and run["ticket_id"] == r["ticket"]["id"]
    assert run["steps"] and run["tool_calls"] and set(run["action_ids"]) == {a["id"] for a in r["actions"]}
    assert run["intent"] == "wrong_item" and run["tenant"] == "demostore"


def test_failed_runs_are_persisted_too(store):
    with pytest.raises(ValueError):
        A.handle_message(store, "   ", None, "CUST1001", use_llm=False)
    assert [r["status"] for r in store.list("workflow_runs")] == ["failed"]


def test_messages_are_sanitized(store):
    r = A.handle_message(store, "hi\x00\x07 there" + "x" * 5000, None, "CUST1001", use_llm=False)
    text = store.get("conversations", r["conversation_id"])["messages"][0]["text"]
    assert "\x00" not in text and len(text) == 4000


# ---- HTTP API -------------------------------------------------------------------------------------------------
@pytest.fixture()
def api():
    from fastapi.testclient import TestClient
    from api import server
    reg = Registry(":memory:", demo_db_path=":memory:")
    reg.create_tenant("acme", "Acme (second DemoStore)", dict(type="demostore"), db_path=":memory:")
    server.set_registry(reg)
    server._hits.clear()
    keys = dict(channel=reg.create_key("demostore", "channel")[0], admin=reg.create_key("demostore", "admin")[0],
                acme=reg.create_key("acme", "admin")[0])
    yield TestClient(server.app), keys, reg
    server.set_registry(None)


def H(key):
    return {"Authorization": f"Bearer {key}"}


def test_api_requires_a_valid_key_and_audits_failures(api):
    client, keys, reg = api
    assert client.get("/health").status_code == 200
    assert client.post("/v1/messages", json={"text": "hi"}).status_code == 401
    assert client.post("/v1/messages", json={"text": "hi"}, headers=H("sp_demostore_wrong")).status_code == 401
    assert any(a["event"] == "api.auth_failed" for a in A.audit.recent(reg.store))


def test_api_message_roundtrip_and_roles(api):
    client, keys, reg = api
    r = client.post("/v1/messages", headers=H(keys["channel"]),
                    json={"customer_id": "CUST1001", "text": "My order was delivered but I received the wrong item", "use_llm": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"]["decision"] == "auto_resolve" and body["ticket"]["status"] == "Resolved" and body["run_id"]
    assert client.get(f"/v1/conversations/{body['conversation_id']}", headers=H(keys["channel"])).json()["messages"]
    assert client.get("/v1/tickets", headers=H(keys["channel"])).status_code == 403
    run = client.get(f"/v1/runs/{body['run_id']}", headers=H(keys["admin"])).json()
    assert run["tool_calls"] and run["decision"] == "auto_resolve"


def test_api_tenant_isolation(api):
    client, keys, reg = api
    body = client.post("/v1/messages", headers=H(keys["admin"]),
                       json={"customer_id": "CUST1001", "text": "I received the wrong item", "use_llm": False}).json()
    other = H(keys["acme"])
    assert client.get(f"/v1/conversations/{body['conversation_id']}", headers=other).status_code == 404
    assert client.get(f"/v1/runs/{body['run_id']}", headers=other).status_code == 404
    assert client.get(f"/v1/tickets/{body['ticket']['id']}", headers=other).status_code == 404
    assert client.post("/v1/messages", headers=other, json={"conversation_id": body["conversation_id"], "text": "hi"}).status_code == 404
    assert reg.workspace("acme").list("workflow_runs") == []


def test_api_validates_input(api):
    client, keys, _ = api
    h = H(keys["channel"])
    assert client.post("/v1/messages", headers=h, json={"text": "x" * 4001}).status_code == 422
    assert client.post("/v1/messages", headers=h, json={"text": "hi", "customer_id": "../etc"}).status_code == 422
    assert client.post("/v1/messages", headers=h, json={"text": "hi", "customer_id": "CUST9999"}).status_code == 404
    first = client.post("/v1/messages", headers=h, json={"text": "hi", "customer_id": "CUST1001", "use_llm": False}).json()
    other = client.post("/v1/messages", headers=h, json={"text": "hi", "customer_id": "CUST1002",
                                                         "conversation_id": first["conversation_id"]})
    assert other.status_code == 403


def test_api_approval_flow_and_key_revocation(api):
    client, keys, reg = api
    adm = H(keys["admin"])
    body = client.post("/v1/messages", headers=adm, json={"customer_id": "CUST1012", "use_llm": False,
                                                          "text": "I want a refund for my laptop, order ORD12353"}).json()
    assert body["approvals"]
    pending = client.get("/v1/approvals", headers=adm).json()
    assert [a["id"] for a in pending] == body["approvals"]
    d = client.post(f"/v1/approvals/{pending[0]['id']}/decision", headers=adm, json={"approve": True, "approver": "Aditi"})
    assert d.status_code == 200 and d.json()["status"] == "approved"
    again = client.post(f"/v1/approvals/{pending[0]['id']}/decision", headers=adm, json={"approve": True, "approver": "Aditi"})
    assert again.status_code == 409
    key_id = next(k["id"] for k in reg.keys("demostore") if k["role"] == "channel")
    reg.revoke_key(key_id)
    assert client.post("/v1/messages", headers=H(keys["channel"]), json={"text": "hi"}).status_code == 401
