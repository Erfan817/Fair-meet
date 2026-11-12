"""城市 POI 检索与高德凭据保护；全部 mock，不读取 Key 或访问真实网络。"""

from __future__ import annotations

import logging
import traceback

import httpx
import pytest
import respx

from fairmeet.adapters import DiscoveryAdapter, DiscoveryPoi, GeoAdapter, GeoUnavailable
from fairmeet.adapters.amap import AmapGeoAdapter
from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.domain import ProvenanceKind

_URL = "https://restapi.amap.com/v3/place/text"
_KEY = "dummy-discovery-secret"


def _poi(**overrides):
    return {
        "id": "B001",
        "name": "原始景点名",
        "cityname": "成都市",
        "address": "原始街道 12 号",
        "location": "104.06,30.66",
        "type": "风景名胜;公园广场",
        **overrides,
    }


def test_discovery_is_optional_and_model_defaults():
    assert isinstance(AmapGeoAdapter(_KEY), DiscoveryAdapter)
    fake = FakeGeoAdapter()
    assert isinstance(fake, GeoAdapter)
    assert not isinstance(fake, DiscoveryAdapter)
    assert not hasattr(fake, "search_city")
    poi = DiscoveryPoi(id="B001", name="景点", city="成都", address="街道", lng=104.06, lat=30.66)
    assert poi.category is None
    assert poi.provenance == ProvenanceKind.EXTERNAL
    assert poi.source == "amap"


@respx.mock
def test_search_city_fields_and_fixed_query():
    route = respx.get(_URL).respond(200, json={"status": "1", "pois": [_poi()]})
    result = AmapGeoAdapter(_KEY).search_city(" 成都 ", " 公园 ")
    assert [poi.model_dump() for poi in result] == [
        {
            "id": "B001",
            "name": "原始景点名",
            "city": "成都市",
            "address": "原始街道 12 号",
            "lng": 104.06,
            "lat": 30.66,
            "category": "风景名胜;公园广场",
            "provenance": ProvenanceKind.EXTERNAL,
            "source": "amap",
        }
    ]
    assert route.call_count == 1
    assert dict(route.calls.last.request.url.params) == {
        "city": "成都",
        "citylimit": "true",
        "keywords": "公园",
        "offset": "10",
        "page": "1",
        "extensions": "base",
        "key": _KEY,
    }


@pytest.mark.parametrize(
    ("cityname", "expected"),
    [("成都市", "成都市"), (["成都市"], "成都市"), ([], ""), (None, ""), ("", ""), ("重庆市", "重庆市")],
)
@respx.mock
def test_result_city_comes_from_provider_not_query(cityname, expected):
    respx.get(_URL).respond(200, json={"status": "1", "pois": [_poi(cityname=cityname)]})
    assert AmapGeoAdapter(_KEY).search_city("成都", "公园")[0].city == expected


@respx.mock
def test_missing_optional_text_is_not_fabricated():
    raw = _poi(address=[], type=[])
    raw.pop("cityname")
    respx.get(_URL).respond(200, json={"status": "1", "pois": [raw]})
    poi = AmapGeoAdapter(_KEY).search_city("成都", "公园")[0]
    assert poi.city == ""
    assert poi.address == ""
    assert poi.category is None


@pytest.mark.parametrize(("city", "keyword"), [("", "公园"), (" \t", "公园"), ("成都", ""), ("成都", " ")])
@respx.mock
def test_empty_city_or_keyword_does_not_request(city, keyword):
    with pytest.raises(GeoUnavailable, match="城市和关键词不能为空"):
        AmapGeoAdapter(_KEY).search_city(city, keyword)
    assert not respx.calls


@pytest.mark.parametrize(
    "location",
    [
        "nan,30", "104,nan", "inf,30", "104,-inf", "181,30", "-181,30", "104,91", "104,-91",
        "bad,30", "104", "104,30,1", "", None, [], {}, 104,
    ],
)
@respx.mock
def test_invalid_coordinates_are_skipped(location):
    respx.get(_URL).respond(200, json={"status": "1", "pois": [_poi(location=location), _poi(id="good")]})
    assert [poi.id for poi in AmapGeoAdapter(_KEY).search_city("成都", "公园")] == ["good"]


@respx.mock
def test_missing_identity_or_location_is_skipped():
    missing_location = _poi()
    missing_location.pop("location")
    respx.get(_URL).respond(
        200,
        json={
            "status": "1",
            "pois": [None, [], "bad", missing_location, _poi(id=""), _poi(name=" "), _poi(id="good")],
        },
    )
    assert [poi.id for poi in AmapGeoAdapter(_KEY).search_city("成都", "公园")] == ["good"]


@pytest.mark.parametrize("location", ["-180,-90", "180,90", "0,0"])
@respx.mock
def test_coordinate_boundaries_are_valid(location):
    respx.get(_URL).respond(200, json={"status": "1", "pois": [_poi(location=location)]})
    poi = AmapGeoAdapter(_KEY).search_city("成都", "公园")[0]
    assert (poi.lng, poi.lat) == tuple(map(float, location.split(",")))


@respx.mock
def test_city_search_has_ten_item_limit_without_paging():
    route = respx.get(_URL).respond(200, json={"status": "1", "pois": [_poi(id=str(i)) for i in range(15)]})
    assert [poi.id for poi in AmapGeoAdapter(_KEY).search_city("成都", "公园")] == list(map(str, range(10)))
    assert route.call_count == 1


@pytest.mark.parametrize("pois", [[], None, [{"location": "nan,30"}], "bad", {"id": "B001"}])
@respx.mock
def test_empty_or_malformed_results_raise(pois):
    respx.get(_URL).respond(200, json={"status": "1", "pois": pois})
    with pytest.raises(GeoUnavailable):
        AmapGeoAdapter(_KEY).search_city("成都", "公园")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text=f"private-body {_KEY}", headers={"X-Private": _KEY}),
        httpx.Response(200, json={"status": "0", "info": f"private-body {_KEY}"}),
        httpx.Response(200, text=f"not-json private-body {_KEY}"),
        httpx.Response(200, json=[_KEY]),
    ],
)
@respx.mock
def test_http_and_json_errors_are_safe(response, caplog):
    caplog.set_level(logging.DEBUG)
    respx.get(_URL).mock(return_value=response)
    with pytest.raises(GeoUnavailable) as exc:
        AmapGeoAdapter(_KEY).search_city("成都", "公园")
    rendered = "".join(traceback.format_exception(exc.value)) + caplog.text
    assert _KEY not in rendered
    assert "private-body" not in rendered
    assert "X-Private" not in rendered


@respx.mock
def test_transport_error_does_not_expose_url_or_exception_chain(caplog):
    caplog.set_level(logging.DEBUG)

    def fail(request):
        logging.getLogger("httpcore.http11").debug("headers: X-Private=%s", _KEY)
        logging.getLogger("httpx").info("request: %s", request.url)
        raise httpx.ConnectError(f"private-body {request.url}", request=request)

    respx.get(_URL).mock(side_effect=fail)
    with pytest.raises(GeoUnavailable, match="^高德请求失败$") as exc:
        AmapGeoAdapter(_KEY).search_city("成都", "公园")
    rendered = "".join(traceback.format_exception(exc.value)) + caplog.text
    assert _KEY not in rendered
    assert "private-body" not in rendered
    assert "X-Private" not in rendered
    assert "ConnectError" not in rendered


@respx.mock
def test_success_httpx_info_log_hides_key_and_restores_logging(caplog):
    caplog.set_level(logging.DEBUG)
    respx.get(_URL).respond(200, json={"status": "1", "pois": [_poi()]})
    AmapGeoAdapter(_KEY).search_city("成都", "公园")
    assert _KEY not in caplog.text
    assert "HTTP Request" not in caplog.text
    logging.getLogger("httpx").info("ordinary-http-log")
    assert "ordinary-http-log" in caplog.text


@respx.mock
def test_redirect_is_not_followed(caplog):
    caplog.set_level(logging.DEBUG)
    route = respx.get(_URL).respond(302, headers={"Location": f"https://other.example/?key={_KEY}"})
    with pytest.raises(GeoUnavailable):
        AmapGeoAdapter(_KEY).search_city("成都", "公园")
    assert route.call_count == 1
    assert respx.calls.call_count == 1
    assert _KEY not in caplog.text
