"""The contract between SupportPilot and a company's own systems.

A company integrates by providing one ``CompanyAdapter``. SupportPilot never owns the business data: agents read
customers, orders, payments … through ``get`` / ``find``, and change anything only through ``perform``, after the
platform's policy engine has authorized the action. Support data (conversations, tickets, workflow runs, audit)
stays in SupportPilot's own store.
"""
from __future__ import annotations

# Business entities an adapter may expose. Agents can read nothing else from the company.
BUSINESS_ENTITIES = ("customers", "orders", "payments", "refunds", "accounts", "subscriptions", "service_lines", "outages",
                     "inventory", "coupons", "service_requests", "visits")


class ActionError(Exception):
    """A business operation was refused or failed. ``ActionError(msg, "blocked")`` marks a policy refusal."""


class AdapterError(Exception):
    """The company system could not be reached or answered with an error (not a business refusal)."""


class CompanyAdapter:
    name = "adapter"
    simulated = False
    description = ""

    def get(self, entity, entity_id):
        """One record by id, or None."""
        raise NotImplementedError

    def find(self, entity, **filters):
        """Records whose fields equal ``filters`` (all records when no filter is given)."""
        raise NotImplementedError

    def perform(self, action, params, ctx):
        """Run one authorized business operation; return ``(data, customer_message)`` or raise ActionError."""
        raise NotImplementedError

    def supported_actions(self):
        raise NotImplementedError

    def entities(self):
        return BUSINESS_ENTITIES

    def describe(self):
        return dict(name=self.name, simulated=self.simulated, description=self.description,
                    entities=list(self.entities()), actions=sorted(self.supported_actions()))
