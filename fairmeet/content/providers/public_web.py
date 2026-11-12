"""免密钥 DuckDuckGo 公开 HTML 搜索；不读取结果网页、不绕过验证或重试。"""

from __future__ import annotations

import os
import threading
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx

from fairmeet.content.models import ContentHit, ContentProviderError
from fairmeet.content.providers.brave import MAX_RESPONSE_BYTES
from fairmeet.content.safety import private_http_logs, public_query, safe_text, safe_url

PUBLIC_SEARCH_URL = "https://html.duckduckgo.com/html/"
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}


class _Results(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.no_results = False
        self._field = ""
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if "no-results" in classes or "result--no-result" in classes:
            self.no_results = True
        if tag == "a" and "result__a" in classes:
            self.rows.append({"url": attrs.get("href"), "title": [], "excerpt": []})
            self._field, self._depth = "title", 1
        elif "result__snippet" in classes and self.rows:
            self._field, self._depth = "excerpt", 1
        elif self._field and tag not in _VOID_TAGS:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._field and tag not in _VOID_TAGS:
            self._depth -= 1
            if self._depth <= 0:
                self._field = ""

    def handle_data(self, data):
        if self._field:
            self.rows[-1][self._field].append(data)


def _result_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit("https:" + value if value.startswith("//") else value)
        if parts.path == "/l/" and parts.hostname in {None, "duckduckgo.com", "html.duckduckgo.com"}:
            value = parse_qs(parts.query).get("uddg", [None])[0]
        return safe_url(value)
    except ValueError:
        return None


class PublicWebContentProvider:
    source = "public_web"
    is_simulated = False

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport
        self._blocked_code: str | None = None
        self._search_lock = threading.Lock()

    @property
    def blocked_code(self) -> str | None:
        return self._blocked_code

    @property
    def daily_limit(self) -> int:
        try:
            return max(0, int(os.environ.get("FAIRMEET_PUBLIC_SEARCH_DAILY_LIMIT", "20")))
        except ValueError:
            return 0

    def available(self) -> bool:
        return (
            self._blocked_code is None
            and os.environ.get("FAIRMEET_PUBLIC_SEARCH_ENABLED", "1") != "0" and self.daily_limit > 0
        )

    def search(self, destination: str, scope: str = "", *, topic: str = "general") -> list[ContentHit]:
        # 同实例串行，避免另一个 worker 在验证/限流熔断后继续发送已排队请求。
        with self._search_lock:
            if self._blocked_code:
                raise ContentProviderError(self._blocked_code)
            return self._search(destination, scope, topic=topic)

    def _search(self, destination: str, scope: str, *, topic: str) -> list[ContentHit]:
        if not self.available():
            raise ContentProviderError("source_disabled")
        started = time.monotonic()
        try:
            with private_http_logs(), httpx.Client(
                timeout=httpx.Timeout(8.0), follow_redirects=False, trust_env=False, transport=self._transport,
            ) as client:
                with client.stream(
                    "GET", PUBLIC_SEARCH_URL, params={"q": public_query(destination, scope, topic)},
                    headers={"Accept": "text/html", "Accept-Encoding": "identity"},
                ) as response:
                    if time.monotonic() - started >= 8.0:
                        raise ContentProviderError("timeout")
                    if response.status_code == 429:
                        raise ContentProviderError("rate_limited")
                    if response.status_code in {202, 401, 403}:
                        raise ContentProviderError("blocked")
                    if response.status_code != 200:
                        raise ContentProviderError("provider_failure")
                    if response.headers.get("content-type", "").split(";")[0].strip().lower() not in {
                        "text/html", "application/xhtml+xml",
                    } or response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                        raise ContentProviderError("invalid_response")
                    length = response.headers.get("content-length")
                    if length and int(length) > MAX_RESPONSE_BYTES:
                        raise ContentProviderError("response_too_large")
                    body = bytearray()
                    stream = [response.content] if response.is_stream_consumed else response.iter_raw()
                    for chunk in stream:
                        if time.monotonic() - started >= 8.0:
                            raise ContentProviderError("timeout")
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ContentProviderError("response_too_large")
            page = body.decode("utf-8", errors="replace")
            lower = page.lower()
            if any(marker in lower for marker in ("anomaly", "captcha", "challenge-form", "bots use duckduckgo")):
                raise ContentProviderError("blocked")
            parser = _Results()
            parser.feed(page)
            if not parser.rows and not (parser.no_results or "no results found" in lower):
                raise ContentProviderError("invalid_response")
            hits = []
            seen = set()
            for row in parser.rows:
                title = safe_text("".join(row["title"]), 200)
                excerpt = safe_text("".join(row["excerpt"]), 1000)
                url = _result_url(row["url"])
                if not title or not url or url in seen:
                    continue
                hits.append(ContentHit(
                    title=title, url=url, excerpt=excerpt, platform="DuckDuckGo 公开网页搜索",
                    content_scope="snippet" if excerpt else "link",
                ))
                seen.add(url)
                if len(hits) == 5:
                    break
            return hits
        except ContentProviderError as exc:
            if exc.code in {"blocked", "rate_limited"}:
                self._blocked_code = exc.code
            raise
        except httpx.TimeoutException:
            raise ContentProviderError("timeout") from None
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise ContentProviderError("provider_failure") from None

    def read(self, url: str) -> None:
        raise ContentProviderError("unsupported_read")
