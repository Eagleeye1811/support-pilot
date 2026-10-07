"""Guided, context-aware "wrong item" conversation for the Telegram bot.

Instead of answering a wrong-item complaint in one shot, the bot talks it through:

  1. find the customer's recent delivered order(s) and show the product: "Is this the order?"
     (guests are asked for an order ID, phone or email first)
  2. ask for a photo of the item that arrived, with a short description
  3. run the full agent pipeline with the photo as evidence → dispatch-scan check, replacement,
     return pickup, ticket
  4. offer a confirmation email (or send it straight away if we already have the address)
  5. only then ask for a rating

The dialog state lives on the chat document (``doc["flow"]``); every step is written to the
conversation transcript and to the turn log, so the website shows it like any other turn.
"""
from __future__ import annotations

import re
from datetime import timedelta

import agents as A
from agents import context_agent, intent_agent, memory_agent
from agents.media import product_image_bytes
from agents.orchestrator_agent import Tracer
from agents.shared_agent import fmt_ts, now, parse_ts

WINDOW_DAYS = 30  # how far back to look for the order the customer means
YES = re.compile(r"^\s*(y|yes|yeah|yep|yup|correct|right|that'?s it|that one|ok|okay|sure|haan|ha)\b", re.I)
NO = re.compile(r"^\s*(n|no|nope|not this|wrong order|different|another)\b", re.I)
SKIP = re.compile(r"^\s*(skip|no photo|don'?t have|can'?t|cannot|no camera|proceed|continue|go ahead|carry on|"
                  r"done|next|move on)\b", re.I)


class WrongItemFlow:
    """Mixin for TelegramBot: the guided wrong-item dialog."""

    # ---- helpers -------------------------------------------------------------------------------
    def _conv(self, doc):
        conv = memory_agent.load_conversation(self.store, doc.get("conv_id"), doc.get("customer_id"), "telegram")
        doc["conv_id"] = conv["id"]
        return conv

    def _say(self, doc, text, buttons=None, user_text=None, photo_sku=None, caption=None):
        """Send a dialog message and keep it in the conversation transcript."""
        conv = self._conv(doc)
        if user_text:
            memory_agent.add_message(conv, "user", user_text, dict(channel="telegram"))
        memory_agent.add_message(conv, "assistant", text if not caption else f"{caption}\n{text}", dict(flow="wrong_item"))
        memory_agent.save(self.store, conv)
        self.save_chat(doc)
        if photo_sku and product_image_bytes(photo_sku):
            return self.send_photo(doc["chat_id"], photo_sku, f"{caption}\n\n{text}" if caption else text, buttons)
        return self.send(doc["chat_id"], text, buttons)

    def _log(self, doc, text, trace, **result):
        result.setdefault("conversation_id", doc.get("conv_id"))
        result.setdefault("channel", "telegram")
        self.record(doc, text, dict(result, trace=trace))

    def _recent_orders(self, cid):
        cutoff = now() - timedelta(days=WINDOW_DAYS)
        orders = [o for o in self.store.list("orders", customer_id=cid)
                  if o.get("status") == "delivered" and o.get("delivered_at") and parse_ts(o["delivered_at"]) >= cutoff]
        return sorted(orders, key=lambda o: o["delivered_at"], reverse=True)

    @staticmethod
    def _items(order):
        return ", ".join(i["name"] for i in order["items"])

    def _end(self, doc):
        doc.pop("flow", None)
        self.save_chat(doc)

    # ---- 1 · start: who is this, which order? ----------------------------------------------------
    def wi_start(self, doc, text, understanding):
        T = Tracer()
        T.note("1 Conversation Understanding",
               f"{understanding['intent']} · {understanding['category']} · conf {understanding['confidence']:.0%}")
        ents = understanding.get("entities") or {}
        order = self.store.get("orders", ents["order_id"]) if ents.get("order_id") else None
        cid = doc.get("customer_id") or (order or {}).get("customer_id")
        if not cid:
            cid, _ = context_agent.resolve_customer_id(self.store, None, ents, {})
        ctx = T.run("2 Customer Context", lambda: A.customer_context(self.store, cid) if cid else None,
                    lambda r: r["headline"] if r else "customer not identified yet")
        if cid and not doc.get("customer_id"):
            doc["customer_id"] = cid
        doc["flow"] = dict(name="wrong_item", step="identify", tries=0)
        if order:
            return self._confirm(doc, order, T, text, ctx, user_text=text)
        if not cid:
            T.note("6 Conversation Memory", "Waiting for an order ID, phone or email to find the order")
            self._log(doc, text, T.steps, understanding=understanding, mode="new")
            return self._say(doc, "Sorry about that! 😟 Let me find your order first. Please send the *order ID* (like ORD12345) "
                                  "or the phone number / email you ordered with.", user_text=text)
        return self._offer_orders(doc, cid, T, text, ctx, understanding, user_text=text)

    def _offer_orders(self, doc, cid, T, text, ctx, understanding=None, user_text=None):
        orders = T.run("4 Troubleshooting", lambda: self._recent_orders(cid),
                       lambda r: f"{len(r)} order(s) delivered in the last {WINDOW_DAYS} days")
        if not orders:
            T.note("6 Conversation Memory", "No recent delivered order — asking for the order ID")
            self._log(doc, text, T.steps, understanding=understanding, context=ctx, mode="new")
            return self._say(doc, f"I can't find an order delivered to you in the last {WINDOW_DAYS} days. "
                                  "Could you send the *order ID* (like ORD12345)?", user_text=user_text)
        doc["flow"]["candidates"] = [o["id"] for o in orders[:4]]
        return self._confirm(doc, orders[0], T, text, ctx, understanding, user_text=user_text)

    def _confirm(self, doc, order, T, text, ctx, understanding=None, user_text=None):
        doc["flow"].update(step="confirm", order_id=order["id"])
        T.note("6 Conversation Memory", f"Found {order['id']} — asking the customer to confirm it")
        self._log(doc, text, T.steps, understanding=understanding, context=ctx, mode="new",
                  reply=f"Is this the order? {order['id']}: {self._items(order)}")
        name = (ctx or {}).get("first_name")
        caption = f"📦 {order['id']} · {self._items(order)}\nDelivered {fmt_ts(order['delivered_at'])}"
        buttons = [[{"text": "✅ Yes, that's the order", "callback_data": "wi:yes"}],
                   [{"text": "🔁 A different order", "callback_data": "wi:other"}]]
        return self._say(doc, f"I'm sorry about the mix-up{', ' + name if name else ''}. I can see this delivery on your "
                              "account — is this the order you're writing about?", buttons, user_text=user_text,
                         photo_sku=order["items"][0]["sku"], caption=caption)

    # ---- 2 · continue the dialog ------------------------------------------------------------------
    def wi_continue(self, doc, text=None, photo_att=None, data=None):
        """Handle the next message of an active wrong-item dialog. Returns False to hand back to the normal flow."""
        flow = doc.get("flow") or {}
        step = flow.get("step")
        if text and not data and step in ("identify", "confirm"):
            u = intent_agent.understand(text, use_llm=False)
            if u["intent"] not in ("wrong_item", "general_inquiry", "affirm", "deny") and u["confidence"] >= 0.6 \
                    and not u["entities"].get("order_id"):
                self._end(doc)  # the customer moved on to a different problem
                return False
        if step == "identify":
            return self._identify(doc, text or "")
        if step == "confirm":
            return self._confirm_reply(doc, text, data)
        if step == "photo":
            return self._photo_step(doc, text, photo_att)
        return False

    def _identify(self, doc, text):
        ents = intent_agent.extract_entities(text)
        order = self.store.get("orders", ents["order_id"]) if ents.get("order_id") else None
        cid = (order or {}).get("customer_id") or context_agent.resolve_customer_id(self.store, doc.get("customer_id"), ents, {})[0]
        if not cid:
            doc["flow"]["tries"] = doc["flow"].get("tries", 0) + 1
            if doc["flow"]["tries"] >= 3:
                self._end(doc)
                return self._say(doc, "I couldn't find that order. You can pick a demo customer with /login and try again.",
                                 user_text=text) or True
            self.save_chat(doc)
            return self._say(doc, "I couldn't match that to an order. Please send the order ID (like ORD12345), "
                                  "or the phone number / email used for the order.", user_text=text) or True
        doc["customer_id"] = doc.get("customer_id") or cid
        T = Tracer()
        ctx = T.run("2 Customer Context", lambda: A.customer_context(self.store, cid), lambda r: r["headline"])
        if order:
            return self._confirm(doc, order, T, text, ctx, user_text=text) or True
        return self._offer_orders(doc, cid, T, text, ctx, user_text=text) or True

    def _confirm_reply(self, doc, text, data):
        flow = doc["flow"]
        if data and data.startswith("wi:order:"):
            order = self.store.get("orders", data.split(":", 2)[2])
            T = Tracer()
            ctx = A.customer_context(self.store, doc.get("customer_id")) if doc.get("customer_id") else None
            return self._confirm(doc, order, T, f"Chose {order['id']}", ctx, user_text=f"It's {order['id']}") or True
        ents = intent_agent.extract_entities(text or "")
        if ents.get("order_id") and self.store.get("orders", ents["order_id"]):
            order = self.store.get("orders", ents["order_id"])
            ctx = A.customer_context(self.store, order["customer_id"])
            return self._confirm(doc, order, Tracer(), text, ctx, user_text=text) or True
        if data == "wi:yes" or (text and YES.match(text)):
            flow["step"] = "photo"
            order = self.store.get("orders", flow["order_id"])
            if doc.get("pending_photo"):  # they already sent a photo before we asked
                return self._finish(doc, doc.pop("pending_photo"), doc.pop("pending_caption", ""), user_text="Yes, that's the order")
            T = Tracer()
            T.note("6 Conversation Memory", f"Order {order['id']} confirmed — waiting for a photo of the received item")
            self._log(doc, "Yes, that's the order", T.steps, reply="Asked for a photo of the received item")
            return self._say(doc, "Thanks for confirming. 📸 Please send a *photo of the item you received*, and add a short "
                                  "description in the caption (for example: “got blue earbuds instead of headphones”).\n\n"
                                  "No photo? Just describe what arrived, or type *skip*.",
                             user_text=None if data else text) or True
        if data == "wi:other" or (text and NO.match(text)):
            others = [o for o in flow.get("candidates", []) if o != flow.get("order_id")]
            buttons = [[{"text": f"📦 {oid} · {self._items(self.store.get('orders', oid))[:40]}",
                         "callback_data": f"wi:order:{oid}"}] for oid in others[:3]]
            flow["step"] = "confirm"
            self.save_chat(doc)
            msg = "No problem. Which order is it? Tap one below, or type the order ID." if buttons else \
                "No problem — please type the order ID (like ORD12345)."
            return self._say(doc, msg, buttons or None, user_text=None if data else text) or True
        return self._say(doc, "Please tap *Yes* if this is the order, or *A different order* — or type the order ID.",
                         [[{"text": "✅ Yes, that's the order", "callback_data": "wi:yes"}],
                          [{"text": "🔁 A different order", "callback_data": "wi:other"}]], user_text=text) or True

    def _photo_step(self, doc, text, photo_att):
        flow = doc["flow"]
        if photo_att:
            desc = (text or "").strip() or flow.get("description", "")
            return self._finish(doc, photo_att, desc)
        if text and SKIP.match(text):
            return self._finish(doc, None, flow.get("description", ""), user_text=text)
        if text:  # a description without a photo: keep it and ask once more for the picture
            if flow.get("description"):
                return self._finish(doc, None, f"{flow['description']} {text}", user_text=text)
            flow["description"] = text.strip()
            self.save_chat(doc)
            return self._say(doc, "Got it, thanks. Could you also attach a *photo* of the item? It speeds things up. "
                                  "(Type *skip* to continue without one.)", user_text=text) or True
        return True

    # ---- 3 · resolve with the full agent pipeline ----------------------------------------------------
    def _finish(self, doc, att_id, description, user_text=None):
        order_id = doc["flow"]["order_id"]
        self._end(doc)
        text = f"I received the wrong item for order {order_id}." + (f" {description}" if description else "")
        self._say(doc, ("Thanks for the photo 🙏 " if att_id else "Thanks! ") +
                  f"Let me check it against order {order_id} and our warehouse dispatch records…", user_text=user_text)
        r = self.run_turn(doc, text, None, attachments=[att_id] if att_id else None, defer_csat=True, offer_email=False)
        self.after_ticket(doc, r)
        return True

    # ---- 4 + 5 · email, then rating --------------------------------------------------------------------
    def after_ticket(self, doc, r):
        """Confirmation email first (ask for the address if we don't have it), then the rating."""
        doc["rating_pending"] = r.get("awaiting") == "csat"
        if r.get("email"):
            e = r["email"]
            self.send(doc["chat_id"], f"📧 Confirmation email sent to {e['to_masked']}." if e["status"] == "sent" else
                      f"📧 I couldn't send the confirmation email ({e.get('error') or e['status']}).")
            return self.ask_rating(doc)
        doc["awaiting_email"] = True
        self.save_chat(doc)
        return self.send(doc["chat_id"], "📧 Would you like a *confirmation email* with these details? "
                                         "Reply with your email address.", [[{"text": "No thanks", "callback_data": "email:skip"}]])

    def ask_rating(self, doc):
        if not doc.pop("rating_pending", False):
            self.save_chat(doc)
            return None
        self.save_chat(doc)
        return self.send(doc["chat_id"], "One last thing — how would you rate this support experience?",
                         [[{"text": "⭐" * i, "callback_data": f"csat:{i}"} for i in range(1, 6)]])
