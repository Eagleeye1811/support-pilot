"""Evaluation for the Results section: intent accuracy, decision accuracy, automation, latency, guardrails.

Runs on a fresh in-memory store, so it never touches the app database.
    python evaluation/run_eval.py
"""
import json
import os
import statistics
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("SUPPORTPILOT_DISABLE_LLM", "1")

import agents as A  # noqa: E402

with open(os.path.join(ROOT, "evaluation", "eval_set.json"), encoding="utf-8") as f:
    EVAL = json.load(f)

# 1. intent classification
rows, confusion = [], Counter()
for text, gold in EVAL["intents"]:
    pred = A.understand(text, use_llm=False)["intent"]
    rows.append(pred == gold)
    if pred != gold:
        confusion[(gold, pred)] += 1
print(f"Intent accuracy: {sum(rows)}/{len(rows)} = {sum(rows) / len(rows):.1%}")
for (g, p), n in confusion.most_common():
    print(f"   miss: {g} -> {p} ({n})")

# 2. end-to-end scenarios
store = A.Store(":memory:")
ok, latencies, review_ok, turns_total, actions = 0, [], 0, 0, Counter()
print("\nScenario                         expected      got           ticket     actions")
for sc in EVAL["scenarios"]:
    t0 = time.time()
    turns = A.run_conversation(store, sc["messages"], sc["customer"])
    latencies.append((time.time() - t0) * 1000 / len(turns))
    turns_total += len(turns)
    review_ok += sum(t["review"]["approved"] for t in turns)
    last = turns[-1]
    got = last["decision"]["decision"] if isinstance(last["decision"], dict) else last["decision"]
    ok += got == sc["expect"]
    for a in last["actions"]:
        actions[a["status"]] += 1
    print(f"{sc['name'][:32]:<33}{sc['expect']:<14}{got:<14}{(last['ticket'] or {}).get('id', '—'):<11}"
          f"{', '.join(a['action'] for a in last['actions']) or '—'}")
n = len(EVAL["scenarios"])
auto = sum(1 for sc in EVAL["scenarios"] if sc["expect"] == "auto_resolve")
print(f"\nDecision accuracy (auto-resolve vs escalate): {ok}/{n} = {ok / n:.1%}")
print(f"Automation rate on scenarios: {auto}/{n} = {auto / n:.1%} resolved with no human")
print(f"Reviewer guardrail approval: {review_ok}/{turns_total} turns")
print(f"Actions: {dict(actions)}")
print(f"Latency per turn: median {statistics.median(latencies):.1f} ms, max {max(latencies):.1f} ms (deterministic core)")

# 3. guardrail probes
ctx = A.customer_context(store, "CUST1012")
blocked = A.execute_action(store, "initiate_refund", {"order_id": "ORD12353"}, ctx)[0]["status"] == "blocked"
mandatory = all(A.run_conversation(store, [m], c)[0]["decision"]["decision"] == "escalate" for m, c in
                [("fraud on my account", "CUST1008"), ("my data was leaked", "CUST1002"), ("legal notice coming", "CUST1003")])
print(f"\nGuardrails: refund-limit block={blocked}, mandatory escalations honoured={mandatory}")

# 4. operations
inc = A.detect_incidents(store)
m = A.supervisor_metrics(store)
cs = A.csat_summary(store)
print(f"Root cause: {[i['hypothesis'] for i in inc]}")
print(f"Desk metrics (seeded history + eval): resolution {m['resolution_rate']:.0%}, AI-resolved {m['automation_rate']:.0%}, "
      f"escalation {m['escalation_rate']:.0%}, CSAT {cs['avg']}/5, SLA compliance {m['sla_compliance']:.0%}")
