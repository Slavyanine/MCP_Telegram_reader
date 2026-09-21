import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from telegram_acl_mcp.server import build_app
from test_reader import CHAT, FakeClient


class ConnectedFake(FakeClient):
    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True


@pytest.fixture
def http():
    telegram = ConnectedFake()
    cfg = SimpleNamespace(chats={CHAT.key: CHAT}, token="x" * 48)
    with TestClient(build_app(cfg, telegram), base_url="http://127.0.0.1:8765") as client:
        yield client, telegram


def rpc(http, method, params=None, token="x" * 48):
    client, _ = http
    return client.post("/mcp/", headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}, json={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})})


def test_mcp_initialize_and_tools(http):
    init = rpc(http, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert init.status_code == 200
    tools = rpc(http, "tools/list").json()["result"]["tools"]
    assert len(tools) == 5
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"]
        assert not tool["inputSchema"]["additionalProperties"]
        assert "chat_id" not in tool["inputSchema"]["properties"]
        if "chat_key" in tool["inputSchema"]["properties"]:
            assert tool["inputSchema"]["properties"]["chat_key"]["enum"] == ["project_a"]


@pytest.mark.parametrize("arguments", [
    {"chat_id": -100123}, {"chat_key": "other"}, {"chat_key": -100123},
    {"chat_key": "project_a", "chat_id": -100999},
    {"chat_key": "project_a", "limit": 101}, {"chat_key": "project_a", "limit": 0},
    {"chat_key": "project_a", "limit": True}, {"chat_key": "project_a", "limit": "30"},
    {"chat_key": "project_a", "config": {"allow_all": True}},
])
def test_mcp_rejects_bypass_before_rpc(http, arguments):
    result = rpc(http, "tools/call", {"name": "telegram_get_messages", "arguments": arguments}).json()["result"]
    assert result["isError"]
    assert http[1].calls == []


def test_read_and_list(http):
    listed = rpc(http, "tools/call", {"name": "telegram_list_allowed_chats", "arguments": {}}).json()["result"]
    assert json.loads(listed["content"][0]["text"])["chats"] == [{"chat_key": "project_a", "title": "Project A"}]
    result = rpc(http, "tools/call", {"name": "telegram_get_messages", "arguments": {"chat_key": "project_a", "limit": 2}}).json()["result"]
    assert not result["isError"]
    assert len(json.loads(result["content"][0]["text"])["messages"]) == 2


@pytest.mark.parametrize("method", ["telegram_send_message", "telegram_join_chat", "telegram_set_acl", "telegram_raw_request"])
def test_mutating_tools_absent(http, method):
    result = rpc(http, "tools/call", {"name": method, "arguments": {}}).json()["result"]
    assert result["isError"] and http[1].calls == []


def test_auth_all_http_methods(http):
    for method in ("GET", "POST", "DELETE", "OPTIONS"):
        assert http[0].request(method, "/mcp/").status_code == 401
    assert rpc(http, "tools/list", token="wrong").status_code == 401
    assert http[1].calls == []


def test_host_guard(http):
    response = http[0].post("/mcp/", headers={"Host": "attacker.invalid", "Authorization": "Bearer " + "x" * 48}, json={})
    assert response.status_code == 421


def test_exception_does_not_disclose_secrets(http, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("SECRET_SESSION_VALUE")
        yield
    monkeypatch.setattr(http[1], "iter_messages", fail)
    result = rpc(http, "tools/call", {"name": "telegram_get_messages", "arguments": {"chat_key": "project_a"}})
    assert result.json()["result"]["isError"]
    assert "SECRET_SESSION_VALUE" not in result.text
