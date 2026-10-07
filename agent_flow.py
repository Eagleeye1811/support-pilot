"""Live agent-flow board: replays one orchestrated turn agent by agent, in plain English.

The agents run in a few milliseconds, so the board replays the real trace at a readable
pace: every agent is visible at once, grouped by stage, and lights up as it works.
"""
from __future__ import annotations

import json

import streamlit as st

from agents.shared_agent import CHANNELS, INTENTS, inr

# stage key, title, one-line purpose
STAGES = [
    ("receive", "Receive", "Take in the message"),
    ("understand", "Understand", "What is the problem and who is asking?"),
    ("investigate", "Investigate", "Look up help articles and check live data"),
    ("decide", "Decide & act", "Fix it automatically, ask, or hand to a human"),
    ("deliver", "Deliver", "Ticket, follow-up and a safety check on the reply"),
]
# trace prefix → (stage, display name, icon)
AGENTS = {
    "Master": ("receive", "Orchestrator", "🧭"),
    "0": ("receive", "Channel adapter", "📥"),
    "1": ("understand", "Understanding", "🧠"),
    "6": ("understand", "Memory", "🗂️"),
    "2": ("understand", "Customer context", "👤"),
    "3": ("investigate", "Knowledge base", "📚"),
    "4": ("investigate", "Troubleshooting", "🔧"),
    "7": ("decide", "Escalation", "⚖️"),
    "5": ("decide", "Actions", "⚡"),
    "8": ("deliver", "Ticket", "🎫"),
    "9": ("deliver", "Tracking", "📍"),
    "10": ("deliver", "Satisfaction", "⭐"),
    "Email": ("deliver", "Email agent", "📧"),
    "Reviewer": ("deliver", "Reviewer", "🛡️"),
}
DECISION_TEXT = {
    "auto_resolve": ("Resolved automatically", "good"),
    "escalate": ("Handed to a human specialist", "warn"),
    "clarify": ("Asked the customer for details", "info"),
    "csat": ("Feedback recorded", "good"),
    "email": ("Email agent ran", "info"),
}
SLOT_NAMES = {"order_id": "order ID", "txn_id": "transaction ID", "ticket_id": "ticket ID", "service_id": "service ID"}


def _key(agent):
    head = agent.split(" ")[0]
    return head if head in AGENTS else ("Reviewer" if agent.startswith("Reviewer") else head)


def _label(intent):
    return INTENTS.get(intent, {}).get("label", (intent or "").replace("_", " "))


def _decision_sentence(d):
    if not isinstance(d, dict):
        return "Checked whether a human is needed."
    conf = f"confidence {d['confidence']:.0%}, needed {d['threshold']:.0%}"
    if d["decision"] == "auto_resolve":
        return f"Safe to fix automatically ({conf})."
    if d["decision"] == "clarify":
        return f"Not enough details yet — will ask the customer ({conf})."
    why = d["reasons"][0] if d.get("reasons") else "rules require a human"
    return f"A human should handle this: {why}."


def _explain(step, r, seen):
    """Plain-English sentence for one trace step, built from the turn's real outputs."""
    k = _key(step["agent"])
    u, ctx, inv = r.get("understanding") or {}, r.get("context"), r.get("investigation") or {}
    if k == "Master":
        return "Read the message and planned which agents to call."
    if k == "0":
        return f"Message arrived on {CHANNELS.get(r.get('channel'), r.get('channel'))} and was converted to a standard format."
    if k == "1" and u:
        ents = ", ".join(f"{SLOT_NAMES.get(e, e.replace('_', ' '))} {inr(v) if e == 'amount' else v}"
                         for e, v in (u.get("entities") or {}).items())
        src = " (an LLM helped classify it)" if u.get("source") == "llm" else ""
        return (f"Recognised “{_label(u['intent'])}” — {u['category']}, {u['priority']} priority, "
                f"customer sounds {u['emotion']}{src}." + (f" Found {ents}." if ents else ""))
    if k == "6":
        mode, issue = r.get("mode"), r.get("issue") or {}
        return {"new": "Opened a new issue for this conversation.",
                "slot_fill": "Matched this reply to the open issue and filled in the missing detail.",
                "follow_up": f"Continued the earlier “{_label(issue.get('intent'))}” issue.",
                "meta": "Small talk — no new issue needed."}.get(mode, "Updated the conversation memory.")
    if k == "2":
        return f"Identified {ctx['name']} ({ctx.get('tier', 'customer')}) — {ctx['headline']}." if ctx else \
            "Customer not identified yet (guest) — no account data available."
    if k == "3":
        arts = r.get("articles") or []
        return f"Best help-center match: “{arts[0]['title']}” ({arts[0]['relevance']:.0%} relevant)." if arts else \
            "No matching help-center article."
    if k == "4" and inv:
        steps = inv.get("steps", [])
        passed = sum(s["status"] == "pass" for s in steps)
        failed = sum(s["status"] == "fail" for s in steps)
        tally = f" ({passed} passed, {failed} found a problem)" if passed or failed else ""
        return f"Ran {len(steps)} check{'s' if len(steps) != 1 else ''} on live data{tally}. Diagnosis: {inv['diagnosis']}"
    if k == "7":
        seen["7"] = seen.get("7", 0) + 1
        if seen["7"] == 1:
            return _decision_sentence(r.get("pre_decision") or r.get("decision"))
        before = (r.get("pre_decision") or {}).get("decision")
        after = (r.get("decision") or {}).get("decision")
        return "Re-checked after the actions: decision unchanged." if before == after else \
            "Re-checked after the actions: something failed, so a human will take over."
    if k == "5":
        acts = r.get("actions") or []
        icon = {"success": "✓", "failed": "✗", "blocked": "⛔", "skipped": "↩"}
        return "Did: " + "; ".join(f"{icon.get(a['status'], '')} {a['label']}" for a in acts) + "." if acts else "No action needed."
    if k == "8" and r.get("ticket"):
        t = r["ticket"]
        return f"Created ticket {t['id']} for {t.get('team')} ({t['priority']} priority) with the full investigation attached."
    if k == "9":
        t = r.get("ticket") or {}
        return f"Ticket {t.get('id', '')} is {t.get('status', 'tracked')}; SLA timer started and the customer is notified." if t else \
            "Looked up the status of the customer's ticket."
    if k == "10":
        return "Recorded the customer's rating — it tunes future escalation decisions." if r.get("csat") else \
            "Will ask the customer to rate the help."
    if k == "Email":
        e = r.get("email") or {}
        return {"sent": f"Sent a real email with the ticket details to {e.get('to_masked')}.",
                "not_configured": "Email is not set up on this server (SMTP_USER / SMTP_PASSWORD) — logged only.",
                "rate_limited": "Skipped: too many emails to this address in the last hour.",
                "blocked": f"Not sent: {e.get('error', '')}"}.get(e.get("status"), f"Email failed: {e.get('error', '')}")
    if k == "Reviewer":
        rv = r.get("review") or {}
        n = len(rv.get("checks", []))
        return f"Checked the reply against {n} safety rules — all passed." if rv.get("approved") else \
            "Safety check failed: " + ", ".join(rv.get("rejected", [])) + " — sent a safe fallback reply."
    return step.get("summary") or ""


def build_flow(turn_text, r):
    """Board data for one turn: the steps that ran (in order) plus the agents that were not needed."""
    seen, steps = {}, []
    for s in r.get("trace") or []:
        k = _key(s["agent"])
        if k not in AGENTS:
            continue
        stage, name, icon = AGENTS[k]
        if k == "7":
            name = "Escalation (after actions)" if seen.get("7") else "Escalation"
        steps.append(dict(stage=stage, agent=k, name=name, icon=icon, ok=s["status"] == "OK",
                          text=_explain(s, r, seen), tech=s.get("summary") or "", ms=s.get("ms", 0)))
    ran = {s["agent"] for s in steps}
    skipped = [dict(stage=st, agent=k, name=n, icon=i) for k, (st, n, i) in AGENTS.items() if k not in ran]
    d = r.get("decision")
    decision = d["decision"] if isinstance(d, dict) else d
    outcome, tone = DECISION_TEXT.get(decision, ("Replied to the customer", "info"))
    return dict(message=turn_text, steps=steps, skipped=skipped, stages=[dict(key=k, title=t, sub=s) for k, t, s in STAGES],
                total_ms=round(sum(s["ms"] for s in steps), 1), outcome=outcome, tone=tone,
                reply=(r.get("reply") or "")[:240], ticket=(r.get("ticket") or {}).get("id"))


def render_flow(turn_text, r, theme="light", height=720, autoplay=True):
    # \u003c keeps customer text from ever closing the <script> block
    data = json.dumps(build_flow(turn_text, r), ensure_ascii=False).replace("<", "\\u003c")
    html = TEMPLATE.replace("__DATA__", data).replace("__THEME__", "dark" if theme == "dark" else "light") \
        .replace("__AUTOPLAY__", "true" if autoplay else "false")
    if hasattr(st, "iframe"):
        st.iframe(html, height=height)
    else:  # older Streamlit without st.iframe
        import streamlit.components.v1 as components
        components.html(html, height=height, scrolling=False)


TEMPLATE = r"""<!doctype html>
<html data-theme="__THEME__"><head><meta charset="utf-8">
<style>
:root{--bg:#ffffff;--panel:#f6f7f9;--line:#d9dde3;--text:#1d2129;--muted:#6b7280;--accent:#2a78d6;--accent-soft:#e3eefb;
 --good:#1a8f5f;--good-soft:#e2f5ec;--warn:#c25e00;--warn-soft:#fdeedd;--info:#2a78d6;--info-soft:#e3eefb;--bad:#c62828}
[data-theme="dark"]{--bg:#0e1117;--panel:#171b23;--line:#2c323d;--text:#e6e8eb;--muted:#9aa3ae;--accent:#5b9cf0;--accent-soft:#1a2a40;
 --good:#4cc38a;--good-soft:#14301f;--warn:#f0a050;--warn-soft:#3a2614;--info:#5b9cf0;--info-soft:#1a2a40;--bad:#ef6b6b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Source Sans Pro",-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{height:100vh;display:flex;flex-direction:column;gap:10px;padding:2px 4px}
.top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.msg{flex:1;min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 10px;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.msg b{color:var(--muted);font-weight:600;margin-right:6px}
button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:8px;
 padding:6px 10px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
.bar{height:4px;background:var(--line);border-radius:4px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .35s ease}
.board{flex:1;overflow-y:auto;padding-right:4px;scroll-behavior:smooth}
.stage{border:1px solid var(--line);border-radius:12px;padding:8px 10px;margin-bottom:6px;transition:border-color .3s,background .3s}
.stage.live{border-color:var(--accent);background:var(--accent-soft)}
.stage h4{margin:0;font-size:13px;letter-spacing:.02em;display:flex;align-items:baseline;gap:8px}
.stage h4 span{font-weight:400;color:var(--muted);font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 2px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid var(--line);
 color:var(--muted);background:var(--bg);transition:all .3s}
.chip .dot{width:7px;height:7px;border-radius:50%;background:var(--line)}
.chip.work{color:var(--accent);border-color:var(--accent)}
.chip.work .dot{background:var(--accent);animation:pulse .8s infinite}
.chip.done{color:var(--text);border-color:var(--good)}
.chip.done .dot{background:var(--good)}
.chip.err .dot{background:var(--bad)}
.chip.skip{opacity:.45;border-style:dashed}
.rows{margin-top:4px}
.row{display:flex;gap:8px;padding:6px 2px 4px;border-top:1px dashed var(--line);animation:in .35s ease}
.row .ic{font-size:16px;line-height:20px}
.row .nm{font-weight:600;font-size:13px}
.row .tx{font-size:13px}
.row .tc{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--muted);margin-top:2px;display:none;word-break:break-word}
.tech .row .tc{display:block}
.typing{color:var(--accent);font-size:13px}
.typing::after{content:"";animation:dots 1s steps(4) infinite}
.end{border-radius:12px;padding:10px 12px;display:none;animation:in .4s ease}
.end.good{background:var(--good-soft);border:1px solid var(--good)}
.end.warn{background:var(--warn-soft);border:1px solid var(--warn)}
.end.info{background:var(--info-soft);border:1px solid var(--info)}
.end .t{font-weight:700}
.end .r{font-size:13px;color:var(--text);margin-top:4px;opacity:.9}
.foot{font-size:12px;color:var(--muted)}
label{font-size:12px;color:var(--muted);display:inline-flex;align-items:center;gap:4px;cursor:pointer}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(1.5)}}
@keyframes in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@keyframes dots{0%{content:""}25%{content:"."}50%{content:".."}75%{content:"..."}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="wrap">
  <div class="top">
    <div class="msg" id="msg"></div>
    <button id="replay" title="Watch the agents again">↻ Replay</button>
    <button id="skip" title="Show everything now">⏭ Skip</button>
  </div>
  <div class="bar"><i id="prog"></i></div>
  <div class="board" id="board"></div>
  <div class="end" id="end"><div class="t" id="endT"></div><div class="r" id="endR"></div></div>
  <div class="foot" id="foot"></div>
</div>
<script>
const D = __DATA__;
const AUTOPLAY = __AUTOPLAY__;
const STEP_MS = 900, WORK_MS = 520;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let timers = [];

$("msg").innerHTML = "<b>Customer:</b>" + esc(D.message);
$("foot").innerHTML = `Real run time <b>${D.total_ms} ms</b> for ${D.steps.length} agent steps — replayed slowly so you can follow it.
  <label style="margin-left:8px"><input type="checkbox" id="techT"> show technical output</label>`;
$("techT").onchange = e => document.body.classList.toggle("tech", e.target.checked);

function build() {
  const board = $("board");
  board.innerHTML = "";
  D.stages.forEach(st => {
    const box = document.createElement("div");
    box.className = "stage"; box.id = "st-" + st.key;
    const chips = [];
    const added = new Set();
    D.steps.forEach((s, i) => { if (s.stage === st.key && !added.has(s.agent)) { added.add(s.agent);
      chips.push(`<span class="chip" id="ch-${s.agent}"><span class="dot"></span>${s.icon} ${esc(AGENT_NAME(s))}</span>`); } });
    D.skipped.filter(s => s.stage === st.key).forEach(s =>
      chips.push(`<span class="chip skip" title="Not needed for this message"><span class="dot"></span>${s.icon} ${esc(s.name)}</span>`));
    box.innerHTML = `<h4>${esc(st.title)} <span>${esc(st.sub)}</span></h4><div class="chips">${chips.join("")}</div><div class="rows" id="rows-${st.key}"></div>`;
    board.appendChild(box);
  });
}
function AGENT_NAME(s){ return s.agent === "7" ? "Escalation" : s.name; }

function showStep(i, instant) {
  const s = D.steps[i];
  document.querySelectorAll(".stage").forEach(b => b.classList.remove("live"));
  const stage = $("st-" + s.stage); stage.classList.add("live");
  const chip = $("ch-" + s.agent); chip.classList.remove("done"); chip.classList.add("work");
  const rows = $("rows-" + s.stage);
  const row = document.createElement("div"); row.className = "row";
  row.innerHTML = `<div class="ic">${s.icon}</div><div><div class="nm">${esc(s.name)}</div>
    <div class="tx typing">working</div><div class="tc">${esc(s.tech)} · ${s.ms} ms</div></div>`;
  rows.appendChild(row);
  const finish = () => {
    const tx = row.querySelector(".tx"); tx.className = "tx"; tx.textContent = s.text;
    chip.classList.remove("work"); chip.classList.add(s.ok ? "done" : "err");
    $("prog").style.width = ((i + 1) / D.steps.length * 100) + "%";
  };
  if (instant) finish(); else { timers.push(setTimeout(finish, WORK_MS)); }
  if (!instant) stage.scrollIntoView({block: "nearest", behavior: "smooth"});
}
function showEnd() {
  document.querySelectorAll(".stage").forEach(b => b.classList.remove("live"));
  const e = $("end"); e.className = "end " + D.tone; e.style.display = "block";
  $("endT").textContent = "Outcome: " + D.outcome + (D.ticket ? " · " + D.ticket : "");
  $("endR").textContent = D.reply ? "Reply: " + D.reply + (D.reply.length >= 240 ? "…" : "") : "";
  $("prog").style.width = "100%";
}
function reset() { timers.forEach(clearTimeout); timers = []; build(); $("end").style.display = "none"; $("prog").style.width = "0"; }
function play() {
  reset();
  D.steps.forEach((_, i) => timers.push(setTimeout(() => showStep(i, false), i * STEP_MS)));
  timers.push(setTimeout(showEnd, D.steps.length * STEP_MS + 200));
}
function skip() { reset(); D.steps.forEach((_, i) => showStep(i, true)); showEnd(); }
$("replay").onclick = play; $("skip").onclick = skip;
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
(AUTOPLAY && !reduce) ? play() : skip();
</script></body></html>"""
