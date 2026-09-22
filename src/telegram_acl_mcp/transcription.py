"""Shared provider lifecycle/errors and native Telegram transcription."""

import httpx
from telethon.tl.functions.messages import TranscribeAudioRequest


class TranscriptionError(Exception):
    def __init__(self, code, *, retryable=False, blocked=False, retry_after=0):
        super().__init__(code)
        self.code, self.retryable, self.blocked = code, retryable, blocked
        self.retry_after = retry_after


class HttpTranscriber:
    external_audio = True

    def __init__(self, settings):
        self.settings = settings
        self.http = None

    async def start(self):
        self.http = httpx.AsyncClient(timeout=self.settings.timeout_seconds, follow_redirects=False)

    async def close(self):
        if self.http is not None:
            await self.http.aclose()
            self.http = None


class TelegramTranscriber:
    external_audio = False

    async def start(self):
        pass

    async def close(self):
        pass

    async def transcribe(self, client, chat, message_id):
        result = await client(TranscribeAudioRequest(peer=chat.peer(), msg_id=message_id))
        if getattr(result, "pending", False):
            return {"status": "pending"}
        text = (getattr(result, "text", None) or "").strip()
        return {"status": "ready", "text": text} if text else {"status": "failed", "error": "empty_transcript"}
