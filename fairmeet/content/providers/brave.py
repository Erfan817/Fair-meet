"""可选 Brave 只读摘要搜索。仅代码就绪，未经真实 API/条款/费用实测。

默认禁用；不读 .env。只连接固定端点，不接受目标 URL、不跟随重定向。
"""

from __future__ import annotations

import json
import os
import time

import httpx

from fairmeet.content.models import ContentHit, ContentProviderError
from fairmeet.content.safety import private_http_logs, public_query, safe_text, safe_url

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_RESPONSE_BYTES = 256 * 1024


class BraveContentProvider:
    source = "brave"
    is_simulated = False

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        # transport 只用于离线 MockTransport 单测；配置仍从进程环境显式读取。
        self._transport = transport

    @property
    def daily_limit(self) -> int:
        try:
            return max(0, int(os.environ.get("FAIRMEET_RESEARCH_DAILY_LIMIT", "0")))
        except ValueError:
            return 0

    def available(self) -> bool:
        return bool(
            os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
            and os.environ.get("FAIRMEET_RESEARCH_BRAVE_VERIFIED") == "1"
            and self.daily_limit > 0
        )

    def search(self, destination: str, scope: str = "", *, topic: str = "general") -> list[ContentHit]:
        if not self.available():
            raise ContentProviderError("source_disabled")
        key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        started = time.monotonic()
        try:
            # trust_env=False 防止意外使用环境代理；不读取用户 cookies 或登录状态。
            with private_http_logs(), httpx.Client(
                timeout=httpx.Timeout(8.0), follow_redirects=False, trust_env=False, transport=self._transport,
            ) as client:
                with client.stream(
                    "GET", BRAVE_SEARCH_URL,
                    params={"q": public_query(destination, scope, topic), "count": 5},
                    headers={"X-Subscription-Token": key, "Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    if time.monotonic() - started >= 8.0:
                        raise ContentProviderError("timeout")
                    if response.status_code == 429:
                        raise ContentProviderError("rate_limited")
                    if response.status_code in {401, 403}:
                        raise ContentProviderError("access_denied")
                    if response.status_code != 200:
                        raise ContentProviderError("provider_failure")
                    if "application/json" not in response.headers.get("content-type", "").lower():
                        raise ContentProviderError("invalid_response")
                    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                        raise ContentProviderError("invalid_response")
                    length = response.headers.get("content-length")
                    if length and int(length) > MAX_RESPONSE_BYTES:
                        raise ContentProviderError("response_too_large")
                    body = bytearray()
                    # iter_raw 不等待攒满 chunk，防止持续滴流绕过总体时间检查。
                    stream = [response.content] if response.is_stream_consumed else response.iter_raw()
                    for chunk in stream:
                        if time.monotonic() - started >= 8.0:
                            raise ContentProviderError("timeout")
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ContentProviderError("response_too_large")
            data = json.loads(body)
            results = data.get("web", {}).get("results", [])
            if not isinstance(results, list):
                raise ContentProviderError("invalid_response")
            hits = []
            for row in results[:5]:
                if not isinstance(row, dict) or not isinstance(row.get("title"), str):
                    continue
                title = safe_text(row["title"], 200, (key,))
                if not title:
                    continue
                hits.append(ContentHit(
                    title=title, url=safe_url(row.get("url") if isinstance(row.get("url"), str) else None, (key,)),
                    excerpt=safe_text(row.get("description", ""), 1000, (key,)),
                    platform="Brave 网页搜索", content_scope="snippet",
                ))
            return hits
        except ContentProviderError:
            raise
        except httpx.TimeoutException:
            raise ContentProviderError("timeout") from None
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise ContentProviderError("provider_failure") from None

    def read(self, url: str) -> None:
        raise ContentProviderError("unsupported_read")
