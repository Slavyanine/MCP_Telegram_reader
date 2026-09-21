"""Bounded in-memory voice download and synchronous SpeechKit REST recognition."""

import asyncio
import io

import httpx

from .messages import index_message


MAX_BYTES = 1_000_000  # Conservative decimal interpretation of the 1 MB limit.
ENDPOINT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"


class TranscriptionError(Exception):
    def __init__(self, code, *, retryable=False, blocked=False):
        super().__init__(code)
        self.code, self.retryable, self.blocked = code, retryable, blocked


class BoundedAudio(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_BYTES:
            raise TranscriptionError("audio_too_large")
        return super().write(data)


def validate_ogg(audio):
    """Read Ogg framing/Opus identification and duration without decoding audio."""
    offset, packet, first, serial, last_granule, pre_skip = 0, bytearray(), True, None, 0, 0
    while offset < len(audio):
        page = audio[offset:]
        if len(page) < 27 or page[:5] != b"OggS\x00":
            raise TranscriptionError("unsupported_media")
        count = page[26]
        sizes = page[27:27 + count]
        end = 27 + count + sum(sizes)
        if len(sizes) != count or end > len(page):
            raise TranscriptionError("unsupported_media")
        page_serial = page[14:18]
        if serial is not None and serial != page_serial:
            raise TranscriptionError("unsupported_media")
        serial = page_serial
        granule = int.from_bytes(page[6:14], "little")
        if granule != 2**64 - 1:
            last_granule = max(last_granule, granule)
        pos = 27 + count
        for size in sizes:
            if first:
                packet.extend(page[pos:pos + size])
                if size < 255:
                    if len(packet) < 19 or packet[:8] != b"OpusHead" or packet[9] != 1:
                        raise TranscriptionError("unsupported_media")
                    pre_skip = int.from_bytes(packet[10:12], "little")
                    first = False
            pos += size
        offset += end
    if first or last_granule <= pre_skip:
        raise TranscriptionError("unsupported_media")
    if last_granule - pre_skip > 30 * 48000:
        raise TranscriptionError("audio_too_long")


class YandexTranscriber:
    def __init__(self, settings):
        self.settings = settings
        self.http = None

    async def start(self):
        self.http = httpx.AsyncClient(timeout=self.settings.timeout_seconds, follow_redirects=False)

    async def close(self):
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    async def transcribe(self, client, chat, message_id, *, telegram_call, since, media_version):
        async def fetch():
            async for msg in client.iter_messages(chat.peer(), limit=1, min_id=message_id - 1, reverse=True):
                row = index_message(chat, msg)
                if row is None or row["message_id"] != message_id or row["date"] < since:
                    raise TranscriptionError("message_unavailable")
                if row["media_version"] != media_version:
                    raise TranscriptionError("media_changed")
                if row["message_type"] != "voice":
                    raise TranscriptionError("unsupported_media")
                duration = row["duration"]
                size = getattr(msg.document, "size", None)
                if not isinstance(duration, (int, float)) or duration <= 0 or not isinstance(size, int) or size <= 0:
                    raise TranscriptionError("invalid_media_metadata")
                if duration > 30:
                    raise TranscriptionError("audio_too_long")
                if size > MAX_BYTES:
                    raise TranscriptionError("audio_too_large")
                return msg
            raise TranscriptionError("message_unavailable")

        message = await telegram_call(fetch)
        with BoundedAudio() as buffer:
            await telegram_call(lambda: client.download_media(message, file=buffer))
            audio = buffer.getvalue()
        validate_ogg(audio)
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                response = await self.http.post(ENDPOINT,
                    params={"format": "oggopus", "lang": self.settings.language},
                    headers={"Authorization": "Api-Key " + self.settings.api_key,
                             "Content-Type": "application/octet-stream"}, content=audio)
        except (httpx.RequestError, TimeoutError):
            raise TranscriptionError("yandex_unavailable", retryable=True) from None
        if response.status_code in (401, 403):
            raise TranscriptionError("yandex_auth_failed", blocked=True)
        if response.status_code == 429 or response.status_code >= 500:
            raise TranscriptionError("yandex_rate_limited" if response.status_code == 429 else "yandex_unavailable", retryable=True)
        if response.status_code != 200:
            raise TranscriptionError("yandex_request_rejected")
        try:
            payload = response.json()
            text = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(text, str):
                raise ValueError()
        except ValueError:
            raise TranscriptionError("yandex_invalid_response", retryable=True) from None
        text = text.strip()
        return {"status": "ready", "text": text} if text else {"status": "failed", "error": "empty_transcript"}
