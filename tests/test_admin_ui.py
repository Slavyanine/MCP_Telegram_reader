import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import Channel, Chat, User

from telegram_acl_mcp import admin_control, admin_deploy, admin_web, config
from telegram_acl_mcp.admin_control import AdminController
from telegram_acl_mcp.admin_deploy import AdminError, Installer, fingerprint, write_json


class Service:
    def __init__(self):
        self.active = True
        self.calls = []
        self.health = [True]
        self.on_start = None

    def command(self, action):
        self.calls.append(action)
        if action == "is-active":
            return self.active
        self.active = action == "start"
        if action == "start" and self.on_start:
            self.on_start()
        return True

    def healthy(self, cfg):
        return self.health.pop(0)


class Client:
    def __init__(self, account_id=101, two_factor=False):
        self.account = SimpleNamespace(id=account_id, first_name="Admin", last_name=None, username="admin", premium=True)
        self.session = SimpleNamespace(save=lambda: "new-session-" + str(account_id))
        self.two_factor = two_factor
        self.disconnected = False
        self.authorized = True
        self.dialog_items = []

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return self.account

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="server-code-hash")

    async def sign_in(self, **kwargs):
        if self.two_factor and "password" not in kwargs:
            raise SessionPasswordNeededError(None)
        return self.account

    async def iter_dialogs(self):
        for item in self.dialog_items:
            yield item


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Exercise real configuration validation; filesystem ownership has its own tests.
    for module in (config, admin_control, admin_deploy, admin_web):
        monkeypatch.setattr(module, "protected_text", lambda p: Path(p).read_text(encoding="utf-8").strip())
    if admin_deploy.os.name == "posix":
        import pwd
        monkeypatch.setattr(admin_deploy.os, "chown", lambda *args: None)
        monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=admin_deploy.os.getuid(), pw_gid=admin_deploy.os.getgid()))
    etc = tmp_path / "etc"
    etc.mkdir()
    databases = tmp_path / "databases"
    databases.mkdir()
    data = {"api_id": 123, "chats": [{"key": "old", "title": "Existing", "kind": "channel", "peer_id": 123, "access_hash": 9223372036854775807}],
            "indexing": {"enabled": False, "database": str(databases / "index.sqlite3"), "history_days": 45}}
    for name, value in (("api_hash", "a" * 32), ("session", "old-session"), ("token", "x" * 48)):
        path = etc / (name + ".secret")
        path.write_text(value)
        data[name + "_file"] = str(path)
    path = etc / "config.json"
    write_json(path, data)
    service = Service()
    installer = Installer(path, tmp_path / "admin", databases, gid=0, service=service)
    client = Client()
    controller = AdminController(installer, lambda *args: client)
    return controller, client, service


def payload(controller):
    return {"revision": controller.revision,
            "chats": [{"id": "channel:123", "key": "old", "title": "Edited"}],
            "settings": {"enabled": False, "transcribe": True, "backend": "telegram"}, "keys": {}}


def test_draft_exact_hash_and_no_secret_leak(setup):
    controller, _, _ = setup
    original = controller.installer.config_path.read_bytes()
    view = controller.save(payload(controller))
    saved = json.loads(controller.saved_path.read_text())
    assert saved["config"]["chats"][0]["access_hash"] == 9223372036854775807
    assert saved["config"]["indexing"]["history_days"] == 45
    assert controller.installer.config_path.read_bytes() == original
    assert "access_hash" not in json.dumps(view)
    assert "old-session" not in json.dumps(view)
    assert "a" * 32 not in json.dumps(view)
    assert AdminController(controller.installer).view()["chats"][0]["title"] == "Edited"


@pytest.mark.parametrize("change", ["empty", "duplicate_key", "unknown", "injected_hash", "key", "bool", "backend", "provider_key", "stale"])
def test_invalid_save_is_atomic(setup, change):
    controller, _, _ = setup
    data = payload(controller)
    if change == "empty": data["chats"] = []
    if change == "duplicate_key": data["chats"] *= 2
    if change == "unknown": data["chats"][0]["id"] = "channel:999"
    if change == "injected_hash": data["chats"][0]["access_hash"] = 42
    if change == "key": data["chats"][0]["key"] = "../bad"
    if change == "bool": data["settings"]["enabled"] = "true"
    if change == "backend": data["settings"]["backend"] = "unknown"
    if change == "provider_key": data["settings"].update(enabled=True, backend="yandex")
    if change == "stale": data["revision"] = "stale"
    before = copy.deepcopy(controller.draft)
    with pytest.raises(AdminError): controller.save(data)
    assert controller.draft == before
    assert not controller.saved_path.exists()


def test_provider_secret_is_staged_not_exposed(setup):
    controller, _, _ = setup
    data = payload(controller)
    data["settings"].update(enabled=True, backend="deepgram")
    data["keys"]["deepgram"] = "test-dg-key"
    view = controller.save(data)
    assert view["keys"]["deepgram"] is True
    assert "test-dg-key" not in json.dumps(view)
    path, cfg = controller.installer.stage(controller.draft)
    assert cfg.indexing.deepgram.api_key == "test-dg-key"
    assert config.load_config(path).session == "old-session"


@pytest.mark.asyncio
async def test_login_2fa_switch_is_staged_and_cancel_safe(setup):
    controller, _, _ = setup
    new = Client(202, two_factor=True)
    controller.client_factory = lambda *args: new
    original = controller.installer.config_path.read_bytes()
    response = await controller.dispatch("login/start", {"phone": "+79991234567"})
    assert response["step"] == "code"
    response = await controller.dispatch("login/finish", {"code": "12345"})
    assert response["step"] == "password"
    assert controller.draft["secrets"]["session"] == "old-session"
    response = await controller.dispatch("login/finish", {"password": "secret-password"})
    assert response["step"] == "complete"
    assert response["state"]["chats"] == []
    assert "index-202-" in controller.draft["config"]["indexing"]["database"]
    assert controller.installer.config_path.read_bytes() == original
    assert "secret-password" not in controller.saved_path.read_text()
    assert "12345" not in controller.saved_path.read_text()
    await controller.dispatch("discard", {})
    assert controller.draft["secrets"]["session"] == "old-session"
    assert new.disconnected


@pytest.mark.asyncio
async def test_login_expiry_does_not_change_current_account(setup):
    controller, client, _ = setup
    await controller.dispatch("login/start", {"phone": "+79991234567"})
    controller.login["expires"] = 0
    with pytest.raises(AdminError, match="истекло"):
        await controller.dispatch("login/finish", {"code": "12345"})
    assert client.disconnected
    assert controller.draft["secrets"]["session"] == "old-session"


@pytest.mark.asyncio
async def test_dialog_pages_preserve_selection_and_resolve_types(setup):
    controller, client, _ = setup
    client.dialog_items = [SimpleNamespace(name=f"User {i}", entity=User(id=i, access_hash=2**62+i)) for i in range(1, 102)]
    client.dialog_items.extend([
        SimpleNamespace(name="Group", entity=Chat(id=10, title="Group", photo=None, participants_count=1, date=None, version=1)),
        SimpleNamespace(name="Channel", entity=Channel(id=11, title="Channel", photo=None, date=None, access_hash=2**62))])
    first = await controller.dispatch("dialogs", {})
    assert len(first["chats"]) == 101  # existing ACL + 100 dialog rows
    assert first["more"]
    second = await controller.dispatch("dialogs", {})
    assert not second["more"]
    assert second["chats"][0]["selected"]
    assert controller.catalog["group:10"]["access_hash"] == 0
    assert controller.catalog["channel:11"]["access_hash"] == 2**62
    data = payload(controller)
    data["chats"].append({"id": "user:1", "key": "person", "title": "Person"})
    controller.save(data)
    assert controller.draft["config"]["chats"][1]["access_hash"] == 2**62+1


def test_apply_success_and_external_conflict(setup):
    controller, _, service = setup
    controller.save(payload(controller))
    result = controller.installer.apply(controller.draft)
    assert result["applied"]
    assert config.load_config(controller.installer.config_path).chats["old"].title == "Edited"
    assert service.calls == ["is-active", "stop", "start"]
    assert not controller.installer.journal.exists()
    with pytest.raises(AdminError, match="изменилась"):
        controller.installer.apply(controller.draft)


def test_rollback_restores_database_wal_and_configuration(setup):
    controller, _, service = setup
    db = controller.installer.database(controller.draft["config"])
    db.write_bytes(b"old SQLite")
    Path(str(db) + "-wal").write_bytes(b"old WAL")
    original = controller.installer.config_path.read_bytes()
    service.health = [False, True]
    calls = []

    def mutate():
        if not calls:
            db.write_bytes(b"purged chat history")
            Path(str(db) + "-wal").unlink()
            Path(str(db) + "-shm").write_bytes(b"new SHM")
        calls.append(True)
    service.on_start = mutate
    controller.save(payload(controller))
    with pytest.raises(AdminError, match="восстановлено"):
        controller.installer.apply(controller.draft)
    assert controller.installer.config_path.read_bytes() == original
    assert db.read_bytes() == b"old SQLite"
    assert Path(str(db) + "-wal").read_bytes() == b"old WAL"
    assert not Path(str(db) + "-shm").exists()
    assert not controller.installer.journal.exists()


def test_failed_rollback_leaves_recovery_journal(setup):
    controller, _, service = setup
    service.health = [False, False]
    with pytest.raises(AdminError, match="журнал"):
        controller.installer.apply(controller.draft)
    assert controller.installer.journal.exists()
    service.health = [True]
    controller.installer.recover_startup()
    assert not controller.installer.journal.exists()


def test_startup_recovers_interrupted_stop(setup):
    controller, _, service = setup
    write_json(controller.installer.state_dir / "stop-intent.json", {"was_active": True})
    service.active = False
    controller.installer.recover_startup()
    assert service.active


def test_database_outside_fixed_state_rejected(setup):
    controller, _, service = setup
    controller.draft["config"]["indexing"]["database"] = str(controller.installer.state_dir / "outside.sqlite3")
    with pytest.raises(AdminError, match="внутри"):
        controller.installer.apply(controller.draft)
    assert service.calls == []


def test_fingerprint_detects_secret_edit(setup):
    controller, _, _ = setup
    Path(controller.draft["config"]["session_file"]).write_text("changed")
    assert fingerprint(controller.installer.config_path) != controller.draft["base"]
    assert controller.view()["conflict"]


def test_recovery_after_interrupted_configuration_install(setup):
    controller, _, service = setup
    original = controller.installer.config_path.read_bytes()
    controller.save(payload(controller))
    def crash():
        raise SystemExit("Simulated broker interruption")
    service.on_start = crash
    with pytest.raises(SystemExit):
        controller.installer.apply(controller.draft)
    assert controller.installer.journal.exists()
    assert controller.installer.config_path.read_bytes() != original
    service.on_start = None
    controller.installer.recover_startup()
    assert controller.installer.config_path.read_bytes() == original
    assert service.active


def test_new_account_failed_start_restores_old_db_and_removes_new_db(setup):
    controller, _, service = setup
    old_db = controller.installer.database(controller.draft["config"])
    old_db.write_bytes(b"original-account")
    new_db = old_db.with_name("new-account.sqlite3")
    controller.draft["config"]["indexing"]["database"] = str(new_db)
    controller.draft["secrets"]["session"] = "new-account"
    service.health = [False, True]
    def start():
        if not service.health or service.health[0] is not False:
            return
        new_db.write_bytes(b"new-account-data")
    service.on_start = start
    with pytest.raises(AdminError, match="восстановлено"):
        controller.installer.apply(controller.draft)
    assert not new_db.exists()
    assert old_db.read_bytes() == b"original-account"
    assert config.load_config(controller.installer.config_path).session == "old-session"


@pytest.mark.asyncio
async def test_expired_session_cannot_apply(setup):
    controller, client, service = setup
    client.authorized = False
    with pytest.raises(AdminError, match="недействительна"):
        await controller.dispatch("apply", {"revision": controller.revision})
    assert service.calls == []
    assert client.disconnected


@pytest.mark.asyncio
async def test_two_tabs_cannot_overwrite_or_apply_stale_revision(setup):
    controller, _, service = setup
    stale = payload(controller)
    controller.save(payload(controller))
    with pytest.raises(AdminError, match="другой вкладке"):
        controller.save(stale)
    with pytest.raises(AdminError, match="изменился"):
        await controller.dispatch("apply", {"revision": stale["revision"]})
    assert service.calls == []


def test_production_health_request_against_real_mcp_app(setup, monkeypatch):
    from starlette.testclient import TestClient
    from telegram_acl_mcp.server import build_app
    controller, client, _ = setup
    cfg = config.load_config(controller.installer.config_path)
    service = admin_deploy.SystemService()
    monkeypatch.setattr(service, "command", lambda action: True)
    with TestClient(build_app(cfg, client), base_url="http://127.0.0.1:8765") as http:
        def post(url, **kwargs):
            kwargs.pop("trust_env", None)
            kwargs.pop("timeout", None)
            return http.post(url, **kwargs)
        monkeypatch.setattr(admin_deploy.httpx, "post", post)
        assert service.healthy(cfg)


@pytest.mark.asyncio
async def test_web_authorization_origin_body_limits_and_assets():
    calls = []
    async def dispatch(operation, data):
        calls.append((operation, data))
        return {"ok": True}
    app = admin_web.build_web("a" * 48, dispatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as web:
        page = await web.get("/")
        assert page.status_code == 200
        assert "no-store" in page.headers["cache-control"]
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert (await web.get("/assets/app.js")).status_code == 200
        assert (await web.get("/assets/secret")).status_code == 404
        assert (await web.post("/api/state", json={})).status_code == 401
        headers = {"Authorization": "Bearer " + "a" * 48}
        assert (await web.post("/api/state", json={}, headers={**headers, "Origin": "https://evil.test"})).status_code == 403
        assert (await web.post("/api/state", json={}, headers={**headers, "Host": "evil.test"})).status_code == 403
        assert (await web.post("/api/state", content="{}", headers=headers)).status_code == 415
        assert (await web.post("/api/state", json=[], headers=headers)).status_code == 400
        assert (await web.post("/api/state", json={"large": "x" * admin_web.REQUEST_LIMIT}, headers=headers)).status_code == 413
        assert (await web.post("/api/shell", json={}, headers=headers)).status_code == 404
        assert calls == []
        assert (await web.post("/api/state", json={}, headers=headers)).status_code == 200
        assert calls == [("state", {})]


@pytest.mark.asyncio
async def test_unexpected_backend_exception_does_not_leak_secrets():
    async def dispatch(*args):
        raise ValueError("secret-session-string")
    app = admin_web.build_web("a" * 48, dispatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as web:
        response = await web.post("/api/state", json={}, headers={"Authorization": "Bearer " + "a" * 48})
        assert response.status_code == 503
        assert "secret-session-string" not in response.text
