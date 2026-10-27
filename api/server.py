"""SupportPilot HTTP API — how a business plugs its website, app or helpdesk into the agentic workflow.

Run:  uvicorn api.server:app --port 8080        (interactive docs at /docs)
Auth: ``Authorization: Bearer <key>`` (or ``X-API-Key``). Keys are per tenant; create them with
      ``python manage.py create-key demostore --role admin`` or in the dashboard's Integration tab.

The caller's backend authenticates its own end customer and passes ``customer_id``; SupportPilot verifies the
customer exists in that tenant's company system. A key can only ever see its own tenant's data: every request
resolves to that tenant's Workspace (its own database), and ids from another tenant simply do not exist there.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import defaultdict, deque
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agents as A  # noqa: E402
from agents import approvals, audit, llm  # noqa: E402
from agents.tenancy import get_registry  # noqa: E402

ID = r"^[A-Za-z0-9_.:-]{1,64}$"
EMAIL = r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$"
RATE_LIMIT_PER_MIN = int(os.getenv("SUPPORTPILOT_API_RATE_LIMIT", "120"))

app = FastAPI(title="SupportPilot API", version="1.0",
              description="Agentic customer support for your business: send customer messages, inspect workflow runs, "
                          "manage tickets and approve sensitive actions.")
_registry_override = None
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def registry():
    return _registry_override or get_registry()


def set_registry(reg):
    """Tests and embedders can point the API at a specific registry."""
    global _registry_override
    _registry_override = reg


class Principal:
    def __init__(self, key, ws):
        self.key, self.ws = key, ws
        self.tenant_id, self.role = key["tenant_id"], key["role"]
        self.actor = f"api:{key['id']}"


def _authenticate(request: Request):
    reg = registry()
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
    key = reg.authenticate(token)
    if key is None:
        audit.log(reg.store, "api.auth_failed", "anonymous", outcome="denied", path=request.url.path,
                  client=request.client.host if request.client else None, key_prefix=token[:12] or None)
        raise HTTPException(401, "Missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
    now = time.time()
    with _hits_lock:
        q = _hits[key["id"]]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            raise HTTPException(429, "Rate limit exceeded")
        q.append(now)
    reg.touch(key)
    return Principal(key, reg.workspace(key["tenant_id"]))


def any_role(p: Principal = Depends(_authenticate)):
    return p


def admin(p: Principal = Depends(_authenticate)):
    if p.role != "admin":
        audit.log(p.ws, "api.forbidden", p.actor, outcome="denied", role=p.role)
        raise HTTPException(403, "This key's role cannot use this endpoint (admin required)")
    return p


# ---- schemas ----------------------------------------------------------------------------------------------
class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000, description="The customer's message")
    customer_id: Optional[str] = Field(None, pattern=ID, description="Your authenticated customer's id")
    conversation_id: Optional[str] = Field(None, pattern=ID, description="Continue an existing conversation")
    channel: Literal["web", "whatsapp", "email", "telegram", "app"] = "web"
    contact_email: Optional[str] = Field(None, pattern=EMAIL, max_length=254,
                                         description="Optional address for ticket emails (requires SMTP setup)")
    use_llm: bool = True


class TransitionIn(BaseModel):
    status: Literal["Assigned", "Pending Customer", "Resolved", "Closed", "Reopened"]
    note: str = Field("", max_length=2000)
    actor: str = Field("Human Agent", min_length=1, max_length=80)


class DecisionIn(BaseModel):
    approve: bool
    approver: str = Field(min_length=1, max_length=80, description="Name of the human approving or rejecting")
    note: str = Field("", max_length=1000)


# ---- endpoints ------------------------------------------------------------------------------------------------
@app.get("/health")
def health():
    st = llm.status()
    return dict(status="ok", llm=dict(mode=st["mode"], reason=st["reason"], model=st["model"], note=st.get("model_note")),
                tenants=len(registry().tenants()))


@app.get("/v1/tenant")
def tenant_info(p: Principal = Depends(any_role)):
    return dict(p.ws.describe(), role=p.role, llm=llm.status())


@app.post("/v1/messages")
def post_message(body: MessageIn, p: Principal = Depends(any_role)):
    ws = p.ws
    if body.customer_id and ws.get("customers", body.customer_id) is None:
        raise HTTPException(404, f"Customer {body.customer_id} not found in {ws.adapter.name}")
    if body.conversation_id:
        conv = ws.get("conversations", body.conversation_id)
        if conv is None:
            raise HTTPException(404, "Conversation not found")
        if body.customer_id and conv.get("customer_id") and conv["customer_id"] != body.customer_id:
            audit.log(ws, "api.conversation_mismatch", p.actor, outcome="denied", conversation_id=body.conversation_id)
            raise HTTPException(403, "Conversation belongs to a different customer")
    try:
        r = A.handle_message(ws, body.text, body.conversation_id, body.customer_id, body.channel,
                             use_llm=body.use_llm, contact_email=body.contact_email)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if r.get("degraded"):
        return dict(reply=r["reply"], conversation_id=r["conversation_id"], run_id=r["run_id"], degraded=True,
                    decision="system_unavailable", llm=r["llm"])
    out = A.summarize_turn(r)
    out.update(run_id=r["run_id"], llm=r["llm"], approvals=[a["data"]["approval_id"] for a in r.get("actions", [])
                                                           if a["status"] == "pending_approval"])
    return out


@app.get("/v1/conversations/{conversation_id}")
def get_conversation(conversation_id: str, p: Principal = Depends(any_role)):
    conv = p.ws.get("conversations", conversation_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found")
    issue = conv.get("active_issue") or {}
    return dict(id=conv["id"], customer_id=conv.get("customer_id"), channel=conv.get("channel"), awaiting=conv.get("awaiting"),
                messages=[dict(role=m["role"], text=m["text"], ts=m["ts"]) for m in conv.get("messages", [])],
                active_issue=dict(intent=issue.get("intent"), status=issue.get("status"), ticket_id=issue.get("ticket_id")) if issue else None)


@app.get("/v1/runs")
def list_runs(limit: int = Query(50, ge=1, le=500), p: Principal = Depends(admin)):
    rows = sorted(p.ws.list("workflow_runs"), key=lambda r: r["id"], reverse=True)[:limit]
    return [{k: r.get(k) for k in ("id", "started_at", "ms", "status", "channel", "conversation_id", "customer_id", "intent",
                                   "intent_source", "decision", "ticket_id", "simulated")} | dict(llm_mode=(r.get("llm") or {}).get("mode"),
                                                                                  tool_calls=len(r.get("tool_calls", [])))
            for r in rows]


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, p: Principal = Depends(admin)):
    run = p.ws.get("workflow_runs", run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/v1/tickets")
def list_tickets(status: Optional[str] = None, limit: int = Query(100, ge=1, le=1000), p: Principal = Depends(admin)):
    rows = p.ws.list("tickets", status=status) if status else p.ws.list("tickets")
    rows.sort(key=lambda t: t["created_at"], reverse=True)
    return [{k: t.get(k) for k in ("id", "title", "status", "priority", "team", "assignee", "customer_id", "created_at",
                                   "escalated", "human_status", "approvals")} for t in rows[:limit]]


@app.get("/v1/tickets/{ticket_id}")
def get_ticket(ticket_id: str, p: Principal = Depends(admin)):
    t = p.ws.get("tickets", ticket_id)
    if t is None:
        raise HTTPException(404, "Ticket not found")
    return dict(t, markdown=A.render_ticket(t))


@app.post("/v1/tickets/{ticket_id}/transition")
def transition_ticket(ticket_id: str, body: TransitionIn, p: Principal = Depends(admin)):
    if p.ws.get("tickets", ticket_id) is None:
        raise HTTPException(404, "Ticket not found")
    try:
        t = A.transition(p.ws, ticket_id, body.status, actor=body.actor, note=body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    audit.log(p.ws, "ticket.transition", f"{body.actor} via {p.actor}", outcome=t["status"], ticket_id=ticket_id, note=body.note or None)
    return dict(id=t["id"], status=t["status"], events=t.get("events", [])[-3:])


@app.get("/v1/approvals")
def list_approvals(status: Optional[Literal["pending", "approved", "rejected", "approved_failed"]] = "pending",
                   p: Principal = Depends(admin)):
    rows = p.ws.list("approvals", status=status) if status else p.ws.list("approvals")
    return sorted(rows, key=lambda a: a["id"])


@app.post("/v1/approvals/{approval_id}/decision")
def decide_approval(approval_id: str, body: DecisionIn, p: Principal = Depends(admin)):
    try:
        return approvals.decide(p.ws, approval_id, body.approve, f"{body.approver} ({p.actor})", body.note)
    except KeyError:
        raise HTTPException(404, "Approval not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.get("/v1/audit")
def get_audit(limit: int = Query(200, ge=1, le=2000), event: Optional[str] = None, p: Principal = Depends(admin)):
    return audit.recent(p.ws, limit, event)


@app.get("/v1/actions")
def action_catalog(p: Principal = Depends(admin)):
    return A.action_catalog(p.ws)


# ---- customer intelligence (Phase 2) --------------------------------------------------------------------------
class RecommendationDecisionIn(BaseModel):
    approve: bool
    approver: str = Field(min_length=1, max_length=80)
    note: str = Field("", max_length=1000)


_analytics_lock = threading.Lock()


@app.post("/v1/analytics/run")
def analytics_run(seed: int = Query(7, ge=0, le=10**6), p: Principal = Depends(admin)):
    """Run the batch analytics pipeline for this tenant (reads support data only; one run at a time)."""
    from analytics import pipeline
    if not _analytics_lock.acquire(blocking=False):
        raise HTTPException(409, "An analytics run is already in progress")
    try:
        rep = pipeline.run(p.ws, seed=seed, actor=p.actor)
    finally:
        _analytics_lock.release()
    return {k: rep[k] for k in ("run_id", "generated_at", "dataset_hash", "synthetic", "counts", "seconds", "recommendations", "notes")}


def _report(p):
    from analytics import pipeline
    rep, _ = pipeline.load_report(p.ws)
    if rep is None:
        raise HTTPException(404, "No analytics run yet — POST /v1/analytics/run")
    return rep


@app.get("/v1/analytics/summary")
def analytics_summary(p: Principal = Depends(admin)):
    rep = _report(p)
    return {k: rep[k] for k in ("run_id", "generated_at", "synthetic", "notes", "counts", "quality", "descriptive", "validation")}


@app.get("/v1/analytics/diagnostics")
def analytics_diagnostics(p: Principal = Depends(admin)):
    return _report(p)["diagnostic"]


@app.get("/v1/analytics/predictions")
def analytics_predictions(p: Principal = Depends(admin)):
    from analytics import pipeline
    rep = _report(p)
    _, wh = pipeline.load_report(p.ws)
    scores = wh.read("live_scores")
    return dict(models=rep["predictive"], live_scores=scores.to_dict("records") if len(scores) else [])


@app.get("/v1/analytics/social")
def analytics_social(p: Principal = Depends(admin)):
    rep = _report(p)
    return dict(kpis=rep["descriptive"]["social"], sources=rep["sources"], **rep["social"])


@app.get("/v1/analytics/recommendations")
def analytics_recommendations(status: Optional[str] = None, p: Principal = Depends(admin)):
    from analytics import prescriptive
    return prescriptive.listing(p.ws, status)


@app.post("/v1/analytics/recommendations/{rec_id}/decision")
def analytics_decide(rec_id: str, body: RecommendationDecisionIn, p: Principal = Depends(admin)):
    from analytics import prescriptive
    try:
        return prescriptive.decide(p.ws, rec_id, body.approve, f"{body.approver} ({p.actor})", body.note, registry=registry())
    except KeyError:
        raise HTTPException(404, "Recommendation not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
