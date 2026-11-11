"""DeepSeek LLM 客户端测试（respx mock 网络，不含真实 key、不联网）。"""

from __future__ import annotations

import httpx
import pytest
import respx

from fairmeet.ai.deepseek import DeepSeekError, DeepSeekLLM


def _llm() -> DeepSeekLLM:
    return DeepSeekLLM(api_key="test-key-not-real", base_url="https://api.deepseek.com", model="deepseek-chat")


@respx.mock
def test_complete_parses_content():
    respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"reply":"hi","clarification_questions":[],"operations":[],"sources":[]}'}}
                ]
            },
        )
    )
    out = _llm().complete("system", "user")
    assert "reply" in out
    # 断言请求带上 Authorization 头（不校验真实值）
    call = respx.calls.last
    assert "authorization" in {k.lower() for k in call.request.headers}


@respx.mock
def test_http_error_raises():
    respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    with pytest.raises(DeepSeekError):
        _llm().complete("s", "u")


@respx.mock
def test_bad_structure_raises():
    respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )
    with pytest.raises(DeepSeekError):
        _llm().complete("s", "u")


@respx.mock
def test_network_error_raises():
    respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(DeepSeekError):
        _llm().complete("s", "u")


def test_key_not_leaked_in_error():
    """确保构造的客户端不把 key 放进任何可读属性名暴露（基本防护）。"""
    llm = DeepSeekLLM(api_key="supersecret-should-not-print")
    # 不在 repr/属性中直接泄露（_api_key 下划线私有，属正常）
    assert "supersecret-should-not-print" not in repr(llm)
