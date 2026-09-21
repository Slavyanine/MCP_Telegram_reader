import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from telegram_acl_mcp.config import IndexConfig
from telegram_acl_mcp.messages import index_message
from telegram_acl_mcp.server import build_app
from telegram_acl_mcp.storage import Store
from telegram_acl_mcp.worker import IndexService
from test_index import media_message
from test_mcp import ConnectedFake, rpc
from test_reader import CHAT


@pytest.mark.parametrize("explicit", [False, True])
def test_disabled_mode_never_opens_db_or_exposes_index_tools(tmp_path, monkeypatch, explicit):
    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled mode must not open SQLite")
    monkeypatch.setattr(Store, "__init__", forbidden)
    cfg = SimpleNamespace(chats={CHAT.key: CHAT}, token="x" * 48)
    if explicit:
        cfg.indexing = IndexConfig(enabled=False, database=str(tmp_path / "absent" / "index.sqlite3"))
    telegram = ConnectedFake()
    with TestClient(build_app(cfg, telegram), base_url="http://127.0.0.1:8765") as client:
        http = (client, telegram)
        assert len(rpc(http, "tools/list").json()["result"]["tools"]) == 5
        for name in ("telegram_search_context", "telegram_transcribe_voice", "telegram_get_index_status"):
            result = rpc(http, "tools/call", {"name": name, "arguments": {"query": "отчет"}}).json()["result"]
            assert result["isError"]
        assert not telegram.calls
        response = rpc(http, "tools/call", {"name": "telegram_get_messages", "arguments": {"chat_key": CHAT.key}}).json()["result"]
        data = json.loads(response["content"][0]["text"])
        assert "transcript" not in data["messages"][0]
    assert not (tmp_path / "absent").exists()


@pytest.fixture(params=[True, False])
def indexed_http(tmp_path, monkeypatch, request):
    settings = IndexConfig(enabled=True, transcribe=request.param,
        database=str(tmp_path / "index.sqlite3"), history_since="2026-09-01T00:00:00Z")
    cfg = SimpleNamespace(chats={CHAT.key: CHAT}, token="x" * 48, indexing=settings)
    telegram = ConnectedFake()
    async def get_me():
        return SimpleNamespace(id=7, premium=True)
    telegram.get_me = get_me
    async def idle(self):
        await asyncio.Event().wait()
    monkeypatch.setattr(IndexService, "run_sync", idle)
    monkeypatch.setattr(IndexService, "run_transcriptions", idle)
    start = IndexService.start
    async def seeded_start(self):
        await start(self)
        msg = media_message(1, "Подготовь отчёт на завтра")
        self.store.save_page(CHAT.marked_id, [index_message(CHAT, msg)])
    monkeypatch.setattr(IndexService, "start", seeded_start)
    with TestClient(build_app(cfg, telegram), base_url="http://127.0.0.1:8765") as client:
        yield (client, telegram), request.param
    # Shutdown closes the DB/process lock, so it can immediately be opened again.
    store = Store(settings, 7, cfg.chats)
    store.close()


def test_tools_and_search_enabled(indexed_http):
    http, transcribe = indexed_http
    tools = rpc(http, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert len(tools) == (8 if transcribe else 7)
    assert "telegram_search_context" in names
    assert ("telegram_transcribe_voice" in names) == transcribe
    result = rpc(http, "tools/call", {"name": "telegram_search_context", "arguments": {"query": "отчет", "outgoing_only": True}}).json()["result"]
    assert not result["isError"]
    data = json.loads(result["content"][0]["text"])
    assert data["results"][0]["message"]["message_id"] == 1
    assert "history_backfill_in_progress" in data["incomplete_reasons"]
    assert data["content_is_untrusted"]
    status = rpc(http, "tools/call", {"name": "telegram_get_index_status", "arguments": {}}).json()["result"]
    assert json.loads(status["content"][0]["text"])["transcription_enabled"] == transcribe


@pytest.mark.parametrize("args", [
    {"query": "отчет", "chat_keys": ["forbidden"]},
    {"query": "отчет", "chat_id": 123},
    {"query": "отчет", "limit": 1000},
    {"query": "отчет", "context_before": 1000},
    {"query": "отчет", "sources": ["file_contents"]},
    {"query": "отчет", "chat_keys": []},
    {"query": "отчет", "date_from": "2026-09-01"},
    {"query": "отчет", "cursor": "invalid"},
])
def test_index_tool_validates_arguments(indexed_http, args):
    http, _ = indexed_http
    result = rpc(http, "tools/call", {"name": "telegram_search_context", "arguments": args}).json()["result"]
    assert result["isError"]
    assert not http[1].calls
