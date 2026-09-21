"""Incremental, peer-scoped polling with transactional page checkpoints."""

from .messages import index_message
from .storage import utcnow


MAX_ID = 2147483648


class Synchronizer:
    def __init__(self, service):
        self.service = service

    async def fetch(self, chat, **kwargs):
        async def collect():
            result = []
            async for message in self.service.client.iter_messages(chat.peer(), **kwargs):
                # A wrong peer must fail the whole page, especially before deletion.
                row = index_message(chat, message)
                if row is None:
                    raise ValueError("Unexpected peer in synchronization")
                result.append(row)
            return result
        return await self.service.telegram_call(collect)

    async def initial(self, chat):
        store, size = self.service.store, self.service.settings.page_size
        rows = await self.fetch(chat, limit=size)
        high = max((r["message_id"] for r in rows), default=0)
        before = min((r["message_id"] for r in rows), default=0)
        done = len(rows) < size or any(r["date"] < store.since for r in rows)
        store.save_page(chat.marked_id, rows, {"high_id": high, "history_before": before,
            "history_done": int(done), "last_sync": utcnow(), "error": None},
            delete_range=(before if rows and not done else 0, MAX_ID))

    async def new(self, chat):
        store, size = self.service.store, self.service.settings.page_size
        state = store.state(chat.marked_id)
        if state["last_sync"] is None:
            await self.initial(chat)
            return
        rows = await self.fetch(chat, limit=size, min_id=state["high_id"], reverse=True)
        high = max((r["message_id"] for r in rows), default=state["high_id"])
        # A full page can have a backlog. last_sync denotes the last caught-up poll.
        store.save_page(chat.marked_id, rows, {"high_id": high, "error": None,
            "last_sync": utcnow() if len(rows) < size else state["last_sync"]})

    async def history(self, chat):
        store, size = self.service.store, self.service.settings.page_size
        state = store.state(chat.marked_id)
        if state["history_done"] or state["last_sync"] is None:
            return
        rows = await self.fetch(chat, limit=size, offset_id=state["history_before"])
        before = min((r["message_id"] for r in rows), default=state["history_before"])
        done = len(rows) < size or any(r["date"] < store.since for r in rows)
        store.save_page(chat.marked_id, rows, {"history_before": before, "history_done": int(done)})

    async def recent(self, chat):
        """Recheck recent edits/deletions even while a long history is loading."""
        store, size = self.service.store, self.service.settings.page_size
        rows = await self.fetch(chat, limit=size)
        low = min((r["message_id"] for r in rows), default=0) if len(rows) == size else 0
        store.save_page(chat.marked_id, rows, delete_range=(low, MAX_ID))

    async def reconcile(self, chat):
        store, size = self.service.store, self.service.settings.page_size
        state = store.state(chat.marked_id)
        if not state["history_done"]:
            return
        upper = state["reconcile_before"] or MAX_ID
        rows = await self.fetch(chat, limit=size, offset_id=state["reconcile_before"])
        done = len(rows) < size or any(r["date"] < store.since for r in rows)
        low = 0 if done else min(r["message_id"] for r in rows)
        store.save_page(chat.marked_id, rows, {"reconcile_before": low,
            "last_reconciled": utcnow() if done else state["last_reconciled"]}, delete_range=(low, upper))

    async def exact(self, chat, message_id):
        rows = await self.fetch(chat, limit=1, min_id=message_id - 1, reverse=True)
        return rows[0] if rows and rows[0]["message_id"] == message_id else None
