"""阶段 D 增强：DeepSeek 真实 LLM 客户端。

实现 orchestrator.LLMClient 端口（complete(system, user) -> str，返回 JSON 字符串）。
走 OpenAI 兼容的 /chat/completions；要求模型输出 JSON（response_format=json_object 可选）。

- Key 仅从配置/环境注入，**不打印、不回显**。
- 网络/HTTP 错误抛 DeepSeekError（不吞）；上层 orchestrator 对返回内容做 JSON/Schema 校验，
  非法即 mode='invalid' 不落提案。
- 测试用 respx mock 网络（测试不含真实 key）。
"""

from __future__ import annotations

import httpx

from fairmeet.ai.contracts import AIResponse  # noqa: F401  (类型参考)
from fairmeet.config import get_deepseek_base_url, get_deepseek_model


class DeepSeekError(Exception):
    pass


class DeepSeekLLM:
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or get_deepseek_base_url()).rstrip("/")
        self._model = model or get_deepseek_model()
        self._timeout = timeout

    def complete(self, system: str, user: str) -> str:
        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise DeepSeekError(f"DeepSeek 请求失败: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            # 不回显请求头/Key；仅状态码与有限正文
            raise DeepSeekError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise DeepSeekError("DeepSeek 响应结构异常") from exc
        return content


def build_llm_from_env():
    """按环境自动选择：有 DEEPSEEK_API_KEY → DeepSeekLLM；否则 FakeLLM。

    供 app 装配使用；无 key 时保持离线 FakeLLM（不报错）。
    """
    from fairmeet.ai.orchestrator import FakeLLM
    from fairmeet.config import has_deepseek_api_key

    if has_deepseek_api_key():
        from fairmeet.config import get_deepseek_api_key

        return DeepSeekLLM(get_deepseek_api_key())
    return FakeLLM()
