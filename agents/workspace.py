"""Workspace — one tenant's view of the world: SupportPilot's own store + the company's adapter + its policy.

Agents receive a Workspace wherever they used to receive a Store, with the same ``get`` / ``list`` / ``put`` API:
  * support collections (conversations, tickets, workflow_runs, audit, …) go to the tenant's SupportPilot store;
  * business collections (customers, orders, payments, …) are **read-only** and served by the company adapter.
    Each read is recorded as a tool call on the current workflow run. Writing them raises PermissionError —
    the only way to change company data is an authorized action (``action_agent.execute``).
"""
from __future__ import annotations

import time
import weakref

from . import runlog
from .policy import Policy
from .shared_agent import COMPANY

_DEFAULT_TENANT = dict(id="demostore", name=COMPANY)


class Workspace:
    def __init__(self, store, adapter=None, tenant=None, policy=None):
        if adapter is None:
            from integrations import DemoStoreAdapter
            adapter = DemoStoreAdapter(store)
        self.platform = store
        self.adapter = adapter
        self.tenant = dict(tenant or _DEFAULT_TENANT)
        self.policy = policy or Policy()

    # ---- routing ---------------------------------------------------------------------------------
    @property
    def path(self):
        return self.platform.path

    def is_business(self, collection):
        from integrations import BUSINESS_ENTITIES
        return collection in BUSINESS_ENTITIES

    def _read(self, op, collection, args, fn):
        name = f"{self.adapter.name}.{collection}.{op}"
        t0 = time.time()
        status, summary = "ok", {}
        try:
            if collection not in self.adapter.entities():
                status, result = "denied", (None if op == "get" else [])
                summary = dict(reason=f"{collection} is not exposed by {self.adapter.name}")
            else:
                result = fn()
                if op == "get":
                    summary = dict(found=result is not None, status=(result or {}).get("status"))
                    status = "ok" if result is not None else "not_found"
                else:
                    summary = dict(count=len(result), ids=[str(r.get("id")) for r in result[:10]])
            return result
        except Exception as exc:
            status, summary = "error", dict(error=f"{type(exc).__name__}: {exc}"[:200])
            raise
        finally:
            runlog.record_tool(dict(tool=name, kind="read", args=args, status=status, result=summary,
                                    ms=round((time.time() - t0) * 1000, 2), simulated=self.adapter.simulated))

    def get(self, collection, doc_id):
        if self.is_business(collection):
            return self._read("get", collection, dict(id=doc_id), lambda: self.adapter.get(collection, doc_id))
        return self.platform.get(collection, doc_id)

    def list(self, collection, where=None, **equals):
        if self.is_business(collection):
            docs = self._read("find", collection, dict(equals), lambda: self.adapter.find(collection, **equals))
            return [d for d in docs if where(d)] if where else docs
        return self.platform.list(collection, where=where, **equals)

    def _guard(self, collection):
        if self.is_business(collection):
            raise PermissionError(f"'{collection}' belongs to {self.adapter.name} and is read-only for agents — "
                                  "change it through an authorized action")

    def put(self, collection, doc_id, data):
        self._guard(collection)
        return self.platform.put(collection, doc_id, data)

    def put_many(self, collection, docs, key="id"):
        self._guard(collection)
        return self.platform.put_many(collection, docs, key)

    def update(self, collection, doc_id, **fields):
        self._guard(collection)
        return self.platform.update(collection, doc_id, **fields)

    def delete(self, collection, doc_id):
        self._guard(collection)
        return self.platform.delete(collection, doc_id)

    def claim(self, collection, doc_id, data=None):
        self._guard(collection)
        return self.platform.claim(collection, doc_id, data)

    def next_seq(self, name, start=1):
        return self.platform.next_seq(name, start)

    def reset(self, world=None):
        self.platform.reset(world)
        return self

    def stats(self):
        return self.platform.stats()

    def collections(self):
        return self.platform.collections()

    def describe(self):
        return dict(tenant=self.tenant, adapter=self.adapter.describe(), policy=self.policy.to_dict())


_wrapped = weakref.WeakKeyDictionary()


def as_workspace(store):
    """A Workspace for ``store``; a bare Store gets the simulated DemoStore adapter and the default policy."""
    if isinstance(store, Workspace):
        return store
    ws = _wrapped.get(store)
    if ws is None:
        ws = _wrapped[store] = Workspace(store)
    return ws
