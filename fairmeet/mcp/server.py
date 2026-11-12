"""阶段 F：MCP 薄适配层。

不引入官方 SDK，只用标准库实现 MCP stdio 传输所需的 JSON-RPC 子集：
`initialize`、`ping`、`tools/list`、`tools/call` 与通知。

工具只是 FairMeet agent HTTP 接口的薄包装——领域语义、权限与审计都在服务端，
这里不做任何本地判断（否则两套规则会漂移）。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, TextIO

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "fairmeet", "version": "0.1.0"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "fairmeet_get_trip_context",
        "description": (
            "读取当前成员可见的旅程上下文：旅程信息、成员、本人需求、"
            "他人偏好（他人硬约束只给数量）、灵感、地点、逐日行程、校验与预算、待确认提案。"
            "不包含邀请令牌。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "fairmeet_get_checks",
        "description": "读取当前旅程的确定性校验结果与预算汇总（硬约束冲突、时间重叠、超支等）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "fairmeet_submit_need",
        "description": (
            "以当前成员身份提交需求。只能提交偏好（preference）或可协商（negotiable）项；"
            "硬约束必须由成员本人确认，本工具会返回 hard_constraint_requires_member。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["preference", "negotiable"]},
                "category": {"type": "string", "description": "例如 interest / budget / dietary"},
                "value": {"type": "string"},
            },
            "required": ["kind", "category", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fairmeet_submit_inspiration",
        "description": (
            "以当前成员身份提交灵感。必须带来源（source_url、source、place_id 或 place_name）；"
            "提交后状态为待确认，不会直接写入行程。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "note": {"type": "string"},
                "source_url": {"type": "string"},
                "source": {"type": "string", "description": "来源标识，例如 agent:web-search"},
                "place_name": {"type": "string"},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
]

ToolHandler = Callable[[dict[str, Any]], Any]


class ToolError(Exception):
    """工具调用失败（来自 FairMeet 的机器可读错误或本地参数问题）。"""

    def __init__(self, message: str, code: str = "tool_error", details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class AgentHttpClient:
    """调用 FairMeet agent HTTP 接口的最小客户端（仅标准库）。"""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {}
            error = parsed.get("error") if isinstance(parsed, dict) else None
            error = error if isinstance(error, dict) else {}
            raise ToolError(
                error.get("message") or f"FairMeet 返回 HTTP {exc.code}",
                error.get("code") or "http_error",
                error.get("details"),
            ) from exc
        except urllib.error.URLError as exc:
            raise ToolError(f"无法连接 FairMeet：{exc.reason}", "connection_failed") from exc

    def context(self) -> Any:
        return self._request("GET", "/api/agent/trip")

    def checks(self) -> Any:
        return self._request("GET", "/api/agent/checks")

    def submit_need(self, kind: str, category: str, value: str) -> Any:
        return self._request(
            "POST", "/api/agent/needs", {"kind": kind, "category": category, "value": value}
        )

    def submit_inspiration(self, **fields: Any) -> Any:
        payload = {k: v for k, v in fields.items() if v is not None}
        return self._request("POST", "/api/agent/inspirations", payload)


def _result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _require(args: dict[str, Any], *names: str) -> None:
    missing = [name for name in names if not str(args.get(name, "")).strip()]
    if missing:
        raise ToolError(f"缺少参数：{', '.join(missing)}", "invalid_arguments", {"missing": missing})


class MCPServer:
    """MCP JSON-RPC 处理器；`client` 只需实现 context/checks/submit_* 方法。"""

    def __init__(self, client: Any) -> None:
        self.client = client

    def _call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "fairmeet_get_trip_context":
            return self.client.context()
        if name == "fairmeet_get_checks":
            return self.client.checks()
        if name == "fairmeet_submit_need":
            _require(args, "kind", "category", "value")
            kind = str(args["kind"])
            if kind not in {"preference", "negotiable"}:
                raise ToolError(
                    "硬约束必须由成员本人确认，MCP 工具不接受 hard",
                    "hard_constraint_requires_member",
                    {"suggested_kind": "preference"},
                )
            return self.client.submit_need(kind, str(args["category"]), str(args["value"]))
        if name == "fairmeet_submit_inspiration":
            _require(args, "title")
            return self.client.submit_inspiration(
                title=str(args["title"]),
                note=args.get("note"),
                source_url=args.get("source_url"),
                source=args.get("source"),
                place_name=args.get("place_name"),
            )
        raise ToolError(f"未知工具：{name}", "unknown_tool")

    def handle(self, message: Any) -> Any:
        if isinstance(message, list):
            responses = [r for r in (self.handle(item) for item in message) if r is not None]
            return responses or None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        method = message.get("method")
        msg_id = message.get("id")
        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        if method == "initialize":
            return _result(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return _result(
                    msg_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"error": {"code": "invalid_arguments", "message": "arguments 必须是对象"}},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        "isError": True,
                    },
                )
            try:
                output = self._call_tool(str(name), args)
            except ToolError as exc:
                payload = {
                    "error": {"code": exc.code, "message": str(exc), "details": exc.details}
                }
                return _result(
                    msg_id,
                    {
                        "content": [
                            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
                        ],
                        "isError": True,
                    },
                )
            return _result(
                msg_id,
                {
                    "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                    "isError": False,
                },
            )
        return _error(msg_id, -32601, f"Method not found: {method}")

    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        """按行读取 JSON-RPC 消息并输出响应（MCP stdio 传输）。"""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(json.dumps(_error(None, -32700, "Parse error")) + "\n")
                stdout.flush()
                continue
            response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()
