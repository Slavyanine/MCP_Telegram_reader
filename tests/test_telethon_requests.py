"""Exercise real Telethon iterators against a fake MTProto transport."""
from datetime import datetime, timezone

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import functions, types

from telegram_acl_mcp.config import Chat
from telegram_acl_mcp.reader import Reader


@pytest.mark.parametrize("kind", ["channel", "group", "user"])
@pytest.mark.parametrize("operation,args", [
    ("telegram_get_messages", {}),
    ("telegram_search_messages", {"query": "Kafka"}),
    ("telegram_get_message_context", {"message_id": 9, "before": 0, "after": 0}),
    ("telegram_get_new_messages", {"after_message_id": 0}),
])
async def test_only_peer_scoped_read_rpcs(kind, operation, args):
    chat = Chat("project_a", "Project A", kind, 123, 456)
    peer = {"channel": types.PeerChannel, "group": types.PeerChat, "user": types.PeerUser}[kind](123)
    requests = []

    class TransportFake(TelegramClient):
        async def __call__(self, request, *a, **kw):
            assert type(request) in {functions.messages.GetHistoryRequest, functions.messages.SearchRequest}
            assert request.peer == chat.peer()
            requests.append(request)
            return types.messages.Messages(messages=[types.Message(id=9, peer_id=peer, date=datetime(2026, 9, 9, tzinfo=timezone.utc), message="Kafka")], chats=[], users=[], topics=[])

    client = TransportFake(StringSession(), 123, "0" * 32, receive_updates=False)
    result = await Reader(client, {chat.key: chat}).execute(operation, {"chat_key": chat.key, **args})
    assert [r["message_id"] for r in result["messages"]] == [9]
    assert requests
