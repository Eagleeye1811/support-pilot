"""Live agent flow — the website's centrepiece.

Replays one orchestrated turn in three synchronized panes:
  · a phone showing the real conversation (customer photo in, product images + email out),
  · the agent network: the orchestrator (an MCP client) sends each `tools/call` as a packet
    to an agent node, which works and sends the result back,
  · the MCP call log with timings and the JSON that actually crossed the wire.
The agents finish in milliseconds; the replay runs at a readable pace and says so.
"""
from __future__ import annotations

import json

import streamlit as st

from agents.email_agent import mask
from agents.media import attachment_data_uri, product_data_uri
from agents.shared_agent import CHANNELS, INTENTS, inr

# node key → (name, icon); order = position around the orbit (clockwise from the top)
NODES = [
    ("channel", "Channel", "📥"), ("understand", "Understanding", "🧠"), ("memory", "Memory", "🗂️"),
    ("context", "Customer", "👤"), ("knowledge", "Knowledge", "📚"), ("troubleshoot", "Troubleshoot", "🔧"),
    ("escalation", "Escalation", "⚖️"), ("actions", "Actions", "⚡"), ("ticket", "Ticket", "🎫"),
    ("tracking", "Tracking", "📍"), ("media", "Media", "🖼️"), ("composer", "Composer", "✍️"),
    ("email", "Email", "📧"), ("satisfaction", "CSAT", "⭐"), ("reviewer", "Reviewer", "🛡️"),
]
TOOL_NODE = {
    "normalize_channel": "channel", "understand_message": "understand", "track_conversation": "memory",
    "customer_context": "context", "search_knowledge": "knowledge", "troubleshoot": "troubleshoot",
    "decide_escalation": "escalation", "execute_actions": "actions", "create_ticket": "ticket",
    "track_resolution": "tracking", "ticket_status": "tracking", "select_media": "media", "send_email": "email",
    "compose_reply": "composer", "polish_reply": "composer", "review_reply": "reviewer", "record_csat": "satisfaction",
}
SLOT_NAMES = {"order_id": "order", "transaction_id": "transaction", "ticket_id": "ticket", "service_id": "service line"}
OUTCOME = {
    "auto_resolve": ("Resolved automatically", "good"), "escalate": ("Handed to a human specialist", "warn"),
    "clarify": ("Asked the customer for details", "info"), "csat": ("Feedback recorded", "good"),
    "email": ("Email Agent ran", "info"),
}


def _label(intent):
    return INTENTS.get(intent, {}).get("label", (intent or "").replace("_", " "))


def _decision_sentence(d):
    if not isinstance(d, dict):
        return "Checked whether a human is needed."
    conf = f"confidence {d['confidence']:.0%} vs {d['threshold']:.0%} needed"
    if d["decision"] == "auto_resolve":
        return f"Safe to fix automatically — {conf}."
    if d["decision"] == "clarify":
        return f"Not enough details yet — will ask the customer ({conf})."
    why = d["reasons"][0] if d.get("reasons") else "rules require a human"
    return f"A human should handle this: {why}."


def explain(step, r, seen):
    """Plain-English sentence for one step, built from the turn's real outputs."""
    tool, agent = step.get("tool"), step.get("agent", "")
    u, ctx, inv = r.get("understanding") or {}, r.get("context"), r.get("investigation") or {}
    if tool == "normalize_channel":
        return f"Message arrived on {CHANNELS.get(r.get('channel'), r.get('channel'))} and was converted to a standard format."
    if tool == "understand_message" and u:
        ents = ", ".join(f"{SLOT_NAMES.get(e, e.replace('_', ' '))} {inr(v) if e == 'amount' else v}"
                         for e, v in (u.get("entities") or {}).items())
        src = " An LLM helped classify it." if u.get("source") == "llm" else ""
        return (f"“{_label(u['intent'])}” — {u['category']}, {u['priority']} priority, customer sounds {u['emotion']}."
                + (f" Found {ents}." if ents else "") + src)
    if tool == "track_conversation":
        seen["memory"] = seen.get("memory", 0) + 1
        if seen["memory"] > 1:
            return "Opened a new issue for this request."
        mode, issue = r.get("mode"), r.get("issue") or {}
        return {"new": "Opened a new issue for this conversation.",
                "slot_fill": "Matched the reply to the open issue and filled in the missing detail.",
                "follow_up": f"Continued the earlier “{_label(issue.get('intent'))}” issue.",
                "meta": "Small talk — no new issue needed."}.get(mode, "Updated the conversation memory.")
    if tool == "customer_context":
        return f"{ctx['name']} ({ctx.get('tier', 'customer')}) — {ctx['headline']}." if ctx else \
            "Customer not identified yet (guest) — no account data."
    if tool == "search_knowledge":
        arts = r.get("articles") or []
        return f"Best match: “{arts[0]['title']}” ({arts[0]['relevance']:.0%} relevant)." if arts else "No matching article."
    if tool == "troubleshoot" and inv:
        steps = inv.get("steps", [])
        failed = sum(s["status"] == "fail" for s in steps)
        tally = f", {failed} found a problem" if failed else ""
        return f"{len(steps)} live check{'s' if len(steps) != 1 else ''}{tally}. {inv['diagnosis']}"
    if tool == "decide_escalation":
        seen["esc"] = seen.get("esc", 0) + 1
        if seen["esc"] == 1:
            return _decision_sentence(r.get("pre_decision") or r.get("decision"))
        before, after = (r.get("pre_decision") or {}).get("decision"), (r.get("decision") or {}).get("decision")
        return "Re-checked after the actions: decision holds." if before == after else \
            "Re-checked after the actions: something failed, so a human takes over."
    if tool == "execute_actions":
        icon = {"success": "✓", "failed": "✗", "blocked": "⛔", "skipped": "↩"}
        return "; ".join(f"{icon.get(a['status'], '')} {a['label']}" for a in r.get("actions") or []) or "No action needed."
    if tool == "create_ticket" and r.get("ticket"):
        t = r["ticket"]
        return f"{t['id']} for {t.get('team')} ({t['priority']}) — investigation, actions and summary attached."
    if tool in ("track_resolution", "ticket_status"):
        t = r.get("ticket") or {}
        return f"{t.get('id', '')} is {t.get('status', 'tracked')}; SLA clock running, customer notified." if t else \
            "Looked up the ticket's status."
    if tool == "select_media":
        media = r.get("media") or []
        return " · ".join(f"{m['role']}: {m['name']}" for m in media) if media else "No product images needed."
    if tool == "send_email":
        e = r.get("email") or {}
        return {"sent": f"Sent a real email to {e.get('to_masked')}.",
                "not_configured": "Email not set up on this server — logged only.",
                "rate_limited": "Skipped: too many emails to this address this hour.",
                "blocked": f"Blocked: {e.get('error', '')}"}.get(e.get("status"), f"Email failed: {e.get('error', '')}")
    if tool == "compose_reply":
        return f"Wrote a reply tuned for a {u.get('emotion', 'calm')} customer, using only verified facts."
    if tool == "polish_reply":
        return "LLM polished the wording (facts verified unchanged)." if r.get("llm_polished") else "Kept the original wording."
    if tool == "review_reply":
        rv = r.get("review") or {}
        return f"All {len(rv.get('checks', []))} safety checks passed." if rv.get("approved") else \
            "Safety check failed — sent a safe fallback reply."
    if tool == "record_csat" or agent.startswith("10"):
        return f"Rated {(r.get('csat') or {}).get('rating')}/5 — tunes future escalation thresholds." if r.get("csat") else \
            "Asked the customer to rate the help."
    return step.get("summary") or ""


def _saved(r):
    out = []
    if r.get("conversation_id"):
        out.append(f"conversations/{r['conversation_id']}")
    if r.get("ticket"):
        out.append(f"tickets/{r['ticket']['id']}")
    if r.get("actions"):
        out.append(f"actions ×{len(r['actions'])}")
    if r.get("attachments"):
        out.append(f"attachments ×{len(r['attachments'])}")
    if r.get("csat"):
        out.append(f"csat/{r['csat']['id']}")
    if r.get("email"):
        out.append(f"emails/{r['email']['id']}")
    return out


def build_view(store, turn):
    """Everything the component needs for one turn, with images inlined as data URIs."""
    r = turn["result"]
    seen, steps = {}, []
    for s in r.get("trace") or []:
        if s.get("agent", "").startswith("Master"):
            continue
        node = TOOL_NODE.get(s.get("tool")) or ("satisfaction" if s.get("agent", "").startswith("10") else None)
        if not node:
            continue
        steps.append(dict(node=node, tool=s.get("tool") or "request rating", transport=s.get("transport", "local"),
                          ms=s.get("ms", 0), ok=s.get("status") == "OK", text=explain(s, r, seen),
                          args=s.get("args", ""), result=s.get("result", "")))
    ctx = r.get("context") or {}
    chat = store.get("telegram_chats", turn.get("chat_id")) if turn.get("chat_id") else None
    who = ctx.get("name") or (chat or {}).get("first_name") or "Guest"
    d = r.get("decision")
    decision = d["decision"] if isinstance(d, dict) else d
    outcome, tone = OUTCOME.get(decision, ("Replied", "info"))
    email = r.get("email")
    return dict(
        seq=turn["seq"], source=turn.get("source"), channel=CHANNELS.get(r.get("channel"), r.get("channel") or "Web"),
        customer=who, tier=ctx.get("tier"), message=turn["text"], ts=turn["ts"],
        photos=[u for u in (attachment_data_uri(store, a) for a in (r.get("attachments") or [])[:3]) if u],
        steps=steps, total_ms=round(sum(s["ms"] for s in steps), 1), transport=r.get("transport", "mcp"),
        reply=(r.get("reply_channel") or r.get("reply") or "").replace("*", ""), csat=r.get("awaiting") == "csat",
        media=[dict(caption=m["caption"], role=m["role"], src=product_data_uri(m["sku"])) for m in r.get("media") or []],
        email=dict(status=email["status"], to=email.get("to_masked") or mask(email.get("to")), subject=email.get("subject")) if email else None,
        outcome=outcome, tone=tone, ticket=(r.get("ticket") or {}).get("id"), saved=_saved(r),
        nodes=[dict(key=k, name=n, icon=i) for k, n, i in NODES],
    )


def render(view, theme="light", height=780, autoplay=True):
    data = json.dumps(view, ensure_ascii=False).replace("<", "\\u003c")  # customer text can never close the script
    html = TEMPLATE.replace("__DATA__", data).replace("__THEME__", "dark" if theme == "dark" else "light") \
        .replace("__AUTOPLAY__", "true" if autoplay else "false")
    if hasattr(st, "iframe"):
        st.iframe(html, height=height)
    else:  # older Streamlit without st.iframe
        import streamlit.components.v1 as components
        components.html(html, height=height, scrolling=True)


TEMPLATE = r"""<!doctype html>
<html data-theme="__THEME__"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#ffffff;--panel:#f5f7fb;--line:#dfe4ec;--text:#18202e;--muted:#667085;--accent:#2563eb;--accent2:#7c3aed;
 --good:#12a150;--warn:#d97706;--bad:#dc2626;--node:#ffffff;--glow:rgba(37,99,235,.35);--bubble-in:#ffffff;--bubble-out:#dcf3ff;
 --phone:#0f172a;--screen:#e9eef5;--chip:#eef2ff}
[data-theme="dark"]{--bg:#0e1117;--panel:#151a23;--line:#273041;--text:#e6e9ef;--muted:#98a2b3;--accent:#60a5fa;--accent2:#a78bfa;
 --good:#34d399;--warn:#fbbf24;--bad:#f87171;--node:#1b2230;--glow:rgba(96,165,250,.4);--bubble-in:#1f2735;--bubble-out:#1d3b55;
 --phone:#020617;--screen:#0b1220;--chip:#1e2540}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Inter","Source Sans Pro",-apple-system,"Segoe UI",Roboto,sans-serif}
.app{display:grid;grid-template-columns:300px minmax(0,1fr) 320px;gap:14px;padding:4px 4px 8px;height:100vh}
@media (max-width:1180px){.app{grid-template-columns:280px minmax(0,1fr);height:auto}.log{grid-column:1/-1;height:360px}}
@media (max-width:760px){.app{grid-template-columns:1fr}.phone{margin:0 auto}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;min-height:0}
/* phone */
.phone{width:280px;height:100%;min-height:600px;max-height:760px;background:var(--phone);border-radius:38px;padding:10px;
 box-shadow:0 18px 40px rgba(0,0,0,.25)}
.screen{height:100%;background:var(--screen);border-radius:30px;overflow:hidden;display:flex;flex-direction:column}
.bar{display:flex;align-items:center;gap:8px;padding:14px 14px 10px;background:var(--panel);border-bottom:1px solid var(--line)}
.ava{width:30px;height:30px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;color:#fff;font-size:15px}
.bar b{font-size:13px;display:block}.bar small{color:var(--muted);font-size:11px}
.chat{flex:1;overflow-y:auto;padding:12px 10px;display:flex;flex-direction:column;gap:8px;scroll-behavior:smooth}
.msg{max-width:86%;padding:8px 10px;border-radius:14px;font-size:12.5px;white-space:pre-wrap;word-wrap:break-word;
 animation:pop .35s ease;box-shadow:0 1px 1px rgba(0,0,0,.06)}
.msg.in{align-self:flex-end;background:var(--bubble-out);border-bottom-right-radius:4px}
.msg.out{align-self:flex-start;background:var(--bubble-in);border-bottom-left-radius:4px}
.msg img{display:block;width:100%;border-radius:10px;margin-bottom:6px}
.album{align-self:flex-start;display:grid;grid-template-columns:1fr 1fr;gap:4px;width:86%;animation:pop .35s ease}
.album figure{margin:0;background:var(--bubble-in);border-radius:10px;overflow:hidden}
.album img{width:100%;display:block}.album figcaption{font-size:10.5px;padding:3px 6px;color:var(--muted)}
.album.one{grid-template-columns:1fr;width:60%}
.typing{align-self:flex-start;background:var(--bubble-in);border-radius:14px;padding:9px 12px;display:flex;gap:4px}
.typing i{width:6px;height:6px;border-radius:50%;background:var(--muted);animation:blink 1s infinite}
.typing i:nth-child(2){animation-delay:.15s}.typing i:nth-child(3){animation-delay:.3s}
.stars{align-self:flex-start;display:flex;gap:4px;animation:pop .35s ease}
.stars span{background:var(--bubble-in);border:1px solid var(--line);border-radius:8px;padding:3px 7px;font-size:11px}
.toast{align-self:center;font-size:11px;color:var(--muted);background:var(--panel);border:1px solid var(--line);border-radius:999px;
 padding:4px 10px;animation:pop .35s ease}
/* network */
.net{display:flex;flex-direction:column;padding:12px 14px;gap:8px;overflow:hidden}
.head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.head h3{margin:0;font-size:15px}
.pill{font-size:11.5px;border-radius:999px;padding:3px 9px;background:var(--chip);color:var(--accent);font-weight:600}
.spacer{flex:1}
button{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:9px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.ticker{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted);height:20px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ticker b{color:var(--accent)}
svg{width:100%;flex:1;min-height:300px}
.edge{stroke:var(--line);stroke-width:1.5}
.edge.hot{stroke:var(--accent);stroke-width:2.5}
.edge.done{stroke:var(--good);stroke-opacity:.45}
.nd circle.base{fill:var(--node);stroke:var(--line);stroke-width:2;transition:all .3s}
.nd text.ic{font-size:21px;dominant-baseline:central;text-anchor:middle}
.nd text.lb{font-size:11.5px;fill:var(--muted);text-anchor:middle;font-weight:600}
.nd.skip{opacity:.28}
.nd.work circle.base{stroke:var(--accent);stroke-width:3;filter:drop-shadow(0 0 10px var(--glow))}
.nd.done circle.base{stroke:var(--good);stroke-width:2.5}
.nd.err circle.base{stroke:var(--bad);stroke-width:3}
.nd .ring{fill:none;stroke:var(--accent);stroke-width:2;opacity:0}
.nd.work .ring{animation:ring 1s ease-out infinite}
.nd .tick{opacity:0;transition:opacity .3s}.nd.done .tick{opacity:1}
.hub circle{fill:url(#hubg)}
.hub text{fill:#fff;text-anchor:middle;font-weight:700}
.hub .pulse{fill:none;stroke:var(--accent);stroke-width:2;opacity:.0;animation:ring 2.4s ease-out infinite}
.packet{fill:var(--accent);filter:drop-shadow(0 0 6px var(--glow))}
.packet.back{fill:var(--good)}
.narr{min-height:58px;border:1px solid var(--line);background:var(--bg);border-radius:14px;padding:9px 12px;display:flex;gap:10px;align-items:flex-start}
.narr .ic{font-size:22px;line-height:1}
.narr b{display:block;font-size:13px}
.narr span{font-size:13px;color:var(--text)}
.outcome{display:none;border-radius:14px;padding:10px 12px;animation:pop .4s ease}
.outcome.good{display:block;background:color-mix(in srgb,var(--good) 12%,transparent);border:1px solid var(--good)}
.outcome.warn{display:block;background:color-mix(in srgb,var(--warn) 12%,transparent);border:1px solid var(--warn)}
.outcome.info{display:block;background:color-mix(in srgb,var(--accent) 12%,transparent);border:1px solid var(--accent)}
.outcome .t{font-weight:700}
.saved{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.saved code{font-size:11px;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:2px 6px;color:var(--muted)}
.foot{font-size:11.5px;color:var(--muted)}
/* log */
.log{display:flex;flex-direction:column;overflow:hidden}
.log h4{margin:0;padding:12px 14px 6px;font-size:13px;display:flex;justify-content:space-between;align-items:center}
.log h4 span{color:var(--muted);font-weight:400;font-size:11.5px}
.calls{flex:1;overflow-y:auto;padding:0 10px 10px;display:flex;flex-direction:column;gap:6px}
.call{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:7px 9px;animation:pop .3s ease;cursor:pointer}
.call .l1{display:flex;gap:6px;align-items:center;font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
.call .l1 .n{color:var(--muted)}.call .l1 .t{color:var(--accent);font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.call .l1 .ms{color:var(--muted)}
.call .l2{font-size:12px;margin-top:3px}
.call pre{display:none;margin:6px 0 0;font-size:10.5px;white-space:pre-wrap;word-break:break-all;color:var(--muted);max-height:180px;overflow:auto;
 background:var(--panel);border-radius:8px;padding:6px}
.call.open pre{display:block}
.call.run{border-color:var(--accent)}
.badge{font-size:10px;border-radius:5px;padding:1px 5px;background:var(--chip);color:var(--accent);font-family:ui-monospace,Menlo,monospace}
@keyframes pop{from{opacity:0;transform:translateY(6px) scale(.98)}to{opacity:1;transform:none}}
@keyframes blink{0%,100%{opacity:.3}50%{opacity:1}}
@keyframes ring{0%{opacity:.8;r:30}100%{opacity:0;r:48}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="app">
  <div class="phone"><div class="screen">
    <div class="bar"><div class="ava">🛟</div><div><b>SupportPilot</b><small id="chan"></small></div></div>
    <div class="chat" id="chat"></div>
  </div></div>

  <div class="card net">
    <div class="head">
      <h3>Live agent flow</h3><span class="pill" id="tp"></span><span class="spacer"></span>
      <button id="speed" title="Playback speed">1×</button><button id="replay">↻ Replay</button><button id="skip">⏭ Skip</button>
    </div>
    <div class="ticker" id="ticker"></div>
    <svg id="svg" viewBox="0 0 720 470" preserveAspectRatio="xMidYMid meet">
      <defs><radialGradient id="hubg"><stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#6d28d9"/></radialGradient></defs>
      <g id="edges"></g><g id="nodes"></g>
      <g class="hub"><circle class="pulse" cx="360" cy="235" r="56"/><circle cx="360" cy="235" r="56"/>
        <text x="360" y="228" font-size="15">Orchestrator</text><text x="360" y="248" font-size="11" opacity=".85">MCP client</text></g>
      <g id="packets"></g>
    </svg>
    <div class="narr" id="narr"><div class="ic">🧭</div><div><b>Waiting</b><span>The orchestrator plans which agents to call.</span></div></div>
    <div class="outcome" id="outcome"><div class="t" id="otitle"></div><div class="saved" id="saved"></div></div>
    <div class="foot" id="foot"></div>
  </div>

  <div class="card log"><h4>MCP calls <span id="count"></span></h4><div class="calls" id="calls"></div></div>
</div>
<script>
const D = __DATA__, AUTOPLAY = __AUTOPLAY__;
// Match the host page's real theme (st.iframe is same-origin); the server-side hint is only a fallback.
function syncTheme() {
  try {
    const host = parent.document.querySelector(".stApp") || parent.document.body;
    const m = getComputedStyle(host).backgroundColor.match(/\d+(\.\d+)?/g);
    if (m) {
      const [r, g, b] = m.map(Number), lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      document.documentElement.dataset.theme = lum < 0.5 ? "dark" : "light";
    }
  } catch (e) { /* not embedded or cross-origin: keep the hint */ }
}
syncTheme(); setInterval(syncTheme, 1500);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const NS = "http://www.w3.org/2000/svg";
const CX = 360, CY = 235, RX = 300, RY = 190;
let speed = 1, timers = [], nodePos = {};
const used = new Set(D.steps.map(s => s.node));
const MCP_CALLS = D.steps.filter(s => s.transport !== "local").length;
const callName = s => s.transport === "local" ? "orchestrator · " + s.tool : "tools/call " + s.tool;

D.nodes.forEach((n, i) => {
  const a = -Math.PI / 2 + i * 2 * Math.PI / D.nodes.length;
  nodePos[n.key] = [CX + RX * Math.cos(a), CY + RY * Math.sin(a)];
});
function el(tag, attrs, parent) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); parent && parent.appendChild(e); return e; }

function drawNetwork() {
  $("edges").innerHTML = ""; $("nodes").innerHTML = ""; $("packets").innerHTML = "";
  D.nodes.forEach(n => {
    const [x, y] = nodePos[n.key];
    el("line", {x1: CX, y1: CY, x2: x, y2: y, class: "edge", id: "e-" + n.key}, $("edges"));
    const g = el("g", {class: "nd" + (used.has(n.key) ? "" : " skip"), id: "n-" + n.key}, $("nodes"));
    el("circle", {class: "ring", cx: x, cy: y, r: 30}, g);
    el("circle", {class: "base", cx: x, cy: y, r: 27}, g);
    const ic = el("text", {class: "ic", x: x, y: y}, g); ic.textContent = n.icon;
    const lb = el("text", {class: "lb", x: x, y: y + 44}, g); lb.textContent = n.name;
    const tk = el("g", {class: "tick"}, g);
    el("circle", {cx: x + 20, cy: y - 20, r: 8, fill: "var(--good)"}, tk);
    const tt = el("text", {x: x + 20, y: y - 16.5, "text-anchor": "middle", "font-size": 11, fill: "#fff", "font-weight": 700}, tk); tt.textContent = "✓";
    const title = el("title", {}, g); title.textContent = used.has(n.key) ? n.name : n.name + " — not needed for this message";
  });
}
function packet(from, to, back, dur) {
  const p = el("circle", {r: 6, class: "packet" + (back ? " back" : ""), cx: from[0], cy: from[1]}, $("packets"));
  const t0 = performance.now();
  (function step(now) {
    const k = Math.min(1, (now - t0) / dur), e = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
    p.setAttribute("cx", from[0] + (to[0] - from[0]) * e); p.setAttribute("cy", from[1] + (to[1] - from[1]) * e);
    if (k < 1) requestAnimationFrame(step); else p.remove();
  })(t0);
}
function nodeName(k) { return (D.nodes.find(n => n.key === k) || {}).name || k; }
function nodeIcon(k) { return (D.nodes.find(n => n.key === k) || {}).icon || "•"; }
function addMsg(html, cls) { const m = document.createElement("div"); m.className = cls; m.innerHTML = html; $("chat").appendChild(m); $("chat").scrollTop = 1e6; return m; }

function phoneStart() {
  $("chat").innerHTML = "";
  $("chan").textContent = D.channel + " · " + D.customer + (D.tier ? " (" + D.tier + ")" : "");
  const photos = D.photos.map(p => `<img src="${p}" alt="customer photo">`).join("");
  addMsg(photos + esc(D.message), "msg in");
}
function phoneTyping() { return addMsg("<i></i><i></i><i></i>", "typing"); }
function phoneEnd() {
  document.querySelectorAll(".typing").forEach(t => t.remove());
  if (D.reply) addMsg(esc(D.reply), "msg out");
  if (D.media.length) addMsg(D.media.map(m => `<figure><img src="${m.src}" alt=""><figcaption>${esc(m.role)}</figcaption></figure>`).join(""),
    "album" + (D.media.length === 1 ? " one" : ""));
  if (D.csat) addMsg("⭐ ⭐⭐ ⭐⭐⭐ ⭐⭐⭐⭐ ⭐⭐⭐⭐⭐".split(" ").map(s => `<span>${s}</span>`).join(""), "stars");
  if (D.email) addMsg(D.email.status === "sent" ? `📧 Email sent to ${esc(D.email.to)}` : `📧 Email ${esc(D.email.status.replace("_", " "))}`, "toast");
}
function logCall(i, s) {
  const c = document.createElement("div"); c.className = "call run"; c.id = "c-" + i;
  c.innerHTML = `<div class="l1"><span class="n">#${i + 1}</span><span class="t">${esc(callName(s))}</span>
    <span class="badge">${esc(s.transport)}</span><span class="ms">${s.ms} ms</span></div>
    <div class="l2">${nodeIcon(s.node)} <b>${esc(nodeName(s.node))}</b> — <span class="txt">working…</span></div>
    <pre>→ arguments ${esc(s.args)}\n\n← result ${esc(s.result)}</pre>`;
  c.onclick = () => c.classList.toggle("open");
  $("calls").appendChild(c); $("calls").scrollTop = 1e6;
  return c;
}
function setState(key, cls) { const n = $("n-" + key); n.classList.remove("work", "done", "err"); if (cls) n.classList.add(cls); }
function narrate(s) {
  $("narr").innerHTML = `<div class="ic">${nodeIcon(s.node)}</div><div><b>${esc(nodeName(s.node))}</b><span>${esc(s.text)}</span></div>`;
}
function stepAt(i, instant) {
  const s = D.steps[i], hub = [CX, CY], node = nodePos[s.node], u = 1 / speed;
  const call = logCall(i, s);
  $("ticker").innerHTML = `→ <b>${esc(callName(s))}</b> ${esc(s.args.slice(0, 120))}`;
  $("e-" + s.node).classList.add("hot");
  const finish = () => {
    setState(s.node, s.ok ? "done" : "err");
    $("e-" + s.node).classList.remove("hot"); $("e-" + s.node).classList.add("done");
    call.classList.remove("run"); call.querySelector(".txt").textContent = s.text;
    narrate(s);
  };
  if (instant) { finish(); return; }
  packet(hub, node, false, 320 * u);
  timers.push(setTimeout(() => setState(s.node, "work"), 320 * u));
  timers.push(setTimeout(() => packet(node, hub, true, 280 * u), 760 * u));
  timers.push(setTimeout(finish, 1040 * u));
}
function showOutcome() {
  $("outcome").className = "outcome " + D.tone;
  $("otitle").textContent = (D.tone === "good" ? "✅ " : D.tone === "warn" ? "🧑‍💼 " : "💬 ") + D.outcome + (D.ticket ? " · " + D.ticket : "");
  $("saved").innerHTML = D.saved.length ? "<span class='foot'>Saved to the data store:</span> " + D.saved.map(x => `<code>${esc(x)}</code>`).join("") : "";
  $("ticker").innerHTML = "✓ turn complete — " + MCP_CALLS + " MCP tool calls";
}
function reset() {
  timers.forEach(clearTimeout); timers = [];
  drawNetwork(); $("calls").innerHTML = ""; $("outcome").className = "outcome";
  $("narr").innerHTML = `<div class="ic">🧭</div><div><b>Orchestrator</b><span>Planning which agents to call over MCP…</span></div>`;
}
function play() {
  reset(); phoneStart();
  const u = 1 / speed, gap = 1150 * u;
  timers.push(setTimeout(phoneTyping, 500 * u));
  D.steps.forEach((_, i) => timers.push(setTimeout(() => stepAt(i, false), 900 * u + i * gap)));
  const end = 900 * u + D.steps.length * gap;
  timers.push(setTimeout(() => { phoneEnd(); showOutcome(); }, end));
}
function skip() { reset(); phoneStart(); D.steps.forEach((_, i) => stepAt(i, true)); phoneEnd(); showOutcome(); }

$("tp").textContent = (D.transport === "mcp" ? "MCP · " : "direct · ") + MCP_CALLS + " tool calls";
$("count").textContent = MCP_CALLS + " calls · " + D.total_ms + " ms total";
$("foot").innerHTML = `Real processing took <b>${D.total_ms} ms</b>; replayed slowly so you can follow it. Click any MCP call to see its JSON.`;
$("replay").onclick = play; $("skip").onclick = skip;
$("speed").onclick = e => { speed = speed === 1 ? 2 : 1; e.target.textContent = speed + "×"; e.target.classList.toggle("on", speed === 2); };
drawNetwork();
(AUTOPLAY && !matchMedia("(prefers-reduced-motion: reduce)").matches) ? play() : skip();
</script></body></html>"""
