import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_acl_mcp import config


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    # Permission behavior is tested separately; temp paths aren't root-owned.
    monkeypatch.setattr(config, "protected_text", lambda path: path.read_text(encoding="utf-8"))
    data = {"api_id": 123, "chats": [{"key": "project_a", "title": "Project A", "kind": "channel", "peer_id": 123, "access_hash": 456}]}
    for key, value in (("api_hash", "fake-hash"), ("session", "fake-session"), ("token", "x" * 48)):
        path = tmp_path / (key + ".secret")
        path.write_text(value, encoding="utf-8")
        data[key + "_file"] = str(path)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def test_acl_immutable(config_file):
    cfg = config.load_config(config_file[0])
    with pytest.raises(TypeError):
        cfg.chats["evil"] = cfg.chats["project_a"]
    with pytest.raises(AttributeError):
        cfg.chats["project_a"].peer_id = 999


def test_indexing_disabled_by_default_and_optional(config_file):
    path, data = config_file
    assert not config.load_config(path).indexing.enabled
    data["indexing"] = {"enabled": False}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not config.load_config(path).indexing.enabled
    data["indexing"] = {"enabled": True, "database": str(path.parent / "index.sqlite3")}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert config.load_config(path).indexing.enabled
    assert not (path.parent / "index.sqlite3").exists()


@pytest.mark.parametrize("settings", [
    {"enabled": "true"}, {"enabled": 1}, {"transcribe": "false"},
    {"unknown": True}, {"page_size": True}, {"page_size": 101},
    {"backend": "groq"}, {"history_since": "2026-09-01"},
    {"history_since": 123}, {"enabled": True, "database": "relative.sqlite"},
    {"max_context": -1}, {"max_attempts": 0}, {"database": None},
])
def test_invalid_index_settings(config_file, settings):
    path, data = config_file
    data["indexing"] = settings
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        config.load_config(path)


@pytest.mark.parametrize("change", ["empty", "duplicate", "hash", "extra", "peer", "key", "relative"])
def test_invalid_config_fails_closed(config_file, change):
    path, data = config_file
    if change == "empty":
        data["chats"] = []
    elif change == "duplicate":
        data["chats"].append(data["chats"][0].copy())
    elif change == "hash":
        data["chats"][0]["access_hash"] = 0
    elif change == "extra":
        data["allow_all"] = True
    elif change == "peer":
        data["chats"][0]["peer_id"] = "@public"
    elif change == "key":
        data["chats"][0]["key"] = "../../config"
    else:
        data["session_file"] = "session.secret"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        config.load_config(path)


@pytest.mark.parametrize("bad", ["owner", "write", "world_read", "symlink", "parent_write", "parent_owner"])
def test_linux_permissions_rejected(monkeypatch, bad):
    # Emulate Linux metadata while running tests on Windows.
    path = Path.cwd() / "never-read.secret"
    def fake_lstat(item):
        leaf = item == path
        mode = (stat.S_IFREG | 0o640) if leaf else (stat.S_IFDIR | 0o755)
        uid = 0
        if leaf:
            if bad == "owner": uid = 1000
            if bad == "write": mode |= 0o020
            if bad == "world_read": mode |= 0o004
            if bad == "symlink": mode = stat.S_IFLNK | 0o777
        else:
            if bad == "parent_write": mode |= 0o020
            if bad == "parent_owner": uid = 1000
        return SimpleNamespace(st_mode=mode, st_uid=uid)
    monkeypatch.setattr(config, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(type(path), "lstat", fake_lstat)
    with pytest.raises(ValueError):
        config.protected_text(path)
