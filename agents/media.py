"""Product images for emails.

Illustrations of the catalog live in ``assets/products/<SKU>.png``. ``ticket_images`` picks the
ones that belong to a case, e.g. *Ordered* vs *Received* for a wrong item.
"""
import base64
import os

from .shared_agent import ROOT, iso, new_id

ASSETS = os.path.join(ROOT, "assets", "products")
ORDER_INTENTS = {"wrong_item", "damaged_item", "delivery_delay", "cancel_order", "return_request", "refund_request",
                 "refund_status", "payment_failure", "double_charge"}


def product_image_path(sku):
    path = os.path.join(ASSETS, f"{sku}.png")
    return path if os.path.exists(path) else None


def product_image_bytes(sku):
    path = product_image_path(sku)
    if not path:
        return None
    with open(path, "rb") as f:
        return f.read()


def ticket_images(store, intent, slots):
    """[{sku, name, role}] for the order the investigation identified — never a guess."""
    order = store.get("orders", (slots or {}).get("order_id")) if (slots or {}).get("order_id") else None
    if intent not in ORDER_INTENTS or not order:
        return []
    ordered, delivered = order.get("items") or [], order.get("delivered_items") or []
    if intent == "wrong_item" and {i["sku"] for i in delivered} != {i["sku"] for i in ordered}:
        picks = [dict(sku=i["sku"], name=i["name"], role="Ordered") for i in ordered[:1]] + \
                [dict(sku=i["sku"], name=i["name"], role="Received") for i in delivered[:1]]
    else:
        picks = [dict(sku=i["sku"], name=i["name"], role="Your order") for i in ordered[:2]]
    return [p for p in picks if product_image_path(p["sku"])]


# ---- customer photos -------------------------------------------------------------------------
MAX_ATTACHMENT_BYTES = 1_000_000


def save_attachment(store, raw, mime="image/jpeg", source="telegram", caption=""):
    """Store a customer photo (base64) as evidence; returns the attachment document."""
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Photo is too large (max 1 MB)")
    att = dict(id=new_id("IMG"), mime=mime, source=source, caption=caption, size=len(raw), created_at=iso(),
               data=base64.b64encode(raw).decode())
    store.put("attachments", att["id"], att)
    return att
