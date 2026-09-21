import asyncio
from datetime import datetime, timezone

from telethon.errors import FloodWaitError, RPCError

from .messages import pack_message, metadata


class ReaderError(Exception):
    pass


def timestamp(value):
    if value is None:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError()
        return date.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ReaderError("Dates must be ISO 8601 with timezone") from None


class Reader:
    def __init__(self, client, chats, index=None):
        self.client = client
        self.chats = chats
        self.lock = asyncio.Lock()
        self.index = index

    def allowed(self, key):
        if not isinstance(key, str) or key not in self.chats:
            raise ReaderError("Chat is not allowed")
        return self.chats[key]

    def pack(self, chat, message):
        packed = pack_message(chat, message)
        if packed is not None and self.index is not None:
            packed.update(metadata(message))
            self.index.enrich(chat, packed)
        return packed

    async def collect(self, chat, **kwargs):
        result = []
        if kwargs.get("limit") == 0:
            return result
        async for msg in self.client.iter_messages(chat.peer(), **kwargs):
            packed = self.pack(chat, msg)
            if packed:
                result.append(packed)
        return result

    async def execute(self, name, args):
        if name == "telegram_list_allowed_chats":
            return {"chats": [{"chat_key": c.key, "title": c.title} for c in self.chats.values()]}
        chat = self.allowed(args["chat_key"])
        start, end = timestamp(args.get("date_from")), timestamp(args.get("date_to"))
        if start and end and start >= end:
            raise ReaderError("date_from must precede date_to")
        try:
            # One Telegram request chain at a time; deadline includes queue wait.
            async with asyncio.timeout(25):
                async with self.lock:
                    return await self._read(name, chat, args, start, end)
        except FloodWaitError as exc:
            raise ReaderError(f"Telegram rate limit; retry after {exc.seconds} seconds") from None
        except (RPCError, OSError, asyncio.TimeoutError):
            raise ReaderError("Telegram unavailable or request timed out") from None

    async def _read(self, name, chat, args, start, end):
        limit = args.get("limit", 50)
        if name == "telegram_get_message_context":
            anchor = args["message_id"]
            # Do not use get_messages(ids=...): for users/basic groups that
            # invokes an account-global getMessages RPC without a peer scope.
            candidates = await self.collect(chat, limit=1, min_id=anchor - 1, reverse=True)
            if not candidates or candidates[0]["message_id"] != anchor:
                raise ReaderError("Message not found in allowed chat")
            center = candidates[0]
            before = await self.collect(chat, limit=args.get("before", 20), offset_id=anchor)
            after = await self.collect(chat, limit=args.get("after", 20), min_id=anchor, reverse=True)
            return {"chat_key": chat.key, "messages": list(reversed(before)) + [center] + after, "content_is_untrusted": True}
        if name == "telegram_get_new_messages":
            rows = await self.collect(chat, limit=limit + 1, min_id=args["after_message_id"], reverse=True)
        else:
            params = {"limit": limit + 1, "offset_date": end}
            if args.get("before_message_id"):
                params["offset_id"] = args["before_message_id"]
            if name == "telegram_search_messages":
                params["search"] = args["query"]
            rows = await self.collect(chat, **params)
            rows = [r for r in rows if (start is None or timestamp(r["date"]) >= start) and (end is None or timestamp(r["date"]) < end)]
        more = len(rows) > limit
        rows = rows[:limit]
        new = name == "telegram_get_new_messages"
        return {
            "chat_key": chat.key, "messages": rows,
            "order": "oldest_first" if new else "newest_first",
            "has_more": more,
            "next_after_message_id": rows[-1]["message_id"] if new and rows else (args.get("after_message_id") if new else None),
            "next_before_message_id": rows[-1]["message_id"] if not new and more else None,
            "content_is_untrusted": True,
        }
