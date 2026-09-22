"""Administrative operations, deliberately absent from the MCP interface."""
import asyncio
import copy
import json
import re
import time
import uuid
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User

from .admin_deploy import AdminError, fingerprint, write_json
from .config import index_config, protected_text


def make_client(session, api_id, api_hash):
    return TelegramClient(StringSession(session), api_id, api_hash,
                          receive_updates=False, flood_sleep_threshold=0, request_retries=0)


def chat_id(row):
    return row["kind"] + ":" + str(row["peer_id"])


def account_view(account):
    return {"id": str(account.id), "name": " ".join(filter(None, [account.first_name, getattr(account, "last_name", None)])),
            "username": getattr(account, "username", None), "premium": bool(getattr(account, "premium", False))}


class AdminController:
    def __init__(self, installer, client_factory=make_client):
        self.installer = installer
        self.client_factory = client_factory
        self.saved_path = installer.state_dir / "draft.json"
        self.lock = asyncio.Lock()
        self.client = None
        self.login = None
        self.iterator = None
        self.more = True
        self.revision = uuid.uuid4().hex
        self.draft = json.loads(protected_text(self.saved_path)) if self.saved_path.exists() else self.fresh()
        self.catalog = {chat_id(row): dict(row) for row in self.draft["config"]["chats"]}

    def fresh(self):
        data = json.loads(protected_text(self.installer.config_path))
        secrets = {key: protected_text(Path(data[key + "_file"])) for key in ("api_hash", "session")}
        for provider in ("yandex", "deepgram"):
            file = data.get("indexing", {}).get(provider, {}).get("api_key_file")
            if file and Path(file).exists():
                secrets[provider] = protected_text(Path(file))
        return {"base": fingerprint(self.installer.config_path), "config": data,
                "secrets": secrets, "account": None, "new_account": False}

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()
        self.client = self.iterator = None
        self.more = True

    async def cancel_login(self):
        if self.login:
            await self.login["client"].disconnect()
        self.login = None

    async def close(self):
        await self.cancel_login()
        await self.disconnect()

    async def expire_login(self):
        if self.login and time.monotonic() > self.login["expires"]:
            await self.cancel_login()

    async def connected(self):
        if not self.client:
            secrets = self.draft["secrets"]
            client = self.client_factory(secrets["session"], self.draft["config"]["api_id"], secrets["api_hash"])
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise AdminError("Сессия Telegram недействительна. Выполните вход заново.")
                account = await client.get_me()
                if account is None:
                    raise AdminError("Не удалось определить аккаунт Telegram.")
                self.draft["account"] = account_view(account)
            except BaseException:
                await client.disconnect()
                raise
            self.client = client
        return self.client

    def view(self):
        selected = {chat_id(row): row for row in self.draft["config"]["chats"]}
        rows = []
        for ident, row in self.catalog.items():
            chosen = selected.get(ident)
            rows.append({"id": ident, "kind": row["kind"], "name": row.get("name", row["title"]),
                         "selected": chosen is not None, "key": chosen["key"] if chosen else "",
                         "title": chosen["title"] if chosen else row["title"],
                         "available": row["kind"] == "group" or bool(row.get("access_hash"))})
        cfg = index_config(self.draft["config"].get("indexing", {}))
        return {"revision": self.revision, "account": self.draft["account"], "new_account": self.draft["new_account"],
                "api_id": self.draft["config"]["api_id"], "chats": rows, "more": self.more,
                "settings": {"enabled": cfg.enabled, "transcribe": cfg.transcribe, "backend": cfg.backend},
                "keys": {p: bool(self.draft["secrets"].get(p)) for p in ("yandex", "deepgram")},
                "saved": self.saved_path.exists(), "conflict": fingerprint(self.installer.config_path) != self.draft["base"]}

    def save(self, payload):
        if payload.get("revision") != self.revision:
            raise AdminError("Черновик изменён в другой вкладке. Обновите страницу.")
        if set(payload) != {"revision", "chats", "settings", "keys"}:
            raise AdminError("Некорректные поля черновика.")
        if not isinstance(payload["chats"], list) or not 1 <= len(payload["chats"]) <= 10000:
            raise AdminError("Выберите хотя бы один чат (не более 10000).")
        candidate = copy.deepcopy(self.draft)
        rows, used_keys, used_peers = [], set(), set()
        for choice in payload["chats"]:
            if not isinstance(choice, dict) or set(choice) != {"id", "key", "title"}:
                raise AdminError("Некорректный выбор чата.")
            ident, key, title = choice["id"], choice["key"], choice["title"]
            if not isinstance(ident, str) or ident not in self.catalog:
                raise AdminError("Чат отсутствует в списке текущего аккаунта.")
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise AdminError("Ключ: латинская строчная буква, затем буквы, цифры или _, до 64 символов.")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
                raise AdminError("Название чата должно содержать от 1 до 200 символов.")
            if key in used_keys or ident in used_peers:
                raise AdminError("Повторяющийся ключ или чат.")
            entity = self.catalog[ident]
            if entity["kind"] != "group" and not entity.get("access_hash"):
                raise AdminError("Telegram не вернул параметры доступа к выбранному чату.")
            rows.append({"key": key, "title": title.strip(), **{k: entity[k] for k in ("kind", "peer_id", "access_hash")}})
            used_keys.add(key)
            used_peers.add(ident)
        settings = payload["settings"]
        if not isinstance(settings, dict) or set(settings) != {"enabled", "transcribe", "backend"}:
            raise AdminError("Некорректные настройки режимов.")
        candidate["config"].setdefault("indexing", {}).update(settings)
        keys = payload["keys"]
        if not isinstance(keys, dict) or set(keys) - {"yandex", "deepgram"}:
            raise AdminError("Некорректные ключи провайдеров.")
        for provider, secret in keys.items():
            if not isinstance(secret, str) or len(secret) > 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in secret):
                raise AdminError("API-ключ должен быть непустой строкой без пробелов.")
            if secret:
                candidate["secrets"][provider] = secret
                # stage() replaces this internal placeholder with the immutable secret path.
                candidate["config"]["indexing"].setdefault(provider, {})["api_key_file"] = str(self.installer.config_path.parent / (provider + ".secret"))
        try:
            cfg = index_config(candidate["config"]["indexing"])
        except (ValueError, TypeError):
            raise AdminError("Проверьте настройки и API-ключ выбранного провайдера.") from None
        if cfg.enabled and cfg.transcribe and cfg.backend != "telegram" and not candidate["secrets"].get(cfg.backend):
            raise AdminError("Введите API-ключ выбранного провайдера.")
        candidate["config"]["chats"] = rows
        write_json(self.saved_path, candidate)
        self.draft = candidate
        self.revision = uuid.uuid4().hex
        return self.view()

    async def dialogs(self):
        client = await self.connected()
        if self.iterator is None:
            self.iterator = client.iter_dialogs().__aiter__()
        for _ in range(100):
            try:
                dialog = await self.iterator.__anext__()
            except StopAsyncIteration:
                self.more = False
                break
            entity = dialog.entity
            kind = "channel" if isinstance(entity, Channel) else "group" if isinstance(entity, Chat) else "user" if isinstance(entity, User) else None
            if kind and not getattr(entity, "migrated_to", None) and not getattr(entity, "deleted", False):
                row = {"kind": kind, "peer_id": entity.id, "access_hash": getattr(entity, "access_hash", 0) or 0,
                       "title": dialog.name or str(entity.id), "name": dialog.name or str(entity.id)}
                self.catalog[chat_id(row)] = row
        return self.view()

    async def begin_login(self, data):
        phone = data.get("phone", "")
        api_id = data.get("api_id") or self.draft["config"]["api_id"]
        api_hash = data.get("api_hash") or self.draft["secrets"]["api_hash"]
        if not isinstance(phone, str) or not re.fullmatch(r"\+[0-9]{7,15}", phone):
            raise AdminError("Введите телефон в международном формате: +79991234567.")
        if type(api_id) is not int or api_id <= 0 or not isinstance(api_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise AdminError("Проверьте API ID и API Hash.")
        await self.cancel_login()
        client = self.client_factory("", api_id, api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except BaseException:
            await client.disconnect()
            raise
        self.login = {"client": client, "phone": phone, "hash": sent.phone_code_hash,
                      "api_id": api_id, "api_hash": api_hash, "expires": time.monotonic() + 600, "step": "code"}
        return {"step": "code", "message": "Введите код, доставленный Telegram. Способ доставки определяет Telegram."}

    async def finish_login(self, data):
        if not self.login:
            raise AdminError("Время входа истекло. Запросите новый код.")
        login = self.login
        client = login["client"]
        try:
            if login["step"] == "password":
                password = data.get("password")
                if not isinstance(password, str) or not password or len(password) > 1024:
                    raise AdminError("Введите пароль двухэтапной аутентификации.")
                account = await client.sign_in(password=password)
            else:
                code = data.get("code")
                if not isinstance(code, str) or not re.fullmatch(r"[0-9]{3,10}", code):
                    raise AdminError("Введите код подтверждения.")
                account = await client.sign_in(phone=login["phone"], code=code, phone_code_hash=login["hash"])
        except SessionPasswordNeededError:
            login["step"] = "password"
            return {"step": "password", "message": "Введите пароль двухэтапной аутентификации Telegram."}
        candidate = copy.deepcopy(self.draft)
        candidate["config"]["api_id"] = login["api_id"]
        candidate["config"]["chats"] = []
        candidate["config"].setdefault("indexing", {})["database"] = str(self.installer.database_dir / ("index-" + str(account.id) + "-" + uuid.uuid4().hex + ".sqlite3"))
        candidate["secrets"].update(session=client.session.save(), api_hash=login["api_hash"])
        candidate.update(account=account_view(account), new_account=True)
        await self.disconnect()
        self.draft, self.catalog = candidate, {}
        self.client = client
        self.login = None
        self.revision = uuid.uuid4().hex
        # A saved previous-account draft must not reappear after a broker restart.
        write_json(self.saved_path, self.draft)
        return {"step": "complete", "state": self.view()}

    def summary(self):
        old = json.loads(protected_text(self.installer.config_path))
        before = {chat_id(row): row for row in old["chats"]}
        after = {chat_id(row): row for row in self.draft["config"]["chats"]}
        original_secrets = self.fresh()["secrets"]
        return {"revision": self.revision, "new_account": self.draft["new_account"],
                "added": [r["title"] for k, r in after.items() if k not in before],
                "removed": [r["title"] for k, r in before.items() if k not in after],
                "edited": [f'{before[k]["key"]} / {before[k]["title"]} → {r["key"]} / {r["title"]}'
                           for k, r in after.items() if k in before and r != before[k]],
                "keys_changed": [p for p in ("yandex", "deepgram") if self.draft["secrets"].get(p) != original_secrets.get(p)],
                "before": {k: getattr(index_config(old.get("indexing", {})), k) for k in ("enabled", "transcribe", "backend")},
                "after": self.view()["settings"]}

    async def dispatch(self, method, data):
        async with self.lock:
            try:
                await self.expire_login()
                if method == "state":
                    view = self.view()
                    view["service_active"] = await asyncio.to_thread(self.installer.service.command, "is-active")
                    return view
                if method == "dialogs":
                    async with asyncio.timeout(45):
                        return await self.dialogs()
                if method == "save":
                    return self.save(data)
                if method == "discard":
                    await self.close()
                    self.draft = self.fresh()
                    self.catalog = {chat_id(r): dict(r) for r in self.draft["config"]["chats"]}
                    self.saved_path.unlink(missing_ok=True)
                    self.revision = uuid.uuid4().hex
                    return self.view()
                if method == "login/start":
                    async with asyncio.timeout(45):
                        return await self.begin_login(data)
                if method == "login/finish":
                    async with asyncio.timeout(45):
                        return await self.finish_login(data)
                if method == "login/cancel":
                    await self.cancel_login()
                    return {"cancelled": True}
                if method == "preview":
                    return self.summary()
                if method == "apply":
                    if data.get("revision") != self.revision:
                        raise AdminError("Черновик изменился. Повторите проверку изменений.")
                    async with asyncio.timeout(45):
                        await self.connected()
                        if not await self.client.is_user_authorized():
                            raise AdminError("Сессия Telegram больше не авторизована.")
                    await self.disconnect()
                    result = await asyncio.to_thread(self.installer.apply, copy.deepcopy(self.draft))
                    self.saved_path.unlink(missing_ok=True)
                    self.draft = self.fresh()
                    self.revision = uuid.uuid4().hex
                    return result
                raise AdminError("Неизвестная административная операция.")
            except FloodWaitError as exc:
                raise AdminError(f"Telegram ограничил частоту запросов. Повторите через {exc.seconds} секунд.") from None
            except RPCError as exc:
                messages = {"PhoneCodeInvalidError": "Неверный код подтверждения.",
                            "PhoneCodeExpiredError": "Код истёк. Запросите новый код.",
                            "PasswordHashInvalidError": "Неверный пароль 2FA.",
                            "PhoneNumberInvalidError": "Неверный номер телефона.",
                            "ApiIdInvalidError": "Неверные API ID или API Hash."}
                raise AdminError(messages.get(type(exc).__name__, "Telegram отклонил запрос. Проверьте аккаунт и повторите позже.")) from None
            except TimeoutError:
                raise AdminError("Telegram не ответил вовремя. Повторите операцию.") from None
