"""Small, single-instance demo gate; not a replacement for per-user authorization."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from fairmeet.db import Database

COOKIE = "fairmeet_demo"
AI_PATH = re.compile(r"^/api/trips/[^/]+/ai/chat$")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class DemoSettings:
    enabled: bool = False
    access_code: str = field(default="", repr=False)
    session_secret: str = field(default="", repr=False)
    secure_cookie: bool = True
    session_hours: int = 12
    writes_per_minute: int = 30
    ai_per_minute: int = 5
    ai_daily_limit: int = 100
    max_body_bytes: int = 65536

    def __post_init__(self):
        if self.enabled and len(self.access_code) < 12:
            raise ValueError("Demo 模式需要至少 12 字符的 FAIRMEET_DEMO_ACCESS_CODE")
        if any(v <= 0 for v in (self.session_hours, self.writes_per_minute, self.ai_per_minute,
                                self.ai_daily_limit, self.max_body_bytes)):
            raise ValueError("Demo 限额与会话有效期必须为正整数")

    @classmethod
    def from_env(cls) -> DemoSettings:
        return cls(
            enabled=os.getenv("FAIRMEET_DEMO", "0") == "1",
            access_code=os.getenv("FAIRMEET_DEMO_ACCESS_CODE", ""),
            session_secret=os.getenv("FAIRMEET_DEMO_SESSION_SECRET", ""),
            secure_cookie=os.getenv("FAIRMEET_DEMO_SECURE_COOKIE", "1") != "0",
            writes_per_minute=int(os.getenv("FAIRMEET_DEMO_WRITES_PER_MINUTE", "30")),
            ai_per_minute=int(os.getenv("FAIRMEET_DEMO_AI_PER_MINUTE", "5")),
            ai_daily_limit=int(os.getenv("FAIRMEET_DEMO_AI_DAILY_LIMIT", "100")),
        )


class DemoGuard:
    """ASGI middleware: bounded body, signed cookie, same-origin writes and quotas.

    No IP headers are trusted. Minute limits are process-local and bounded; the
    shared daily AI quota is persisted in SQLite and survives process restarts.
    Run exactly one worker/instance; use an edge limiter for a larger deployment.
    """

    def __init__(self, app, settings: DemoSettings, database: Database, templates):
        self.app, self.settings, self.db, self.templates = app, settings, database, templates
        self.key = hashlib.sha256(
            (settings.access_code + ":" + (settings.session_secret or secrets.token_urlsafe(32))).encode()
        ).digest()
        self.windows: dict[str, deque] = {}
        self.lock = threading.Lock()
        with self.db.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS demo_ai_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL)")

    def _signature(self, value: str) -> str:
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def _issue(self) -> str:
        payload = f"{secrets.token_hex(16)}.{int(time.time())}"
        return f"{payload}.{self._signature(payload)}"

    def _session(self, token: str) -> str | None:
        try:
            sid, issued, sig = token.split(".")
            age = time.time() - int(issued)
            if (re.fullmatch(r"[0-9a-f]{32}", sid) and 0 <= age < self.settings.session_hours * 3600
                    and hmac.compare_digest(sig, self._signature(f"{sid}.{issued}"))):
                return sid
        except (ValueError, TypeError):
            pass
        return None

    def _rate(self, key: str, limit: int) -> int:
        now = time.monotonic()
        with self.lock:
            # Prune all stale keys: a visitor cannot grow this map indefinitely.
            for name in list(self.windows):
                q = self.windows[name]
                while q and q[0] <= now - 60:
                    q.popleft()
                if not q:
                    del self.windows[name]
            if key not in self.windows and len(self.windows) >= 2048:
                return 60  # Fail closed rather than evict an active limit.
            q = self.windows.setdefault(key, deque())
            if len(q) >= limit:
                return max(1, math.ceil(q[0] + 60 - now))
            q.append(now)
        return 0

    def _daily_ai(self) -> bool:
        day = datetime.now(timezone.utc).date().isoformat()
        with self.lock, self.db.cursor() as cur:
            cur.execute("DELETE FROM demo_ai_usage WHERE day < ?", (day,))
            cur.execute("INSERT OR IGNORE INTO demo_ai_usage VALUES (?, 0)", (day,))
            cur.execute("UPDATE demo_ai_usage SET count=count+1 WHERE day=? AND count < ?",
                        (day, self.settings.ai_daily_limit))
            return cur.rowcount == 1

    @staticmethod
    def _error(message: str, status: int, retry: int | None = None):
        headers = {"Retry-After": str(retry)} if retry else None
        return JSONResponse({"detail": message}, status_code=status, headers=headers)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.settings.enabled:
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        path, method = request.url.path, request.method

        async def protected_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
                ]
            await send(message)

        async def respond(response):
            await response(scope, receive, protected_send)

        if path == "/api/health" and method in {"GET", "HEAD"}:
            return await self.app(scope, receive, protected_send)
        if path in {"/static/app.css", "/static/demo.css"} and method in {"GET", "HEAD"}:
            return await self.app(scope, receive, protected_send)

        if method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            expected = urlsplit(str(request.url))
            actual = urlsplit(origin) if origin else None
            if (request.headers.get("sec-fetch-site") == "cross-site" or
                    (actual and (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc))):
                return await respond(self._error("请从本站页面提交操作", 403))

        sid = self._session(request.cookies.get(COOKIE, ""))
        if path != "/demo/login" and not sid:
            if path.startswith("/api/") or method not in SAFE_METHODS:
                return await respond(self._error("请先输入体验码进入 Demo", 401))
            return await respond(RedirectResponse("/demo/login", status_code=303))

        if method not in SAFE_METHODS:
            retry = self._rate("global-write", 120) or self._rate(f"write:{sid or 'login'}", self.settings.writes_per_minute)
            if retry:
                return await respond(self._error("操作过于频繁，请稍后再试", 429, retry))
            # Bound actual streamed bytes, not just the untrusted Content-Length.
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.settings.max_body_bytes:
                    return await respond(self._error("请求内容过大，请缩短后重试", 413))
                if not message.get("more_body", False):
                    break
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()
        else:
            replay = receive

        if path == "/demo/login":
            error = None
            status = 200
            if method == "POST":
                retry = self._rate("login", 10)
                if retry:
                    return await respond(self._error("体验码尝试过于频繁，请稍后再试", 429, retry))
                try:
                    code = parse_qs(bytes(body).decode("utf-8"), max_num_fields=8).get("access_code", [""])[0]
                except (ValueError, UnicodeError):
                    code = ""
                if hmac.compare_digest(code.encode(), self.settings.access_code.encode()):
                    response = RedirectResponse("/", status_code=303)
                    response.set_cookie(COOKIE, self._issue(), max_age=self.settings.session_hours * 3600,
                                        httponly=True, secure=self.settings.secure_cookie, samesite="lax")
                    return await respond(response)
                error, status = "体验码不正确，请检查后重试。", 401
            elif method != "GET":
                return await respond(self._error("不支持的请求方法", 405))
            response = self.templates.TemplateResponse(
                request, "demo_login.html", {"error": error}, status_code=status,
            )
            return await respond(response)

        if path == "/demo/logout" and method == "POST":
            response = RedirectResponse("/demo/login", status_code=303)
            response.delete_cookie(COOKIE, secure=self.settings.secure_cookie, httponly=True, samesite="lax")
            return await respond(response)

        if method == "POST" and AI_PATH.fullmatch(path):
            retry = self._rate(f"ai:{sid}", self.settings.ai_per_minute)
            if retry:
                return await respond(self._error("AI 请求过于频繁，请稍后再试", 429, retry))
            if not self._daily_ai():
                return await respond(self._error("今天的 Demo AI 体验次数已用完，请明天再来（UTC 零点重置）", 429, 3600))
        return await self.app(scope, replay, protected_send)
