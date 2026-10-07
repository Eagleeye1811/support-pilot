"""Media Agent — product images for replies, emails and the website, plus customer photo attachments.

Product illustrations live in ``assets/products/<SKU>.png`` (drawn by
``scripts/render_product_images.py``). Customer photos (e.g. a damaged item sent
from Telegram) are stored base64 in the ``attachments`` collection.
"""
import base64
import os
from typing import Any, Optional

from .shared_agent import ROOT, iso, new_id
from .tools import register

ASSETS = os.path.join(ROOT, "assets", "products")
ORDER_INTENTS = {"wrong_item", "damaged_item", "delivery_delay", "cancel_order", "return_request", "refund_request",
                 "refund_status", "payment_failure", "double_charge"}
MAX_ATTACHMENT_BYTES = 1_000_000


def product_image_path(sku):
    path = os.path.join(ASSETS, f"{sku}.png")
    return path if os.path.exists(path) else None


def product_image_bytes(sku):
    path = product_image_path(sku)
    if not path:
        return None
    with open(path, "rb") as f:
        return f.read()


def data_uri(raw, mime="image/png"):
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}" if raw else None


def product_data_uri(sku):
    return data_uri(product_image_bytes(sku))


# ---- customer photos -----------------------------------------------------------------------
def save_attachment(store, raw, mime="image/jpeg", source="telegram", caption=""):
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Photo is too large (max 1 MB)")
    att = dict(id=new_id("IMG"), mime=mime, source=source, caption=caption, size=len(raw), created_at=iso(),
               data=base64.b64encode(raw).decode())
    store.put("attachments", att["id"], att)
    return att


def attachment_data_uri(store, att_id):
    att = store.get("attachments", att_id)
    return f"data:{att['mime']};base64,{att['data']}" if att else None


# ---- the MCP tool ----------------------------------------------------------------------------
def _order(store, slots):
    """Only the order the investigation identified — never a guess, so the picture always matches the case."""
    oid = (slots or {}).get("order_id")
    return store.get("orders", oid) if oid else None


def select_media(store, intent: str, investigation: Optional[dict[str, Any]] = None, context: Optional[dict[str, Any]] = None,
                 slots: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Media Agent: pick the product images that belong in the reply (e.g. ordered vs received for a wrong item)."""
    if intent not in ORDER_INTENTS:
        return []
    order = _order(store, slots)
    if not order:
        return []
    picks = []
    ordered = order.get("items") or []
    delivered = order.get("delivered_items") or []
    if intent == "wrong_item" and {i["sku"] for i in delivered} != {i["sku"] for i in ordered}:
        picks += [dict(sku=i["sku"], name=i["name"], role="Ordered") for i in ordered[:1]]
        picks += [dict(sku=i["sku"], name=i["name"], role="Received") for i in delivered[:1]]
    else:
        picks += [dict(sku=i["sku"], name=i["name"], role="Your order") for i in ordered[:2]]
    for p in picks:
        p["order_id"] = order["id"]
        p["caption"] = f"{p['role']}: {p['name']} ({order['id']})"
    return [p for p in picks if product_image_path(p["sku"])]


register("select_media", select_media, "Media Agent")
