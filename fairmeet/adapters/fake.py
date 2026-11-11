"""Mock 地理适配器（离线/测试用）。

不访问网络；用确定性规则返回结果，便于断网可跑的验收测试。
也可通过 `unavailable=True` 模拟外部不可用，断言上层的降级分支。
"""

from __future__ import annotations

from fairmeet.adapters import (
    GeocodeResult,
    GeoPoint,
    GeoUnavailable,
    PoiResult,
    RouteResult,
)
from fairmeet.domain import ProvenanceKind, TransportMode


class FakeGeoAdapter:
    """确定性假实现：同一地址永远得到同一坐标。"""

    def __init__(self, unavailable: bool = False) -> None:
        self.unavailable = unavailable

    def _guard(self) -> None:
        if self.unavailable:
            raise GeoUnavailable("mock adapter 被设为不可用")

    def geocode(self, address: str) -> GeocodeResult:
        self._guard()
        address = address.strip()
        if not address:
            raise GeoUnavailable("空地址")
        # 由字符串稳定派生一个成都附近的坐标（GCJ-02 范围附近）
        h = sum(ord(c) for c in address)
        lng = 104.0 + (h % 100) / 1000.0
        lat = 30.55 + (h % 70) / 1000.0
        return GeocodeResult(lng=lng, lat=lat, provenance=ProvenanceKind.EXTERNAL, source="mock")

    def search_poi(self, center: GeoPoint, keyword: str, radius_m: int = 1500) -> list[PoiResult]:
        self._guard()
        results = []
        for i in range(3):
            results.append(
                PoiResult(
                    name=f"{keyword}-候选{i + 1}",
                    category=keyword,
                    lng=center.lng + i * 0.001,
                    lat=center.lat + i * 0.001,
                    distance_m=float(100 * (i + 1)),
                    provenance=ProvenanceKind.EXTERNAL,
                    source="mock",
                )
            )
        return results

    def route(self, origin: GeoPoint, destination: GeoPoint, mode: TransportMode) -> RouteResult:
        self._guard()
        # 用粗略欧氏距离 * 地球尺度换算，明确标注为估算
        dx = (destination.lng - origin.lng) * 111_000
        dy = (destination.lat - origin.lat) * 111_000
        dist = (dx * dx + dy * dy) ** 0.5
        speed = {TransportMode.WALK: 5_000, TransportMode.TRANSIT: 20_000, TransportMode.DRIVE: 30_000}.get(
            mode, 20_000
        )
        minutes = round(dist / speed * 60, 1) if dist else 0.0
        return RouteResult(
            distance_m=round(dist, 1),
            duration_minutes=minutes,
            provenance=ProvenanceKind.ESTIMATED,
            source="mock-estimate",
        )
