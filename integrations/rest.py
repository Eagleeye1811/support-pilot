"""Adapter for a company's own REST API — how a real business plugs in without replacing its systems.

The company exposes (or wraps its existing services behind) a small HTTP contract:

    GET  {base_url}/{entity}/{id}                 → 200 record JSON | 404
    GET  {base_url}/{entity}?field=value&…        → 200 [records]  (or {"items": [records]})
    POST {base_url}/actions/{action}              body {"params": {...}, "customer_id": "..."}
                                                  → 200 {"data": {...}, "message": "customer-facing text"}
                                                  → 4xx {"error": "reason"}   (business refusal)

Entities are those in ``BUSINESS_ENTITIES`` that the company chooses to expose; actions are the ones it lists
in ``actions``. Authentication is a bearer token read from an environment variable, never stored in the database.
"""
from __future__ import annotations

from urllib.parse import quote

from .base import BUSINESS_ENTITIES, ActionError, AdapterError, CompanyAdapter


class RestAdapter(CompanyAdapter):
    simulated = False

    def __init__(self, base_url, token=None, actions=(), entities=BUSINESS_ENTITIES, name="REST API", timeout=10,
                 session=None):
        import requests
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.actions = set(actions)
        self._entities = tuple(e for e in entities if e in BUSINESS_ENTITIES)
        self.name = name
        self.description = f"Company REST API at {self.base_url}"
        self.timeout = timeout
        self.http = session or requests.Session()

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(self, method, path, **kw):
        try:
            resp = self.http.request(method, f"{self.base_url}/{path}", headers=self._headers(), timeout=self.timeout, **kw)
        except Exception as exc:
            raise AdapterError(f"{self.name} unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise AdapterError(f"{self.name} error HTTP {resp.status_code}")
        return resp

    def get(self, entity, entity_id):
        if entity not in self._entities or not entity_id:
            return None
        resp = self._request("GET", f"{entity}/{quote(str(entity_id), safe='')}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise AdapterError(f"{self.name} GET {entity} → HTTP {resp.status_code}")
        return resp.json()

    def find(self, entity, **filters):
        if entity not in self._entities:
            return []
        resp = self._request("GET", entity, params={k: v for k, v in filters.items() if v is not None})
        if resp.status_code != 200:
            raise AdapterError(f"{self.name} GET {entity} → HTTP {resp.status_code}")
        body = resp.json()
        return body.get("items", []) if isinstance(body, dict) else list(body)

    def perform(self, action, params, ctx):
        if action not in self.actions:
            raise ActionError(f"{self.name} does not support '{action}'", "blocked")
        resp = self._request("POST", f"actions/{quote(action, safe='')}",
                             json=dict(params=params, customer_id=(ctx or {}).get("customer_id")))
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.status_code != 200:
            raise ActionError(str(body.get("error") or f"HTTP {resp.status_code}"))
        return body.get("data") or {}, str(body.get("message") or "")

    def supported_actions(self):
        return set(self.actions)

    def entities(self):
        return self._entities
