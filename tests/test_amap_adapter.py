"""高德真实适配器 mock 测试（respx 拦截 httpx，不联网、不含真实 key）。"""

from __future__ import annotations

import httpx
import pytest
import respx

from fairmeet.adapters import GeoPoint, GeoUnavailable
from fairmeet.adapters.amap import AmapGeoAdapter
from fairmeet.domain import TransportMode

_BASE = "https://restapi.amap.com/v3"
_KEY = "fake-test-key"


def _adapter() -> AmapGeoAdapter:
    return AmapGeoAdapter(_KEY)


@respx.mock
def test_geocode_ok():
    respx.get(f"{_BASE}/geocode/geo").mock(
        return_value=httpx.Response(200, json={"status": "1", "info": "OK", "geocodes": [{"location": "104.05,30.66"}]})
    )
    g = _adapter().geocode("宽窄巷子")
    assert round(g.lng, 2) == 104.05 and round(g.lat, 2) == 30.66
    assert g.provenance.value == "external"
    # key 通过 query 传入（存在即可，不校验真实值）
    assert "key" in respx.calls.last.request.url.params


@respx.mock
def test_geocode_status_error_raises():
    respx.get(f"{_BASE}/geocode/geo").mock(
        return_value=httpx.Response(200, json={"status": "0", "info": "INVALID_USER_KEY"})
    )
    with pytest.raises(GeoUnavailable):
        _adapter().geocode("x")


@respx.mock
def test_geocode_no_result_raises():
    respx.get(f"{_BASE}/geocode/geo").mock(
        return_value=httpx.Response(200, json={"status": "1", "info": "OK", "geocodes": []})
    )
    with pytest.raises(GeoUnavailable):
        _adapter().geocode("不存在的地方")


@respx.mock
def test_geocode_http500_raises():
    respx.get(f"{_BASE}/geocode/geo").mock(return_value=httpx.Response(500))
    with pytest.raises(GeoUnavailable):
        _adapter().geocode("x")


@respx.mock
def test_poi_ok_and_skips_bad_location():
    respx.get(f"{_BASE}/place/around").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "1",
                "info": "OK",
                "pois": [
                    {"name": "咖啡A", "type": "餐饮;咖啡厅", "location": "104.06,30.66", "distance": "120"},
                    {"name": "坏数据无location"},
                ],
            },
        )
    )
    pois = _adapter().search_poi(GeoPoint(lng=104.05, lat=30.66), "咖啡")
    assert len(pois) == 1
    assert pois[0].name == "咖啡A" and pois[0].distance_m == 120.0


@respx.mock
def test_poi_empty_raises():
    respx.get(f"{_BASE}/place/around").mock(return_value=httpx.Response(200, json={"status": "1", "pois": []}))
    with pytest.raises(GeoUnavailable):
        _adapter().search_poi(GeoPoint(lng=104.05, lat=30.66), "咖啡")


@respx.mock
def test_route_ok():
    respx.get(f"{_BASE}/direction/walking").mock(
        return_value=httpx.Response(
            200,
            json={"status": "1", "route": {"paths": [{"distance": "2879", "duration": "2304"}]}},
        )
    )
    r = _adapter().route(GeoPoint(lng=1, lat=1), GeoPoint(lng=2, lat=2), TransportMode.WALK)
    assert r.distance_m == 2879.0
    assert round(r.duration_minutes, 1) == 38.4
    assert r.provenance.value == "external"


@respx.mock
def test_route_no_paths_raises():
    respx.get(f"{_BASE}/direction/walking").mock(
        return_value=httpx.Response(200, json={"status": "1", "route": {"paths": []}})
    )
    with pytest.raises(GeoUnavailable):
        _adapter().route(GeoPoint(lng=1, lat=1), GeoPoint(lng=2, lat=2), TransportMode.WALK)


def test_network_error_raises():
    with respx.mock:
        respx.get(f"{_BASE}/geocode/geo").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(GeoUnavailable):
            _adapter().geocode("x")
