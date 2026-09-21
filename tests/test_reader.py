from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from telegram_acl_mcp.config import Chat
from telegram_acl_mcp.reader import Reader, ReaderError


CHAT = Chat("project_a", "Project A", "channel", 123, 456)


def message(number, chat_id=CHAT.marked_id):
    return SimpleNamespace(id=number, chat_id=chat_id, date=datetime(2026, 9, number, tzinfo=timezone.utc), sender_id=7, sender=SimpleNamespace(first_name="Иван", last_name=None, username="ivan"), message="Kafka API release", reply_to_msg_id=None, media=None)


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [message(i) for i in (1, 4, 9, 15, 20)]
        self.calls = []

    async def iter_messages(self, peer, **kwargs):
        self.calls.append((peer, kwargs))
        rows = sorted(self.rows, key=lambda x: x.id, reverse=not kwargs.get("reverse", False))
        if kwargs.get("offset_id"):
            rows = [r for r in rows if r.id < kwargs["offset_id"]]
        if kwargs.get("min_id"):
            rows = [r for r in rows if r.id > kwargs["min_id"]]
        if kwargs.get("offset_date"):
            rows = [r for r in rows if r.date < kwargs["offset_date"]]
        for row in rows[:kwargs["limit"]]:
            yield row

    async def get_messages(self, peer, ids):
        self.calls.append((peer, {"ids": ids}))
        return next((r for r in self.rows if r.id == ids), None)


@pytest.fixture
def reader():
    return Reader(FakeClient(), {CHAT.key: CHAT})


async def test_unknown_chat_never_reaches_telegram(reader):
    for key in ("-100123", -100123, "@public", "../config.json", "other"):
        with pytest.raises(ReaderError):
            await reader.execute("telegram_get_messages", {"chat_key": key})
    assert reader.client.calls == []


async def test_latest_and_pagination(reader):
    page = await reader.execute("telegram_get_messages", {"chat_key": CHAT.key, "limit": 2})
    assert [r["message_id"] for r in page["messages"]] == [20, 15]
    assert page["has_more"] and page["next_before_message_id"] == 15
    second = await reader.execute("telegram_get_messages", {"chat_key": CHAT.key, "limit": 2, "before_message_id": 15})
    assert [r["message_id"] for r in second["messages"]] == [9, 4]
    assert second["messages"][0]["sender_name"] == "Иван"


async def test_search_date_boundaries(reader):
    page = await reader.execute("telegram_search_messages", {"chat_key": CHAT.key, "query": "API", "date_from": "2026-09-04T00:00:00Z", "date_to": "2026-09-15T00:00:00Z"})
    assert [r["message_id"] for r in page["messages"]] == [9, 4]
    peer, params = reader.client.calls[0]
    assert peer.channel_id == CHAT.peer_id and peer.access_hash == CHAT.access_hash
    assert params["search"] == "API"


async def test_new_messages_no_gap_with_small_pages(reader):
    cursor, found = 0, []
    for _ in range(10):
        page = await reader.execute("telegram_get_new_messages", {"chat_key": CHAT.key, "after_message_id": cursor, "limit": 2})
        found.extend(r["message_id"] for r in page["messages"])
        cursor = page["next_after_message_id"]
        if not page["has_more"]:
            break
    assert found == [1, 4, 9, 15, 20]
    empty = await reader.execute("telegram_get_new_messages", {"chat_key": CHAT.key, "after_message_id": cursor})
    assert empty["messages"] == [] and empty["next_after_message_id"] == 20


async def test_context_uses_actual_neighbors_not_numeric_range(reader):
    page = await reader.execute("telegram_get_message_context", {"chat_key": CHAT.key, "message_id": 9, "before": 1, "after": 1})
    assert [r["message_id"] for r in page["messages"]] == [4, 9, 15]


async def test_context_wrong_peer_fails_closed(reader):
    reader.client.rows = [message(9, chat_id=-999)]
    with pytest.raises(ReaderError, match="not found"):
        await reader.execute("telegram_get_message_context", {"chat_key": CHAT.key, "message_id": 9})
    assert len(reader.client.calls) == 1


async def test_wrong_peer_in_history_is_removed(reader):
    reader.client.rows = [message(9, chat_id=-999)]
    assert (await reader.execute("telegram_get_messages", {"chat_key": CHAT.key}))["messages"] == []


@pytest.mark.parametrize("dates", [{"date_from": "2026-09-01"}, {"date_to": "bad"}, {"date_from": "2026-09-02T00:00:00Z", "date_to": "2026-09-01T00:00:00Z"}])
async def test_invalid_dates_no_rpc(reader, dates):
    with pytest.raises(ReaderError):
        await reader.execute("telegram_get_messages", {"chat_key": CHAT.key, **dates})
    assert not reader.client.calls
