"""SupportPilot operator CLI — tenants, API keys and an LLM connectivity check.

    python manage.py tenants
    python manage.py create-key demostore --role admin --label "helpdesk"
    python manage.py keys [tenant]
    python manage.py revoke-key KEY-0001
    python manage.py create-tenant acme "Acme Corp" --rest-url https://api.acme.example/support \
                                   --token-env ACME_API_TOKEN --actions initiate_refund,cancel_order
    python manage.py set-mode demostore create_replacement approval      # auto | approval | disabled
    python manage.py llm-check
    python manage.py analytics-run demostore [--seed 7] [--anchor 2026-10-27T12:00:00+00:00]
                                   [--mastodon mastodon.social:customerservice --mastodon-limit 120]
    python manage.py recommendations demostore
    python manage.py decide demostore REC-1234ABCD approve --approver "Kavya Menon" --note "go"
"""
import argparse
import json
import sys

import agents as A
from agents import llm
from agents.tenancy import get_registry


def main(argv=None):
    p = argparse.ArgumentParser(description="SupportPilot operator CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tenants")
    k = sub.add_parser("create-key")
    k.add_argument("tenant")
    k.add_argument("--role", choices=["channel", "admin"], default="channel")
    k.add_argument("--label", default="")
    ks = sub.add_parser("keys")
    ks.add_argument("tenant", nargs="?")
    r = sub.add_parser("revoke-key")
    r.add_argument("key_id")
    t = sub.add_parser("create-tenant")
    t.add_argument("tenant")
    t.add_argument("name")
    t.add_argument("--rest-url", help="Company API base URL (omit for a simulated DemoStore tenant)")
    t.add_argument("--token-env", help="Environment variable holding the company API token")
    t.add_argument("--actions", default="", help="Comma-separated actions the company API supports")
    m = sub.add_parser("set-mode")
    m.add_argument("tenant")
    m.add_argument("action", choices=sorted(A.ACTIONS))
    m.add_argument("mode", choices=["auto", "approval", "disabled"])
    sub.add_parser("llm-check")
    an = sub.add_parser("analytics-run")
    an.add_argument("tenant", nargs="?", default="demostore")
    an.add_argument("--seed", type=int, default=7)
    an.add_argument("--anchor", help="ISO timestamp the synthetic history ends at (default: now) — fix it to reproduce a run")
    an.add_argument("--mastodon", help="instance:hashtag — also fetch real public posts, e.g. mastodon.social:customerservice")
    an.add_argument("--mastodon-limit", type=int, default=120)
    rc = sub.add_parser("recommendations")
    rc.add_argument("tenant", nargs="?", default="demostore")
    dc = sub.add_parser("decide")
    dc.add_argument("tenant")
    dc.add_argument("rec_id")
    dc.add_argument("decision", choices=["approve", "reject"])
    dc.add_argument("--approver", required=True)
    dc.add_argument("--note", default="")
    args = p.parse_args(argv)
    reg = get_registry()

    if args.cmd == "tenants":
        for t in reg.tenants():
            print(f"{t['id']:<14} {t['name']:<40} adapter={t['adapter']['type']:<9} db={t['db_path']}")
    elif args.cmd == "create-key":
        key, rec = reg.create_key(args.tenant, args.role, args.label)
        print(f"Created {rec['id']} ({rec['role']}) for {rec['tenant_id']}. Store this key now — it is not shown again:\n{key}")
    elif args.cmd == "keys":
        for rec in reg.keys(args.tenant):
            print(f"{rec['id']}  {rec['tenant_id']:<12} {rec['role']:<8} {rec['prefix']}…  revoked={rec['revoked']}  "
                  f"last used {rec.get('last_used_at') or 'never'}  {rec.get('label', '')}")
    elif args.cmd == "revoke-key":
        print("Revoked", reg.revoke_key(args.key_id)["id"])
    elif args.cmd == "create-tenant":
        adapter = dict(type="demostore") if not args.rest_url else dict(
            type="rest", base_url=args.rest_url, token_env=args.token_env,
            actions=[a for a in args.actions.split(",") if a])
        print(json.dumps(reg.create_tenant(args.tenant, args.name, adapter), indent=2))
    elif args.cmd == "set-mode":
        rec = reg.tenant(args.tenant)
        if rec is None:
            sys.exit(f"Unknown tenant {args.tenant}")
        policy = dict(rec.get("policy") or {})
        policy.setdefault("action_modes", {})[args.action] = args.mode
        reg.update_policy(args.tenant, policy)
        print(f"{args.tenant}: {args.action} → {args.mode}")
    elif args.cmd == "analytics-run":
        import datetime as dt
        from analytics import pipeline
        from analytics.social_sources import MastodonSource
        sources = []
        if args.mastodon:
            inst, _, tag = args.mastodon.partition(":")
            sources.append(MastodonSource(inst, tag or "customerservice", args.mastodon_limit))
        anchor = dt.datetime.fromisoformat(args.anchor) if args.anchor else None
        rep = pipeline.run(reg.workspace(args.tenant), seed=args.seed, anchor=anchor, social_sources=sources or None,
                           actor="manage.py")
        print(json.dumps({k: rep[k] for k in ("run_id", "dataset_hash", "synthetic", "counts", "sources", "seconds", "validation")},
                         indent=2, default=str))
        print(f"{len(rep['recommendations'])} proposed recommendation(s): python manage.py recommendations {args.tenant}")
    elif args.cmd == "recommendations":
        from analytics import prescriptive
        for r in prescriptive.listing(reg.workspace(args.tenant)):
            print(f"{r['id']}  {r['status']:<9} #{r.get('rank', '-'):<3} score {r['score']:<6} {r['title']}")
    elif args.cmd == "decide":
        from analytics import prescriptive
        r = prescriptive.decide(reg.workspace(args.tenant), args.rec_id, args.decision == "approve", args.approver, args.note,
                                registry=reg)
        print(f"{r['id']} {r['status']}", r.get("applied") or "")
    elif args.cmd == "llm-check":
        st = llm.status()
        print(f"Provider {st['provider']} · model {st['model']} · configured={st['configured']} · mode={st['mode']} ({st['reason']})")
        if st["configured"] and st["mode"] != "fallback":
            res = llm.chat('Reply with the JSON {"ok": true}', json_mode=True, max_tokens=20, purpose="health-check")
            print("Live call:", "OK" if res.ok else f"FAILED — {res.error}", f"({res.ms:.0f} ms)")
            sys.exit(0 if res.ok else 1)


if __name__ == "__main__":
    main()
