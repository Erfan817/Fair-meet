"""阶段 D：数据源可用性台账与降级。

- 记录每个外部数据源（地理编码/POI/路线/内容解析等）的可用性与检查时间。
- AI/界面引用外部数据时应据此标注来源与可用性；不可用时明确降级（如「已用估算」），不假装实时。
- 台账持久化到 source_availability 表；探测用注入的适配器（测试用 Fake，不联网）。
"""

from __future__ import annotations

import uuid

from fairmeet.adapters import GeoAdapter, GeoPoint, GeoUnavailable
from fairmeet.db import Database
from fairmeet.domain import TransportMode, utcnow


class SourceRegistry:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, source: str, available: bool, detail: str | None = None) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO source_availability(id,source,available,detail,checked_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(source) DO UPDATE SET available=excluded.available,"
                " detail=excluded.detail, checked_at=excluded.checked_at",
                (uuid.uuid4().hex, source, 1 if available else 0, detail, utcnow()),
            )

    def list(self) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute("SELECT source,available,detail,checked_at FROM source_availability ORDER BY source")
            return [
                {
                    "source": r["source"],
                    "available": bool(r["available"]),
                    "detail": r["detail"],
                    "checked_at": r["checked_at"],
                }
                for r in cur.fetchall()
            ]

    def probe_geo(self, geo: GeoAdapter, address: str = "成都") -> dict:
        """探测地理适配器是否可用，并记入台账。不抛异常，返回状态。"""
        try:
            geo.geocode(address)
            self.record("geo", True, "探测成功")
            return {"source": "geo", "available": True}
        except GeoUnavailable as exc:
            self.record("geo", False, str(exc))
            return {"source": "geo", "available": False, "detail": str(exc)}

    def probe_route(self, geo: GeoAdapter) -> dict:
        try:
            geo.route(GeoPoint(lng=104.05, lat=30.66), GeoPoint(lng=104.08, lat=30.65), TransportMode.WALK)
            self.record("route", True, "探测成功")
            return {"source": "route", "available": True}
        except GeoUnavailable as exc:
            self.record("route", False, str(exc))
            return {"source": "route", "available": False, "detail": str(exc)}
