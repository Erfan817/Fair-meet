"""独立、只读研究的输入及规范化资料契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fairmeet.services import FairMeetError


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(max_length=4000)
    intent: Literal["discover", "plan", "check_place"] = "discover"
    scope: str = Field(default="", max_length=200)
    enabled_sources: list[Literal["fake", "brave", "public_web", "amap_poi"]] = Field(
        default_factory=lambda: ["fake"], max_length=3,
    )
    consent_version: str = Field(default="v1", max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=80)
    network_consent: bool = Field(default=False, strict=True)

    @field_validator("enabled_sources")
    @classmethod
    def unique_sources(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("来源不能重复")
        return value


class ResearchError(FairMeetError):
    """仅使用固定、可展示的业务消息；不得包含上游异常或响应正文。"""

    def __init__(self, message: str, code: str = "research_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ContentHit:
    """Provider 返回的不可信资料。ID、可信程度、位置均由服务端决定。"""

    title: str
    url: str | None = None
    excerpt: str = ""
    platform: str = "web"
    published_at: str | None = None
    content_scope: Literal["body", "snippet", "link", "structured"] = "snippet"
    category: Literal["activity", "meal"] = "activity"
    area: str = ""


class ContentProviderError(Exception):
    def __init__(self, code: str) -> None:
        # 不接收原始错误文本，避免密钥、URL 或正文进入错误记录。
        super().__init__(code)
        self.code = code


PROVIDER_MESSAGES = {
    "source_disabled": "来源未启用：请检查该来源服务端开关、所需配置及正数每日配额。",
    "unsupported_read": "首版只支持搜索摘要，不支持读取任意 URL 的正文或评论。",
    "daily_limit": "该来源今日配额已用尽，请使用已有资料或稍后重试。",
    "rate_limited": "来源返回限流，本轮不再重试。",
    "blocked": "公开搜索遇到访问限制或人机验证，已停止，不绕过验证或自动重试。",
    "access_denied": "来源拒绝访问，本轮不尝试登录或切换账号。",
    "timeout": "来源请求超时，本轮不再重试。",
    "response_too_large": "来源响应超过大小限制，未保存响应正文。",
    "invalid_response": "来源响应格式不受支持，未保存响应正文。",
    "provider_failure": "来源请求失败，未保存上游错误详情。",
    "invalid_evidence": "来源返回的资料格式不受支持，未生成无依据候选。",
}
