"""Extract only Telegram message data; never download media or resolve URLs."""

import re
import unicodedata
from datetime import timezone

from telethon.tl.types import MessageEntityUrl, MessageEntityTextUrl


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")


def iso(value):
    return value.astimezone(timezone.utc).isoformat() if value else None


def pack_message(chat, message):
    if message is None or getattr(message, "chat_id", None) != chat.marked_id:
        return None
    sender = getattr(message, "sender", None)
    name = None
    if sender:
        name = getattr(sender, "title", None) or " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
    return {
        "message_id": message.id,
        "date": message.date.isoformat(),
        "sender_id": message.sender_id,
        "sender_name": name,
        "sender_username": getattr(sender, "username", None),
        "text": (message.message or "")[:16000],
        "reply_to_message_id": getattr(message, "reply_to_msg_id", None),
        "has_media": bool(getattr(message, "media", None)),
    }


def metadata(message):
    text = message.message or ""
    encoded = text.encode("utf-16-le")
    urls = []
    for entity in getattr(message, "entities", None) or []:
        if isinstance(entity, MessageEntityTextUrl):
            urls.append(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            urls.append(encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le", errors="replace"))
    # Also cover plain URLs in messages without Telegram entities.
    urls.extend(re.findall(r"(?:https?://|www\.)[^\s<>]+", text))
    preview = getattr(message, "web_preview", None)
    if preview is not None and getattr(preview, "url", None):
        urls.append(preview.url)
    for row in getattr(getattr(message, "reply_markup", None), "rows", None) or []:
        for button in row.buttons:
            if getattr(button, "url", None):
                urls.append(button.url)
    file = getattr(message, "file", None)
    voice = getattr(message, "voice", None) is not None
    video_note = getattr(message, "video_note", None) is not None
    document = getattr(message, "document", None)
    kind = "voice" if voice else "video_note" if video_note else "file" if document else "media" if getattr(message, "media", None) else "text"
    return {
        "outgoing": bool(getattr(message, "out", False)),
        "message_type": kind,
        "filename": getattr(file, "name", None) if document else None,
        "urls": list(dict.fromkeys(u for u in urls if isinstance(u, str) and u)),
        "duration": getattr(file, "duration", None) if voice or video_note else None,
        "edit_date": iso(getattr(message, "edit_date", None)),
        "media_version": str(getattr(document, "id", "")) if voice or video_note else "",
    }


def index_message(chat, message):
    packed = pack_message(chat, message)
    if packed is None:
        return None
    packed.update(metadata(message))
    packed["text"] = message.message or ""
    packed["date"] = iso(message.date)
    return packed
