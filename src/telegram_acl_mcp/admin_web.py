"""Unprivileged loopback web UI and fixed-operation local administrative broker."""
import argparse
import asyncio
from contextlib import suppress
import hmac
import json
import logging
import os
from pathlib import Path
import re
import stat

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
import uvicorn

from .admin_control import AdminController
from .admin_deploy import AdminError, Installer
from .config import protected_text


OPERATIONS = {"state", "dialogs", "save", "discard", "login/start", "login/finish", "login/cancel", "preview", "apply"}
REQUEST_LIMIT = 1_000_000
RESPONSE_LIMIT = 16_000_000
ASSETS = Path(__file__).parent / "admin_static"


async def broker_call(socket_path, operation, data):
    async with asyncio.timeout(300):
        reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=RESPONSE_LIMIT)
        try:
            writer.write(json.dumps({"operation": operation, "data": data}).encode() + b"\n")
            await writer.drain()
            response = json.loads(await reader.readline())
            if "error" in response:
                raise AdminError(response["error"])
            return response["result"]
        finally:
            writer.close()
            await writer.wait_closed()


def build_web(token, dispatch, port=8766):
    expected = ("Bearer " + token).encode("ascii")
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {"http://" + host for host in hosts}

    async def endpoint(request):
        operation = request.path_params["operation"]
        if operation not in OPERATIONS:
            return JSONResponse({"error": "Неизвестная операция."}, status_code=404)
        authorizations = request.headers.getlist("authorization")
        if len(authorizations) != 1 or not hmac.compare_digest(authorizations[0].encode("utf-8"), expected):
            return JSONResponse({"error": "Неверный токен администратора."}, status_code=401)
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return JSONResponse({"error": "Ожидается JSON."}, status_code=415)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > REQUEST_LIMIT:
                return JSONResponse({"error": "Слишком большой запрос."}, status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return JSONResponse({"error": "Некорректный JSON."}, status_code=400)
        try:
            return JSONResponse(await dispatch(operation, data))
        except AdminError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception:
            logging.warning("Admin backend unavailable (operation=%s)", operation)
            return JSONResponse({"error": "Административная служба недоступна. После применения проверьте состояние перед повтором."}, status_code=503)

    async def index(request):
        return FileResponse(ASSETS / "index.html")

    async def asset(request):
        name = request.path_params["name"]
        if name not in {"app.js", "style.css"}:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(ASSETS / name)

    app = Starlette(routes=[Route("/", index), Route("/assets/{name}", asset),
                            Route("/api/{operation:path}", endpoint, methods=["POST"])])

    class Guard:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            headers = scope["headers"]
            host = [v.decode("latin1") for k, v in headers if k == b"host"]
            origin = [v.decode("latin1") for k, v in headers if k == b"origin"]
            if len(host) != 1 or host[0] not in hosts or (origin and (len(origin) != 1 or origin[0] not in origins)):
                return await JSONResponse({"error": "Недопустимый адрес или источник запроса."}, status_code=403)(scope, receive, send)

            async def secure_send(message):
                if message["type"] == "http.response.start":
                    message["headers"] = list(message["headers"]) + [
                        (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                        (b"content-security-policy", b"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
                await send(message)
            return await app(scope, receive, secure_send)
    return Guard()


async def serve_broker(args):
    import grp
    import fcntl
    if os.geteuid() != 0:
        raise SystemExit("The fixed-purpose broker must run as root")
    os.umask(0o077)
    gid = grp.getgrnam("telegram-mcp").gr_gid
    admin_gid = grp.getgrnam("telegram-acl-admin").gr_gid
    installer = Installer(args.config, args.state, args.databases, gid=gid)
    # A second broker must not unlink a live socket or run another installer.
    process_lock = (installer.state_dir / "broker.lock").open("a+b")
    try:
        fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another administrative broker is running") from None
    await asyncio.to_thread(installer.recover_startup)
    controller = AdminController(installer)

    async def handle(reader, writer):
        try:
            async with asyncio.timeout(10):
                raw = await reader.readline()
            if len(raw) > REQUEST_LIMIT:
                raise AdminError("Слишком большой запрос.")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"operation", "data"} or payload["operation"] not in OPERATIONS or not isinstance(payload["data"], dict):
                raise AdminError("Некорректная операция.")
            result = {"result": await controller.dispatch(payload["operation"], payload["data"])}
        except AdminError as exc:
            result = {"error": str(exc)}
        except Exception:
            logging.warning("Administrative operation failed; details suppressed to protect credentials")
            result = {"error": "Операция не выполнена. Проверьте параметры и состояние службы."}
        try:
            writer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    if args.socket.exists():
        if not stat.S_ISSOCK(args.socket.lstat().st_mode):
            raise SystemExit("Refusing to replace a non-socket file")
        args.socket.unlink()
    server = await asyncio.start_unix_server(handle, path=str(args.socket), limit=REQUEST_LIMIT + 1)
    os.chown(args.socket, 0, admin_gid)
    os.chmod(args.socket, 0o660)

    async def expiry():
        while True:
            await asyncio.sleep(30)
            async with controller.lock:
                await controller.expire_login()

    task = asyncio.create_task(expiry())
    try:
        async with server:
            await server.serve_forever()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await controller.close()
        process_lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, default=Path("/etc/telegram-acl-admin/token.secret"))
    parser.add_argument("--socket", type=Path, default=Path("/run/telegram-acl-admin/broker.sock"))
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    token = protected_text(args.token_file)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise SystemExit("Invalid admin token")

    async def dispatch(operation, data):
        return await broker_call(args.socket, operation, data)

    uvicorn.run(build_web(token, dispatch, args.port), host="127.0.0.1", port=args.port,
                access_log=False, log_level="warning", proxy_headers=False)


def broker_main():
    parser = argparse.ArgumentParser(description="Fixed-purpose root admin broker (Unix socket only)")
    parser.add_argument("--config", type=Path, default=Path("/etc/telegram-acl-mcp/config.json"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/telegram-acl-admin"))
    parser.add_argument("--databases", type=Path, default=Path("/var/lib/telegram-acl-mcp"))
    parser.add_argument("--socket", type=Path, default=Path("/run/telegram-acl-admin/broker.sock"))
    args = parser.parse_args()
    if os.name != "posix":
        raise SystemExit("The production broker requires Linux/systemd")
    asyncio.run(serve_broker(args))
