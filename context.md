# SupportPilot Demonstration Context

A complete explanation of SupportPilot for the demonstration, viva or judging session. It assumes the reader has
not seen the code.

---

## 1. Project identity

**Project:** SupportPilot
**Problem statement:** PS-04 — Agentic Customer Support System
**Domain:** Customer Experience
**Interface:** Streamlit (UI) + MCP server (tools for Claude / any MCP client)
**Architecture:** Deterministic multi-agent pipeline (11 agents + 3 advanced modules) with a Planner → Executor →
Reviewer orchestrator, lightweight RAG, guarded action execution, SQLite state, optional LLM polish.

A traditional chatbot answers *"Please contact support."* SupportPilot behaves like a support engineer:

```text
Customer issue
      ↓  Understand intent (intent, category, priority, sentiment)
      ↓  Gather context (account, orders, tickets, subscription, transactions)
      ↓  Investigate (diagnostic workflow over real data)
      ↓  Take actions (refund, replacement, unlock, sync retry, technician…)
      ↓  Resolve automatically — or escalate with full context
      ↓  Generate a structured ticket
      ↓  Track resolution, notify customer, monitor SLA
      ↓  Collect CSAT → adjust future escalation decisions
```

The fictional company **ShopNova** runs an e-commerce store, the **Nova Plus / Premium** subscription (SaaS) and the
**NovaFiber** broadband service (telecom), so all three action families in the problem statement are real.

---

## 2. The problem

Support teams spend most of their time on repetitive, data-lookup issues (where is my order, refund status, locked
account, failed payment). Rule-based chatbots deflect them without solving them; generic LLM chatbots answer
fluently but cannot verify or act, and sometimes invent facts. The goal of PS-04 is to **reduce human workload while
keeping resolution quality high**: resolve what can be resolved safely, and hand everything else to humans in a
form they can act on immediately.

---

## 3. Walk-through of one message

Message from Priya (CUST1002, Plus): *"My payment failed but money was deducted."*

1. **Omnichannel adapter** — normalizes the web/WhatsApp/email/Telegram/app payload to text + sender identifiers.
2. **Conversation Understanding** — `{"intent": "payment_failure", "category": "Billing", "priority": "high",
   "sentiment": "negative"}`, emotion *frustrated*, confidence 95%.
3. **Memory** — opens a new issue; required slot `order_id` is missing.
4. **Customer Context** — "Plus User · Tickets: 0 · Last purchase: Today".
5. **Knowledge** — KB-003 *Payment failed but money was deducted*, KB-004 workflow (reliability 94%).
6. **Troubleshooting** — cannot run without the order → **clarify**: *"Could you share your Order ID?"*
7. Customer replies *"ORD12346"* → Memory fills the slot (mode `slot_fill`) — the issue is not forgotten.
8. **Troubleshooting** — locate payment TXN… → gateway status FAILED (PG-504 timeout) → bank debit confirmed ₹2,499 →
   gateway health: *Possible PayFast outage, 16 failures in 24h (32× normal)* → recommend auto-reversal refund.
9. **Escalation (pre-action)** — combined confidence 93% ≥ 70% → auto-resolve.
10. **Action Execution** — `initiate_refund` (within the ₹10,000 AI limit, idempotent, audited).
11. **Ticket** — TKT-… *Payment failed but amount deducted — ORD12346*, Resolved by SupportPilot AI.
12. **Tracking** — confirmation notification on the customer's channel; lifecycle → resolved; CSAT requested.
13. **Reviewer** — reply claims only successful actions, refund within limit, no PII → approved.

The *Agent trace* tab shows each of these steps with timings and the escalation rules that were evaluated.

---

## 4. The agents

| # | Agent | Key design decision |
|---|---|---|
| 1 | Conversation Understanding | Phrase hits weigh 3×, keywords 1×; confidence = 1 − e^(−score/3), penalized when a second intent competes. Keyword-only questions ("what are the delivery charges?") go to the FAQ path instead of a workflow. Priority bumps for urgency words, anger, high amounts. |
| 2 | Customer Context | Identity from session, email, phone, order id or ticket id — a guest can be identified mid-conversation. Orders of other customers are never disclosed. |
| 3 | Knowledge Retrieval | BM25 + intent/category boost × article reliability, so the most *reliable* workflow wins, not just the most similar text. |
| 4 | Troubleshooting | One workflow per intent, each a sequence of evidence checks with pass/fail/warn status, a diagnosis, a confidence and recommended actions. Orders are inferred when exactly one order fits (e.g. the only recent delivery). |
| 5 | Action Execution | Allow-list, ownership check, AI refund limit, 30-day idempotency for money/order actions, fallback chains (remote reset → technician visit), full audit log. |
| 6 | Memory | Active issue + slots + lifecycle; bare "12346" fills an Order ID; a different strong intent opens a new issue and archives the old one. |
| 7 | Escalation | Combined confidence = 0.35 × intent + 0.65 × investigation (halved if an action failed). Threshold 70%, learned per intent from CSAT, +10 pts for angry customers. Mandatory escalation for fraud, breach, legal. |
| 8 | Ticket Generation | Investigation, actions taken, escalation reasons, "Needs X Review", recommended next step, SLA, summary. |
| 9 | Resolution Tracking | Validated lifecycle transitions, least-loaded assignment, notification on every change, SLA monitor with one-time auto-escalation. |
| 10 | Customer Satisfaction | CSAT + comment sentiment + resolution quality (CSAT 50%, first-contact resolution 20%, SLA 20%, no reopen 10%). |
| 11 | Master Orchestrator | Planner announces the plan, executors run, a Reviewer guardrail can veto the reply and fall back to a safe hand-off. |

Advanced: **Root Cause Discovery** (`insights_agent`), **Omnichannel adapter** (`channel_agent`), **Sentiment-aware
response composer** (`response_agent`).

---

## 5. Escalation — genuine decision-making

Rules evaluated on every turn (all shown in the trace):

1. Mandatory escalation (fraud / data breach / legal)
2. Customer asked for a human
3. Required details missing → *clarify* (escalate after two failed attempts)
4. Investigation not auto-resolvable (high-value claim, overdue refund, security hold, scan mismatch not confirmed)
5. An action failed or was blocked by a guardrail
6. Confidence below threshold
7. Angry customer with repeat contacts

**Example — John Doe (CUST1004):** payment successful, entitlement sync failed → the agent first *tries*
`retry_subscription_sync`; it fails with ENT-503 → post-action decision escalates to Engineering with ticket:

```
Issue: Premium subscription not activated
Investigation: Payment successful · Premium status pending activation · Subscription sync failed
Actions Taken: Retry Subscription Sync — failed (ENT-503)
Status: Needs Engineering Review · Priority: HIGH · Respond by: +15 min (Premium SLA)
```

**Feedback loop:** AI-resolved refund requests historically scored 2.9/5 CSAT, so the learned threshold for
`refund_request` is 90% — refunds now go to Payments for confirmation, while intents with ≥ 4.5 CSAT get a lower bar.

---

## 6. Safety and guardrails

- The LLM is optional and never decides: it may only pick an intent from the closed list when rules are unsure, and
  rephrase replies — rejected unless every id, amount and date is preserved.
- AI refunds are capped at ₹10,000; larger refunds are blocked and escalated.
- Actions verify that the order / subscription / line belongs to the customer; other customers' data is never shown.
- Fraud: protective security hold *before* escalation; customers are told never to share OTP/PIN.
- Data breach: sessions revoked, Security Incident Response with DPO notification guidance (DPDP Act 2023).
- Legal: acknowledged and routed; the assistant makes no commitments.
- Reviewer guardrail checks every turn before the reply is sent.

---

## 7. Demo script (≈ 6 minutes)

1. **Wrong item** (Aarav) — show the five troubleshooting steps and two actions; rate 5 ⭐ → ticket closes.
2. **Payment failed** (Priya) — multi-turn: Order ID question → "ORD12346" → refund. Point out the PayFast incident in the evidence.
3. **Premium not activated** (John Doe) — attempt, failure, escalation; open the ticket in the *Tickets* tab.
4. **Angry customer** (Meera) — empathy-first reply, CRITICAL priority, human hand-off.
5. **Fraud** (Karan) — protective hold + Trust & Safety.
6. **Supervisor dashboard** — root cause card, SLA monitor → *Run SLA monitor & auto-escalate*, learned thresholds.
7. **Tickets** — resolve John Doe's ticket with a resolution note → new KB article appears in *Knowledge base*.
8. **Omnichannel** — send the email payload; show the email-formatted reply.
9. **MCP** — in Claude: "Use SupportPilot to handle: my internet is down (customer CUST1006)". The ticket appears in the dashboard.

---

## 8. Results

Run `python evaluation/run_eval.py`. On the bundled set: 40/40 intents, 17/17 auto-resolve vs escalate
decisions, ~71% of scenarios resolved without a human, 100% reviewer approval, ~5 ms per turn (deterministic core),
refund-limit and mandatory-escalation probes pass. Honest caveat: the labeled set was written with the rules, so it
is a regression benchmark; unseen real messages are needed to measure generalization.

---

## 9. Lab experiment mapping

E1 structured I/O, trace logs, reviewer · E2 rule-based decisions (escalation, SLA) · E3 tool agents with fallback
(LLM optional) · E4 evidence-backed answers (troubleshooting steps, KB citations) · E5 decompose → investigate → act
pipeline · E6 tool-style agent functions exposed over MCP · E7 scoring (confidence, CSAT, resolution quality) ·
E8 escalation & SLA alerting · E9 multi-agent delegation via orchestrator · E10 full Planner-Executor-Reviewer system
with observability + Streamlit.

---

## 10. Likely questions

- **Is this just a RAG chatbot?** No. Retrieval is one of eleven agents; the system investigates real records,
  executes state-changing actions and decides when not to act.
- **What if the AI is wrong?** Confidence thresholds, mandatory escalation categories, refund limits, ownership
  checks, idempotency and a reviewer guardrail bound the damage; every action is audited and reversible by a human.
- **How does it learn?** CSAT on AI-resolved tickets adjusts per-intent thresholds; human resolutions become new KB
  articles when they contain new knowledge.
- **How would it scale to production?** Replace the SQLite store and synthetic world with real order / payment /
  identity / OSS APIs behind the same action functions; the MCP server already exposes the system to other agents.
