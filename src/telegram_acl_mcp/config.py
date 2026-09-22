import json
import os
import re
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser


def protected_text(path: Path) -> str:
    """On Linux require root ownership and deny all non-owner writes."""
    if os.name == "posix":
        leaf = path.lstat()
        if not stat.S_ISREG(leaf.st_mode) or leaf.st_mode & 0o007:
            raise ValueError("Configuration/secrets must be regular files with no world access")
        for item in (path, *path.absolute().parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("Configuration/secrets and parent directories must be root-owned, non-writable by others, and not symlinks")
    return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Chat:
    key: str
    title: str
    kind: str
    peer_id: int
    access_hash: int = 0

    @property
    def marked_id(self):
        return -(1000000000000 + self.peer_id) if self.kind == "channel" else (-self.peer_id if self.kind == "group" else self.peer_id)

    def peer(self):
        if self.kind == "channel":
            return InputPeerChannel(self.peer_id, self.access_hash)
        if self.kind == "user":
            return InputPeerUser(self.peer_id, self.access_hash)
        return InputPeerChat(self.peer_id)


@dataclass(frozen=True)
class YandexConfig:
    api_key_file: str = ""
    language: str = "ru-RU"
    timeout_seconds: int = 30
    api_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class DeepgramConfig:
    api_key_file: str = ""
    model: str = "nova-3"
    language: str = "ru"
    smart_format: bool = True
    timeout_seconds: int = 120
    download_timeout_seconds: int = 120
    max_audio_bytes: int = 25_000_000
    max_audio_duration_seconds: int = 3600
    api_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class IndexConfig:
    enabled: bool = False
    database: str = "/var/lib/telegram-acl-mcp/index.sqlite3"
    history_since: str | None = None
    history_days: int = 90
    sync_interval_seconds: int = 30
    page_size: int = 100
    transcribe: bool = True
    backend: str = "telegram"
    yandex: YandexConfig = YandexConfig()
    deepgram: DeepgramConfig = DeepgramConfig()
    request_interval_seconds: int = 2
    max_attempts: int = 8
    max_pending_polls: int = 60
    max_queue_size: int = 10000
    max_results: int = 50
    max_context: int = 10


def index_config(data):
    if not isinstance(data, dict) or set(data) - set(IndexConfig.__dataclass_fields__):
        raise ValueError("Invalid indexing settings")
    data = dict(data)
    yandex = data.pop("yandex", {})
    if not isinstance(yandex, dict) or set(yandex) - {"api_key_file", "language", "timeout_seconds"}:
        raise ValueError("Invalid indexing.yandex settings")
    deepgram = data.pop("deepgram", {})
    if not isinstance(deepgram, dict) or set(deepgram) - (set(DeepgramConfig.__dataclass_fields__) - {"api_key"}):
        raise ValueError("Invalid indexing.deepgram settings")
    cfg = IndexConfig(**data, yandex=YandexConfig(**yandex), deepgram=DeepgramConfig(**deepgram))
    for key in ("enabled", "transcribe"):
        if type(getattr(cfg, key)) is not bool:
            raise ValueError(f"indexing.{key} must be boolean")
    bounds = {"history_days": (1, 36500), "sync_interval_seconds": (1, 86400),
              "page_size": (1, 100), "request_interval_seconds": (1, 3600),
              "max_attempts": (1, 100), "max_pending_polls": (1, 1000),
              "max_queue_size": (1, 1000000), "max_results": (1, 100), "max_context": (0, 20)}
    for key, (low, high) in bounds.items():
        value = getattr(cfg, key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Invalid indexing.{key}")
    if cfg.backend not in ("telegram", "yandex", "deepgram"):
        raise ValueError("Unsupported transcription backend")
    if cfg.enabled and cfg.transcribe and cfg.backend == "yandex":
        yc = cfg.yandex
        if not isinstance(yc.api_key_file, str) or not yc.api_key_file or not Path(yc.api_key_file).is_absolute():
            raise ValueError("indexing.yandex.api_key_file must be an absolute path")
        if not isinstance(yc.language, str) or not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", yc.language):
            raise ValueError("Invalid indexing.yandex.language")
        if type(yc.timeout_seconds) is not int or not 1 <= yc.timeout_seconds <= 300:
            raise ValueError("Invalid indexing.yandex.timeout_seconds")
    if cfg.enabled and cfg.transcribe and cfg.backend == "deepgram":
        dc = cfg.deepgram
        if not isinstance(dc.api_key_file, str) or not dc.api_key_file or not Path(dc.api_key_file).is_absolute():
            raise ValueError("indexing.deepgram.api_key_file must be an absolute path")
        if not isinstance(dc.model, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", dc.model):
            raise ValueError("Invalid indexing.deepgram.model")
        if not isinstance(dc.language, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*|multi", dc.language):
            raise ValueError("Invalid indexing.deepgram.language")
        if type(dc.smart_format) is not bool:
            raise ValueError("indexing.deepgram.smart_format must be boolean")
        for key, (low, high) in {"timeout_seconds": (1, 600), "download_timeout_seconds": (1, 600),
                                 "max_audio_bytes": (1, 100_000_000),
                                 "max_audio_duration_seconds": (1, 14400)}.items():
            value = getattr(dc, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid indexing.deepgram.{key}")
    if not isinstance(cfg.database, str) or not cfg.database.strip():
        raise ValueError("Invalid indexing.database")
    if cfg.enabled and not Path(cfg.database).is_absolute():
        raise ValueError("indexing.database must be an absolute path")
    if cfg.history_since is not None:
        try:
            value = datetime.fromisoformat(cfg.history_since.replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError()
        except (AttributeError, TypeError, ValueError):
            raise ValueError("indexing.history_since must be ISO 8601 with timezone") from None
    return cfg


@dataclass(frozen=True)
class Config:
    chats: object
    api_id: int
    api_hash: str
    session: str
    token: str
    indexing: IndexConfig = IndexConfig()


def load_config(path: Path) -> Config:
    data = json.loads(protected_text(path))
    if set(data) - {"indexing"} != {"chats", "api_id", "api_hash_file", "session_file", "token_file"}:
        raise ValueError("Invalid config keys")
    indexing = index_config(data.get("indexing", {}))
    if indexing.enabled and indexing.transcribe and indexing.backend in ("yandex", "deepgram"):
        provider = getattr(indexing, indexing.backend)
        key = protected_text(Path(provider.api_key_file)).strip()
        if not key or any(ord(c) <= 32 or ord(c) >= 127 for c in key):
            raise ValueError(f"Invalid {indexing.backend} API key")
        indexing = replace(indexing, **{indexing.backend: replace(provider, api_key=key)})
    if type(data["api_id"]) is not int or data["api_id"] <= 0:
        raise ValueError("Invalid api_id")
    chats = {}
    marked = set()
    for row in data["chats"]:
        if set(row) - {"key", "title", "kind", "peer_id", "access_hash"}:
            raise ValueError("Invalid ACL fields")
        chat = Chat(**row)
        if not isinstance(chat.key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", chat.key):
            raise ValueError("Invalid chat key")
        if not isinstance(chat.title, str) or not 1 <= len(chat.title) <= 200:
            raise ValueError("Invalid chat title")
        if chat.kind not in {"channel", "group", "user"} or type(chat.peer_id) is not int or not 0 < chat.peer_id < 1000000000000:
            raise ValueError("Invalid peer")
        if type(chat.access_hash) is not int or not -(2**63) <= chat.access_hash < 2**63:
            raise ValueError("Invalid access hash")
        if chat.kind != "group" and not chat.access_hash:
            raise ValueError("Channel/user requires access_hash from the same Telegram account")
        if chat.key in chats or chat.marked_id in marked:
            raise ValueError("Duplicate ACL entry")
        chats[chat.key] = chat
        marked.add(chat.marked_id)
    if not chats:
        raise ValueError("ACL must not be empty")
    secrets = {}
    for key in ("api_hash", "session", "token"):
        secret_path = Path(data[key + "_file"])
        if not secret_path.is_absolute():
            raise ValueError("Secret paths must be absolute")
        secrets[key] = protected_text(secret_path)
        if not secrets[key]:
            raise ValueError("Empty secret")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secrets["token"]):
        raise ValueError("Token must be 32-256 URL-safe characters")
    return Config(MappingProxyType(chats), data["api_id"], indexing=indexing, **secrets)
