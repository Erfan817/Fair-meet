"""阶段 F：MCP 薄适配层（不联网，使用假客户端与进程内 TestClient）。"""

from __future__ import annotations

import io
import json
import urllib.error
import warnings

import pytest

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from fairmeet.app import create_app  # noqa: E402
from fairmeet.db import Database  # noqa: E402
from fairmeet.mcp.server import AgentHttpClient, MCPServer, ToolError  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def context(self):
        self.calls.append(("context",))
        return {"trip": {"id": "t1"}, "needs": {"mine": []}}

    def checks(self):
        self.calls.append(("checks",))
        return {"check": {"ok": True}, "budget": {}}

    def submit_need(self, kind, category, value):
        self.calls.append(("submit_need", kind, category, value))
        return {"id": "n1", "kind": kind, "category": category, "value": value}

    def submit_inspiration(self, **fields):
        self.calls.append(("submit_inspiration", fields))
        return {"id": "i1", **fields}


def _call(server: MCPServer, name: str, arguments: dict) -> dict:
    return server.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


def test_initialize_and_ping():
    server = MCPServer(FakeClient())
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in init["result"]["capabilities"]
    assert init["result"]["serverInfo"]["name"] == "fairmeet"

    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}


def test_tools_list_exposes_read_and_write_tools():
    server = MCPServer(FakeClient())
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {
        "fairmeet_get_trip_context",
        "fairmeet_get_checks",
        "fairmeet_submit_need",
        "fairmeet_submit_inspiration",
    }
    need_schema = next(t for t in tools if t["name"] == "fairmeet_submit_need")["inputSchema"]
    assert need_schema["properties"]["kind"]["enum"] == ["preference", "negotiable"]


def test_tools_call_returns_json_text():
    server = MCPServer(FakeClient())
    response = _call(server, "fairmeet_get_trip_context", {})
    result = response["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["trip"]["id"] == "t1"


def test_tools_call_submit_need_and_inspiration():
    client = FakeClient()
    server = MCPServer(client)

    _call(server, "fairmeet_submit_need", {"kind": "preference", "category": "兴趣", "value": "博物馆"})
    _call(server, "fairmeet_submit_inspiration", {"title": "火锅", "source_url": "https://e.test/1"})

    assert client.calls[0] == ("submit_need", "preference", "兴趣", "博物馆")
    assert client.calls[1][0] == "submit_inspiration"
    assert client.calls[1][1]["source_url"] == "https://e.test/1"


def test_hard_constraint_is_rejected_before_reaching_server():
    client = FakeClient()
    server = MCPServer(client)
    response = _call(
        server, "fairmeet_submit_need", {"kind": "hard", "category": "日期", "value": "周日"}
    )
    result = response["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["error"]["code"] == (
        "hard_constraint_requires_member"
    )
    assert client.calls == []


def test_tool_errors_are_reported_with_codes():
    class FailingClient(FakeClient):
        def submit_inspiration(self, **fields):
            raise ToolError("必须带来源", "source_required", {"accepted": ["source_url"]})

    server = MCPServer(FailingClient())
    result = _call(server, "fairmeet_submit_inspiration", {"title": "无来源"})["result"]
    assert result["isError"] is True
    error = json.loads(result["content"][0]["text"])["error"]
    assert error["code"] == "source_required"
    assert error["details"]["accepted"] == ["source_url"]


def test_missing_arguments_and_unknown_tools():
    server = MCPServer(FakeClient())
    missing = _call(server, "fairmeet_submit_need", {"kind": "preference"})["result"]
    assert json.loads(missing["content"][0]["text"])["error"]["code"] == "invalid_arguments"

    unknown = _call(server, "fairmeet_do_everything", {})["result"]
    assert json.loads(unknown["content"][0]["text"])["error"]["code"] == "unknown_tool"


def test_notifications_get_no_response_and_unknown_method_errors():
    server = MCPServer(FakeClient())
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    error = server.handle({"jsonrpc": "2.0", "id": 5, "method": "resources/list"})
    assert error["error"]["code"] == -32601


def test_serve_reads_lines_and_skips_notifications():
    server = MCPServer(FakeClient())
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\nnot-json\n")
    stdout = io.StringIO()
    server.serve(stdin, stdout)

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(responses) == 3  # initialize / tools/list / parse error
    assert responses[0]["id"] == 1
    assert responses[1]["result"]["tools"]
    assert responses[2]["error"]["code"] == -32700


def test_http_client_sends_bearer_and_parses_structured_errors(monkeypatch):
    seen: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"ok": True}).encode()

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse()

    monkeypatch.setattr("fairmeet.mcp.server.urllib.request.urlopen", fake_urlopen)
    client = AgentHttpClient("http://fairmeet.test/", "fm_agent_x")
    assert client.context() == {"ok": True}
    assert seen["url"] == "http://fairmeet.test/api/agent/trip"
    assert seen["auth"] == "Bearer fm_agent_x"

    def failing_urlopen(request, timeout):
        body = io.BytesIO(
            json.dumps(
                {"error": {"code": "token_revoked", "message": "令牌已被撤销", "details": {}}}
            ).encode()
        )
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, body)

    monkeypatch.setattr("fairmeet.mcp.server.urllib.request.urlopen", failing_urlopen)
    with pytest.raises(ToolError) as excinfo:
        client.context()
    assert excinfo.value.code == "token_revoked"


class _InProcessAgentClient:
    """用 TestClient 模拟真实 HTTP 层，验证 MCP → API → 服务 的完整接线。"""

    def __init__(self, client: TestClient, token: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    def context(self):
        response = self.client.get("/api/agent/trip", headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def checks(self):
        return self.client.get("/api/agent/checks", headers=self.headers).json()

    def submit_need(self, kind, category, value):
        return self.client.post(
            "/api/agent/needs",
            headers=self.headers,
            json={"kind": kind, "category": category, "value": value},
        ).json()

    def submit_inspiration(self, **fields):
        payload = {k: v for k, v in fields.items() if v is not None}
        return self.client.post("/api/agent/inspirations", headers=self.headers, json=payload).json()


def test_mcp_tools_reach_the_real_service_layer():
    client = TestClient(create_app(Database(":memory:")))
    trip = client.post(
        "/api/trips", json={"title": "MCP 行程", "owner_name": "小林", "destination": "成都"}
    ).json()
    trip_id = trip["trip"]["id"]
    owner = trip["owner"]["id"]
    delegation = client.post(
        f"/api/trips/{trip_id}/agent-delegations?member_id={owner}",
        json={"agent_name": "MCP Agent"},
    ).json()

    server = MCPServer(_InProcessAgentClient(client, delegation["token"]))

    context = _call(server, "fairmeet_get_trip_context", {})["result"]
    assert context["isError"] is False
    assert json.loads(context["content"][0]["text"])["trip"]["title"] == "MCP 行程"

    submitted = _call(
        server,
        "fairmeet_submit_need",
        {"kind": "negotiable", "category": "预算", "value": "人均不超过 500"},
    )["result"]
    assert submitted["isError"] is False
    assert json.loads(submitted["content"][0]["text"])["source"] == "agent:MCP Agent"

    needs = client.get(f"/api/trips/{trip_id}/needs").json()
    assert any(n["value"] == "人均不超过 500" for n in needs)
