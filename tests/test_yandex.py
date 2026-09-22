import asyncio
from dataclasses import replace

import httpx
import pytest

from telegram_acl_mcp.config import IndexConfig, YandexConfig
from telegram_acl_mcp.search import search
from telegram_acl_mcp.server import tool_definitions
from telegram_acl_mcp.storage import Store
from telegram_acl_mcp.worker import IndexService
from telegram_acl_mcp.yandex import MAX_BYTES
from telegram_acl_mcp.audio import BoundedAudio, validate_ogg
from telegram_acl_mcp.transcription import TranscriptionError
from test_index import add, media_message
from test_reader import CHAT, FakeClient


def ogg(seconds=12, channels=1):
    head = b"OpusHead" + bytes([1, channels]) + b"\x00" * 9
    def page(payload, granule):
        return b"OggS\x00\x00" + granule.to_bytes(8, "little") + b"\x00" * 12 + bytes([1, len(payload)]) + payload
    return page(head, 0) + page(b"audio", seconds * 48000)


@pytest.fixture
async def service(tmp_path):
    settings = IndexConfig(enabled=True, backend="yandex", database=str(tmp_path / "index.sqlite3"),
        history_since="2026-09-01T00:00:00Z", yandex=YandexConfig(api_key="test-secret"))
    msg = media_message(1, voice=True)
    msg.document.size = len(ogg())
    client = FakeClient([msg])
    client.downloads = 0
    async def download(message, file):
        client.downloads += 1
        file.write(ogg())
    client.download_media = download
    svc = IndexService(client, {CHAT.key: CHAT}, settings)
    svc.premium = False
    svc.store = Store(settings, 7, svc.chats)
    async def immediate(operation):
        return await operation()
    svc.telegram_call = immediate
    add(svc.store, msg)
    yield svc
    await svc.stop()


def transport(service, handler):
    service.transcriber.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_yandex_without_premium_and_search(service):
    def respond(request):
        assert request.url.host == "stt.api.cloud.yandex.net"
        assert dict(request.url.params) == {"format": "oggopus", "lang": "ru-RU"}
        assert request.headers["authorization"] == "Api-Key test-secret"
        assert request.content == ogg()
        return httpx.Response(200, json={"result": "Подготовь отчет"})
    transport(service, respond)
    assert await service.process_one()
    hit = search(service.store, {"query": "отчет"})["results"][0]["message"]
    assert hit["transcript_source"] == "yandex"
    assert not service.transcription_blocked_reason()
    assert all(peer == CHAT.peer() for peer, _ in service.client.calls)


@pytest.mark.parametrize("status,code,state", [(401, "yandex_auth_failed", "queued"),
    (403, "yandex_auth_failed", "queued"), (429, "yandex_rate_limited", "queued"),
    (503, "yandex_unavailable", "queued"), (400, "yandex_request_rejected", "failed")])
async def test_http_errors(service, status, code, state):
    transport(service, lambda request: httpx.Response(status, text="private upstream error"))
    await service.process_one()
    result = service.store.get(CHAT.marked_id, 1)
    assert (result["transcript_error"], result["transcript_status"]) == (code, state)
    if status in (401, 403):
        assert service.transcription_blocked_reason() == code
        assert not await service.process_one()
        assert service.client.downloads == 1
    elif state == "queued":
        assert service.store.claim() is None


@pytest.mark.parametrize("payload,error", [({"result": " "}, "empty_transcript"),
    ({"unexpected": "secret"}, "yandex_invalid_response"), ([], "yandex_invalid_response")])
async def test_bad_results(service, payload, error):
    transport(service, lambda request: httpx.Response(200, json=payload))
    await service.process_one()
    assert service.store.get(CHAT.marked_id, 1)["transcript_error"] == error


async def test_network_retry_exhaustion(service):
    service.settings = replace(service.settings, max_attempts=1)
    def fail(request):
        raise httpx.ReadTimeout("secret", request=request)
    transport(service, fail)
    await service.process_one()
    row = service.store.get(CHAT.marked_id, 1)
    assert row["transcript_status"] == "failed"
    assert row["transcript_error"] == "yandex_unavailable"


@pytest.mark.parametrize("change", ["duration", "size", "video", "peer", "history", "missing"])
async def test_rejected_before_download(service, change):
    msg = service.client.rows[0]
    if change == "duration": msg.file.duration = 31
    if change == "size": msg.document.size = MAX_BYTES + 1
    if change == "video": msg.voice, msg.video_note = None, msg.document
    if change == "peer": msg.chat_id = -999
    if change == "history": service.store.since = "2026-09-02T00:00:00+00:00"
    if change == "missing": service.client.rows = []
    await service.process_one()
    assert service.client.downloads == 0


async def test_download_actual_size_bounded(service):
    async def large(message, file):
        file.write(b"x" * (MAX_BYTES + 1))
    service.client.download_media = large
    await service.process_one()
    assert service.store.get(CHAT.marked_id, 1)["transcript_error"] == "audio_too_large"


def test_audio_boundaries_and_format():
    validate_ogg(ogg(30))
    for audio in (ogg(31), ogg(channels=2), b"not ogg", ogg()[:-1]):
        with pytest.raises(TranscriptionError):
            validate_ogg(audio)
    with BoundedAudio() as buffer:
        buffer.write(b"x" * MAX_BYTES)
        with pytest.raises(TranscriptionError):
            buffer.write(b"x")


async def test_cancel_http_requeues_and_rpc_remains_available(service):
    entered = asyncio.Event()
    async def waiting(request):
        entered.set()
        await asyncio.Event().wait()
    transport(service, waiting)
    # Exercise the actual shared RPC lock while HTTP is pending.
    service.telegram_call = IndexService.telegram_call.__get__(service)
    task = asyncio.create_task(service.process_one())
    try:
        await asyncio.wait_for(entered.wait(), 8)
        service.next_rpc = 0
        await asyncio.wait_for(service.sync.exact(CHAT, 1), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service.store.get(CHAT.marked_id, 1)["transcript_status"] == "queued"


@pytest.mark.parametrize("previous,current", [(a, b) for a in ("telegram", "yandex", "deepgram")
    for b in ("telegram", "yandex", "deepgram") if a != b])
@pytest.mark.parametrize("state", ["queued", "pending", "processing"])
def test_switch_backend_preserves_cache_and_failed_jobs(tmp_path, previous, current, state):
    settings = IndexConfig(enabled=True, database=str(tmp_path / "db"), backend=previous)
    store = Store(replace(settings, history_since="2026-09-01T00:00:00Z"), 7, {CHAT.key: CHAT})
    for number in (1, 2, 3): add(store, media_message(number, voice=True))
    store.finish(store.claim(), "готово", backend=previous)
    store.defer(store.claim(), "failed", "transcription_failed")
    store.defer(store.claim(), state, delay=999, attempt=True, poll=True)
    store.close()
    store = Store(replace(settings, backend=current), 7, {CHAT.key: CHAT})
    try:
        job = store.claim()
        assert job["attempts"] == job["polls"] == 0
        assert store.get(CHAT.marked_id, 3)["transcript_source"] == previous
        assert store.get(CHAT.marked_id, 2)["transcript_status"] == "failed"
        store.prioritize(CHAT.marked_id, 2, retry_failed=True)
        assert store.get(CHAT.marked_id, 2)["transcript_status"] == "queued"
    finally:
        store.close()


async def test_disabled_transcription_has_no_network(service):
    service.settings = replace(service.settings, transcribe=False)
    assert not await service.process_one()
    assert not service.client.calls


async def test_changed_media_between_checks_never_downloaded(service):
    # The first fetch belongs to the worker; the second is the download guard.
    original = service.client.iter_messages
    calls = 0
    async def changing(peer, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            service.client.rows[0].document.id = 999
        async for msg in original(peer, **kwargs):
            yield msg
    service.client.iter_messages = changing
    await service.process_one()
    assert service.client.downloads == 0
    assert service.store.get(CHAT.marked_id, 1)["transcript_error"] == "media_changed"


async def test_http_lifecycle(service):
    await service.transcriber.start()
    http = service.transcriber.http
    assert not http.is_closed
    await service.stop()
    assert http.is_closed
    assert service.transcriber.http is None


def test_mcp_description_matches_backend():
    for backend in ("telegram", "yandex"):
        tool = next(t for t in tool_definitions({CHAT.key: CHAT}, IndexConfig(enabled=True, backend=backend))
                    if t.name == "telegram_transcribe_voice")
        assert ("uploads it to SpeechKit" in tool.description) == (backend == "yandex")
