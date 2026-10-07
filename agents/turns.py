"""Turn log — every processed message from any channel, newest last, for the live agent-flow view."""
from .shared_agent import iso

KEEP = 200


def record_turn(store, source, text, result, chat_id=None):
    """Store one orchestrated turn (the full result minus the conversation object)."""
    seq = store.next_seq("turn", 1)
    result = {k: v for k, v in result.items() if k != "conversation"}
    doc = dict(id=f"{seq:06d}", seq=seq, source=source, chat_id=chat_id, conv_id=result.get("conversation_id"),
               text=text, ts=iso(), result=result)
    store.put("turns", doc["id"], doc)
    if seq > KEEP:  # keep the log small: the website only replays recent turns
        store.delete("turns", f"{seq - KEEP:06d}")
    return doc


def latest_seq(store):
    """Sequence number of the newest turn — a single counter read, cheap enough to poll every few seconds."""
    return store.peek_seq("turn")


def recent_turns(store, limit=30, **equals):
    """The newest turns (oldest first). Reads only the documents it needs instead of the whole log."""
    if equals:
        return sorted(store.list("turns", **equals), key=lambda t: t["seq"])[-limit:]
    last = latest_seq(store)
    docs = (store.get("turns", f"{seq:06d}") for seq in range(max(1, last - limit + 1), last + 1))
    return [d for d in docs if d]
