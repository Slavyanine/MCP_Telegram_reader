"""Synchronous SpeechKit REST recognition of bounded OggOpus audio."""

import asyncio

import httpx

from .audio import download_voice
from .transcription import HttpTranscriber, TranscriptionError


MAX_BYTES = 1_000_000
ENDPOINT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"


class YandexTranscriber(HttpTranscriber):
    async def transcribe(self, client, chat, message_id, *, telegram_call, since, media_version,
                         download_call=None):
        audio = await download_voice(client, chat, message_id, telegram_call=telegram_call,
            download_call=download_call or telegram_call, since=since, media_version=media_version,
            max_bytes=MAX_BYTES, max_duration=30)
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
