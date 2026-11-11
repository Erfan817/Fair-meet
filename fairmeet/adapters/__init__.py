"""外部能力适配层（阶段 C）。

用 Protocol 定义「端口」，领域/服务只依赖端口，不依赖具体高德实现或 HTTP 格式。
默认实现为高德（amap），测试与离线场景用 mock（FakeGeoAdapter）。

- 所有坐标统一 GCJ-02（高德坐标系）。
- 返回值都带 provenance，标明 external/estimated/unknown，禁止凭空精确数字。
- 外部不可用时，端口实现应抛 GeoUnavailable；上层据此降级为「估算/未知」而非崩溃。
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel

from fairmeet.domain import ProvenanceKind, TransportMode


class GeoPoint(BaseModel):
    lng: float
    lat: float


class GeocodeResult(BaseModel):
    lng: float
    lat: float
    provenance: ProvenanceKind = ProvenanceKind.EXTERNAL
    source: str = "amap"


class PoiResult(BaseModel):
    name: str
    category: Optional[str] = None
    lng: float
    lat: float
    distance_m: Optional[float] = None
    provenance: ProvenanceKind = ProvenanceKind.EXTERNAL
    source: str = "amap"


class RouteResult(BaseModel):
    distance_m: Optional[float] = None
    duration_minutes: Optional[float] = None
    provenance: ProvenanceKind = ProvenanceKind.EXTERNAL
    source: str = "amap"


class GeoUnavailable(Exception):
    """外部地理/路线服务不可用（超时/限流/无结果/未配置 Key）。上层应降级。"""


@runtime_checkable
class GeoAdapter(Protocol):
    """地理编码/POI/路线 的统一端口。"""

    def geocode(self, address: str) -> GeocodeResult: ...

    def search_poi(self, center: GeoPoint, keyword: str, radius_m: int = 1500) -> list[PoiResult]: ...

    def route(self, origin: GeoPoint, destination: GeoPoint, mode: TransportMode) -> RouteResult: ...
