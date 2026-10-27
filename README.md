# SupportPilot – Agentic Customer Support System (PS-04, Customer Experience)

A self-managing, multi-agent customer support desk that **understands** the issue, **gathers customer context**,
**investigates** like a support engineer, **takes real actions**, **resolves automatically**, **escalates intelligently
with a complete ticket**, **tracks the resolution** and **learns from CSAT**. UI: Streamlit. Tools: MCP server.

```
Customer issue → Understand intent → Gather context → Investigate → Take actions
             → Resolve automatically / Escalate if needed → Generate ticket → Track resolution → CSAT feedback
```

Example (PS scenario, persona `CUST1001`): *"My order was delivered but I received the wrong item"* →
verify shipment → compare purchased vs dispatch scan → check return policy → check stock → **create replacement** →
**generate return label** → notify customer → ticket `Resolved by AI` → CSAT. No human intervention.

## Run (macOS / Linux)
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
python evaluation/run_eval.py        # metrics for the Results section
python -m pytest -q                 # all tests, no network needed
uvicorn api.server:app --port 8080   # HTTP API (see Phase 1 platform)
```
Windows: `python -m venv .venv`, `.venv\Scripts\activate.bat`, then the same commands (`python -m streamlit run app.py`).

The database (`data/supportpilot.db`, SQLite) is created and seeded automatically on first run with a synthetic
support world anchored to *now*. Use **Reset demo data** in the sidebar to re-seed. Python ≥ 3.10.

LLM: set `GROQ_API_KEY` (and optionally `GROQ_MODEL`) in `.env` or `.streamlit/secrets.toml`. The LLM reads each
message (structured intent / emotion / order id, merged with the rules) and writes the reply from verified facts.
Without a key — or when a call fails, times out or returns something invalid — SupportPilot runs in **fallback
mode** on deterministic rules, and says so (sidebar badge, each turn's run record, `/health`). See *Phase 1 platform*.

## Phase 1 platform — integrate SupportPilot with an existing business

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
                             ├── DemoStoreAdapter  SIMULATED ShopNova systems, synthetic data (integrations/demostore.py)
                             └── RestAdapter       the company's own REST API (integrations/rest.py)
```

**Run the API**
```
pip install -r requirements.txt
python manage.py create-key demostore --role admin --label "helpdesk"     # shown once; stored hashed
uvicorn api.server:app --port 8080                                         # docs: http://localhost:8080/docs
curl -s localhost:8080/v1/messages -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"customer_id": "CUST1001", "text": "I received the wrong item"}'
```
| Endpoint | Role | Purpose |
|---|---|---|
| `GET /health` | — | liveness + LLM mode (llm / degraded / fallback) |
| `POST /v1/messages` | channel, admin | one customer message through the whole workflow (`conversation_id` continues it) |
| `GET /v1/conversations/{id}` | channel, admin | transcript and issue status |
| `GET /v1/runs`, `/v1/runs/{id}` | admin | workflow runs: steps, tool calls, LLM calls, outcome |
| `GET /v1/tickets`, `/v1/tickets/{id}`, `POST …/transition` | admin | human-agent ticket work |
| `GET /v1/approvals`, `POST /v1/approvals/{id}/decision` | admin | approve / reject held actions |
| `GET /v1/audit`, `GET /v1/actions` | admin | audit log, action catalog with this tenant's policy |

Your backend authenticates its customer and passes `customer_id`; SupportPilot checks the customer exists in your
system and that a conversation belongs to that customer.

**Connect your own systems** — expose (or wrap) this contract and register a tenant:
`GET {base}/{entity}/{id}`, `GET {base}/{entity}?field=value`, `POST {base}/actions/{action}` → `{"data", "message"}`
(entities: customers, orders, payments, refunds, accounts, subscriptions, service_lines, …).
```
python manage.py create-tenant acme "Acme Corp" --rest-url https://api.acme.example/support \
       --token-env ACME_API_TOKEN --actions create_replacement,generate_return_label,initiate_refund
```
Each tenant gets its own database file (`data/tenants/<id>.db`); the registry and hashed keys live in
`data/platform.db` (`SUPPORTPILOT_PLATFORM_DB`). `tests/test_platform.py` runs a whole conversation through
`RestAdapter` against an HTTP-shaped fake company API.

**Guardrails — the LLM never authorizes anything**
- Business data is read-only for agents (`Workspace` raises on writes); every read is logged as a tool call.
- `agents/policy.py` decides every action: allow-list → supported by the adapter → tenant switch → identified
  customer → ownership of every referenced order/payment/subscription/line → AI refund limit → tenant
  `approval` mode → sensitive actions requested directly by an external AI client (MCP, actor `AI:mcp`).
- Held actions go to the **Approvals** queue (*Tickets & approvals* page or API). Approving runs them as the human, resolves the
  ticket and notifies the customer; rejecting changes nothing. Change modes:
  `python manage.py set-mode demostore create_replacement approval` (`auto` / `approval` / `disabled`).
- If an issue was understood **only** by the LLM (the rules disagreed or saw nothing), sensitive actions
  (refund, cancel, replacement, return, subscription changes) wait for the customer's explicit *yes*.
- LLM output is validated: closed intent list, order ids only if literally in the message, replies rejected if
  they drop, change or add any id, amount, date or number (one retry, then the deterministic draft is sent).
- Inputs: 4,000-char cap, control characters stripped, id patterns and enums validated by the API; per-key rate limit
  (`SUPPORTPILOT_API_RATE_LIMIT`, default 120/min). Dashboard login: set `SUPPORTPILOT_DASHBOARD_PASSWORD`.
- Audit log (`audit` collection, *Settings → Audit log*, `/v1/audit`): every authorization and its outcome,
  approvals, human ticket updates, API auth failures, key changes, demo resets.
- If the company system is unreachable the customer is told so and nothing is changed (run status `degraded`).

**Configuration**
| Setting | Default | |
|---|---|---|
| `GROQ_API_KEY` | — | enables the LLM; without it: fallback mode |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | `GROQ_FALLBACK_MODELS` is tried if it is retired/unavailable |
| `GROQ_BASE_URL` | Groq | any OpenAI-compatible endpoint |
| `SUPPORTPILOT_DISABLE_LLM` | — | `1` forces fallback mode (tests, evaluation) |
| `SUPPORTPILOT_DB` / `SUPPORTPILOT_PLATFORM_DB` | `data/supportpilot.db` / `data/platform.db` | demo tenant store / registry |
| `SUPPORTPILOT_TENANT` | `demostore` | tenant served by `mcp_server.py` |
| `SUPPORTPILOT_DASHBOARD_PASSWORD` | — | require a password for the Streamlit dashboard |
| `SUPPORTPILOT_API_RATE_LIMIT` | 120 | requests per minute per API key |

Check the LLM connection with `python manage.py llm-check`.

## Dashboard, knowledge base and where data lives

`streamlit run app.py` opens seven pages:

| Page | What it's for |
|---|---|
| **Support desk** | Chat as a customer (web chat) or watch the live Telegram bot. Each reply shows *where it came from*: help articles, company records read, actions and the policy decision. |
| **Tickets & approvals** | Work tickets, approve or reject held actions, inspect every saved workflow run. |
| **Live operations** | Open workload, SLA risk, incidents detected now, CSAT by issue, escalation thresholds in use. |
| **Customer intelligence** | Phase 2 analytics and recommendations (see below). |
| **Knowledge base** | Browse, search and edit the help centre; version history per article. |
| **Data explorer** | Every stored record, by store and collection, searchable and downloadable as CSV. |
| **Settings** | Company integration, LLM/email status, action policy, API keys, audit log, demo reset. |

**Knowledge base.** The help centre is written as Markdown articles in `docs/help-center/<section>/` (7 customer
sections, plus internal agent playbooks that are never shown to customers). On first use they are imported into the
support database (`kb_articles`). The dashboard edits that copy, and every edit is versioned in `kb_history`.
`tests/test_knowledge.py` fails if an article's policy numbers (return windows, refund limit and timelines, claim
limits, coupons, lock threshold, plan prices, response times) disagree with the code.

**Where data is stored**

| Store | File | Holds |
|---|---|---|
| SupportPilot database (per business) | `data/supportpilot.db` (demo) · `data/tenants/<id>.db` | conversations, tickets, workflow runs, actions, approvals, audit, knowledge base, recommendations. SQLite table `docs(collection, id, data JSON, updated)` |
| Company system | via its adapter (REST API); the simulated DemoStore keeps its synthetic data in the demo database as separate collections | customers, orders, payments, subscriptions, … (read-only to agents) |
| Analytics warehouse (per business) | `data/analytics/<id>.db` | cleaned and derived tables (normal SQL tables), rebuilt by each analytics run |
| Platform registry | `data/platform.db` | tenants, API keys (hashes only), platform audit |

All four can be browsed in the **Data explorer**, or from a terminal, e.g.
`sqlite3 data/supportpilot.db "SELECT id, json_extract(data,'$.status') FROM docs WHERE collection='tickets'"`.

## Phase 2 — Customer Intelligence Platform

Support activity and social media feedback become ranked, evidence-backed business recommendations. Analytics run
as a **separate batch job** per tenant: they read support data only, write to a per-tenant warehouse
(`data/analytics/<tenant>.db`, rebuildable), and the only write-back is *proposed* recommendations that a human
approves. Live conversations never wait on analytics.

```
tenant Workspace (read-only) ─┐                      ┌─ descriptive  trends, complaints, sentiment, resolution, CSAT
synthetic history (labelled) ─┼─▶ clean & normalise ─┼─ diagnostic   event impact · operational correlation · anomalies
social sources ───────────────┘   (quality report)   ├─ predictive   7-day forecast · escalation risk · low-CSAT risk
                                        │            ├─ social       topics · sentiment · emerging issues · lead/lag
                                 analytics warehouse └─ prescriptive ranked recommendations ─▶ human approves ─▶ task /
                                                                                              escalation-threshold change
                                                                                              (read by live support)
```

**Run the demonstration**
```
pip install -r requirements.txt
streamlit run app.py        # page "Customer intelligence" → Run analytics pipeline
# or headless, reproducibly:
python manage.py analytics-run demostore --anchor 2026-10-27T12:00:00+00:00 [--mastodon mastodon.social:customerservice]
python manage.py recommendations demostore
python manage.py decide demostore REC-XXXXXXXX approve --approver "Kavya Menon"
# API (admin key): POST /v1/analytics/run · GET /v1/analytics/{summary,diagnostics,predictions,social,recommendations}
#                  POST /v1/analytics/recommendations/{id}/decision
```

**Data — what is real and what is not.** ShopNova is fictional, so its 180-day helpdesk history, social posts,
event log and gateway statistics are **synthetic** (`analytics/synthetic.py`, seeded). Every record carries
`source="synthetic"` and the dashboard says so. Live SupportPilot tickets, workflow runs and actions are ingested as
`source="supportpilot"`; seeded demo tickets are excluded so nothing is counted twice. **Real** public posts can be
added from any Mastodon hashtag (public API, no credentials, `source="mastodon"`, author handles hashed). They are
real but not about ShopNova, so they show the connector working. The synthetic data deliberately contains
duplicates, stale re-exports, missing values, inconsistent spellings, mixed timestamp formats and invalid ratings;
the *Data quality* tab counts every fix. Same inputs + seed + anchor → identical dataset hash.

**Diagnostics** grade every event × issue association. The tests are a weekday-adjusted Poisson test, Holm-corrected
across all pairs (160 here), plus attribute concentration (e.g. 85% of payment complaints on PayFast), onset timing
and overlapping events. Results are labelled *association, not proof of causation*. Because the generator knows the
true effects (never an input to the analysis), the diagnostics are scored against it:
**precision 1.0, recall 0.71** (5/7 effects ≥ ×2 found). The two misses are a one-day partial window and a
low-volume intent. A control event with no effect (homepage redesign) is never flagged. Gateway failure rate vs
payment complaints: PayFast r = 0.67, block-permutation p = 0.002; other gateways: none.

**Predictive models**: evaluated on later data they never saw (seed 7, anchor 2026-10-27, 9,308 clean tickets)

| Model | Protocol | Baselines | Result on held-out data |
|---|---|---|---|
| Daily volume, 7 days ahead | rolling origin, 16 weekly folds; method chosen on older 8, reported on newer 8 | seasonal naive MAE 8.54, 7-day mean 9.02 | ridge on lags: **MAE 7.82** (MAPE 13.4%), **+8.4% skill** vs seasonal naive; 80% interval from backtest residuals |
| Escalation risk at creation | time split 70/15/15, features known at creation only | prevalence AUC 0.50, per-intent rate AUC 0.68 | logistic regression **AUC 0.78**, PR-AUC 0.75, Brier 0.189; gain +0.10 (95% CI 0.07–0.13) |
| Low-CSAT (≤ 2) at resolution | same; re-opening excluded (can follow the rating) | prevalence 0.50, per-intent 0.65 | gradient boosting **AUC 0.91**, PR-AUC 0.66, lift 4.5× in top decile; gain +0.26 (CI 0.20–0.32) |

The interpretable logistic model is kept unless another beats it by ≥ 0.005 validation AUC. A test trains on shuffled
labels and checks the system then reports **no** skill. These scores show the method works on synthetic
data; they say nothing about real-world accuracy until the models are retrained on the company's own history.
Live tickets are scored as **advisory** risk lists only.

**Recommendations** are ranked by impact × confidence ÷ effort (a prioritisation heuristic, not a savings
forecast). Each one comes with evidence and a caveat. Types: root causes behind recent spikes, emerging social
issues, intents whose AI resolutions customers rate worse (→ raise that intent's escalation threshold), forecast
workload vs capacity (assumption: 25 escalated tickets per agent per day), SLA breaches, proactive follow-up for
live customers at low-CSAT risk, and unexplained anomalies. Approving creates an internal task or sets an escalation
threshold override in the tenant policy, which live support uses immediately and which can be reverted. Every
decision is audited. A re-run refreshes open recommendations and never resurrects decided ones.

## Deploy on Streamlit Community Cloud
1. Push this folder to a new GitHub repo.
2. New app → main file `app.py` → Advanced settings → Python 3.11+.
3. (Optional) add `GROQ_API_KEY` and `TELEGRAM_BOT_TOKEN` under *Secrets*.

## Real Telegram bot
Customers can talk to SupportPilot from the Telegram app on their phone; the website shows the conversation and
the agents' flow live on **Support desk → Telegram**.
1. In Telegram open **@BotFather** → `/newbot` → pick a name and a username ending in `bot` → copy the token.
2. Set `TELEGRAM_BOT_TOKEN` in Streamlit *Secrets* (or `.env` locally) and restart the app.
3. Open the bot on your phone → **Start** → pick a demo customer (or continue as guest) → describe the issue.

How it works: `telegram_bot.py` long-polls the Bot API on a background thread inside the Streamlit server
(no webhook or public URL needed), runs each message through the same orchestrator with `channel="telegram"`,
replies in the chat (⭐ buttons for CSAT) and stores every turn for the website. Ticket updates a human agent makes
on the **Tickets & approvals** page are pushed back to the customer's chat. Commands: `/start`, `/login`, `/guest`, `/new`, `/help`.
Run it on its own with `python telegram_bot.py` (share the database via `SUPPORTPILOT_DB`).

Notes: only one copy of the bot may poll a token at a time — stop the local app while the cloud app is running, or
use a second bot for local testing. Streamlit Cloud puts idle apps to sleep; while it sleeps the bot does not answer
until someone opens the website. Telegram messages are visible to anyone who can open the site.

### Guided wrong-item conversation (Telegram)
A wrong-item complaint is handled as a real conversation (`telegram_wrong_item.py`), not a one-shot answer:
1. **Find the order.** The agent finds the customer's recently delivered order and shows the product picture:
   *"Is this the order?"* (Guests are first asked for an order ID, phone or email.)
2. **Get a photo.** It asks for a photo of the item that arrived, with a short description. No photo? Describing it or
   typing *skip* works.
3. **Resolve.** The full agent pipeline runs with the photo as evidence: dispatch-scan check, replacement, free return
   pickup and a ticket ID.
4. **Email.** It offers a confirmation email, or sends it immediately if the address is known.
5. **Rate.** Only then does it ask for a ⭐ rating.

Asking for a mail in your own words ("send me a confirmation mail") works at any point. Changing the subject mid-way
hands over to the normal flow.

## Email agent (real email)
`agents/email_agent.py` sends real email through Gmail SMTP.
- **Setup:** set `SMTP_USER` (the Gmail address that sends) and `SMTP_PASSWORD` (a Google **App Password**: Google Account
  → Security → 2-Step Verification → App passwords) in Streamlit *Secrets* or `.env`.
- **When it sends:** after a ticket is created, the Telegram bot asks once for an email address (or the customer sends
  `/email you@x.com`). The ticket confirmation goes out with the reply, the ticket facts and *Ordered* / *Received*
  product images (`assets/products/`). Later tickets in that chat are emailed automatically, and status changes a human
  agent makes on the **Tickets & approvals** page are emailed as updates.
- **Safety:** mail goes only to an address the customer gave, never to the seeded demo addresses. At most 5 emails per
  address per hour. Every attempt (sent / failed / not configured / rate-limited / blocked) is logged in the `emails`
  collection, and the **Email agent** step appears in the agent flow.

## How each PS-04 component is implemented (`agents/`)
| # | Agent (PS) | Module | What it really does |
|---|---|---|---|
| 1 | Conversation Understanding | `intent_agent.py` | 23 intents, phrase/keyword scoring with explainable confidence, category, priority (bumped for urgency/anger/high amounts), sentiment + emotion, entities (order/ticket/txn/service ids, email, phone, ₹ amount, plan) |
| 2 | Customer Context | `context_agent.py` | Resolves the customer from session, email, phone, order id or ticket id; account, tier, purchases, previous tickets, subscription, transactions, auth state, telecom lines, flags (Premium, repeat contact, at-risk) |
| 3 | Knowledge Base | `knowledge_agent.py` + `docs/help-center/` | BM25 over the help centre (45 articles) and internal playbooks; intent/category boosts × reliability; picks the **most reliable workflow** |
| 4 | Troubleshooting | `troubleshooting_agent.py` | 20 diagnostic workflows over live data (dispatch scans, gateway status, bank debit, lock counters, entitlement sync, outage map, optical signal…) → evidence steps, diagnosis, confidence, recommended actions |
| 5 | Action Execution | `action_agent.py` | 18 actions (E-commerce: cancel, return label, refund, replacement, expedite, coupon, reconcile · SaaS: reset password, unlock, sync retry, reactivate, upgrade, cancel subscription · Telecom: service request, technician visit, remote reset · Security hold · Notify). Guardrails: allow-list, ownership check, ₹10,000 AI refund limit, idempotency, fallback chains, audit log |
| 6 | Multi-Turn Memory | `memory_agent.py` | Active issue with slots; "Can you share Order ID?" → "ORD12346" (or just "12346") fills the slot; issue lifecycle new → investigating → awaiting customer → resolved/escalated → closed |
| 7 | Escalation Decision | `escalation_agent.py` | Always escalate fraud / data breach / legal; escalate on human request, non-resolvable investigation, failed/blocked action, **confidence < 70%** (threshold learned per intent from CSAT), angry repeat contact; every rule's outcome is reported |
| 8 | Ticket Generation | `ticket_agent.py` | Ticket with customer context, investigation, actions taken, escalation reasons, "Needs X Review", recommended next step, SLA, conversation summary — humans don't reread the chat |
| 9 | Resolution Tracking | `tracking_agent.py` | Opened → Assigned → Pending Customer → Resolved → Closed (+ reopen), least-loaded assignment, automatic customer notifications on every transition, SLA monitor with auto-escalation |
| 10 | Customer Satisfaction | `satisfaction_agent.py` | CSAT 1–5 (chat or ⭐ widget), comment sentiment, resolution-quality score; **feeds back** into per-intent escalation thresholds |
| 11 | Master Orchestrator | `orchestrator_agent.py` | Planner → executor agents → Reviewer guardrail (no unexecuted claims, refund limit, complete escalation ticket, mandatory escalations, PII) with a timed trace |

### Advanced features
| Feature | Where |
|---|---|
| Omnichannel (Website chat, WhatsApp, Email, Telegram, Mobile App) | `channel_agent.py` normalizes native payloads and formats replies per channel |
| Sentiment-aware conversations | empathy-first replies, Premium acknowledgement, stricter threshold (+10 pts) and priority bump for angry customers |
| Root cause discovery | `insights_agent.py`: 24h vs 14-day baseline spike detection with drill-down → *"Possible PayFast payment gateway outage"* (planted in the seed data) and live outage map; troubleshooting cites active incidents |
| Auto knowledge-base expansion | resolving a ticket with a novel resolution note writes a new FAQ (`KB-AUTO-###`) that retrieval uses immediately |
| SLA monitoring | Premium high priority: 15-min response; at-risk at 75% of the deadline; auto-escalation to the Escalation Desk |
| Supervisor dashboard | open tickets, resolution / AI-resolved / escalation rates, avg resolution time (AI vs human), CSAT, SLA risk, volume by category, CSAT by issue, channel mix, learned thresholds, notifications |

## Demo personas (sidebar → *Demo scenarios*)
| Customer | Scenario | Outcome |
|---|---|---|
| CUST1001 Aarav (Premium) | wrong item delivered | replacement + return pickup, auto-resolved |
| CUST1002 Priya (Plus) | payment failed, money deducted | asks Order ID → auto-reversal refund |
| CUST1003 Rohan | account locked | unlock + reset link |
| CUST1004 John Doe (Premium) | subscription paid, not activated | sync retry fails → **Engineering ticket** |
| CUST1005 Sneha (Premium) | same, but fixable | sync retry succeeds |
| CUST1006 Vikram | internet down | area outage detected, linked service request |
| CUST1007 Ananya | slow / disconnecting | remote reset fails → technician visit (fallback) |
| CUST1008 Karan | unauthorized transaction | security hold + **Trust & Safety** escalation |
| CUST1009 Meera (Premium) | late order / cancellable order | expedite + coupon / cancel + refund |
| CUST1010 Arjun | charged twice | duplicate capture refunded |
| CUST1011 Fatima | refund status | on-track ETA reported |
| CUST1012 Dev (Premium) | ₹54,999 laptop damaged | QC pickup + **Fulfilment approval** escalation |

## MCP integration
`mcp_server.py` uses the official `mcp` Python SDK (v2 `MCPServer`, falls back to v1 `FastMCP`) and shares the app's
database, so tickets created from Claude appear on the dashboard immediately.

**Tools (18):** `support_handle_message` (full workflow, multi-turn via `conversation_id`), `support_channel_message`,
`support_classify_intent`, `support_get_customer_context`, `support_search_knowledge`, `support_troubleshoot`,
`support_execute_action`, `support_list_actions`, `support_get_ticket`, `support_list_tickets`,
`support_update_ticket` (with KB expansion), `support_record_csat`, `support_sla_scan`,
`support_root_cause_incidents`, `support_dashboard_metrics`, `support_reset_demo_data`, `support_get_workflow_run`,
`support_list_approvals` (read-only). Sensitive actions requested through `support_execute_action` are held for human
approval. The MCP server runs with operator trust — keep `--http` bound to localhost.
**Resources:** `supportpilot://kb`, `supportpilot://intents`, `supportpilot://agents`. **Prompt:** `triage_customer_issue`.

```
# Claude Code
claude mcp add supportpilot -- /absolute/path/SupportPilot/.venv/bin/python /absolute/path/SupportPilot/mcp_server.py

# Streamable HTTP (remote clients) → http://127.0.0.1:8000/mcp
python mcp_server.py --http --port 8000
```
Claude Desktop (`claude_desktop_config.json`):
```json
{"mcpServers": {"supportpilot": {"command": "/absolute/path/SupportPilot/.venv/bin/python",
                                 "args": ["/absolute/path/SupportPilot/mcp_server.py"]}}}
```

## Architecture
Inbound (any channel) → Orchestrator plan → Omnichannel adapter → Understanding → Memory → Customer context →
Knowledge retrieval → Troubleshooting → Escalation (pre-action) → Action execution → Escalation (post-action) →
Ticket → Resolution tracking → Response (sentiment-aware) → CSAT → Reviewer guardrail → Streamlit / MCP.
All numbers, ids and decisions come from deterministic tools over the data — the optional LLM never decides or invents facts.

## Data
Synthetic and reproducible (`data/generate_data.py`, seed 42): 42 customers, ~90 orders, payments, refunds, auth
accounts, subscriptions, NovaFiber lines, an active area outage, inventory, ~250 historical tickets over 21 days with a
planted PayFast payment-failure spike, and ~150 CSAT responses. `python data/generate_data.py` writes a JSON snapshot.

## Evaluation (`python evaluation/run_eval.py`)
Labeled utterances and end-to-end scenarios in `evaluation/eval_set.json`: intent accuracy, auto-resolve vs escalate
accuracy, automation rate, reviewer approval, action outcomes, latency, guardrail probes and desk metrics. The labeled
set was written alongside the rules, so treat its scores as regression checks, not as generalization accuracy —
add real, unseen customer messages for an honest benchmark.

## Project structure
```
telegram_bot.py         real Telegram channel (long polling) feeding Support desk → Telegram
agent_flow.py           live agent-flow board (replays each turn stage by stage)
app.py                  Streamlit UI (chat, Telegram live, agent trace, tickets workspace, supervisor dashboard, KB, omnichannel, architecture)
mcp_server.py           MCP server (stdio / streamable HTTP)
agents/                 one module per agent + shared_agent.py (taxonomy, SLA, LLM) + store.py (SQLite)
data/generate_data.py   synthetic support world
docs/help-center/       knowledge base: 45 Markdown help articles + agent playbooks
evaluation/             eval set + runner
tests/                  pytest suite
context.md              demo / viva explanation
```
