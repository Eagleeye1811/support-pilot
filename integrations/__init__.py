"""Company integration layer: one adapter per company system SupportPilot works with.

* ``DemoStoreAdapter`` — simulated ShopNova backend with synthetic data (demos, tests).
* ``RestAdapter`` — a real company's REST API.
"""
from .base import BUSINESS_ENTITIES, ActionError, AdapterError, CompanyAdapter
from .demostore import DemoStoreAdapter
from .rest import RestAdapter


def build_adapter(spec, store):
    """Create the adapter a tenant's configuration describes."""
    import os
    kind = (spec or {}).get("type", "demostore")
    if kind == "demostore":
        return DemoStoreAdapter(store)
    if kind == "rest":
        token = os.getenv(spec["token_env"]) if spec.get("token_env") else None
        return RestAdapter(spec["base_url"], token=token, actions=spec.get("actions", ()),
                           entities=spec.get("entities") or BUSINESS_ENTITIES, name=spec.get("name", "REST API"))
    raise ValueError(f"Unknown adapter type '{kind}'")


__all__ = ["BUSINESS_ENTITIES", "ActionError", "AdapterError", "CompanyAdapter", "DemoStoreAdapter", "RestAdapter",
           "build_adapter"]
