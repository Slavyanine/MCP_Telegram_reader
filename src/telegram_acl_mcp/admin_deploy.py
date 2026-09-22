"""Fixed-purpose configuration installer. Only the local admin broker uses this."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import httpx

from .config import IndexConfig, load_config, protected_text


class AdminError(Exception):
    """A deliberately safe, user-visible administrative error."""


def atomic_write(path, value, mode=0o600, gid=None):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value if isinstance(value, bytes) else value.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chmod(temporary, mode)
            if gid is not None:
                os.chown(temporary, 0, gid)
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value, **kwargs):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2), **kwargs)


def fingerprint(path):
    raw = protected_text(path)
    data = json.loads(raw)
    paths = [data[k] for k in ("api_hash_file", "session_file", "token_file")]
    for provider in ("yandex", "deepgram"):
        secret = data.get("indexing", {}).get(provider, {}).get("api_key_file")
        if secret:
            paths.append(secret)
    digest = hashlib.sha256(raw.encode())
    for name in sorted(paths):
        path = Path(name)
        digest.update(name.encode())
        digest.update(protected_text(path).encode() if path.exists() else b"<missing>")
    return digest.hexdigest()


class SystemService:
    unit = "telegram-acl-mcp.service"

    def command(self, action):
        if action not in {"start", "stop", "is-active"}:
            raise ValueError("Unsupported service action")
        result = subprocess.run(["/usr/bin/systemctl", action, self.unit],
                                capture_output=True, timeout=60, check=False)
        if action != "is-active" and result.returncode:
            raise AdminError("Не удалось выполнить операцию со службой MCP.")
        return result.returncode == 0

    def healthy(self, config):
        # A real MCP request verifies startup, authorization and generated ACL schema.
        expected = set(config.chats)
        for _ in range(30):
            if not self.command("is-active"):
                time.sleep(1)
                continue
            try:
                response = httpx.post("http://127.0.0.1:8765/mcp/",
                    headers={"Authorization": "Bearer " + config.token,
                             "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    timeout=2, trust_env=False)
                tools = response.json()["result"]["tools"]
                definitions = {tool["name"]: tool for tool in tools}
                keys = definitions["telegram_get_messages"]["inputSchema"]["properties"]["chat_key"]["enum"]
                if set(keys) == expected and (("telegram_search_context" in definitions) == config.indexing.enabled):
                    if ("telegram_transcribe_voice" in definitions) == (config.indexing.enabled and config.indexing.transcribe):
                        return True
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass
            time.sleep(1)
        return False


class Installer:
    def __init__(self, config_path, state_dir, database_dir, gid=None, service=None):
        self.config_path = Path(config_path)
        self.state_dir = Path(state_dir)
        self.database_dir = Path(database_dir)
        self.gid = gid
        self.service = service or SystemService()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.journal = self.state_dir / "transaction.json"

    def database(self, data):
        path = Path(data.get("indexing", {}).get("database", IndexConfig().database))
        # Administrative backups only operate on databases inside the fixed state directory.
        root = self.database_dir.resolve()
        if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root) or path.resolve() == root:
            raise AdminError("Для панели база индекса должна находиться внутри каталога состояния MCP.")
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise AdminError("Символические ссылки в пути базы не допускаются.")
        return path

    def stage(self, draft):
        revision = self.config_path.parent / "admin-revisions" / uuid.uuid4().hex
        revision.mkdir(parents=True, mode=0o750)
        if os.name == "posix":
            for directory in (revision.parent, revision):
                os.chmod(directory, 0o750)
                os.chown(directory, 0, self.gid)
        data = json.loads(json.dumps(draft["config"]))
        for name, secret in draft["secrets"].items():
            path = revision / (name + ".secret")
            atomic_write(path, secret, mode=0o640, gid=self.gid)
            if name in ("session", "api_hash"):
                data[name + "_file"] = str(path)
            elif name in ("yandex", "deepgram"):
                data.setdefault("indexing", {}).setdefault(name, {})["api_key_file"] = str(path)
            else:
                raise AdminError("Неизвестный тип секрета.")
        candidate = revision / "config.json"
        write_json(candidate, data, mode=0o640, gid=self.gid)
        cfg = load_config(candidate)
        self.database(data)
        return candidate, cfg

    def recover(self):
        if not self.journal.exists():
            return False
        transaction = json.loads(protected_text(self.journal))
        self.service.command("stop")
        backup = Path(transaction["backup"])
        for record in transaction["databases"]:
            path = self.database({"indexing": {"database": record["path"]}})
            for suffix in ("", "-wal", "-shm"):
                target = Path(str(path) + suffix)
                if target.is_symlink():
                    raise AdminError("Восстановление остановлено: обнаружена символическая ссылка.")
                source = backup / (record["slot"] + suffix)
                if source.exists():
                    shutil.copy2(source, target)
                    if os.name == "posix":
                        # The service user owns its SQLite files, unlike configuration files.
                        os.chown(target, transaction["uid"], transaction["gid"])
                else:
                    target.unlink(missing_ok=True)
        atomic_write(self.config_path, (backup / "config.json").read_bytes(), mode=0o640, gid=self.gid)
        if transaction["was_active"]:
            self.service.command("start")
            if not self.service.healthy(load_config(self.config_path)):
                raise AdminError("Откат восстановил файлы, но служба не прошла проверку. Требуется администратор VPS.")
        self.journal.unlink()
        return True

    def apply(self, draft):
        if self.journal.exists():
            raise AdminError("Предыдущая операция требует восстановления; перезапустите административную службу.")
        if fingerprint(self.config_path) != draft["base"]:
            raise AdminError("Рабочая конфигурация изменилась. Сбросьте черновик и загрузите её заново.")
        candidate, cfg = self.stage(draft)
        old = json.loads(protected_text(self.config_path))
        new = json.loads(protected_text(candidate))
        paths = list(dict.fromkeys([self.database(old), self.database(new)]))
        backup = self.state_dir / ("backup-" + uuid.uuid4().hex)
        backup.mkdir(mode=0o700)
        was_active = self.service.command("is-active")
        # Record stop intent before stopping: a broker crash must not silently leave MCP stopped.
        intent = self.state_dir / "stop-intent.json"
        write_json(intent, {"was_active": was_active})
        self.service.command("stop")
        try:
            if fingerprint(self.config_path) != draft["base"]:
                raise AdminError("Конфигурация была изменена другим администратором.")
            atomic_write(backup / "config.json", self.config_path.read_bytes())
            secret_paths = [old[k] for k in ("api_hash_file", "session_file", "token_file")]
            for provider in ("yandex", "deepgram"):
                secret_path = old.get("indexing", {}).get(provider, {}).get("api_key_file")
                if secret_path and Path(secret_path).exists():
                    secret_paths.append(secret_path)
            manifest = {}
            for index, secret_path in enumerate(dict.fromkeys(secret_paths)):
                name = "secret-" + str(index)
                atomic_write(backup / name, protected_text(Path(secret_path)))
                manifest[name] = secret_path
            write_json(backup / "secrets.json", manifest)
            records = []
            uid = gid = 0
            if os.name == "posix":
                import pwd
                info = pwd.getpwnam("telegram-mcp")
                uid, gid = info.pw_uid, info.pw_gid
            for index, path in enumerate(paths):
                slot = "database-" + str(index)
                records.append({"path": str(path), "slot": slot})
                for suffix in ("", "-wal", "-shm"):
                    source = Path(str(path) + suffix)
                    if source.is_symlink():
                        raise AdminError("Символические ссылки в файлах базы не допускаются.")
                    if source.exists():
                        shutil.copy2(source, backup / (slot + suffix))
            # No state-changing startup happens until the entire backup is durable.
            for file in backup.iterdir():
                with file.open("rb+") as stream:
                    os.fsync(stream.fileno())
            if os.name == "posix":
                directory = os.open(backup, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            write_json(self.journal, {"backup": str(backup), "databases": records,
                                     "was_active": was_active, "uid": uid, "gid": gid})
            intent.unlink(missing_ok=True)
            atomic_write(self.config_path, candidate.read_bytes(), mode=0o640, gid=self.gid)
            self.service.command("start")
            if not self.service.healthy(cfg):
                raise AdminError("Новая конфигурация не прошла проверку запуска.")
            self.journal.unlink()
            return {"applied": True, "message": "Настройки применены. Переподключите Hermes к MCP."}
        except Exception:
            if self.journal.exists():
                try:
                    self.recover()
                except Exception:
                    raise AdminError("Применение и автоматический откат не завершены. Требуется администратор VPS; журнал восстановления сохранён.") from None
            elif was_active:
                self.service.command("start")
            intent.unlink(missing_ok=True)
            raise AdminError("Применение не выполнено. Предыдущее состояние восстановлено.") from None

    def recover_startup(self):
        recovered = self.recover()
        intent = self.state_dir / "stop-intent.json"
        if intent.exists():
            if not recovered and json.loads(protected_text(intent))["was_active"]:
                self.service.command("start")
            intent.unlink()
