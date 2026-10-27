"""Tenants and API keys — which companies use this SupportPilot, and who may call it.

The registry lives in its own SQLite file (``data/platform.db``, or ``SUPPORTPILOT_PLATFORM_DB``). Each tenant has:
  * its own SupportPilot store (separate SQLite file) — conversations, tickets, runs and audit of one company
    never share a database with another's, so isolation does not depend on remembering a filter;
  * an adapter spec (``demostore`` simulated backend, or ``rest`` for the company's own API);
  * a policy (AI refund limit, per-action auto / approval / disabled).
API keys are random 32-byte tokens shown once; only their SHA-256 hash is stored. Roles: ``channel`` (send
customer messages, read those conversations) and ``admin`` (tickets, approvals, runs, audit).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading

from . import audit
from .policy import Policy
from .shared_agent import COMPANY, ROOT, iso
from .store import Store, default_db_path
from .workspace import Workspace

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
ROLES = ("channel", "admin")
DEMO_TENANT = "demostore"


def platform_db_path():
    return os.getenv("SUPPORTPILOT_PLATFORM_DB") or os.path.join(ROOT, "data", "platform.db")


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


class Registry:
    def __init__(self, path=None, demo_db_path=None):
        self.store = Store(path or platform_db_path(), seed=False)
        self._workspaces = {}
        self._lock = threading.RLock()
        if not self.store.get("tenants", DEMO_TENANT):
            self.create_tenant(DEMO_TENANT, f"{COMPANY} (DemoStore simulation)", dict(type="demostore"),
                               db_path=demo_db_path or "default")

    # ---- tenants ----------------------------------------------------------------------------------
    def create_tenant(self, tenant_id, name, adapter, policy=None, db_path=None):
        if not TENANT_ID_RE.match(tenant_id or ""):
            raise ValueError("Tenant id must be 2-31 lowercase letters, digits or dashes")
        if self.store.get("tenants", tenant_id):
            raise ValueError(f"Tenant {tenant_id} already exists")
        if adapter.get("type") not in ("demostore", "rest"):
            raise ValueError("adapter.type must be 'demostore' or 'rest'")
        if adapter["type"] == "rest" and not adapter.get("base_url"):
            raise ValueError("A rest adapter needs base_url")
        rec = dict(id=tenant_id, name=name, adapter=adapter, policy=Policy.from_dict(policy).to_dict(),
                   db_path=db_path or os.path.join(ROOT, "data", "tenants", f"{tenant_id}.db"), created_at=iso(), active=True)
        self.store.put("tenants", tenant_id, rec)
        audit.log(self.store, "tenant.created", "operator", tenant_id=tenant_id, adapter=adapter.get("type"))
        return rec

    def tenants(self):
        return sorted(self.store.list("tenants"), key=lambda t: t["id"])

    def tenant(self, tenant_id):
        return self.store.get("tenants", tenant_id)

    def update_policy(self, tenant_id, policy):
        rec = self.tenant(tenant_id)
        if rec is None:
            raise KeyError(tenant_id)
        rec["policy"] = Policy.from_dict(policy).to_dict()
        self.store.put("tenants", tenant_id, rec)
        with self._lock:
            if tenant_id in self._workspaces:
                self._workspaces[tenant_id].policy = Policy.from_dict(rec["policy"])
        audit.log(self.store, "tenant.policy_updated", "operator", tenant_id=tenant_id)
        return rec

    def workspace(self, tenant_id):
        """The tenant's Workspace (its own store, adapter and policy), created once per process."""
        with self._lock:
            ws = self._workspaces.get(tenant_id)
            if ws is not None:
                return ws
            rec = self.tenant(tenant_id)
            if rec is None or not rec.get("active", True):
                raise KeyError(f"Unknown tenant {tenant_id}")
            from integrations import build_adapter
            path = default_db_path() if rec["db_path"] == "default" else rec["db_path"]  # "default" follows SUPPORTPILOT_DB
            if path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            store = Store(path, seed=rec["adapter"]["type"] == "demostore")
            ws = Workspace(store, build_adapter(rec["adapter"], store), dict(id=rec["id"], name=rec["name"]),
                           Policy.from_dict(rec.get("policy")))
            self._workspaces[tenant_id] = ws
            return ws

    # ---- API keys -----------------------------------------------------------------------------------
    def create_key(self, tenant_id, role="channel", label=""):
        """Returns (plaintext_key, record). The plaintext is not stored anywhere — show it once."""
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if not self.tenant(tenant_id):
            raise KeyError(f"Unknown tenant {tenant_id}")
        key = f"sp_{tenant_id}_{secrets.token_urlsafe(32)}"
        seq = self.store.next_seq("api_key", 1)
        rec = dict(id=f"KEY-{seq:04d}", tenant_id=tenant_id, role=role, label=label[:80], hash=_hash(key),
                   prefix=key[:len(tenant_id) + 8], created_at=iso(), revoked=False, last_used_at=None)
        self.store.put("api_keys", rec["id"], rec)
        audit.log(self.store, "api_key.created", "operator", tenant_id=tenant_id, key_id=rec["id"], role=role)
        return key, rec

    def keys(self, tenant_id=None):
        rows = self.store.list("api_keys", tenant_id=tenant_id) if tenant_id else self.store.list("api_keys")
        return [{k: v for k, v in r.items() if k != "hash"} for r in sorted(rows, key=lambda r: r["id"])]

    def revoke_key(self, key_id):
        rec = self.store.get("api_keys", key_id)
        if rec is None:
            raise KeyError(key_id)
        rec["revoked"] = True
        rec["revoked_at"] = iso()
        self.store.put("api_keys", key_id, rec)
        audit.log(self.store, "api_key.revoked", "operator", tenant_id=rec["tenant_id"], key_id=key_id)
        return rec

    def authenticate(self, key):
        """The active key record for ``key``, or None."""
        if not key or not key.startswith("sp_") or len(key) > 200:
            return None
        h = _hash(key)
        for rec in self.store.list("api_keys", hash=h):
            if hmac.compare_digest(rec["hash"], h) and not rec.get("revoked"):
                tenant = self.tenant(rec["tenant_id"])
                if tenant and tenant.get("active", True):
                    return rec
        return None

    def touch(self, rec):
        rec = self.store.get("api_keys", rec["id"]) or rec
        rec["last_used_at"] = iso()
        self.store.put("api_keys", rec["id"], rec)


_REGISTRY = None
_REG_LOCK = threading.Lock()


def get_registry():
    """Process-wide registry (Streamlit app, API server, MCP server, Telegram bot)."""
    global _REGISTRY
    with _REG_LOCK:
        if _REGISTRY is None:
            _REGISTRY = Registry()
        return _REGISTRY
