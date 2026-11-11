"""高德（Amap）地理适配器（阶段 C）。

真实实现，调用高德 Web 服务 REST API：地理编码 / 周边 POI / 路径规划。
- 网络异常、非 200、status!=1、无结果 → 统一抛 GeoUnavailable，供上层降级。
- 不打印/回显 Key。Key 由构造参数注入（来自 config，最终来自 .env）。
- 测试不调用此实现（用 FakeGeoAdapter）；如需 mock 网络，用 respx 拦截 httpx。
"""

from __future__ import annotations

import httpx

from fairmeet.adapters import (
    GeocodeResult,
    GeoPoint,
    GeoUnavailable,
    PoiResult,
    RouteResult,
)
from fairmeet.domain import ProvenanceKind, TransportMode

_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
_POI_URL = "https://restapi.amap.com/v3/place/around"
_ROUTE_URL = "https://restapi.amap.com/v3/direction/walking"  # 步行；驾车/公交另用对应端点

_MODE_TO_AMAP = {
    TransportMode.WALK: "walking",
    TransportMode.TRANSIT: "transit",
    TransportMode.DRIVE: "driving",
    TransportMode.TAXI: "driving",
}


class AmapGeoAdapter:
    def __init__(self, key: str, timeout: float = 5.0) -> None:
        self._key = key
        self._timeout = timeout

    def _get(self, url: str, params: dict[str, str]) -> dict:
        params = {**params, "key": self._key}
        try:
            resp = httpx.get(url, params=params, timeout=self._timeout)
        except httpx.HTTPError as exc:  # 断网/超时/连接错误
            raise GeoUnavailable(f"高德请求失败: {exc}") from exc
        if resp.status_code != 200:
            raise GeoUnavailable(f"高德 HTTP {resp.status_code}")
        data = resp.json()
        if str(data.get("status")) != "1":
            raise GeoUnavailable(f"高德返回错误: {data.get('info')}")
        return data

    def geocode(self, address: str) -> GeocodeResult:
        if not address.strip():
            raise GeoUnavailable("空地址")
        data = self._get(_GEOCODE_URL, {"address": address})
        geocodes = data.get("geocodes") or []
        if not geocodes:
            raise GeoUnavailable(f"高德未识别地址: {address}")
        lng_s, lat_s = geocodes[0]["location"].split(",")
        return GeocodeResult(lng=float(lng_s), lat=float(lat_s), provenance=ProvenanceKind.EXTERNAL, source="amap")

    def search_poi(self, center: GeoPoint, keyword: str, radius_m: int = 1500) -> list[PoiResult]:
        data = self._get(
            _POI_URL,
            {"location": f"{center.lng},{center.lat}", "keywords": keyword, "radius": str(radius_m)},
        )
        pois = data.get("pois") or []
        out: list[PoiResult] = []
        for p in pois:
            try:
                lng_s, lat_s = p["location"].split(",")
            except (KeyError, ValueError):
                continue
            dist = p.get("distance")
            out.append(
                PoiResult(
                    name=p.get("name", ""),
                    category=p.get("type"),
                    lng=float(lng_s),
                    lat=float(lat_s),
                    distance_m=float(dist) if dist not in (None, "") else None,
                    provenance=ProvenanceKind.EXTERNAL,
                    source="amap",
                )
            )
        if not out:
            raise GeoUnavailable(f"高德周边搜索无结果: {keyword}")
        return out

    def route(self, origin: GeoPoint, destination: GeoPoint, mode: TransportMode) -> RouteResult:
        amap_mode = _MODE_TO_AMAP.get(mode, "walking")
        url = f"https://restapi.amap.com/v3/direction/{amap_mode}"
        data = self._get(
            url,
            {"origin": f"{origin.lng},{origin.lat}", "destination": f"{destination.lng},{destination.lat}"},
        )
        paths = ((data.get("route") or {}).get("paths")) or []
        if not paths:
            raise GeoUnavailable("高德路径无结果")
        path = paths[0]
        distance_m = float(path["distance"]) if path.get("distance") else None
        duration_s = float(path["duration"]) if path.get("duration") else None
        return RouteResult(
            distance_m=distance_m,
            duration_minutes=round(duration_s / 60, 1) if duration_s is not None else None,
            provenance=ProvenanceKind.EXTERNAL,
            source="amap",
        )
