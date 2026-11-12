"""仅规范化展示链接，不发请求、不做 DNS、不允许任意 URL 读取。"""

from __future__ import annotations

import html
import ipaddress
import logging
import re
import threading
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit, urlunsplit

_TAG = re.compile(r"<[^>]*>")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.I)
_CREDENTIAL = re.compile(
    r"(?:bearer\s+\S+|(?:token|api[_-]?key|secret|password|authorization|signature|auth)\s*[=:]\s*[^\s&]+)",
    re.I,
)


def safe_text(value: str, limit: int, secrets: tuple[str, ...] = ()) -> str:
    text = html.unescape(str(value))
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已移除凭据]")
    text = _TAG.sub("", text)
    text = _URL.sub("[链接见来源]", text)
    text = _CREDENTIAL.sub("[已移除凭据]", text)
    text = "".join(c for c in text if c >= " " or c in "\n\t")
    return text.strip()[:limit]


def safe_url(value: str | None, secrets: tuple[str, ...] = ()) -> str | None:
    """去除全部 query/fragment 及 userinfo；拒绝非公开/特殊目标。"""
    if not value or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parts.port
        if port is not None and port != (443 if parts.scheme.lower() == "https" else 80):
            return None
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".invalid", ".test")):
            return None
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return None
            host = f"[{host}]" if address.version == 6 else host
        except ValueError:
            if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
                return None
            # 拒绝旧式数字 IP 表示法，不依赖操作系统解析规则。
            if not re.search(r"[a-z]", host) or host.startswith("0x"):
                return None
        path = parts.path or "/"
        decoded = unquote(path)
        if any(secret and secret in unquote(value) for secret in secrets):
            return None
        if re.search(r"(?:token|sign(?:ature)?|auth|api[_-]?key|secret|password)[=/]", decoded, re.I):
            return None
        return urlunsplit((parts.scheme.lower(), host, path, "", ""))
    except (ValueError, UnicodeError):
        return None


_LOG_CONTEXT = threading.local()
_LOG_FILTER_LOCK = threading.Lock()
_LOG_FILTERS_INSTALLED = False


class _PrivateHttpFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # httpx INFO 会包含 query，httpcore DEBUG 可能包含响应头/异常 URL。
        # 只静默当前研究请求线程；不影响同进程其他 HTTP 调用与应用日志。
        return not getattr(_LOG_CONTEXT, "private", False)


@contextmanager
def private_http_logs():
    global _LOG_FILTERS_INSTALLED
    with _LOG_FILTER_LOCK:
        if not _LOG_FILTERS_INSTALLED:
            for name in (
                "httpx", "httpcore.connection", "httpcore.connection_pool", "httpcore.http11",
                "httpcore.http2", "httpcore.proxy", "httpcore.socks", "httpcore._sync.connection",
            ):
                logging.getLogger(name).addFilter(_PrivateHttpFilter())
            _LOG_FILTERS_INSTALLED = True
    previous = getattr(_LOG_CONTEXT, "private", False)
    _LOG_CONTEXT.private = True
    try:
        yield
    finally:
        _LOG_CONTEXT.private = previous


TOPIC_TERMS = {
    "general": "公园 博物馆 旅行 攻略",
    "dining": "餐厅 美食 攻略",
    "culture": "博物馆 展览 攻略",
    "outdoors": "公园 户外 攻略",
}


def infer_topic(message: str) -> str:
    """本地固定词识别，不调用模型、不把原始聊天或成员需求发给 Provider。"""
    lower = message.lower()
    categories = (
        ("dining", ("餐", "美食", "吃", "火锅", "小吃", "咖啡", "restaurant", "dining", "food")),
        ("culture", ("博物馆", "文化", "历史", "展览", "艺术", "museum", "culture")),
        ("outdoors", ("公园", "户外", "散步", "徒步", "自然", "outdoor", "hiking")),
    )
    scores = {topic: sum(word in lower for word in words) for topic, words in categories}
    ranked = sorted(scores, key=scores.get, reverse=True)
    if scores[ranked[0]] == 0 or scores[ranked[0]] == scores[ranked[1]]:
        return "general"
    return ranked[0]


def public_query(destination: str, scope: str, topic: str = "general") -> str:
    # message、needs、member 等从不进入此函数。scope 为成员明确选择的外发区域。
    destination = safe_text(destination, 200)
    scope = safe_text(scope, 200)
    terms = TOPIC_TERMS.get(topic, TOPIC_TERMS["general"])
    return " ".join(part for part in (destination, scope, terms) if part)
