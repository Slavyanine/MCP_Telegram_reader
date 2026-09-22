"""Deepgram pre-recorded REST transcription, without conversion or disk audio."""

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .audio import download_voice
from .transcription import HttpTranscriber, TranscriptionError


ENDPOINT = "https://api.deepgram.com/v1/listen"


def retry_after_seconds(value):
    """Accept HTTP delay-seconds or HTTP-date; bound untrusted server delays."""
    if not value:
        return 0
    try:
        seconds = int(value) if value.isascii() and value.isdigit() else (
            parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        return max(0, min(86400, seconds))
    except (ValueError, TypeError, OverflowError):
        return 0


class DeepgramTranscriber(HttpTranscriber):
    async def transcribe(self, client, chat, message_id, *, telegram_call, download_call,
                         since, media_version):
        audio = await download_voice(client, chat, message_id, telegram_call=telegram_call,
            download_call=download_call, since=since, media_version=media_version,
            max_bytes=self.settings.max_audio_bytes, max_duration=self.settings.max_audio_duration_seconds)
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                response = await self.http.post(ENDPOINT,
                    params={"model": self.settings.model, "language": self.settings.language,
                            "smart_format": str(self.settings.smart_format).lower()},
                    headers={"Authorization": "Token " + self.settings.api_key, "Content-Type": "audio/ogg"},
                    content=audio)
        except (httpx.RequestError, TimeoutError):
            raise TranscriptionError("deepgram_unavailable", retryable=True) from None
        if response.status_code in (401, 403):
            raise TranscriptionError("deepgram_auth_failed", blocked=True)
        if response.status_code == 402:
            raise TranscriptionError("deepgram_payment_required", blocked=True)
        if response.status_code in (408, 422, 429) or response.status_code >= 500:
            raise TranscriptionError(
                "deepgram_rate_limited" if response.status_code == 429 else "deepgram_unavailable",
                retryable=True, retry_after=retry_after_seconds(response.headers.get("Retry-After"))
                if response.status_code == 429 else 0)
        if response.status_code != 200:
            raise TranscriptionError("deepgram_request_rejected")
        try:
            payload = response.json()
            text = payload["results"]["channels"][0]["alternatives"][0]["transcript"]
            if not isinstance(text, str):
                raise ValueError()
        except (ValueError, KeyError, IndexError, TypeError):
            raise TranscriptionError("deepgram_invalid_response", retryable=True) from None
        text = text.strip()
        return {"status": "ready", "text": text} if text else {"status": "failed", "error": "empty_transcript"}
