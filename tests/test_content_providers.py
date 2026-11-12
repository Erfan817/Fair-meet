"""来源与 URL 防护离线测试；所有 HTTP 均为 MockTransport。"""

from __future__ import annotations

import json

import httpx
import pytest

from fairmeet.content import BraveContentProvider, ContentProviderError, FakeContentProvider, PublicWebContentProvider
from fairmeet.content.providers.brave import BRAVE_SEARCH_URL, MAX_RESPONSE_BYTES
from fairmeet.content.safety import public_query, safe_text, safe_url


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "synthetic-offline-key")
    monkeypatch.setenv("FAIRMEET_RESEARCH_BRAVE_VERIFIED", "1")
    monkeypatch.setenv("FAIRMEET_RESEARCH_DAILY_LIMIT", "3")


def test_brave_disabled_by_default(monkeypatch):
    for key in ("BRAVE_SEARCH_API_KEY", "FAIRMEET_RESEARCH_BRAVE_VERIFIED", "FAIRMEET_RESEARCH_DAILY_LIMIT"):
        monkeypatch.delenv(key, raising=False)
    provider = BraveContentProvider(transport=httpx.MockTransport(lambda req: pytest.fail("默认关闭不应发送请求")))
    assert not provider.available()
    with pytest.raises(ContentProviderError) as error:
        provider.search("成都")
    assert error.value.code == "source_disabled"


@pytest.mark.parametrize("key,value", [
    ("BRAVE_SEARCH_API_KEY", ""), ("FAIRMEET_RESEARCH_BRAVE_VERIFIED", "0"),
    ("FAIRMEET_RESEARCH_DAILY_LIMIT", "0"), ("FAIRMEET_RESEARCH_DAILY_LIMIT", "-1"),
    ("FAIRMEET_RESEARCH_DAILY_LIMIT", "invalid"),
])
def test_each_gate_required(configured, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    assert not BraveContentProvider().available()


def test_brave_fixed_endpoint_snippet_redaction_and_bounded_results(configured):
    seen = []

    def handler(req):
        seen.append(req)
        assert str(req.url).split("?")[0] == BRAVE_SEARCH_URL
        assert req.headers["x-subscription-token"] == "synthetic-offline-key"
        assert req.method == "GET" and not req.content
        assert req.url.params["q"] == public_query("成都", "武侯区")
        assert "member" not in str(req.url) and "message" not in str(req.url)
        return httpx.Response(200, json={"web": {"results": [{
            "title": "<b>合成</b>公开攻略 synthetic-offline-key",
            "url": f"https://user:password@example.org/entry-{i}?token=private&sign=hidden#auth",
            "description": "<script>不应执行</script>" + "合成摘要" * 500,
            "latitude": 30.6, "longitude": 104.1,
        } for i in range(8)]}})

    provider = BraveContentProvider(transport=httpx.MockTransport(handler))
    hits = provider.search("成都", "武侯区")
    assert len(hits) == 5 and len(seen) == 1
    for hit in hits:
        assert hit.content_scope == "snippet"
        assert "synthetic-offline-key" not in hit.title and "<b>" not in hit.title
        assert hit.url.startswith("https://example.org/entry-") and "?" not in hit.url and "@" not in hit.url
        assert len(hit.excerpt) <= 1000 and "<script>" not in hit.excerpt
        assert not hasattr(hit, "lat")


@pytest.mark.parametrize("status,expected", [(429, "rate_limited"), (401, "access_denied"),
                                             (403, "access_denied"), (500, "provider_failure"),
                                             (302, "provider_failure")])
def test_provider_errors_no_retry_no_redirect_no_body_leak(configured, status, expected):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, text="secret response-body", headers={"location": "http://127.0.0.1/private"})

    provider = BraveContentProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ContentProviderError) as error:
        provider.search("成都")
    assert error.value.code == expected and "secret" not in str(error.value)
    assert len(seen) == 1


def test_timeout_no_error_detail(configured):
    def handler(req):
        raise httpx.ReadTimeout("secret synthetic-offline-key", request=req)

    provider = BraveContentProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ContentProviderError) as error:
        provider.search("成都")
    assert error.value.code == "timeout" and "secret" not in str(error.value)


@pytest.mark.parametrize("response,expected", [
    (httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1), headers={"content-type": "application/json"}),
     "response_too_large"),
    (httpx.Response(200, text="<html>secret</html>"), "invalid_response"),
    (httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"}), "provider_failure"),
    (httpx.Response(200, json={"web": {"results": "secret"}}), "invalid_response"),
])
def test_response_limits_and_types(configured, response, expected):
    provider = BraveContentProvider(transport=httpx.MockTransport(lambda req: response))
    with pytest.raises(ContentProviderError) as error:
        provider.search("成都")
    assert error.value.code == expected


class LargeStream(httpx.SyncByteStream):
    def __iter__(self):
        for _ in range(40):
            yield b"x" * 8192


def test_stream_limit_without_content_length(configured):
    def handler(req):
        return httpx.Response(200, stream=LargeStream(), headers={"content-type": "application/json"})

    provider = BraveContentProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ContentProviderError) as error:
        provider.search("成都")
    assert error.value.code == "response_too_large"


@pytest.mark.parametrize("provider", [FakeContentProvider(), BraveContentProvider(), PublicWebContentProvider()])
def test_read_arbitrary_url_unsupported(provider):
    with pytest.raises(ContentProviderError) as error:
        provider.read("http://169.254.169.254/latest/meta-data")
    assert error.value.code == "unsupported_read"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "//example.org/x",
    "http://localhost/x", "http://local.localhost/x", "http://192.168.1.1/x", "http://127.0.0.1/x",
    "http://169.254.169.254/x", "http://[::1]/x", "http://[fc00::1]/x", "http://example.local/x",
    "http://2130706433/x", "http://0x7f000001/x", "https://example.org:444/x",
    "https://example.org/\nunsafe", "https://example.org\\@127.0.0.1/x", "https://example.org:bad/x",
    "https://example.org/path/token=secret", "https://example.org/auth/secret",
])
def test_dangerous_display_urls_rejected(url):
    assert safe_url(url) is None


def test_url_credentials_query_and_fragment_removed():
    assert safe_url("https://alice:secret@Example.ORG:443/path?a=1&token=secret#sign=secret") == "https://example.org/path"
    assert safe_url("https://example.org/path?auth=secret&signature=secret&key=secret") == "https://example.org/path"
    assert safe_url("https://example.org/synthetic-key", ("synthetic-key",)) is None
    assert safe_text("<b>text</b> https://example.org/?token=secret Bearer secret", 1000) == (
        "text [链接见来源] [已移除凭据]"
    )


def test_topic_is_fixed_public_words_not_raw_message(configured):
    seen = []

    def handler(req):
        seen.append(req.url.params["q"])
        return httpx.Response(200, json={"web": {"results": []}})

    provider = BraveContentProvider(transport=httpx.MockTransport(handler))
    provider.search("成都", "武侯区", topic="dining")
    provider.search("成都", "", topic="原始隐私不是合法topic")
    assert seen[0] == "成都 武侯区 餐厅 美食 攻略"
    assert seen[1] == public_query("成都", "") and "原始隐私" not in seen[1]


def test_slow_trickle_hits_total_deadline(configured, monkeypatch):
    from fairmeet.content.providers import brave
    now = [0.0]
    monkeypatch.setattr(brave.time, "monotonic", lambda: now[0])

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                now[0] += 2.0
                yield b" "

    provider = BraveContentProvider(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=SlowStream(), headers={"content-type": "application/json"}),
    ))
    with pytest.raises(ContentProviderError) as error:
        provider.search("成都")
    assert error.value.code == "timeout" and now[0] <= 10


def test_provider_has_no_raw_url_error_or_logged_response(configured, caplog):
    provider = BraveContentProvider(transport=httpx.MockTransport(
        lambda req: httpx.Response(403, text="sensitive body"),
    ))
    with pytest.raises(ContentProviderError):
        provider.search("成都")
    assert "sensitive body" not in caplog.text
    assert "synthetic-offline-key" not in caplog.text
    assert json.dumps(public_query("成都", "锦江区"), ensure_ascii=False)


def test_http_debug_logs_cannot_leak_scope_key_url_or_headers(configured, caplog):
    import logging

    caplog.set_level(logging.DEBUG)

    def handler(req):
        logging.getLogger("httpcore.http11").debug("synthetic-offline-key sensitive response header")
        return httpx.Response(200, json={"web": {"results": []}})

    provider = BraveContentProvider(transport=httpx.MockTransport(handler))
    provider.search("成都", "区域合成隐私")
    assert "区域合成隐私" not in caplog.text and "HTTP Request" not in caplog.text
    assert "synthetic-offline-key" not in caplog.text
    # 上下文退出后，同一线程的其他客户端日志不受影响。
    logging.getLogger("httpx").info("ordinary-client-log")
    assert "ordinary-client-log" in caplog.text
