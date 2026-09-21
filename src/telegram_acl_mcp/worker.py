"""Lifecycle and scheduling of the optional index and native transcription."""

import asyncio
import logging
import time

from telethon.errors import FloodWaitError, RPCError

from .reader import ReaderError
from .search import search
from .storage import Store
from .sync import Synchronizer
from .transcription import TelegramTranscriber


class IndexService:
    def __init__(self, client, chats, settings):
        self.client, self.chats, self.settings = client, chats, settings
        self.store = None
        self.tasks = []
        self.premium = None
        self.premium_checked = 0.0
        self.next_rpc = 0.0
        self.rpc_lock = asyncio.Lock()
        self.transcriber = TelegramTranscriber()
        self.sync = Synchronizer(self)
        self.last_error = None

    async def start(self):
        account = await self.telegram_call(self.client.get_me)
        if account is None:
            raise RuntimeError("Cannot identify Telegram account for index")
        self.premium = bool(getattr(account, "premium", False))
        self.premium_checked = time.monotonic()
        self.store = Store(self.settings, account.id, self.chats)
        self.tasks.append(asyncio.create_task(self.run_sync(), name="telegram-index-sync"))
        if self.settings.transcribe:
            self.tasks.append(asyncio.create_task(self.run_transcriptions(), name="telegram-transcription"))

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.store:
            self.store.close()
            self.store = None

    async def telegram_call(self, operation):
        # Sleeps occur outside the lock; a pending transcript never holds it.
        while True:
            async with self.rpc_lock:
                delay = self.next_rpc - time.monotonic()
                if delay <= 0:
                    try:
                        async with asyncio.timeout(20):
                            return await operation()
                    except FloodWaitError as exc:
                        self.next_rpc = time.monotonic() + exc.seconds
                        raise
                    finally:
                        self.next_rpc = max(self.next_rpc, time.monotonic() + self.settings.request_interval_seconds)
            await asyncio.sleep(delay)

    async def run_sync(self):
        while True:
            for chat in self.chats.values():
                try:
                    await self.sync.new(chat)
                    await self.sync.recent(chat)
                    await self.sync.history(chat)
                    await self.sync.reconcile(chat)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reason = "rate_limited" if isinstance(exc, FloodWaitError) else "synchronization_failed"
                    self.last_error = reason
                    self.store.save_page(chat.marked_id, [], {"error": reason})
                    logging.warning("Index synchronization failed for configured chat %s", chat.key)
            await asyncio.sleep(self.settings.sync_interval_seconds)

    async def update_premium(self):
        if time.monotonic() - self.premium_checked >= 300:
            account = await self.telegram_call(self.client.get_me)
            self.premium = bool(account and getattr(account, "premium", False))
            self.premium_checked = time.monotonic()

    async def process_one(self):
        await self.update_premium()
        if not self.premium:
            return False
        job = self.store.claim()
        if job is None:
            return False
        chat = next((c for c in self.chats.values() if c.marked_id == job["peer_id"]), None)
        if chat is None:
            self.store.defer(job, "failed", "chat_not_allowed")
            return True
        try:
            current = await self.sync.exact(chat, job["message_id"])
            if current is None:
                self.store.save_page(chat.marked_id, [], delete_range=(job["message_id"], job["message_id"] + 1))
                return True
            self.store.save_page(chat.marked_id, [current])
            if current["media_version"] != job["media_version"] or current["message_type"] not in {"voice", "video_note"}:
                return True  # save_page invalidated the old job and transcript.
            result = await self.telegram_call(lambda: self.transcriber.transcribe(self.client, chat, job["message_id"]))
            if result["status"] == "ready":
                self.store.finish(job, result["text"])
            elif result["status"] == "pending":
                exhausted = job["polls"] + 1 >= self.settings.max_pending_polls
                self.store.defer(job, "failed" if exhausted else "pending",
                    "pending_timeout" if exhausted else None,
                    delay=max(5, self.settings.request_interval_seconds), poll=True)
            else:
                self.store.defer(job, "failed", result["error"])
        except asyncio.CancelledError:
            self.store.defer(job, "queued", delay=1)
            raise
        except FloodWaitError as exc:
            self.store.defer(job, "queued", "rate_limited", delay=exc.seconds)
        except Exception as exc:
            name = type(exc).__name__
            if name == "PremiumAccountRequiredError":
                self.premium = False
                self.premium_checked = time.monotonic()
                self.store.defer(job, "queued", "premium_required", delay=300)
            else:
                permanent = name in {"MsgIdInvalidError", "MsgVoiceMissingError", "MsgVoiceTooLongError", "TranscriptionFailedError", "ChannelPrivateError", "ChatAdminRequiredError"}
                exhausted = job["attempts"] + 1 >= self.settings.max_attempts
                self.store.defer(job, "failed" if permanent or exhausted else "queued",
                    "transcription_failed" if permanent else "telegram_unavailable",
                    delay=min(3600, 5 * 2 ** min(job["attempts"], 10)), attempt=True)
        return True

    async def run_transcriptions(self):
        while True:
            try:
                processed = await self.process_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "transcription_worker_error"
                logging.warning("Transcription worker temporarily unavailable")
                processed = False
            await asyncio.sleep(0 if processed else self.settings.request_interval_seconds)

    def enrich(self, chat, packed):
        cached = self.store.get(chat.marked_id, packed["message_id"]) if self.store else None
        if cached and cached["media_version"] == packed["media_version"] and cached["message_type"] == packed["message_type"]:
            for key in ("transcript", "transcript_source", "transcript_status", "transcript_error", "transcript_note"):
                if key in cached:
                    packed[key] = cached[key]
        else:
            packed.update(transcript=None, transcript_source=None,
                transcript_status="not_indexed" if self.settings.transcribe else "disabled")

    def status(self, keys):
        return {"enabled": True, "transcription_enabled": self.settings.transcribe,
                "backend": self.settings.backend,
                "premium": self.premium,
                "transcription_blocked_reason": self.transcription_blocked_reason(),
                "background_tasks_running": bool(self.tasks) and all(not t.done() for t in self.tasks),
                "last_background_error": self.last_error,
                "chats": self.store.status(keys), "content_is_untrusted": True}

    def transcription_blocked_reason(self):
        if not self.settings.transcribe:
            return "disabled"
        return "premium_required" if self.premium is False else None

    async def execute(self, name, args):
        if self.store is None:
            raise ReaderError("Index is not ready")
        keys = args.get("chat_keys", list(self.chats))
        if not keys or any(k not in self.chats for k in keys):
            raise ReaderError("Chat is not allowed")
        if name == "telegram_get_index_status":
            return self.status(keys)
        if name == "telegram_search_context":
            result = search(self.store, args)
            result["transcription_enabled"] = self.settings.transcribe
            result["transcription_blocked_reason"] = self.transcription_blocked_reason()
            return result
        if name != "telegram_transcribe_voice":
            raise ReaderError("Unknown index tool")
        if not self.settings.transcribe:
            raise ReaderError("Transcription is disabled by administrator")
        chat = self.chats.get(args["chat_key"])
        if chat is None:
            raise ReaderError("Chat is not allowed")
        try:
            async with asyncio.timeout(25):
                row = await self.sync.exact(chat, args["message_id"])
        except (RPCError, OSError, asyncio.TimeoutError):
            raise ReaderError("Telegram unavailable or request timed out") from None
        if row is None:
            self.store.save_page(chat.marked_id, [], delete_range=(args["message_id"], args["message_id"] + 1))
            raise ReaderError("Message not found in allowed chat")
        if row["message_type"] not in {"voice", "video_note"}:
            raise ReaderError("Message is not a voice message or video note")
        if row["date"] < self.store.since:
            raise ReaderError("Message is outside configured index history")
        self.store.save_page(chat.marked_id, [row])
        self.store.prioritize(chat.marked_id, row["message_id"], retry_failed=args.get("retry_failed", False))
        return {"chat_key": chat.key, "message": self.store.get(chat.marked_id, row["message_id"]),
                "transcription_blocked_reason": self.status([chat.key])["transcription_blocked_reason"],
                "content_is_untrusted": True}
