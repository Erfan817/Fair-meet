"""公开搜索的合成 HTML 测试；全部离线，无 API key、登录或真实请求。"""

import logging
from urllib.parse import quote

import httpx
import pytest

from fairmeet.content import ContentProviderError, PublicWebContentProvider
from fairmeet.content.providers.brave import MAX_RESPONSE_BYTES
from fairmeet.content.providers.public_web import PUBLIC_SEARCH_URL
from fairmeet.content.safety import public_query


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    for key in ("FAIRMEET_PUBLIC_SEARCH_ENABLED", "FAIRMEET_PUBLIC_SEARCH_DAILY_LIMIT", "BRAVE_SEARCH_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def html_response(page, **kwargs):
    return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"}, **kwargs)


def test_realistic_html_fixed_endpoint_and_result_scope():
    seen = []
    target = quote("https://example.org/museum?token=secret#private", safe="")
    page = f'''<html><div class="result results_links web-result">
    <h2><a class="result__a" href="//duckduckgo.com/l/?uddg={target}&amp;rut=tracking">成都<b>博物馆</b></a></h2>
    <a class="result__snippet">合成<b>摘要</b><br>仅供核对</a></div>
    <div><a class="result__a" href="https://example.org/park">公园链接</a></div></html>'''

    def handler(req):
        seen.append(req)
        assert str(req.url).split("?")[0] == PUBLIC_SEARCH_URL
        assert req.method == "GET" and not req.content
        assert req.url.params["q"] == public_query("成都", "武侯区", "culture")
        assert "authorization" not in req.headers and "cookie" not in req.headers
        return html_response(page)

    hits = PublicWebContentProvider(transport=httpx.MockTransport(handler)).search("成都", "武侯区", topic="culture")
    assert len(hits) == 2 and len(seen) == 1
    assert hits[0].title == "成都博物馆" and hits[0].excerpt == "合成摘要仅供核对"
    assert hits[0].url == "https://example.org/museum" and hits[0].content_scope == "snippet"
    assert hits[1].content_scope == "link" and hits[1].excerpt == ""
    assert all(h.platform == "DuckDuckGo 公开网页搜索" for h in hits)


@pytest.mark.parametrize("target", [
    "http://127.0.0.1/x", "http://169.254.169.254/x", "http://localhost/", "javascript:alert(1)",
    "file:///etc/passwd", "https://example.org:444/x", "https://example.org/auth/secret",
])
def test_jump_urls_are_decoded_then_validated(target):
    page = f'<a class="result__a" href="/l/?uddg={quote(target, safe="")}">不安全资料</a>'
    provider = PublicWebContentProvider(transport=httpx.MockTransport(lambda req: html_response(page)))
    assert provider.search("成都") == []


def test_five_hits_dedup_safe_direct_urls_and_text():
    page = '<a class="result__a" href="javascript:alert(1)">危险</a>' + "".join(
        f'<a class="result__a" href="https://user:password@example.org/{i}?sign=secret#private">'
        f'<b>合成 {i}</b> token=secret</a><div class="result__snippet">{"摘要" * 600}</div>'
        for i in [0, 0, 1, 2, 3, 4, 5, 6]
    )
    hits = PublicWebContentProvider(transport=httpx.MockTransport(lambda req: html_response(page))).search("成都")
    assert len(hits) == 5 and len({h.url for h in hits}) == 5
    assert all(len(h.excerpt) <= 1000 and "secret" not in h.title and "?" not in h.url for h in hits)
    assert all("@" not in h.url and "<b>" not in h.title for h in hits)


@pytest.mark.parametrize("status,code", [(202, "blocked"), (403, "blocked"), (429, "rate_limited"),
                                         (302, "provider_failure"), (500, "provider_failure")])
def test_status_no_redirect_no_retry(status, code):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, text="private upstream", headers={"location": "http://127.0.0.1/secret"})

    with pytest.raises(ContentProviderError) as error:
        PublicWebContentProvider(transport=httpx.MockTransport(handler)).search("成都")
    assert error.value.code == code and str(error.value) == code and len(calls) == 1


@pytest.mark.parametrize("marker", ['<form action="/anomaly.js">', '<div class="captcha">',
                                    '<form id="challenge-form">', "Unfortunately, bots use DuckDuckGo too."])
def test_challenge_is_not_an_empty_success(marker):
    with pytest.raises(ContentProviderError, match="^blocked$"):
        PublicWebContentProvider(transport=httpx.MockTransport(lambda req: html_response(marker))).search("成都")


@pytest.mark.parametrize("page", ['<div class="no-results">没有结果</div>', "<h1>No results found</h1>"])
def test_explicit_no_results(page):
    assert PublicWebContentProvider(transport=httpx.MockTransport(lambda req: html_response(page))).search("成都") == []


@pytest.mark.parametrize("response,code", [
    (html_response("x" * (MAX_RESPONSE_BYTES + 1)), "response_too_large"),
    (httpx.Response(200, headers={"content-type": "text/html", "content-length": str(MAX_RESPONSE_BYTES + 1)}),
     "response_too_large"),
    (httpx.Response(200, json={}), "invalid_response"),
    (httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"}), "invalid_response"),
    (html_response("<html>unexpected page</html>"), "invalid_response"),
])
def test_response_guards(response, code):
    with pytest.raises(ContentProviderError, match=f"^{code}$"):
        PublicWebContentProvider(transport=httpx.MockTransport(lambda req: response)).search("成都")


def test_streaming_size_total_timeout_and_http_error(monkeypatch):
    from fairmeet.content.providers import public_web
    now = [0.0]
    monkeypatch.setattr(public_web.time, "monotonic", lambda: now[0])

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(40):
                now[0] += tick[0]
                yield b"x" * 8192

    tick = [0.0]
    provider = PublicWebContentProvider(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, stream=Stream(), headers={"content-type": "text/html"}),
    ))
    with pytest.raises(ContentProviderError, match="^response_too_large$"):
        provider.search("成都")
    tick[0] = 2.0
    with pytest.raises(ContentProviderError, match="^timeout$"):
        provider.search("成都")

    def handler(req):
        raise httpx.ReadTimeout("sensitive URL and response", request=req)
    with pytest.raises(ContentProviderError, match="^timeout$"):
        PublicWebContentProvider(transport=httpx.MockTransport(handler)).search("成都")


@pytest.mark.parametrize("status,page,code", [(202, "", "blocked"), (403, "", "blocked"),
                                             (429, "", "rate_limited"), (200, "captcha", "blocked")])
def test_block_or_rate_limit_latches_same_instance(status, page, code):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, text=page, headers={"content-type": "text/html"})

    provider = PublicWebContentProvider(transport=httpx.MockTransport(handler))
    assert provider.available() and provider.blocked_code is None
    for _ in range(3):
        with pytest.raises(ContentProviderError, match=f"^{code}$"):
            provider.search("成都")
        assert not provider.available() and provider.blocked_code == code
    assert len(calls) == 1
    assert PublicWebContentProvider().available()  # 只保持本实例内存状态，不持久化。


def test_defaults_gates_read_and_private_logs(monkeypatch, caplog):
    provider = PublicWebContentProvider(transport=httpx.MockTransport(
        lambda req: html_response('<div class="no-results">empty</div>'),
    ))
    assert provider.available() and provider.daily_limit == 20 and not provider.is_simulated
    caplog.set_level(logging.DEBUG)
    provider.search("成都", "合成私密区域", topic="原始消息不外发")
    assert "合成私密区域" not in caplog.text and "HTTP Request" not in caplog.text
    with pytest.raises(ContentProviderError, match="^unsupported_read$"):
        provider.read("http://169.254.169.254/")
    monkeypatch.setenv("FAIRMEET_PUBLIC_SEARCH_DAILY_LIMIT", "7")
    assert provider.daily_limit == 7
    for value in ("0", "-1", "invalid"):
        monkeypatch.setenv("FAIRMEET_PUBLIC_SEARCH_DAILY_LIMIT", value)
        assert not provider.available()
    monkeypatch.delenv("FAIRMEET_PUBLIC_SEARCH_DAILY_LIMIT")
    monkeypatch.setenv("FAIRMEET_PUBLIC_SEARCH_ENABLED", "0")
    with pytest.raises(ContentProviderError, match="^source_disabled$"):
        provider.search("成都")
