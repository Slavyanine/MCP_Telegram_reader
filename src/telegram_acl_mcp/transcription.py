"""Native Telegram transcription. One RPC per attempt; pending is persisted."""

from telethon.tl.functions.messages import TranscribeAudioRequest


class TelegramTranscriber:
    async def transcribe(self, client, chat, message_id):
        result = await client(TranscribeAudioRequest(peer=chat.peer(), msg_id=message_id))
        if getattr(result, "pending", False):
            return {"status": "pending"}
        text = (getattr(result, "text", None) or "").strip()
        return {"status": "ready", "text": text} if text else {"status": "failed", "error": "empty_transcript"}
