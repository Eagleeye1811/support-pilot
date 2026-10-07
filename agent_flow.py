"""Live agent flow: replays one orchestrated turn — message in, agents at work, reply out.

The agents run in a few milliseconds, so the view replays the real trace at a readable pace:
the incoming message, a stage stepper, each agent working then reporting in plain English,
and finally the reply with its outcome. It follows the page's light / dark theme.
"""
from __future__ import annotations

import json

import streamlit as st

from agents.shared_agent import CHANNELS, INTENTS, fmt_ts, inr

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


def build_flow(turn_text, r, store=None, ts=None):
    """Board data for one turn: the message, the steps that ran (in order), the agents not needed, and the reply."""
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
    skipped = [dict(stage=st_, agent=k, name=n, icon=i) for k, (st_, n, i) in AGENTS.items() if k not in ran]
    d = r.get("decision")
    decision = d["decision"] if isinstance(d, dict) else d
    outcome, tone = DECISION_TEXT.get(decision, ("Replied to the customer", "info"))
    ctx = r.get("context") or {}
    photos = []
    for att_id in (r.get("attachments") or [])[:2]:
        att = store.get("attachments", att_id) if store else None
        if att:
            photos.append(f"data:{att['mime']};base64,{att['data']}")
    email = r.get("email") or None
    return dict(
        message=turn_text, customer=ctx.get("name") or "Guest", tier=ctx.get("tier"),
        channel=CHANNELS.get(r.get("channel"), r.get("channel") or "Website Chat"), ts=fmt_ts(ts) if ts else "",
        photos=photos, steps=steps, skipped=skipped,
        stages=[dict(key=k, title=t, sub=s) for k, t, s in STAGES],
        total_ms=round(sum(s["ms"] for s in steps), 1), outcome=outcome, tone=tone,
        reply=(r.get("reply") or "").replace("**", ""), ticket=(r.get("ticket") or {}).get("id"),
        email=dict(status=email.get("status"), to=email.get("to_masked")) if email else None)


def render_flow(turn_text, r, theme="light", height=720, autoplay=True, store=None, ts=None):
    # < keeps customer text from ever closing the <script> block
    data = json.dumps(build_flow(turn_text, r, store, ts), ensure_ascii=False).replace("<", "\\u003c")
    html = TEMPLATE.replace("__DATA__", data).replace("__THEME__", "dark" if theme == "dark" else "light") \
        .replace("__AUTOPLAY__", "true" if autoplay else "false")
    if hasattr(st, "iframe"):
        st.iframe(html, height=height)
    else:  # older Streamlit without st.iframe
        import streamlit.components.v1 as components
        components.html(html, height=height, scrolling=False)


TEMPLATE = r"""<!doctype html>
<html data-theme="__THEME__"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#ffffff;--panel:#f6f8fb;--card:#ffffff;--line:#e3e8ef;--text:#111827;--muted:#6b7280;--accent:#2563eb;
 --accent-soft:#e8f0fe;--good:#15803d;--good-soft:#e7f6ec;--warn:#b45309;--warn-soft:#fdf1e2;--bad:#b91c1c;
 --in:#eef2f7;--out:#e3f0ff;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08)}
[data-theme="dark"]{--bg:#0e1117;--panel:#141922;--card:#1a202b;--line:#2a3240;--text:#e8ebf0;--muted:#9aa4b2;--accent:#60a5fa;
 --accent-soft:#17263d;--good:#4ade80;--good-soft:#132a1d;--warn:#fbbf24;--warn-soft:#33260f;--bad:#f87171;
 --in:#202734;--out:#173150;--shadow:none}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
 font:14px/1.45 "Source Sans Pro","Inter",-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{height:100vh;display:flex;flex-direction:column;gap:10px;padding:2px 2px 6px}
/* header */
.top{display:flex;align-items:center;gap:8px}
.top .pill{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:560px){.top h3{display:none}.lbl{display:none}}
.top h3{margin:0;font-size:15px;font-weight:700}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;border-radius:999px;padding:3px 10px;
 background:var(--panel);color:var(--muted);border:1px solid var(--line)}
.pill i{width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.live{color:var(--accent);background:var(--accent-soft);border-color:transparent}
.pill.live i{animation:pulse 1s infinite}
.pill.done{color:var(--good);background:var(--good-soft);border-color:transparent}
.sp{flex:1}
button{font:inherit;font-size:12px;white-space:nowrap;flex:none;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;
 padding:4px 10px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
/* message cards */
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px;box-shadow:var(--shadow)}
.who{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);margin-bottom:6px}
.ava{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-size:13px;color:#fff;font-weight:700;flex:none}
.ava.cust{background:#64748b}.ava.bot{background:linear-gradient(135deg,#2563eb,#7c3aed)}
.who b{color:var(--text);font-size:13px}
.new{margin-left:auto;white-space:nowrap;font-size:10.5px;font-weight:700;letter-spacing:.04em;color:#fff;background:var(--accent);border-radius:6px;
 padding:2px 7px;animation:blink 1.2s ease 3}
.bubble{border-radius:12px;padding:8px 11px;white-space:pre-wrap;word-wrap:break-word;font-size:13.5px}
.bubble.in{background:var(--in)}
.bubble.out{background:var(--out);max-height:120px;overflow:auto}
.photos{display:flex;gap:6px;margin-bottom:6px}.photos img{height:64px;border-radius:10px;border:1px solid var(--line)}
#msg{animation:slide .45s ease}
/* stepper */
.stepper{display:flex;align-items:flex-start;padding:2px 4px}
.stg{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;position:relative;font-size:11.5px;color:var(--muted);text-align:center}
.stg .dot{width:26px;height:26px;border-radius:50%;border:2px solid var(--line);background:var(--card);display:grid;place-items:center;
 font-size:12px;font-weight:700;z-index:1;transition:all .3s}
.stg::before{content:"";position:absolute;top:12px;left:-50%;width:100%;height:2px;background:var(--line);z-index:0}
.stg:first-child::before{display:none}
.stg.done .dot{background:var(--good);border-color:var(--good);color:#fff}
.stg.done::before,.stg.now::before{background:var(--good)}
.stg.now .dot{border-color:var(--accent);color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft)}
.stg.now{color:var(--accent);font-weight:700}.stg.done{color:var(--text)}
.stg.idle{opacity:.55}
/* timeline */
.feed{flex:1;min-height:0;overflow-y:auto;padding:2px 4px 2px 2px;scroll-behavior:smooth}
.row{display:flex;gap:10px;padding:5px 8px;border-radius:12px;animation:slide .3s ease;transition:background .3s}
.row .ic{width:28px;height:28px;border-radius:10px;background:var(--panel);border:1px solid var(--line);display:grid;place-items:center;
 font-size:16px;flex:none}
.row .bd{flex:1;min-width:0}
.row .nm{display:flex;align-items:center;gap:6px;font-weight:700;font-size:13px}
.row .st{font-size:11px;font-weight:600;border-radius:6px;padding:1px 6px}
.row .tx{font-size:13px;color:var(--text);margin-top:1px}
.row .tc{display:none;font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--muted);margin-top:3px;word-break:break-word}
.tech .row .tc{display:block}
.row.work{background:var(--accent-soft)}
.row.work .ic{border-color:var(--accent)}
.row.work .st{color:var(--accent);background:var(--card)}
.row.work .tx{color:var(--muted)}
.row.ok .st{color:var(--good);background:var(--good-soft)}
.row.err .st{color:var(--bad)}
.spin{width:11px;height:11px;border:2px solid var(--accent);border-right-color:transparent;border-radius:50%;display:inline-block;
 animation:spin .7s linear infinite;vertical-align:-1px}
.skipped{font-size:12px;color:var(--muted);padding:4px 8px}
/* outcome */
#out{display:none;animation:slide .45s ease}
.oc{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:12px;font-weight:600;border-radius:999px;padding:3px 10px}
.chip.good{color:var(--good);background:var(--good-soft)}.chip.warn{color:var(--warn);background:var(--warn-soft)}
.chip.info{color:var(--accent);background:var(--accent-soft)}
.foot{font-size:11.5px;color:var(--muted);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
label{display:inline-flex;gap:4px;align-items:center;cursor:pointer}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes slide{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="wrap">
  <div class="top">
    <h3>Live agent flow</h3><span class="pill" id="status"><i></i><span id="stxt">Waiting</span></span><span class="sp"></span>
    <button id="replay" title="Watch it again">↻<span class="lbl"> Replay</span></button><button id="skip" title="Show everything now">⏭<span class="lbl"> Skip</span></button>
  </div>

  <div class="card" id="msg">
    <div class="who"><div class="ava cust" id="ini"></div><div><b id="cust"></b> <span id="meta"></span></div><span class="new" id="newb">NEW MESSAGE</span></div>
    <div class="photos" id="photos"></div>
    <div class="bubble in" id="mtext"></div>
  </div>

  <div class="stepper" id="stepper"></div>
  <div class="feed" id="feed"></div>

  <div class="card" id="out">
    <div class="who"><div class="ava bot">🛟</div><div><b>SupportPilot</b> replied to the customer</div></div>
    <div class="bubble out" id="rtext"></div>
    <div class="oc" id="chips"></div>
  </div>
  <div class="foot"><span id="ftxt"></span><label><input type="checkbox" id="techT"> technical details</label></div>
</div>
<script>
const D = __DATA__, AUTOPLAY = __AUTOPLAY__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const STEP = 1000, WORK = 520;
let timers = [];

// Follow the host page's real theme (st.iframe is same-origin); the server-side hint is only a fallback.
function syncTheme() {
  try {
    const host = parent.document.querySelector(".stApp") || parent.document.body;
    const m = getComputedStyle(host).backgroundColor.match(/\d+(\.\d+)?/g);
    if (m) {
      const [r, g, b] = m.map(Number);
      document.documentElement.dataset.theme = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.5 ? "dark" : "light";
    }
  } catch (e) {}
}
syncTheme(); setInterval(syncTheme, 1500);

$("cust").textContent = D.customer + (D.tier ? " · " + D.tier : "");
$("meta").textContent = "via " + D.channel + (D.ts ? " · " + D.ts : "");
$("ini").textContent = (D.customer || "?").trim()[0].toUpperCase();
$("mtext").textContent = D.message;
$("photos").innerHTML = D.photos.map(p => `<img src="${p}" alt="photo from the customer">`).join("");
$("photos").style.display = D.photos.length ? "flex" : "none";
$("rtext").textContent = D.reply || "—";
$("techT").onchange = e => document.body.classList.toggle("tech", e.target.checked);
const stageIndex = k => D.stages.findIndex(s => s.key === k);
const used = new Set(D.steps.map(s => s.stage));

function setStatus(cls, text) { $("status").className = "pill " + cls; $("stxt").textContent = text; }
function drawStepper(current, doneUpTo) {
  $("stepper").innerHTML = D.stages.map((s, i) => {
    const cls = i < doneUpTo ? "done" : i === current ? "now" : (used.has(s.key) ? "" : "idle");
    return `<div class="stg ${cls}" title="${esc(s.sub)}"><div class="dot">${i < doneUpTo ? "✓" : i + 1}</div>${esc(s.title.replace(" & act", ""))}</div>`;
  }).join("");
}
function addRow(s, i, instant) {
  const row = document.createElement("div");
  row.className = "row " + (instant ? "" : "work"); row.id = "r" + i;
  row.innerHTML = `<div class="ic">${s.icon}</div><div class="bd"><div class="nm">${esc(s.name)}
    <span class="st"><span class="spin"></span> working</span></div>
    <div class="tx">Working on it…</div><div class="tc">${esc(s.tech)} · ${s.ms} ms</div></div>`;
  $("feed").appendChild(row); $("feed").scrollTop = $("feed").scrollHeight;
  return row;
}
function finishRow(row, s) {
  row.className = "row " + (s.ok ? "ok" : "err");
  row.querySelector(".st").textContent = s.ok ? "✓ done" : "✗ error";
  row.querySelector(".tx").textContent = s.text;
}
function showOutcome() {
  drawStepper(-1, D.stages.length);
  $("out").style.display = "block";
  const chips = [`<span class="chip ${D.tone}">${D.tone === "good" ? "✅" : D.tone === "warn" ? "🧑‍💼" : "💬"} ${esc(D.outcome)}</span>`];
  if (D.ticket) chips.push(`<span class="chip info">🎫 ${esc(D.ticket)}</span>`);
  if (D.email) chips.push(`<span class="chip ${D.email.status === "sent" ? "good" : "warn"}">📧 ${D.email.status === "sent" ? "Email sent to " + esc(D.email.to) : "Email " + esc(D.email.status.replace("_", " "))}</span>`);
  $("chips").innerHTML = chips.join("");
  if (D.skipped.length) {
    const sk = document.createElement("div"); sk.className = "skipped";
    sk.textContent = "Not needed for this message: " + D.skipped.map(s => s.name).join(", ");
    $("feed").appendChild(sk);
  }
  setStatus("done", `Done · ${D.steps.length} agent steps in ${D.total_ms} ms`);
}
function reset() {
  timers.forEach(clearTimeout); timers = [];
  $("feed").innerHTML = ""; $("out").style.display = "none";
  drawStepper(-1, 0);
}
function play() {
  reset();
  $("newb").style.display = "inline-block";
  setStatus("live", "New message received");
  D.steps.forEach((s, i) => {
    const t = 700 + i * STEP;
    timers.push(setTimeout(() => {
      const si = stageIndex(s.stage);
      drawStepper(si, si);
      setStatus("live", `Processing · step ${i + 1} of ${D.steps.length}`);
      const row = addRow(s, i);
      timers.push(setTimeout(() => finishRow(row, s), WORK));
    }, t));
  });
  timers.push(setTimeout(() => { $("newb").style.display = "none"; showOutcome(); },
                         700 + D.steps.length * STEP + 200));
}
function skip() {
  reset(); $("newb").style.display = "none";
  D.steps.forEach((s, i) => finishRow(addRow(s, i, true), s));
  $("feed").scrollTop = 0;
  showOutcome();
}
$("ftxt").textContent = `The agents finished in ${D.total_ms} ms — replayed step by step so you can follow it.`;
$("replay").onclick = play; $("skip").onclick = skip;
(AUTOPLAY && !matchMedia("(prefers-reduced-motion: reduce)").matches) ? play() : skip();
</script></body></html>"""
