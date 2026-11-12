"""内容端口不接触数据库、Geo、成员需求或原始聊天消息。"""

from __future__ import annotations

from typing import Protocol

from fairmeet.content.models import ContentHit


class ContentPort(Protocol):
    source: str
    is_simulated: bool

    def available(self) -> bool: ...

    def search(self, destination: str, scope: str = "", *, topic: str = "general") -> list[ContentHit]: ...

    def read(self, url: str) -> None: ...
