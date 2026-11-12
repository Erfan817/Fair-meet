"""Offline tests for real POI search as a separately labelled source."""

from __future__ import annotations

import time

import pytest

from fairmeet.adapters import DiscoveryPoi, GeoUnavailable
from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.content import ResearchRequest
from fairmeet.content.models import ContentProviderError
from fairmeet.content.providers.amap_poi import AmapContentProvider
from fairmeet.content.service import ResearchService
from fairmeet.db import Database
from fairmeet.domain import TripCreate
from fairmeet.itinerary import ItineraryService
from fairmeet.services import TripService


class Geo(FakeGeoAdapter):
    def __init__(self):
        super().__init__()
        self.calls = []

    def search_city(self, city, keyword):
        self.calls.append((city, keyword))
        return [
            DiscoveryPoi(
                id="poi-test", name="合成公园", city="成都市", address="合成地址", lng=104.1, lat=30.6, category="公园"
            )
        ]


def test_no_fake_geo_masquerading_as_real_search():
    assert not AmapContentProvider(FakeGeoAdapter()).available()
    with pytest.raises(ContentProviderError):
        AmapContentProvider(FakeGeoAdapter()).search("成都")


def test_real_poi_has_structured_scope_not_reviews():
    geo = Geo()
    provider = AmapContentProvider(geo)
    hits = provider.search("成都", "", topic="culture")
    assert geo.calls == [("成都", "博物馆")]
    assert len(hits) == 1
    assert hits[0].content_scope == "structured"
    assert "不含用户点评" in hits[0].excerpt
    assert "amap.com/place/poi-test" in hits[0].url
    assert hits[0].title == "合成公园"


def test_foreign_city_skipped_and_failure_safe():
    class Foreign(Geo):
        def search_city(self, city, keyword):
            return [DiscoveryPoi(id="x", name="合成地点", city="北京市", address="", lng=116, lat=40)]

    assert AmapContentProvider(Foreign()).search("成都") == []

    class Broken(Geo):
        def search_city(self, city, keyword):
            raise GeoUnavailable("secret-do-not-show")

    with pytest.raises(ContentProviderError) as caught:
        AmapContentProvider(Broken()).search("成都")
    assert caught.value.code == "provider_failure" and "secret" not in str(caught.value)


def test_source_consent_and_structured_evidence_round_trip():
    db = Database(":memory:")
    trips = TripService(db)
    geo = Geo()
    itin = ItineraryService(db, trips, geo)
    service = ResearchService(db, trips, itin)
    try:
        trip, owner = trips.create_trip(TripCreate(title="合成来源验收"))
        source = next(s for s in service.sources(trip.id, owner.id) if s["id"] == "amap_poi")
        assert source["available"] and "非点评" in source["name"]
        service.set_consent(trip.id, owner.id, True)
        run = service.create(
            trip.id,
            owner.id,
            ResearchRequest(
                message="公园", enabled_sources=["amap_poi"], network_consent=True, idempotency_key="amap-1"
            ),
        )
        for _ in range(100):
            run = service.get(trip.id, owner.id, run["id"])
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert run["status"] == "succeeded" and not run["is_simulated"]
        assert run["documents"][0]["content_scope"] == "structured"
        assert "不含点评或攻略" in run["reply"]
        assert geo.calls == [("成都", "公园")]
    finally:
        service.close()
        db.close()
