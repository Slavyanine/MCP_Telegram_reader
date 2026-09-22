"""Shared bounded, in-memory download of Telegram voice messages."""

import io
import math

from .messages import index_message
from .transcription import TranscriptionError


class BoundedAudio(io.BytesIO):
    def __init__(self, max_bytes=1_000_000):
        super().__init__()
        self.max_bytes = max_bytes

    def write(self, data):
        if self.tell() + len(data) > self.max_bytes:
            raise TranscriptionError("audio_too_large")
        return super().write(data)


def validate_ogg(audio, max_duration=30):
    """Read Ogg framing/Opus identification and duration without decoding audio."""
    offset, packet, first, serial, last_granule, pre_skip = 0, bytearray(), True, None, 0, 0
    while offset < len(audio):
        page = memoryview(audio)[offset:]
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
    if last_granule - pre_skip > max_duration * 48000:
        raise TranscriptionError("audio_too_long")


async def download_voice(client, chat, message_id, *, telegram_call, download_call,
                         since, media_version, max_bytes, max_duration):
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
            if (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
                    or type(size) is not int or size <= 0):
                raise TranscriptionError("invalid_media_metadata")
            if duration > max_duration:
                raise TranscriptionError("audio_too_long")
            if size > max_bytes:
                raise TranscriptionError("audio_too_large")
            return msg
        raise TranscriptionError("message_unavailable")

    message = await telegram_call(fetch)
    with BoundedAudio(max_bytes) as buffer:
        await download_call(lambda: client.download_media(message, file=buffer))
        audio = buffer.getvalue()
    validate_ogg(audio, max_duration)
    return audio
