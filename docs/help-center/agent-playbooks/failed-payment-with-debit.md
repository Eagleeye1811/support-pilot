---
id: KB-004
title: Playbook: failed payment with a bank debit
type: troubleshooting
category: Billing
audience: internal
intents: payment_failure
owner: Payments
reliability: 0.94
last_reviewed: 2026-09-18
steps: Locate order and payment | Check gateway status | Confirm bank debit | Check gateway health for an incident | Initiate auto-reversal refund (within AI refund limit) | Notify customer
---

# Playbook: failed payment with a bank debit

Use when a customer reports a failed payment but a debit. Never ask the customer to pay again before the gateway status is known. If the gateway shows the payment *captured*, reconcile instead of refunding. Refunds above ₹10,000 are queued for Payments approval.
