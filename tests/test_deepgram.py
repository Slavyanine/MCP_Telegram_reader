import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from telethon.errors import FloodWaitError

from telegram_acl_mcp.audio import validate_ogg
from telegram_acl_mcp.config import DeepgramConfig, IndexConfig
from telegram_acl_mcp.deepgram import retry_after_seconds
from telegram_acl_mcp.search import search
from telegram_acl_mcp.server import tool_definitions
from telegram_acl_mcp.storage import Store
from telegram_acl_mcp.transcription import TranscriptionError
from telegram_acl_mcp.worker import IndexService
from test_index import add, media_message
from test_reader import CHAT, FakeClient
from test_yandex import ogg


def result(text="Подготовь отчет"):
    return {"results": {"channels": [{"alternatives": [{"transcript": text}]}]}}


@pytest.fixture
async def service(tmp_path):
    settings = IndexConfig(enabled=True, backend="deepgram", database=str(tmp_path / "index.sqlite3"),
        history_since="2026-09-01T00:00:00Z", deepgram=DeepgramConfig(api_key="test-secret"))
    msg = media_message(1, voice=True)
    msg.file.duration = 60
    msg.document.size = len(ogg(60))
    client = FakeClient([msg])
    client.downloads = 0
    async def download(message, file):
        client.downloads += 1
        file.write(ogg(60))
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


@pytest.mark.parametrize("smart_format", [True, False])
async def test_request_search_without_premium(service, smart_format):
    service.transcriber.settings = replace(service.settings.deepgram, smart_format=smart_format)
    def respond(request):
        assert str(request.url).startswith("https://api.deepgram.com/v1/listen?")
        assert dict(request.url.params) == {"model": "nova-3", "language": "ru", "smart_format": str(smart_format).lower()}
        assert request.headers["authorization"] == "Token test-secret"
        assert request.headers["content-type"] == "audio/ogg"
        assert request.content == ogg(60)
        return httpx.Response(200, json=result())
    transport(service, respond)
    assert await service.process_one()
    hit = search(service.store, {"query": "отчет"})["results"][0]["message"]
    assert hit["transcript_source"] == "deepgram"
    assert not service.transcription_blocked_reason()
    assert service.status([CHAT.key])["backend"] == "deepgram"
    assert all(peer == CHAT.peer() for peer, _ in service.client.calls)


@pytest.mark.parametrize("status,code,state", [
    (401, "deepgram_auth_failed", "queued"), (403, "deepgram_auth_failed", "queued"),
    (402, "deepgram_payment_required", "queued"), (429, "deepgram_rate_limited", "queued"),
    (408, "deepgram_unavailable", "queued"), (422, "deepgram_unavailable", "queued"),
    (503, "deepgram_unavailable", "queued"), (400, "deepgram_request_rejected", "failed"),
    (413, "deepgram_request_rejected", "failed"), (415, "deepgram_request_rejected", "failed"),
    (302, "deepgram_request_rejected", "failed")])
async def test_http_errors(service, status, code, state):
    transport(service, lambda request: httpx.Response(status, text="private upstream error",
                                                    headers={"Retry-After": "90"}))
    before = time.time()
    await service.process_one()
    row = service.store.get(CHAT.marked_id, 1)
    assert (row["transcript_error"], row["transcript_status"]) == (code, state)
    if status in (401, 402, 403):
        assert service.transcription_blocked_reason() == code
        assert not await service.process_one()
        assert service.client.downloads == 1
    elif state == "queued":
        assert service.store.claim() is None
    if status == 429:
        job = service.store.db.execute("SELECT next_attempt,attempts FROM transcription_jobs").fetchone()
        assert job["next_attempt"] >= before + 90
        assert job["attempts"] == 1


@pytest.mark.parametrize("payload", [None, [], {}, {"results": None}, {"results": {"channels": []}},
    {"results": {"channels": [{"alternatives": []}]}}, result(None), result(42)])
async def test_invalid_response(service, payload):
    transport(service, lambda request: httpx.Response(200, json=payload))
    await service.process_one()
    row = service.store.get(CHAT.marked_id, 1)
    assert row["transcript_error"] == "deepgram_invalid_response"
    assert row["transcript_status"] == "queued"


@pytest.mark.parametrize("mode", ["empty", "invalid_json", "network", "timeout"])
async def test_empty_and_retry_exhaustion(service, mode):
    service.settings = replace(service.settings, max_attempts=1)
    def respond(request):
        if mode == "network":
            raise httpx.ReadTimeout("private", request=request)
        if mode == "timeout":
            raise TimeoutError("private")
        return httpx.Response(200, json=result(" ")) if mode == "empty" else httpx.Response(200, text="broken")
    transport(service, respond)
    await service.process_one()
    row = service.store.get(CHAT.marked_id, 1)
    assert row["transcript_status"] == "failed"
    assert row["transcript_error"] == {"empty": "empty_transcript", "invalid_json": "deepgram_invalid_response",
        "network": "deepgram_unavailable", "timeout": "deepgram_unavailable"}[mode]


@pytest.mark.parametrize("change", ["duration", "size", "video", "peer", "history", "missing", "nan"])
async def test_rejected_before_download(service, change):
    msg = service.client.rows[0]
    if change == "duration": msg.file.duration = 3601
    if change == "size": msg.document.size = 25_000_001
    if change == "video": msg.voice, msg.video_note = None, msg.document
    if change == "peer": msg.chat_id = -999
    if change == "history": service.store.since = "2026-09-02T00:00:00+00:00"
    if change == "missing": service.client.rows = []
    if change == "nan": msg.file.duration = float("nan")
    await service.process_one()
    assert service.client.downloads == 0


async def test_changed_media_between_checks(service):
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


@pytest.mark.parametrize("audio,error", [(b"x" * 501, "audio_too_large"),
    (ogg(3601), "audio_too_long"), (ogg()[:-1], "unsupported_media"), (b"bad", "unsupported_media")])
async def test_actual_audio_validation(service, audio, error):
    service.transcriber.settings = replace(service.settings.deepgram, max_audio_bytes=500)
    async def download(message, file):
        file.write(audio)
    service.client.download_media = download
    await service.process_one()
    assert service.store.get(CHAT.marked_id, 1)["transcript_error"] == error


async def test_audio_over_yandex_size_limit(service):
    # Many Ogg pages, staying within the configured Deepgram buffer limit.
    audio = ogg(60) + ogg(60)[47:] * 40000
    assert len(audio) > 1_000_000
    service.client.rows[0].document.size = len(audio)
    async def download(message, file):
        file.write(audio)
    service.client.download_media = download
    def respond(request):
        assert request.content == audio
        return httpx.Response(200, json=result())
    transport(service, respond)
    await service.process_one()
    assert service.store.get(CHAT.marked_id, 1)["transcript_status"] == "ready"


@pytest.mark.parametrize("phase", ["download", "http"])
async def test_cancel_requeues_and_rpc_unblocked(service, phase):
    entered = asyncio.Event()
    async def waiting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    if phase == "download":
        service.client.download_media = waiting
    else:
        transport(service, waiting)
    service.telegram_call = IndexService.telegram_call.__get__(service)
    service.settings = replace(service.settings, request_interval_seconds=0)
    task = asyncio.create_task(service.process_one())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(service.sync.exact(CHAT, 1), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service.store.get(CHAT.marked_id, 1)["transcript_status"] == "queued"


async def test_download_timeout_and_floodwait(service):
    service.transcriber.settings = replace(service.settings.deepgram, download_timeout_seconds=0.01)
    async def waiting():
        await asyncio.Event().wait()
    with pytest.raises(TimeoutError):
        await service.download_call(waiting)
    async def flood(message, file):
        raise FloodWaitError(request=None, capture=45)
    service.client.download_media = flood
    before = time.monotonic()
    await service.process_one()
    assert service.next_rpc >= before + 45
    assert service.store.get(CHAT.marked_id, 1)["transcript_error"] == "rate_limited"
    assert service.store.claim() is None


@pytest.mark.parametrize("flag", ["transcribe", "enabled"])
async def test_disabled_has_no_network(service, flag):
    service.settings = replace(service.settings, **{flag: False})
    assert not await service.process_one()
    assert not service.client.calls
    assert service.client.downloads == 0


async def test_lifecycle(service):
    await service.transcriber.start()
    http = service.transcriber.http
    assert not http.follow_redirects
    await service.stop()
    assert http.is_closed
    assert service.transcriber.http is None


def test_audio_boundary_and_retry_after():
    validate_ogg(ogg(3600), 3600)
    with pytest.raises(TranscriptionError, match="audio_too_long"):
        validate_ogg(ogg(3601), 3600)
    assert retry_after_seconds("90") == 90
    for value in (None, "", "-1", "nan", "garbage", "1.5"):
        assert retry_after_seconds(value) == 0
    assert retry_after_seconds("999999") == 86400
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    assert 58 <= retry_after_seconds(future) <= 60


def test_mcp_description():
    settings = IndexConfig(enabled=True, backend="deepgram",
        deepgram=DeepgramConfig(max_audio_bytes=12345, max_audio_duration_seconds=123))
    tool = next(t for t in tool_definitions({CHAT.key: CHAT}, settings) if t.name == "telegram_transcribe_voice")
    assert "uploads it to Deepgram" in tool.description
    assert "12345 bytes" in tool.description
    assert "123 seconds" in tool.description
    assert "video notes unsupported" in tool.description
    assert not tool.annotations.readOnlyHint
