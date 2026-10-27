# SupportPilot — Agentic Customer Support System

> **Problem statement:** PS-04 · Customer Experience
> **Stack:** Python 3.10+ · Streamlit · FastAPI · MCP · SQLite · scikit-learn · optional LLM (Groq, OpenAI-compatible)

SupportPilot is a multi-agent customer support desk that runs itself. It **understands** the customer's issue,
**gathers their context**, **investigates** the way a support engineer would, **takes real actions** (refunds,
replacements, account unlocks, technician visits…), **resolves the issue on its own** or **escalates it with a complete
ticket**, **tracks it to closure** and **learns from customer satisfaction (CSAT) ratings**.

```
Customer issue → Understand intent → Gather context → Investigate → Take actions
             → Resolve automatically / Escalate if needed → Generate ticket → Track resolution → CSAT feedback
```

**Example** (demo customer `CUST1001`): *"My order was delivered but I received the wrong item"* → verify the shipment →
compare what was bought with the dispatch scan → check the return policy → check stock → **create a replacement** →
**generate a return label** → notify the customer → ticket `Resolved by AI` → ask for a CSAT rating. No human needed.

**Design principle:** every number, id and decision comes from deterministic tools over the data. The LLM (optional)
reads messages and writes replies, but it **never authorizes an action or invents a fact**. Without an LLM key the
whole system still works, on rules.

---

## Contents

1. [Quick start (5 minutes)](#1-quick-start-5-minutes)
2. [Configuration](#2-configuration)
3. [Running each component](#3-running-each-component)
4. [Reviewer's guide](#4-reviewers-guide)
5. [Architecture](#5-architecture)
6. [Guardrails and safety](#6-guardrails-and-safety)
7. [Phase 2 — Customer Intelligence](#7-phase-2--customer-intelligence)
8. [Data and storage](#8-data-and-storage)
9. [Testing and evaluation](#9-testing-and-evaluation)
10. [Deployment](#10-deployment)
11. [Project structure](#11-project-structure)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Quick start (5 minutes)

### Prerequisites

| Requirement | Notes |
|---|---|
| Python **3.10 or newer** (3.11 recommended) | `python3 --version` |
| `pip` and `venv` | ship with Python |
| Git | to clone the repository |
| *(optional)* Groq API key | free at <https://console.groq.com/keys>; enables the LLM |
| *(optional)* Telegram bot token, Gmail App Password | only for the real Telegram and email channels |

No database server, Docker or Node.js is needed. SQLite files are created automatically.

### Install and run

**macOS / Linux**
```bash
git clone <repository-url> SupportPilot
cd SupportPilot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # optional: add your GROQ_API_KEY here
streamlit run app.py
```

**Windows (PowerShell or cmd)**
```bat
git clone <repository-url> SupportPilot
cd SupportPilot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m streamlit run app.py
```

Open <http://localhost:8501>. On first start SupportPilot creates `data/supportpilot.db` and fills it with a synthetic
support world (customers, orders, payments, tickets…) dated relative to *now*.

### Verify the install
```bash
python -m pytest -q              # full test suite, offline, under a minute
python manage.py llm-check       # is the LLM reachable? (says "fallback" if no key is set)
```

### Try it
In the sidebar open **Demo scenarios**, pick **CUST1001 Aarav** and send *"I received the wrong item"*. Under the reply,
expand the agent trace to see every step: the records read, the help articles used, the action taken and the policy
decision.

---

## 2. Configuration

Everything is optional. With no configuration at all, SupportPilot runs the full demo in **fallback mode**
(deterministic rules, no LLM, no Telegram, no email).

### Where to put settings

Settings are read in this order; the first non-empty value wins. Placeholder values such as `your_key_here` count as
unset.

1. Environment variables
2. `.env` in the project root (copy `.env.example`)
3. `.streamlit/secrets.toml` (copy `.streamlit/secrets.toml.example`); this is also where Streamlit Cloud *Secrets* go

`.env` and `.streamlit/secrets.toml` are in `.gitignore`. **Never commit real keys.**

### All settings

| Setting | Default | Purpose |
|---|---|---|
| **LLM** | | |
| `GROQ_API_KEY` | — | Turns the LLM on. Without it the app runs in fallback mode. |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Chat model to use. |
| `GROQ_FALLBACK_MODELS` | `openai/gpt-oss-120b,openai/gpt-oss-20b` | Tried in order if `GROQ_MODEL` is retired or unavailable to your key. |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint works. |
| `SUPPORTPILOT_DISABLE_LLM` | — | `1` forces fallback mode (useful for tests and evaluation). |
| **Channels** | | |
| `TELEGRAM_BOT_TOKEN` | — | Real Telegram bot ([setup](#telegram-bot)). |
| `SMTP_USER` | — | Gmail address that sends confirmation emails ([setup](#email-agent)). |
| `SMTP_PASSWORD` | — | Google **App Password** (not your normal password). |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `465` | Another SMTP-over-SSL server. |
| **Security** | | |
| `SUPPORTPILOT_DASHBOARD_PASSWORD` | — | Requires a password for the Streamlit dashboard. **Set this whenever others can reach the dashboard**, because it can approve refunds. |
| `SUPPORTPILOT_API_RATE_LIMIT` | `120` | HTTP API requests per minute per API key. |
| **Storage** | | |
| `SUPPORTPILOT_DB` | `data/supportpilot.db` | Demo tenant database. Point the app, MCP server and Telegram bot at the same file to share data. |
| `SUPPORTPILOT_PLATFORM_DB` | `data/platform.db` | Tenant registry and hashed API keys. |
| `SUPPORTPILOT_ANALYTICS_DIR` | `data/analytics/` | Phase 2 analytics warehouses. |
| `SUPPORTPILOT_TENANT` | `demostore` | Tenant served by `mcp_server.py`. |

### LLM modes

The current mode is shown in the sidebar badge, in every workflow run record and at `GET /health`:

| Mode | Meaning |
|---|---|
| `llm` | The LLM classifies each message (merged with the rules) and writes the reply from verified facts. |
| `degraded` | A call failed, timed out or returned something invalid. That turn used the rules instead. |
| `fallback` | No key, or `SUPPORTPILOT_DISABLE_LLM=1`. Rules only. |

---

## 3. Running each component

All commands are run from the project root with the virtual environment active.

| Component | Command | Opens at |
|---|---|---|
| Dashboard (main UI) | `streamlit run app.py` | <http://localhost:8501> |
| HTTP API | `uvicorn api.server:app --port 8080` | <http://localhost:8080/docs> |
| MCP server (stdio) | `python mcp_server.py` | used by Claude Desktop / Claude Code |
| MCP server (HTTP) | `python mcp_server.py --http --port 8000` | <http://127.0.0.1:8000/mcp> |
| Telegram bot (standalone) | `python telegram_bot.py` | your bot in Telegram |
| Analytics batch job | `python manage.py analytics-run demostore` | results in the dashboard |
| Tests | `python -m pytest -q` | — |
| Evaluation | `python evaluation/run_eval.py` | prints metrics |

### Dashboard

`streamlit run app.py` opens seven pages:

| Page | What it's for |
|---|---|
| **Support desk** | Chat as a customer (web chat) or watch the live Telegram bot. Each reply shows where it came from: help articles, company records read, actions taken and the policy decision. |
| **Tickets & approvals** | Work tickets, approve or reject held actions, inspect every saved workflow run. |
| **Live operations** | Open workload, SLA risk, incidents detected now, CSAT by issue, escalation thresholds in use. |
| **Customer intelligence** | Phase 2 analytics and recommendations ([section 7](#7-phase-2--customer-intelligence)). |
| **Knowledge base** | Browse, search and edit the help centre, with version history per article. |
| **Data explorer** | Every stored record, by store and collection, searchable and downloadable as CSV. |
| **Settings** | Company integration, LLM and email status, action policy, API keys, audit log, **Reset demo data**. |

### HTTP API

The API lets an existing website, app or helpdesk send customer messages to SupportPilot.

```bash
# 1. Create an API key (printed once, stored hashed)
python manage.py create-key demostore --role admin --label "helpdesk"

# 2. Start the server
uvicorn api.server:app --port 8080

# 3. Send a message
export KEY=<the key printed in step 1>
curl -s localhost:8080/v1/messages \
     -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"customer_id": "CUST1001", "text": "I received the wrong item"}'
```

| Endpoint | Role | Purpose |
|---|---|---|
| `GET /health` | — | Liveness and LLM mode |
| `GET /v1/tenant` | channel, admin | The tenant this key belongs to |
| `POST /v1/messages` | channel, admin | Run one customer message through the whole workflow (pass `conversation_id` to continue a conversation) |
| `GET /v1/conversations/{id}` | channel, admin | Transcript and issue status |
| `GET /v1/runs`, `GET /v1/runs/{id}` | admin | Workflow runs: steps, tool calls, LLM calls, outcome |
| `GET /v1/tickets`, `GET /v1/tickets/{id}`, `POST /v1/tickets/{id}/transition` | admin | Human ticket work |
| `GET /v1/approvals`, `POST /v1/approvals/{id}/decision` | admin | Approve or reject held actions |
| `GET /v1/audit`, `GET /v1/actions` | admin | Audit log; action catalog with this tenant's policy |
| `POST /v1/analytics/run`, `GET /v1/analytics/{summary,diagnostics,predictions,social,recommendations}`, `POST /v1/analytics/recommendations/{id}/decision` | admin | Phase 2 analytics |

`channel` keys can only send messages and read conversations; `admin` keys can do everything. Your backend
authenticates its own customer and passes `customer_id`. SupportPilot checks that the customer exists in your system and
that the conversation belongs to that customer.

### Connecting a real business (multi-tenant)

Each business is a **tenant** with its own database (`data/tenants/<id>.db`). Its business data comes from a
**CompanyAdapter**:

- `DemoStoreAdapter` (`integrations/demostore.py`): the simulated, fictional ShopNova company used in the demo.
- `RestAdapter` (`integrations/rest.py`): the company's own REST API, implementing this contract:

```
GET  {base}/{entity}/{id}            entities: customers, orders, payments, refunds, accounts,
GET  {base}/{entity}?field=value               subscriptions, service_lines, …
POST {base}/actions/{action}         → {"data": …, "message": "…"}
```

```bash
export ACME_API_TOKEN=...
python manage.py create-tenant acme "Acme Corp" --rest-url https://api.acme.example/support \
       --token-env ACME_API_TOKEN --actions create_replacement,generate_return_label,initiate_refund
python manage.py create-key acme --role channel --label "website chat"
```

`tests/test_platform.py` runs a whole conversation through `RestAdapter` against a fake company API.

### Management CLI (`manage.py`)

| Command | Purpose |
|---|---|
| `tenants` | List tenants |
| `create-tenant <id> <name> [--rest-url URL --token-env VAR --actions a,b]` | Register a business (omit `--rest-url` for a simulated DemoStore) |
| `create-key <tenant> [--role channel\|admin] [--label TEXT]` | Create an API key |
| `keys [tenant]` / `revoke-key <key_id>` | List / revoke keys |
| `set-mode <tenant> <action> auto\|approval\|disabled` | Per-action policy, e.g. require human approval for replacements |
| `llm-check` | Test the LLM connection |
| `analytics-run [tenant] [--seed N] [--anchor ISO] [--mastodon instance:hashtag]` | Run the Phase 2 pipeline |
| `recommendations [tenant]` | List recommendations |
| `decide <tenant> <rec_id> approve\|reject --approver NAME [--note TEXT]` | Decide a recommendation |

### MCP server (Claude and other MCP clients)

`mcp_server.py` uses the official `mcp` Python SDK and shares the app's database, so a ticket created from Claude shows
up on the dashboard immediately.

```bash
# Claude Code
claude mcp add supportpilot -- /absolute/path/SupportPilot/.venv/bin/python /absolute/path/SupportPilot/mcp_server.py
```

Claude Desktop (`claude_desktop_config.json`):
```json
{"mcpServers": {"supportpilot": {"command": "/absolute/path/SupportPilot/.venv/bin/python",
                                 "args": ["/absolute/path/SupportPilot/mcp_server.py"]}}}
```

- **18 tools:** `support_handle_message` (full workflow, multi-turn via `conversation_id`), `support_channel_message`,
  `support_classify_intent`, `support_get_customer_context`, `support_search_knowledge`, `support_troubleshoot`,
  `support_execute_action`, `support_list_actions`, `support_get_ticket`, `support_list_tickets`,
  `support_update_ticket`, `support_record_csat`, `support_sla_scan`, `support_root_cause_incidents`,
  `support_dashboard_metrics`, `support_reset_demo_data`, `support_get_workflow_run`, `support_list_approvals`.
- **Resources:** `supportpilot://kb`, `supportpilot://intents`, `supportpilot://agents`. **Prompt:** `triage_customer_issue`.
- Sensitive actions requested through `support_execute_action` are held for human approval. The MCP server runs with
  operator trust, so keep `--http` bound to localhost (the default host is `127.0.0.1`).

### Telegram bot

Customers can talk to SupportPilot from the Telegram app; the dashboard shows the conversation and the agent flow live
on **Support desk → Telegram**.

1. In Telegram open **@BotFather** → `/newbot` → choose a name and a username ending in `bot` → copy the token.
2. Set `TELEGRAM_BOT_TOKEN` in `.env` (or Streamlit *Secrets*) and restart the app. The bot starts automatically on a
   background thread inside the Streamlit server, using long polling (no webhook or public URL needed).
3. Open the bot on your phone → **Start** → pick a demo customer (or continue as a guest) → describe the issue.

Commands: `/start`, `/login`, `/guest`, `/new`, `/help`, `/email you@example.com`.

A wrong-item complaint becomes a guided conversation (`telegram_wrong_item.py`): confirm the order (with the product
picture) → ask for a photo of what arrived (or a description, or *skip*) → run the full pipeline (replacement, free
return pickup, ticket) → offer a confirmation email → ask for a ⭐ rating. Changing the subject mid-way hands over to the
normal flow. Ticket updates a human makes on **Tickets & approvals** are pushed back to the customer's chat.

> Only **one** process may poll a bot token at a time. Stop the local app while a cloud deployment is running, or use a
> second bot for local testing. Telegram conversations are visible to anyone who can open the dashboard.

### Email agent

`agents/email_agent.py` sends real email through Gmail SMTP.

1. Turn on 2-Step Verification for the sending Google account.
2. Create an App Password: Google Account → Security → 2-Step Verification → **App passwords**.
3. Set `SMTP_USER` (the Gmail address) and `SMTP_PASSWORD` (the 16-character App Password) in `.env` or *Secrets*.

After a ticket is created, the Telegram bot asks once for an email address. The confirmation contains the reply, the
ticket facts and *Ordered* / *Received* product images. Later tickets in that chat, and status changes made by a human,
are emailed automatically. Mail only goes to an address the customer gave (never to the seeded demo addresses), with at
most 5 emails per address per hour. Every attempt is logged in the `emails` collection.

---

## 4. Reviewer's guide

### A 10-minute tour

1. `streamlit run app.py` → **Support desk**, then run these demo customers from the sidebar:

   | Customer | Say… | What to look for |
   |---|---|---|
   | CUST1001 Aarav (Premium) | "I received the wrong item" | Investigation → replacement + return label → auto-resolved |
   | CUST1002 Priya (Plus) | "My payment failed but money was deducted" | Asks for the Order ID, **remembers the issue** on the next message, refunds |
   | CUST1004 John Doe (Premium) | "I paid for Premium but it's not activated" | Sync retry fails → **Engineering** ticket with full context |
   | CUST1008 Karan | "There's a transaction I didn't make" | Mandatory escalation: security hold + **Trust & Safety** |
   | CUST1012 Dev (Premium) | "My ₹54,999 laptop arrived damaged" | Above the AI refund limit → **Fulfilment approval** escalation |

2. Expand the **agent trace** under any reply: each agent's input, output and timing, plus the policy decision.
3. **Tickets & approvals:** open the escalated tickets. A human can act on them without rereading the chat.
4. **Live operations:** the planted PayFast payment-gateway spike is detected as a likely root cause.
5. **Customer intelligence → Run analytics pipeline:** ranked recommendations with evidence; approve one.
6. **Settings → Audit log:** every authorization and its outcome.

All 12 demo customers:

| Customer | Scenario | Outcome |
|---|---|---|
| CUST1001 Aarav (Premium) | wrong item delivered | replacement + return pickup, auto-resolved |
| CUST1002 Priya (Plus) | payment failed, money deducted | asks for Order ID → auto-reversal refund |
| CUST1003 Rohan | account locked | unlock + reset link |
| CUST1004 John Doe (Premium) | subscription paid, not activated | sync retry fails → Engineering ticket |
| CUST1005 Sneha (Premium) | same, but fixable | sync retry succeeds |
| CUST1006 Vikram | internet down | area outage detected, linked service request |
| CUST1007 Ananya | slow / disconnecting | remote reset fails → technician visit (fallback) |
| CUST1008 Karan | unauthorized transaction | security hold + Trust & Safety escalation |
| CUST1009 Meera (Premium) | late order / cancellable order | expedite + coupon / cancel + refund |
| CUST1010 Arjun | charged twice | duplicate capture refunded |
| CUST1011 Fatima | refund status | on-track ETA reported |
| CUST1012 Dev (Premium) | ₹54,999 laptop damaged | QC pickup + Fulfilment approval escalation |

### Where to read the code

| Question | Start here |
|---|---|
| How is one message processed end to end? | `agents/orchestrator_agent.py` (`handle_message`) |
| How is an action authorized? | `agents/policy.py`, then `agents/action_agent.py` |
| How is the LLM kept honest? | `agents/llm.py`, and the reviewer guardrail in `agents/orchestrator_agent.py` |
| When does it escalate? | `agents/escalation_agent.py` |
| How does it plug into a real company? | `integrations/base.py`, `integrations/rest.py`, `agents/workspace.py`, `agents/tenancy.py` |
| HTTP API and auth | `api/server.py` |
| Phase 2 analytics | `analytics/pipeline.py` and the stage modules beside it |
| A longer narrative walkthrough | `context.md` |

### Honest limitations

- **ShopNova is fictional.** Customers, orders and the 180-day analytics history are synthetic and seeded. Real public
  posts can be pulled from Mastodon to show the social connector working, but they are not about ShopNova.
- The evaluation set was written alongside the rules, so its scores are **regression checks**, not generalization
  accuracy.
- The Phase 2 model scores show that the method works on synthetic data. They say nothing about real-world accuracy until
  the models are retrained on a company's own history.

---

## 5. Architecture

```
 Your website / app / helpdesk ──HTTP──▶ api/server.py (FastAPI, per-tenant API keys)
 Telegram · Streamlit · MCP ─────────────▶ agents.handle_message  ── one persisted workflow run per message
                                              │  understand (rules + LLM) → context → KB → troubleshoot →
                                              │  escalate? → [customer confirms] → policy.authorize → act →
                                              │  ticket (+ approvals) → track → LLM reply (fact-checked) → reviewer
                                              ▼
                     Workspace (tenant) ── support data: conversations, tickets, workflow_runs, actions,
                       │                    approvals, audit  → the tenant's own SQLite file
                       └── CompanyAdapter ── business data (read-only) + authorized operations
                             ├── DemoStoreAdapter  simulated ShopNova systems, synthetic data
                             └── RestAdapter       the company's own REST API
```

The **orchestrator** follows a Planner → Executor → Reviewer pattern. The full per-message pipeline is:
omnichannel adapter → understanding → memory → customer context → knowledge retrieval → troubleshooting → escalation
(pre-action) → action execution → escalation (post-action) → ticket → resolution tracking → sentiment-aware response →
reviewer guardrail → CSAT.

### The agents (`agents/`)

| # | Agent | Module | What it does |
|---|---|---|---|
| 1 | Conversation Understanding | `intent_agent.py` | 23 intents with explainable confidence; category, priority (raised for urgency, anger, high amounts), sentiment and emotion; entities (order/ticket/transaction/service ids, email, phone, ₹ amount, plan) |
| 2 | Customer Context | `context_agent.py` | Identifies the customer from the session, email, phone, order id or ticket id; loads account, tier, purchases, previous tickets, subscription, transactions, login state, broadband lines, flags (Premium, repeat contact, at-risk) |
| 3 | Knowledge Base | `knowledge_agent.py` + `docs/help-center/` | BM25 search over 45 help articles and internal playbooks, boosted by intent and category and weighted by reliability; picks the most reliable workflow |
| 4 | Troubleshooting | `troubleshooting_agent.py` | 20 diagnostic workflows over live data (dispatch scans, gateway status, bank debit, lock counters, entitlement sync, outage map, optical signal…) → evidence, diagnosis, confidence, recommended actions |
| 5 | Action Execution | `action_agent.py` | 18 actions across e-commerce (cancel, return label, refund, replacement, expedite, coupon, reconcile), SaaS (reset password, unlock, sync retry, reactivate, upgrade, cancel subscription), telecom (service request, technician visit, remote reset), security hold and notify; idempotent, with fallback chains |
| 6 | Multi-turn Memory | `memory_agent.py` | Keeps the active issue and its missing details: *"Can you share your Order ID?"* → *"12346"* fills it; lifecycle new → investigating → awaiting customer → resolved/escalated → closed |
| 7 | Escalation Decision | `escalation_agent.py` | Always escalates fraud, data breach and legal issues; also on request for a human, unresolvable investigation, failed or blocked action, confidence < 70% (threshold learned per intent from CSAT), angry repeat contact; reports every rule's outcome |
| 8 | Ticket Generation | `ticket_agent.py` | Ticket with customer context, investigation, actions taken, escalation reasons, team to review, recommended next step, SLA and conversation summary |
| 9 | Resolution Tracking | `tracking_agent.py` | Opened → Assigned → Pending Customer → Resolved → Closed (+ reopen); least-loaded assignment; customer notified on every change; SLA monitor with auto-escalation |
| 10 | Customer Satisfaction | `satisfaction_agent.py` | CSAT 1–5 (chat or ⭐ buttons), comment sentiment, resolution-quality score; **feeds back** into per-intent escalation thresholds |
| 11 | Master Orchestrator | `orchestrator_agent.py` | Planner → executor agents → reviewer guardrail, with a timed trace |

Supporting modules: `channel_agent.py` (Website chat, WhatsApp, Email, Telegram and Mobile App payloads),
`insights_agent.py` (24-hour vs 14-day spike detection → *"Possible PayFast payment gateway outage"*), `policy.py`,
`approvals.py`, `audit.py`, `runlog.py`, `llm.py`, `email_agent.py`, `media.py`, `workspace.py`, `tenancy.py`, `store.py`.

Other features: sentiment-aware replies (empathy first, Premium acknowledgement, stricter threshold for angry
customers); automatic knowledge-base growth (a ticket resolved with a new resolution note becomes a `KB-AUTO-###`
article that search uses immediately); SLA monitoring (Premium high priority: 15-minute response, at risk at 75% of the
deadline).

---

## 6. Guardrails and safety

**The LLM never authorizes anything.**

- **Read-only business data.** Agents cannot write to company data (`Workspace` raises on writes); every read is
  logged as a tool call.
- **Policy check on every action** (`agents/policy.py`): allow-list → supported by the adapter → enabled for the tenant
  → customer identified → customer owns every referenced order, payment, subscription or line → within the
  **₹10,000 AI refund limit** → tenant mode is not `approval` → not a sensitive action requested directly by an external
  AI client (MCP).
- **Human approval queue.** Held actions wait on **Tickets & approvals** (or the API). Approving runs the action as
  the human, resolves the ticket and notifies the customer; rejecting changes nothing.
- **Customer confirmation.** If only the LLM understood the issue (the rules disagreed or found nothing), sensitive
  actions (refund, cancel, replacement, return, subscription changes) wait for the customer's explicit *yes*.
- **Validated LLM output.** Intents must be from the closed list; order ids are accepted only if they literally appear
  in the message; a reply is rejected if it drops, changes or adds any id, amount, date or number (one retry, then the
  deterministic draft is sent).
- **Reviewer guardrail.** Blocks claims about actions that weren't executed, refunds over the limit, incomplete
  escalation tickets, missed mandatory escalations and exposed personal data.
- **Input limits.** 4,000-character messages, control characters stripped, id patterns validated, per-key rate limit.
- **Audit log** (*Settings → Audit log*, `GET /v1/audit`): every authorization and its outcome, approvals, human ticket
  updates, API auth failures, key changes, demo resets.
- **Fails safe.** If the company system is unreachable, the customer is told so and nothing changes (run status
  `degraded`).
- **Secrets.** API keys are shown once and stored hashed. `.env` and `secrets.toml` are git-ignored.

---

## 7. Phase 2 — Customer Intelligence

Support activity and social media feedback become ranked, evidence-backed business recommendations. Analytics run as a
**separate batch job** per tenant: they read support data only and write to a rebuildable warehouse
(`data/analytics/<tenant>.db`). The only write-back is a *proposed* recommendation that a human approves. Live
conversations never wait on analytics.

```
tenant Workspace (read-only) ─┐                      ┌─ descriptive  trends, complaints, sentiment, resolution, CSAT
synthetic history (labelled) ─┼─▶ clean & normalise ─┼─ diagnostic   event impact · operational correlation · anomalies
social sources ───────────────┘   (quality report)   ├─ predictive   7-day forecast · escalation risk · low-CSAT risk
                                        │            ├─ social       topics · sentiment · emerging issues · lead/lag
                                 analytics warehouse └─ prescriptive ranked recommendations ─▶ human approves ─▶ task /
                                                                                              escalation-threshold change
```

**Run it**
```bash
# In the dashboard: Customer intelligence → Run analytics pipeline. Or headless and reproducible:
python manage.py analytics-run demostore --anchor 2026-10-27T12:00:00+00:00 [--mastodon mastodon.social:customerservice]
python manage.py recommendations demostore
python manage.py decide demostore REC-XXXXXXXX approve --approver "Kavya Menon"
```

**Data.** The 180-day history, social posts, event log and gateway statistics are synthetic (`analytics/synthetic.py`,
seeded), and every record carries `source="synthetic"`. Live SupportPilot tickets are ingested as
`source="supportpilot"`. Real Mastodon posts (`source="mastodon"`, author handles hashed) need no credentials. The
synthetic data deliberately contains duplicates, stale re-exports, missing values, inconsistent spellings, mixed
timestamp formats and invalid ratings; the *Data quality* tab counts every fix. The same inputs, seed and anchor
produce an identical dataset hash.

**Diagnostics** test every event × issue pair with a weekday-adjusted Poisson test, Holm-corrected across all 160
pairs, plus attribute concentration, onset timing and overlapping events. Results are labelled *association, not proof
of causation*. Scored against the generator's hidden true effects: **precision 1.0, recall 0.71** (5 of 7 effects ≥ ×2
found). A control event with no effect is never flagged. PayFast failure rate vs payment complaints: r = 0.67,
block-permutation p = 0.002.

**Predictive models**, evaluated on later data they never saw (seed 7, anchor 2026-10-27, 9,308 clean tickets):

| Model | Protocol | Baselines | Held-out result |
|---|---|---|---|
| Daily volume, 7 days ahead | rolling origin, 16 weekly folds (choose on older 8, report on newer 8) | seasonal naive MAE 8.54 · 7-day mean 9.02 | ridge on lags **MAE 7.82** (MAPE 13.4%), +8.4% skill; 80% interval |
| Escalation risk at creation | time split 70/15/15, creation-time features only | prevalence AUC 0.50 · per-intent 0.68 | logistic regression **AUC 0.78**, PR-AUC 0.75, Brier 0.189 |
| Low CSAT (≤ 2) at resolution | same | prevalence 0.50 · per-intent 0.65 | gradient boosting **AUC 0.91**, PR-AUC 0.66, 4.5× lift in top decile |

The interpretable logistic model is kept unless another model beats it by ≥ 0.005 validation AUC. A test trains on
shuffled labels and checks that the system then reports **no** skill. Live tickets are scored as **advisory** risk
lists only.

**Recommendations** are ranked by impact × confidence ÷ effort, a prioritisation heuristic rather than a savings
forecast, and each comes with evidence and a caveat. Approving one creates an internal task or sets an escalation
threshold override that live support uses immediately and that can be reverted. Every decision is audited.

---

## 8. Data and storage

The demo data is synthetic and reproducible (`data/generate_data.py`, seed 42): 42 customers, ~90 orders, payments,
refunds, login accounts, subscriptions, NovaFiber broadband lines, an active area outage, inventory, ~250 historical
tickets over 21 days (with a planted PayFast payment-failure spike) and ~150 CSAT responses. The fictional company
**ShopNova** runs an online store, the **Nova Plus / Premium** subscription and **NovaFiber** broadband, which covers the
e-commerce, SaaS and telecom action families.

| Store | File | Holds |
|---|---|---|
| Support database (per tenant) | `data/supportpilot.db` (demo) · `data/tenants/<id>.db` | conversations, tickets, workflow runs, actions, approvals, audit, knowledge base, recommendations. Table `docs(collection, id, data JSON, updated)` |
| Company system | via its adapter; the simulated DemoStore keeps its data in the demo database as separate collections | customers, orders, payments, subscriptions… (read-only to agents) |
| Analytics warehouse (per tenant) | `data/analytics/<id>.db` | cleaned and derived SQL tables, rebuilt on every run |
| Platform registry | `data/platform.db` | tenants, API key hashes, platform audit |

All of them can be browsed in **Data explorer**, or from a terminal:
```bash
sqlite3 data/supportpilot.db "SELECT id, json_extract(data,'$.status') FROM docs WHERE collection='tickets'"
```

**Knowledge base.** Help articles are Markdown files in `docs/help-center/<section>/` (7 customer sections plus internal
`agent-playbooks/`, never shown to customers). They are imported into the database on first use; dashboard edits are
versioned in `kb_history`. `tests/test_knowledge.py` fails if an article's policy numbers (return windows, refund limit,
coupons, plan prices, response times…) disagree with the code.

**Reset.** **Settings → Reset demo data** re-seeds the demo. To start completely fresh, stop the app and delete
`data/*.db`, `data/tenants/` and `data/analytics/`.

---

## 9. Testing and evaluation

```bash
python -m pytest -q                         # whole suite; offline, no API keys needed
python -m pytest tests/test_agents.py -q    # one area
python evaluation/run_eval.py               # metrics report
```

| Test file | Covers |
|---|---|
| `tests/test_agents.py` | Each agent and the end-to-end demo scenarios |
| `tests/test_platform.py` | Tenancy, API auth and roles, policy, approvals, `RestAdapter` against a fake company API |
| `tests/test_analytics.py` | Phase 2 pipeline, reproducibility, shuffled-label sanity check |
| `tests/test_knowledge.py` | Help articles agree with the policy numbers in code |
| `tests/test_telegram.py` | Telegram flows, including the guided wrong-item conversation |
| `tests/test_email.py` | Email agent safety rules and rate limits |

`evaluation/run_eval.py` uses `evaluation/eval_set.json` (labelled messages and end-to-end scenarios) to report intent
accuracy, auto-resolve vs escalate accuracy, automation rate, reviewer approval, action outcomes, latency, guardrail
probes and desk metrics.

---

## 10. Deployment

### Streamlit Community Cloud
1. Push the repository to GitHub.
2. On <https://share.streamlit.io>: **New app** → main file `app.py` → *Advanced settings* → Python 3.11.
3. Paste your settings into *Secrets* in TOML format (see `.streamlit/secrets.toml.example`). At minimum set
   `SUPPORTPILOT_DASHBOARD_PASSWORD`; add `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `SMTP_USER`, `SMTP_PASSWORD` as needed.

Streamlit Cloud puts idle apps to sleep, and the Telegram bot doesn't answer until someone opens the site again. Its
file system is not permanent, so the SQLite data resets when the app restarts.

### GitHub Codespaces / Dev Containers
`.devcontainer/devcontainer.json` uses Python 3.11, installs `requirements.txt` and starts the dashboard on port 8501
automatically.

### HTTP API in production
Run behind HTTPS (a reverse proxy), e.g. `uvicorn api.server:app --host 0.0.0.0 --port 8080`. The rate limit is kept
in process memory, so with several workers each one counts separately. Use `channel` keys for customer-facing
integrations and keep `admin` keys on the server side. Keys can also be sent as an `X-API-Key` header.

---

## 11. Project structure

```
SupportPilot/
├── app.py                    Streamlit entry point (navigation, login, sidebar)
├── ui/                       dashboard pages: desk, operations, live_ops, intelligence, knowledge, data_explorer, settings
├── agent_flow.py             live agent-flow board (replays each turn stage by stage)
├── agents/                   one module per agent + policy, approvals, audit, LLM, tenancy, workspace, SQLite store
├── integrations/             CompanyAdapter interface, DemoStoreAdapter (simulated), RestAdapter (real company API)
├── api/server.py             FastAPI HTTP API with per-tenant API keys
├── mcp_server.py             MCP server (stdio or streamable HTTP)
├── telegram_bot.py           real Telegram channel (long polling)
├── telegram_wrong_item.py    guided wrong-item conversation for Telegram
├── analytics/                Phase 2: ingest, descriptive, diagnostic, predictive, social, prescriptive, warehouse
├── manage.py                 admin CLI: tenants, keys, policy modes, LLM check, analytics
├── data/generate_data.py     synthetic support world (databases are created here at runtime)
├── docs/help-center/         knowledge base: Markdown help articles + internal agent playbooks
├── assets/products/          product images used in Telegram and emails
├── evaluation/               labelled eval set + runner
├── tests/                    pytest suite
├── context.md                long-form walkthrough for demos and viva
├── requirements.txt
├── .env.example              template for local settings
└── .streamlit/               theme config + secrets template
```

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError` | Activate the virtual environment (`source .venv/bin/activate`) and run `pip install -r requirements.txt`. |
| `streamlit: command not found` (often on Windows) | Use `python -m streamlit run app.py`. |
| Sidebar says **fallback** although a key is set | Run `python manage.py llm-check`. Make sure the key isn't the placeholder `your_key_here` and that `.env` is in the project root. If the model is retired, set `GROQ_MODEL` or `GROQ_FALLBACK_MODELS`. |
| Telegram bot doesn't answer | Only one process may poll a token: stop other copies (local and cloud). Check `TELEGRAM_BOT_TOKEN`, restart the app. |
| Telegram `Conflict` / `409` errors | Another instance is polling the same token. Stop it or use a second bot. |
| Emails not sent | `SMTP_PASSWORD` must be a Google **App Password** (2-Step Verification on). Check the `emails` collection in **Data explorer** for the logged reason. |
| API returns `401` / `403` | Send `Authorization: Bearer <key>`; admin endpoints need a key created with `--role admin`. |
| API returns `429` | Rate limit reached; raise `SUPPORTPILOT_API_RATE_LIMIT`. |
| Demo data looks stale or broken | **Settings → Reset demo data**, or stop the app and delete `data/*.db`. |
| MCP client sees different data from the dashboard | Point both at the same database with `SUPPORTPILOT_DB=/absolute/path/supportpilot.db`. |
| Port already in use | `streamlit run app.py --server.port 8502` or `uvicorn api.server:app --port 8081`. |
