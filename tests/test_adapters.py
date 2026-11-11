"""阶段 C：地理适配器测试（离线，不访问真实网络）。

覆盖：
- FakeGeoAdapter：确定性 geocode、route 返回 estimated、unavailable 时三个方法都抛 GeoUnavailable。
- AmapGeoAdapter：用 respx mock httpx，验证 geocode/search_poi/route 的解析与降级路径，
  断言请求带 key 参数（只校验存在，不断言真实 key 值）。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from fairmeet.adapters import GeoPoint, GeoUnavailable
from fairmeet.adapters.amap import AmapGeoAdapter
from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.domain import ProvenanceKind, TransportMode

# ===========================================================================
# FakeGeoAdapter
# ===========================================================================


def test_fake_geocode_is_deterministic():
    a = FakeGeoAdapter()
    r1 = a.geocode("宽窄巷子")
    r2 = a.geocode("宽窄巷子")
    # 同一地址永远得到同一坐标
    assert (r1.lng, r1.lat) == (r2.lng, r2.lat)
    assert r1.provenance == ProvenanceKind.EXTERNAL
    assert r1.source == "mock"
    # 空白地址视为不可用
    with pytest.raises(GeoUnavailable):
        a.geocode("   ")


def test_fake_route_returns_estimated():
    a = FakeGeoAdapter()
    origin = GeoPoint(lng=104.06, lat=30.57)
    dest = GeoPoint(lng=104.08, lat=30.65)
    route = a.route(origin, dest, TransportMode.WALK)
    assert route.provenance == ProvenanceKind.ESTIMATED
    assert route.source == "mock-estimate"
    assert route.distance_m is not None and route.distance_m > 0
    assert route.duration_minutes is not None and route.duration_minutes > 0


def test_fake_unavailable_raises_for_all_methods():
    a = FakeGeoAdapter(unavailable=True)
    with pytest.raises(GeoUnavailable):
        a.geocode("任何地址")
    with pytest.raises(GeoUnavailable):
        a.search_poi(GeoPoint(lng=104.0, lat=30.6), "咖啡")
    with pytest.raises(GeoUnavailable):
        a.route(GeoPoint(lng=104.0, lat=30.6), GeoPoint(lng=104.1, lat=30.7), TransportMode.TRANSIT)


# ===========================================================================
# AmapGeoAdapter（respx mock httpx，不访问真实网络）
# ===========================================================================

_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"


def test_amap_geocode_parses_location():
    adapter = AmapGeoAdapter("test-key")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_GEOCODE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "1",
                    "info": "OK",
                    "geocodes": [{"formatted_address": "成都市宽窄巷子", "location": "104.055,30.669"}],
                },
            )
        )
        result = adapter.geocode("宽窄巷子")

    assert route.called
    assert result.lng == pytest.approx(104.055)
    assert result.lat == pytest.approx(30.669)
    assert result.provenance == ProvenanceKind.EXTERNAL
    assert result.source == "amap"
    # 请求必须带上 key 参数（只校验存在，不校验真实值）
    assert "key" in route.calls.last.request.url.params


def test_amap_geocode_status_error_raises():
    adapter = AmapGeoAdapter("test-key")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_GEOCODE_URL).mock(
            return_value=httpx.Response(200, json={"status": "0", "info": "INVALID_USER_KEY"})
        )
        with pytest.raises(GeoUnavailable):
            adapter.geocode("宽窄巷子")


def test_amap_geocode_empty_geocodes_raises():
    adapter = AmapGeoAdapter("test-key")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_GEOCODE_URL).mock(
            return_value=httpx.Response(200, json={"status": "1", "info": "OK", "geocodes": []})
        )
        with pytest.raises(GeoUnavailable):
            adapter.geocode("不存在的地方")


def test_amap_http_500_raises():
    adapter = AmapGeoAdapter("test-key")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_GEOCODE_URL).mock(return_value=httpx.Response(500, text="internal error"))
        with pytest.raises(GeoUnavailable):
            adapter.geocode("宽窄巷子")


def test_amap_search_poi_parses():
    adapter = AmapGeoAdapter("test-key")
    url = "https://restapi.amap.com/v3/place/around"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "1",
                    "info": "OK",
                    "pois": [
                        {"name": "星巴克", "type": "餐饮服务", "location": "104.06,30.60", "distance": "120"},
                        {"name": "无坐标POI", "type": "其他"},  # 缺 location 应跳过
                    ],
                },
            )
        )
        results = adapter.search_poi(GeoPoint(lng=104.06, lat=30.60), "咖啡", radius_m=800)

    assert route.called
    assert len(results) == 1
    poi = results[0]
    assert poi.name == "星巴克"
    assert poi.lng == pytest.approx(104.06)
    assert poi.lat == pytest.approx(30.60)
    assert poi.distance_m == pytest.approx(120.0)
    assert poi.source == "amap"
    assert "key" in route.calls.last.request.url.params
    # radius 参数应透传
    assert route.calls.last.request.url.params["radius"] == "800"


def test_amap_search_poi_empty_raises():
    adapter = AmapGeoAdapter("test-key")
    url = "https://restapi.amap.com/v3/place/around"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url).mock(
            return_value=httpx.Response(200, json={"status": "1", "info": "OK", "pois": []})
        )
        with pytest.raises(GeoUnavailable):
            adapter.search_poi(GeoPoint(lng=104.06, lat=30.60), "不存在")


def test_amap_route_parses():
    adapter = AmapGeoAdapter("test-key")
    url = "https://restapi.amap.com/v3/direction/walking"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "1",
                    "info": "OK",
                    "route": {"paths": [{"distance": "1200", "duration": "900"}]},
                },
            )
        )
        result = adapter.route(
            GeoPoint(lng=104.06, lat=30.60), GeoPoint(lng=104.07, lat=30.61), TransportMode.WALK
        )

    assert route.called
    assert result.distance_m == pytest.approx(1200.0)
    # 900 秒 -> 15 分钟
    assert result.duration_minutes == pytest.approx(15.0)
    assert result.source == "amap"
    assert "key" in route.calls.last.request.url.params


def test_amap_route_no_paths_raises():
    adapter = AmapGeoAdapter("test-key")
    url = "https://restapi.amap.com/v3/direction/walking"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url).mock(
            return_value=httpx.Response(200, json={"status": "1", "info": "OK", "route": {"paths": []}})
        )
        with pytest.raises(GeoUnavailable):
            adapter.route(GeoPoint(lng=104.06, lat=30.60), GeoPoint(lng=104.07, lat=30.61), TransportMode.WALK)
