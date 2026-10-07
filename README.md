# SupportPilot – Agentic Customer Support System (PS-04, Customer Experience)

A self-managing, multi-agent customer support desk that **understands** the issue, **gathers customer context**,
**investigates** like a support engineer, **takes real actions**, **resolves automatically**, **escalates intelligently
with a complete ticket**, **tracks the resolution** and **learns from CSAT**.

- **MCP-native:** every agent is a real MCP tool. The orchestrator is an MCP client that calls them over JSON-RPC, and
  the same tools are published to Claude Desktop.
- **Real channels:** customers write from **Telegram** on their phone (text and photos) and get replies with product
  images; the **Email agent** sends real emails over Gmail SMTP.
- **Live website:** a single Streamlit page replays every turn as a live agent flow (phone · agent network · MCP call log),
  with a calm view of the data store underneath.

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
pip install pytest && python -m pytest -q
```
Windows: `python -m venv .venv`, `.venv\Scripts\activate.bat`, then the same commands (`python -m streamlit run app.py`).

The database (`data/supportpilot.db`, SQLite) is created and seeded automatically on first run with a synthetic
support world anchored to *now*; delete the file to re-seed. Python ≥ 3.10.

Optional LLM: set `GROQ_API_KEY` (and `GROQ_MODEL`) in `.env` or `.streamlit/secrets.toml` (see the `.example`
files). The LLM only breaks ties on ambiguous intents (choosing from the closed intent list) and polishes reply
wording — the polished reply is rejected unless every id, amount and date survives. **Everything works without a key.**

## Deploy on Streamlit Community Cloud
1. Push this folder to a new GitHub repo.
2. New app → main file `app.py` → Advanced settings → Python 3.11+.
3. Add under *Secrets* (all optional):
   ```toml
   TELEGRAM_BOT_TOKEN = "123456:ABC..."     # from @BotFather
   SMTP_USER = "you@gmail.com"              # Gmail that sends the emails
   SMTP_PASSWORD = "abcd efgh ijkl mnop"    # Google App Password (needs 2-step verification)
   GROQ_API_KEY = "gsk_..."                 # optional LLM polish
   ```

## The website
One page, no sidebar:
1. **Status strip:** Telegram bot, Email agent, MCP (number of agent tools, transport), LLM.
2. **Live agent flow:** the newest turn from any channel replays automatically, within ~2 s of arriving.
   - **Phone:** the customer's message (and photo), then the reply with product images, ⭐ buttons and an email toast.
   - **Agent network:** the orchestrator (MCP client) sends each `tools/call` as a packet to one of 15 agents; the node
     works, the result returns, and the agent says in plain English what it did.
   - **MCP call log:** every call with its timing and the JSON arguments/result that crossed the wire.
   - **Footer:** the outcome, plus exactly what was saved to the data store (ticket, conversation, actions, email…).
3. **No Telegram? Try it here:** the only input on the site (customer, message, optional photo and email).
4. **Inside the data store:**
   - **Overview:** KPIs, root-cause alerts and the volume chart.
   - **Tickets:** opening a ticket shows photos and product images, plus a human-agent update form whose updates reach
     the customer on Telegram and by email.
   - Customers & orders, the product catalog, the email log with rendered previews, and the live MCP tool catalog.

## Email agent
`agents/email_agent.py` sends real email with Gmail SMTP (`SMTP_USER` + a Google **App Password** in `SMTP_PASSWORD`).
- **What it sends:** ticket confirmations (status badge, the reply, Ordered/Received product images inline, ticket facts)
  and status updates when a human agent changes the ticket on the website.
- **Who gets it:** only an address the customer gave in this conversation. The bot asks after a ticket is created, or the
  customer sends `/email you@x.com`. Seeded demo addresses are never emailed.
- **Limits and logging:** at most 5 emails per address per hour. Every attempt (sent / failed / not configured /
  rate-limited / blocked) is logged and shown on the website.

## Product images
`assets/products/*.png` are flat illustrations of the 14 catalog items, drawn as SVG by
`scripts/render_product_images.py` and rendered once to PNG. The **Media agent** (`agents/media.py`) picks the right
ones per turn, e.g. *Ordered* vs *Received* for a wrong item. They are sent as a Telegram album, embedded in emails and
shown on the website. Customer photos sent from Telegram (or uploaded on the website) are stored as evidence, add a
"Photo evidence" step to troubleshooting, and are attached to the ticket.

## Real Telegram bot
Customers can talk to SupportPilot from the Telegram app on their phone; the website replays every turn live.
1. In Telegram open **@BotFather** → `/newbot` → pick a name and a username ending in `bot` → copy the token.
2. Set `TELEGRAM_BOT_TOKEN` in Streamlit *Secrets* (or `.env` locally) and restart the app.
3. Open the bot on your phone → **Start** → pick a demo customer (or continue as guest) → describe the issue.

How it works: `telegram_bot.py` long-polls the Bot API on a background thread inside the Streamlit server
(no webhook or public URL needed), runs each message through the same orchestrator with `channel="telegram"`,
replies in the chat (product images, ⭐ buttons for CSAT), accepts **photos** as evidence, offers an **email copy** of
each ticket and stores every turn for the website. Ticket updates a human agent makes on the website are pushed back to
the customer's chat and email. Commands: `/start`, `/login`, `/guest`, `/email you@x.com`, `/new`, `/help`.
Run it on its own with `python telegram_bot.py` (share the database via `SUPPORTPILOT_DB`).

Notes: only one copy of the bot may poll a token at a time — stop the local app while the cloud app is running, or
use a second bot for local testing. Streamlit Cloud puts idle apps to sleep; while it sleeps the bot does not answer
until someone opens the website. Telegram messages are visible to anyone who can open the site.

## How each PS-04 component is implemented (`agents/`)
| # | Agent (PS) | Module | What it really does |
|---|---|---|---|
| 1 | Conversation Understanding | `intent_agent.py` | 23 intents, phrase/keyword scoring with explainable confidence, category, priority (bumped for urgency/anger/high amounts), sentiment + emotion, entities (order/ticket/txn/service ids, email, phone, ₹ amount, plan) |
| 2 | Customer Context | `context_agent.py` | Resolves the customer from session, email, phone, order id or ticket id; account, tier, purchases, previous tickets, subscription, transactions, auth state, telecom lines, flags (Premium, repeat contact, at-risk) |
| 3 | Knowledge Base | `knowledge_agent.py` + `docs/support_kb.json` | BM25 over FAQs, policies, product docs and troubleshooting guides; intent/category boosts × reliability; picks the **most reliable workflow** |
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
| Omnichannel (Website chat, WhatsApp, Email, Telegram, Mobile App) | `channel_agent.py` normalizes native payloads and formats replies per channel; *Omnichannel* tab |
| Sentiment-aware conversations | empathy-first replies, Premium acknowledgement, stricter threshold (+10 pts) and priority bump for angry customers |
| Root cause discovery | `insights_agent.py`: 24h vs 14-day baseline spike detection with drill-down → *"Possible PayFast payment gateway outage"* (planted in the seed data) and live outage map; troubleshooting cites active incidents |
| Auto knowledge-base expansion | resolving a ticket with a novel resolution note writes a new FAQ (`KB-AUTO-###`) that retrieval uses immediately |
| SLA monitoring | Premium high priority: 15-min response; at-risk at 75% of the deadline; auto-escalation to the Escalation Desk |
| Supervisor dashboard | open tickets, resolution / AI-resolved / escalation rates, avg resolution time (AI vs human), CSAT, SLA risk, volume by category, CSAT by issue, channel mix, learned thresholds, notifications |

## Demo personas (Telegram `/login`, or *Try it here* on the website)
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
**Inside the app:** `agents/tools.py` registers every agent as a JSON-in/JSON-out tool. `agents/mcp_bus.py` builds an
`MCPServer` from them and opens an in-process `mcp.Client` session on a background event loop. The orchestrator
(`handle_message`) makes every agent step a real `tools/call`, and the trace records tool, transport, timing,
arguments and result.
- **State:** the conversation and the active issue are passed in and returned explicitly, because MCP arguments are copies.
- **Fallback:** `SUPPORTPILOT_MCP=0` (or an MCP failure) falls back to direct calls, marked `transport: direct`.
- **Tests:** they check that both paths give the same outcomes.

**For Claude:** `mcp_server.py` uses the official `mcp` Python SDK (v2 `MCPServer`, falls back to v1 `FastMCP`) and
shares the app's database, so tickets created from Claude appear on the website immediately. It publishes **33 tools**:

**Agent tools (17)**, the same ones the orchestrator calls: `normalize_channel`, `understand_message`,
`track_conversation`, `customer_context`, `search_knowledge`, `troubleshoot`, `decide_escalation`, `execute_actions`,
`create_ticket`, `track_resolution`, `ticket_status`, `select_media`, `send_email`, `compose_reply`, `polish_reply`,
`review_reply`, `record_csat`.

**Desk tools (16):** `support_handle_message` (full workflow, multi-turn via `conversation_id`), `support_channel_message`,
`support_classify_intent`, `support_get_customer_context`, `support_search_knowledge`, `support_troubleshoot`,
`support_execute_action`, `support_list_actions`, `support_get_ticket`, `support_list_tickets`,
`support_update_ticket` (with KB expansion), `support_record_csat`, `support_sla_scan`,
`support_root_cause_incidents`, `support_dashboard_metrics`, `support_reset_demo_data`.
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
Ticket → Resolution tracking → Media → Response (sentiment-aware) → Email → CSAT → Reviewer guardrail →
Telegram / website / MCP. Every arrow is an MCP `tools/call` from the orchestrator (MCP client) to an agent tool.
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
app.py                  Streamlit website (status, live agent flow, try-it box, data store views)
ui/flow_component.py    the live agent-flow component (phone · agent network · MCP call log)
telegram_bot.py         real Telegram channel (long polling): text + photos in, replies + product images out, email capture
mcp_server.py           MCP server for Claude (stdio / streamable HTTP): agent tools + desk tools
agents/                 one module per agent + shared_agent.py (taxonomy, SLA, secrets) + store.py (SQLite)
agents/tools.py         MCP tool registry (every agent as a tool)
agents/mcp_bus.py       the orchestrator's in-process MCP client/server
agents/email_agent.py   Gmail SMTP email agent;  agents/media.py: product images + customer photos
assets/products/        product illustrations (SVG + PNG);  scripts/render_product_images.py draws them
data/generate_data.py   synthetic support world
docs/support_kb.json    knowledge base (FAQs, policies, docs, troubleshooting workflows)
evaluation/             eval set + runner
tests/                  pytest suite
context.md              demo / viva explanation
```
