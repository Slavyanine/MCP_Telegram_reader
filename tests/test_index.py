import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from telegram_acl_mcp.config import Chat, IndexConfig
from telegram_acl_mcp.messages import index_message, metadata
from telegram_acl_mcp.reader import Reader, ReaderError
from telegram_acl_mcp.search import search
from telegram_acl_mcp.storage import Store
from telegram_acl_mcp.worker import IndexService
from test_reader import CHAT, FakeClient, message


OTHER = Chat("other", "Other", "user", 456, 789)


def media_message(number, text="", *, voice=False, filename=None, out=True, chat=CHAT):
    msg = message(number, chat.marked_id)
    msg.message = text
    msg.out = out
    msg.document = SimpleNamespace(id=number * 100) if voice or filename else None
    msg.voice = msg.document if voice else None
    msg.video_note = None
    msg.file = SimpleNamespace(name=filename, duration=12)
    msg.media = msg.document
    return msg


@pytest.fixture
def settings(tmp_path):
    return IndexConfig(enabled=True, database=str(tmp_path / "index.sqlite3"),
        history_since="2026-09-01T00:00:00Z", page_size=2, request_interval_seconds=1)


@pytest.fixture
def store(settings):
    value = Store(settings, 7, {CHAT.key: CHAT, OTHER.key: OTHER})
    yield value
    value.close()


def add(store, msg, chat=CHAT):
    store.save_page(chat.marked_id, [index_message(chat, msg)])


def test_metadata_extracts_hidden_and_utf16_urls_without_fetching():
    msg = media_message(1, "😀 https://example.org отчёт", filename="Отчёт.xlsx")
    msg.entities = [MessageEntityUrl(offset=3, length=19), MessageEntityTextUrl(offset=23, length=5, url="https://private.test/report")]
    meta = metadata(msg)
    assert meta["filename"] == "Отчёт.xlsx"
    assert meta["urls"] == ["https://example.org", "https://private.test/report"]
    assert meta["outgoing"]


def test_search_all_sources_filters_and_context(store):
    add(store, media_message(1, "Иван, подготовь отчёт на завтра"))
    add(store, media_message(2, voice=True))
    job = store.claim()
    store.finish(job, "Мария, подготовь отчет на завтра")
    add(store, media_message(3, filename="Отчёт_сентябрь.xlsx"))
    link = media_message(4, "документ")
    link.entities = [MessageEntityTextUrl(0, 8, "https://example.org/отчет/сентябрь")]
    add(store, link)
    response = media_message(5, "Сделаю", out=False)
    response.reply_to_msg_id = 1
    add(store, response)
    result = search(store, {"query": "ОТЧЕТ", "chat_keys": [CHAT.key]})
    assert {hit["message"]["message_id"] for hit in result["results"]} == {1, 2, 3, 4}
    assert {s for hit in result["results"] for s in hit["matched_sources"]} == {"text", "voice", "filename", "url"}
    voice = search(store, {"query": "отчет", "sources": ["voice"]})["results"][0]
    assert voice["message"]["transcript_source"] == "telegram"
    assert voice["message"]["text"] == ""
    assert "not a verbatim" in voice["message"]["transcript_note"]
    assert search(store, {"query": "сентябрь.xlsx", "sources": ["filename"]})["results"]
    assert search(store, {"query": "example.org/отчет", "sources": ["url"]})["results"]
    assert not search(store, {"query": "Сделаю", "outgoing_only": True})["results"]
    assert not search(store, {"query": "отчет", "sender_id": 999})["results"]
    assert not search(store, {"query": "отчет", "date_from": "2026-09-05T00:00:00Z"})["results"]
    context = store.context(CHAT.marked_id, 5, 1, 1)
    assert context["reply_to"]["message_id"] == 1
    assert context["before"][0]["message_id"] == 4


def test_literal_fts_queries_short_terms_and_cross_source_matching(store):
    add(store, media_message(1, 'a "quoted" OR test', filename="сводный_отчет.xlsx"))
    assert search(store, {"query": '"quoted"'})["results"]
    assert search(store, {"query": "a отчет"})["results"]
    assert not search(store, {"query": "missing OR отчет"})["results"]
    assert not search(store, {"query": "' UNION SELECT * --"})["results"]


def test_cursor_bound_to_query_scope_and_revision(store):
    for i in range(1, 4):
        add(store, media_message(i, "отчет"))
    args = {"query": "отчет", "limit": 1}
    page1 = search(store, args)
    page2 = search(store, {**args, "cursor": page1["next_cursor"]})
    assert page1["results"][0]["message"]["message_id"] != page2["results"][0]["message"]["message_id"]
    with pytest.raises(ReaderError):
        search(store, {**args, "query": "другое", "cursor": page1["next_cursor"]})
    add(store, media_message(4, "отчет"))
    with pytest.raises(ReaderError, match="stale cursor"):
        search(store, {**args, "cursor": page1["next_cursor"]})


def test_edit_delete_and_media_replacement_invalidate_search(store):
    msg = media_message(1, "старый отчет", voice=True)
    add(store, msg)
    job = store.claim()
    store.finish(job, "старое поручение")
    msg.message = "новая задача"
    add(store, msg)
    assert not search(store, {"query": "отчет"})["results"]
    assert search(store, {"query": "поручение"})["results"]
    msg.document.id = 999
    add(store, msg)
    assert not search(store, {"query": "поручение"})["results"]
    assert store.get(CHAT.marked_id, 1)["transcript_status"] == "queued"
    store.finish(job, "stale result")
    assert not search(store, {"query": "stale"})["results"]
    store.save_page(CHAT.marked_id, [], delete_range=(1, 2))
    assert not search(store, {"query": "задача"})["results"]
    assert store.db.execute("SELECT count(*) FROM transcription_jobs").fetchone()[0] == 0


def test_durable_queue_deduplication_and_account_acl_binding(settings):
    store = Store(settings, 7, {CHAT.key: CHAT, OTHER.key: OTHER})
    add(store, media_message(1, voice=True))
    add(store, media_message(1, voice=True))
    add(store, media_message(2, "private", chat=OTHER), OTHER)
    assert store.claim()
    assert store.claim() is None
    with pytest.raises((OSError, BlockingIOError)):
        Store(settings, 7, {CHAT.key: CHAT})
    store.close()
    with pytest.raises(ValueError, match="another Telegram account"):
        Store(settings, 999, {CHAT.key: CHAT})
    store = Store(settings, 7, {CHAT.key: CHAT})
    try:
        assert store.claim()  # interrupted processing recovered after restart
        assert not search(store, {"query": "private"})["results"]
        assert store.db.execute("SELECT count(*) FROM messages WHERE peer_id=?", (OTHER.marked_id,)).fetchone()[0] == 0
        with pytest.raises(ValueError, match="not allowed"):
            store.get(OTHER.marked_id, 2)
        with pytest.raises(ReaderError):
            search(store, {"query": "private", "chat_keys": [OTHER.key]})
    finally:
        store.close()


def test_queue_limit_refills_without_losing_old_messages(settings):
    store = Store(replace(settings, max_queue_size=1), 7, {CHAT.key: CHAT})
    try:
        add(store, media_message(1, voice=True))
        add(store, media_message(2, voice=True))
        assert store.get(CHAT.marked_id, 2)["transcript_status"] == "not_queued"
        first = store.claim()
        store.finish(first, "первое")
        second = store.claim()
        assert second["message_id"] == 2
    finally:
        store.close()


@pytest.fixture
def service(settings, store):
    client = FakeClient()
    svc = IndexService(client, {CHAT.key: CHAT, OTHER.key: OTHER}, settings)
    svc.store = store
    svc.premium = True
    import time
    svc.premium_checked = time.monotonic()
    async def immediate(operation):
        return await operation()
    svc.telegram_call = immediate
    return svc


async def test_sync_backfill_new_and_reconciliation(service):
    chat = CHAT
    await service.sync.new(chat)
    assert service.store.state(chat.marked_id)["high_id"] == 20
    for _ in range(3):
        await service.sync.history(chat)
    assert service.store.state(chat.marked_id)["history_done"]
    assert service.store.status([chat.key])[0]["messages"] == 5
    service.client.rows = [message(i) for i in (1, 9, 15, 20, 22, 24, 26)]
    service.client.rows[1].message = "измененный отчет"
    await service.sync.new(chat)
    await service.sync.new(chat)
    for _ in range(5):
        await service.sync.reconcile(chat)
    assert service.store.get(chat.marked_id, 4) is None
    assert service.store.get(chat.marked_id, 9)["text"] == "измененный отчет"
    assert service.store.get(chat.marked_id, 26)
    assert service.store.state(chat.marked_id)["last_reconciled"]
    assert all(peer == chat.peer() for peer, _ in service.client.calls)


async def test_failed_page_does_not_delete_or_advance_checkpoint(service):
    add(service.store, media_message(1, "keep"))
    before = service.store.state(CHAT.marked_id)
    async def broken(peer, **kwargs):
        yield message(2)
        raise OSError("network failed")
    service.client.iter_messages = broken
    with pytest.raises(OSError):
        await service.sync.recent(CHAT)
    assert service.store.get(CHAT.marked_id, 1)
    assert service.store.state(CHAT.marked_id) == before
    service.client = FakeClient([message(2, chat_id=-999)])
    with pytest.raises(ValueError):
        await service.sync.recent(CHAT)
    assert service.store.get(CHAT.marked_id, 1)


async def test_transcription_pending_then_ready_with_original_caption(service):
    msg = media_message(1, "подпись", voice=True)
    service.client.rows = [msg]
    add(service.store, msg)
    results = iter([{"status": "pending"}, {"status": "ready", "text": "Иван подготовь отчет"}])
    async def transcribe(*args):
        return next(results)
    service.transcriber.transcribe = transcribe
    await service.process_one()
    assert service.store.get(CHAT.marked_id, 1)["transcript_status"] == "pending"
    assert not search(service.store, {"query": "отчет"})["results"]
    with service.store.db:
        service.store.db.execute("UPDATE transcription_jobs SET next_attempt=0")
    await service.process_one()
    data = service.store.get(CHAT.marked_id, 1)
    assert data["text"] == "подпись" and data["transcript_status"] == "ready"
    reader = Reader(service.client, service.chats, index=service)
    result = await reader.execute("telegram_get_messages", {"chat_key": CHAT.key})
    assert result["messages"][0]["transcript"] == "Иван подготовь отчет"


async def test_floodwait_and_no_premium_preserve_jobs(service):
    msg = media_message(1, voice=True)
    service.client.rows = [msg]
    add(service.store, msg)
    service.premium = False
    assert await service.process_one() is False
    assert not service.client.calls
    service.premium = True
    async def flood(*args):
        raise FloodWaitError(request=None, capture=60)
    service.transcriber.transcribe = flood
    await service.process_one()
    row = service.store.db.execute("SELECT * FROM transcription_jobs").fetchone()
    assert row["status"] == "queued" and row["attempts"] == 0 and row["error"] == "rate_limited"
    assert service.store.claim() is None


async def test_pending_poll_limit_and_permanent_failure(service):
    service.settings = replace(service.settings, max_pending_polls=1)
    msg = media_message(1, voice=True)
    service.client.rows = [msg]
    add(service.store, msg)
    async def pending(*args):
        return {"status": "pending"}
    service.transcriber.transcribe = pending
    await service.process_one()
    data = service.store.get(CHAT.marked_id, 1)
    assert data["transcript_status"] == "failed" and data["transcript_error"] == "pending_timeout"
    assert "voice_transcripts_unavailable" in search(service.store, {"query": "отчет"})["incomplete_reasons"]


async def test_transcribe_tool_does_not_wait_and_checks_scope(service):
    service.client.rows = [media_message(1, voice=True)]
    result = await service.execute("telegram_transcribe_voice", {"chat_key": CHAT.key, "message_id": 1})
    assert result["message"]["transcript_status"] == "queued"
    with pytest.raises(ReaderError):
        await service.execute("telegram_transcribe_voice", {"chat_key": "forbidden", "message_id": 1})
    with pytest.raises(ReaderError, match="not found"):
        await service.execute("telegram_transcribe_voice", {"chat_key": CHAT.key, "message_id": 2})


async def test_native_backend_uses_only_scoped_rpc():
    from telethon.tl.functions.messages import TranscribeAudioRequest
    from telegram_acl_mcp.transcription import TelegramTranscriber
    class Client:
        async def __call__(self, request):
            assert isinstance(request, TranscribeAudioRequest)
            assert request.peer == CHAT.peer() and request.msg_id == 123
            return SimpleNamespace(pending=False, text="отчет на завтра")
    assert (await TelegramTranscriber().transcribe(Client(), CHAT, 123))["text"] == "отчет на завтра"


async def test_background_end_to_end_and_shutdown(settings):
    """Run the real sync and queue loops against a fake Telegram transport."""
    client = FakeClient([media_message(1, "Подготовь отчет", out=True), media_message(2, voice=True)])
    async def get_me():
        return SimpleNamespace(id=7, premium=True)
    client.get_me = get_me
    service = IndexService(client, {CHAT.key: CHAT}, settings)
    async def immediate(operation):
        return await operation()
    service.telegram_call = immediate
    called = asyncio.Event()
    async def transcribe(*args):
        called.set()
        return {"status": "ready", "text": "Мария, отчет на завтра"}
    service.transcriber.transcribe = transcribe
    try:
        await service.start()
        await asyncio.wait_for(called.wait(), timeout=3)
        response = await service.execute("telegram_search_context", {"query": "отчет", "outgoing_only": True})
        assert {hit["message"]["message_id"] for hit in response["results"]} == {1, 2}
        assert service.status([CHAT.key])["background_tasks_running"]
    finally:
        await service.stop()
    assert service.store is None and not service.tasks


def test_disabling_transcription_hides_existing_transcripts(settings):
    store = Store(settings, 7, {CHAT.key: CHAT})
    add(store, media_message(1, voice=True))
    store.finish(store.claim(), "секретное поручение")
    store.close()
    store = Store(replace(settings, transcribe=False), 7, {CHAT.key: CHAT})
    try:
        assert store.get(CHAT.marked_id, 1)["transcript"] is None
        assert store.get(CHAT.marked_id, 1)["transcript_status"] == "disabled"
        assert not search(store, {"query": "поручение"})["results"]
        assert store.db.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 1
    finally:
        store.close()


async def test_cancelled_job_is_requeued_and_does_not_block_reader(service):
    msg = media_message(1, voice=True)
    service.client.rows = [msg]
    add(service.store, msg)
    started = asyncio.Event()
    async def waiting(*args):
        started.set()
        await asyncio.Event().wait()
    service.transcriber.transcribe = waiting
    task = asyncio.create_task(service.process_one())
    await asyncio.wait_for(started.wait(), 1)
    try:
        reader = Reader(service.client, service.chats, index=service)
        response = await asyncio.wait_for(reader.execute("telegram_get_messages", {"chat_key": CHAT.key}), 1)
        assert response["messages"][0]["message_id"] == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service.store.get(CHAT.marked_id, 1)["transcript_status"] == "queued"


async def test_explicit_retry_after_failed_transcription(service):
    msg = media_message(1, voice=True)
    service.client.rows = [msg]
    add(service.store, msg)
    job = service.store.claim()
    service.store.defer(job, "failed", "transcription_failed", attempt=True)
    args = {"chat_key": CHAT.key, "message_id": 1}
    assert (await service.execute("telegram_transcribe_voice", args))["message"]["transcript_status"] == "failed"
    result = await service.execute("telegram_transcribe_voice", {**args, "retry_failed": True})
    assert result["message"]["transcript_status"] == "queued"
    assert service.store.claim()["attempts"] == 0


async def test_deleting_entire_chat_clears_index(service):
    add(service.store, media_message(1, "отчет"))
    service.client.rows = []
    await service.sync.recent(CHAT)
    assert not search(service.store, {"query": "отчет"})["results"]
