"""Evidence -> real activity proposal; all HTTP and LLM work is mocked."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from fairmeet.adapters import DiscoveryPoi, GeoPoint, RouteResult
from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.app import create_app
from fairmeet.content import ContentHit
from fairmeet.db import Database
from fairmeet.demo import DemoSettings
from fairmeet.domain import ProvenanceKind


class PublicSource:
    source = "public_web"
    is_simulated = False
    daily_limit = 10

    def available(self):
        return True

    def search(self, city, scope="", *, topic="general"):
        return [
            ContentHit(
                title="成都人民公园和成都博物馆游览攻略",
                url="https://example.org/chengdu",
                excerpt="人民公园适合散步，成都博物馆展览须查询预约与开放安排。",
                platform="公开搜索",
            )
        ]


class Extractor:
    def __init__(self, malicious=False):
        self.calls = []
        self.malicious = malicious

    def complete(self, system, user):
        self.calls.append((system, user))
        docs = json.loads(user)["documents"]
        eid = docs[0]["id"]
        return json.dumps(
            {
                "places": [
                    {"name": "人民公园", "evidence_ids": [eid], "category": "activity"},
                    {
                        "name": "成都博物馆" if not self.malicious else "模型虚构餐厅",
                        "evidence_ids": [eid],
                        "category": "activity",
                    },
                ]
            },
            ensure_ascii=False,
        )


class DiscoveryGeo(FakeGeoAdapter):
    def __init__(self, ambiguous=False):
        super().__init__()
        self.ambiguous = ambiguous
        self.calls = []

    def search_city(self, city, keyword):
        self.calls.append((city, keyword))
        poi = DiscoveryPoi(
            id=keyword,
            name=keyword,
            city="成都市",
            address="公开地址",
            lng=104.06,
            lat=30.66,
            provenance=ProvenanceKind.EXTERNAL,
        )
        return [poi, poi] if self.ambiguous else [poi]

    def route(self, origin: GeoPoint, destination: GeoPoint, mode):
        return RouteResult(duration_minutes=20, distance_m=1000, source="amap")


@pytest.fixture()
def setup():
    db = Database(":memory:")
    model, geo = Extractor(), DiscoveryGeo()
    app = create_app(db, geo=geo, llm=model, demo_settings=DemoSettings(), content_provider=PublicSource())
    with TestClient(app) as client:
        data = client.post("/api/trips", json={"title": "虚构规划测试", "destination": "成都"}).json()
        tid, mid = data["trip"]["id"], data["owner"]["id"]
        day = client.post(f"/api/trips/{tid}/days", params={"member_id": mid}, json={"day_index": 1}).json()
        client.post(f"/api/trips/{tid}/research-consent", params={"member_id": mid}, json={"enabled": True})
        run = client.post(
            f"/api/trips/{tid}/research",
            params={"member_id": mid},
            json={
                "message": "公园散步",
                "enabled_sources": ["public_web"],
                "network_consent": True,
                "idempotency_key": "search-1",
            },
        ).json()
        for _ in range(100):
            run = client.get(f"/api/trips/{tid}/research/{run['id']}", params={"member_id": mid}).json()
            if run["status"] not in {"running", "queued"}:
                break
            time.sleep(0.01)
        assert run["status"] == "succeeded", run
        yield client, tid, mid, day["id"], run, model, geo
    db.close()


def plan(setup, **extra):
    client, tid, mid, did, run, _, _ = setup
    payload = {
        "candidate_ids": [run["candidates"][0]["id"]],
        "day_ids": [did],
        "idempotency_key": "plan-1",
        "model_consent": True,
        **extra,
    }
    return client.post(f"/api/trips/{tid}/research/{run['id']}/plan", params={"member_id": mid}, json=payload)


def test_evidence_planning_never_writes_before_confirm_and_links_real_places(setup):
    client, tid, mid, did, run, model, geo = setup
    response = plan(setup, geo_consent=True)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["planning"]["model_mode"] == "deepseek"
    assert len(result["planning"]["places"]) == 2
    assert all(p["match_status"] == "map_matched" for p in result["planning"]["places"])
    assert client.get(f"/api/trips/{tid}/items").json() == []
    assert client.get(f"/api/trips/{tid}/places").json() == []
    assert len(model.calls) == 1
    assert plan(setup, geo_consent=True).json() == result
    assert len(model.calls) == 1
    pid = result["proposal"]["id"]
    confirmed = client.post(f"/api/trips/{tid}/proposals/{pid}/confirm", params={"member_id": mid})
    assert confirmed.status_code == 200, confirmed.text
    assert len(confirmed.json()["executed"]) == 2
    items = client.get(f"/api/trips/{tid}/items").json()
    assert all(i["item_type"] == "activity" and i["place_id"] for i in items)
    assert all(i["cost"] is None for i in items)
    assert len(client.get(f"/api/trips/{tid}/transports").json()) == 1
    assert len(client.get(f"/api/trips/{tid}/places").json()) == 2


def test_model_cannot_invent_name_or_evidence(setup):
    client, tid, mid, did, run, model, geo = setup
    model.malicious = True
    response = plan(setup)
    assert response.status_code == 200, response.text
    assert [p["name"] for p in response.json()["planning"]["places"]] == ["人民公园"]
    assert geo.calls == []


def test_no_model_consent_does_not_call_model(setup):
    _, _, _, _, _, model, _ = setup
    response = plan(setup, model_consent=False)
    assert response.status_code == 200, response.text
    assert response.json()["planning"]["model_mode"] == "rules"
    assert {p["name"] for p in response.json()["planning"]["places"]} == {"人民公园", "成都博物馆"}
    assert not model.calls


def test_sensitive_hard_need_blocks_before_model(setup):
    client, tid, mid, _, _, model, _ = setup
    client.post(
        f"/api/trips/{tid}/needs",
        params={"member_id": mid},
        json={"kind": "hard", "category": "dietary", "value": "严重过敏"},
    )
    response = plan(setup)
    assert response.status_code == 400
    assert not model.calls


def test_stale_plan_context_and_revoked_consent_block_confirm(setup):
    client, tid, mid, _, _, _, _ = setup
    response = plan(setup)
    assert response.status_code == 200
    pid = response.json()["proposal"]["id"]
    client.post(f"/api/trips/{tid}/research-consent", params={"member_id": mid}, json={"enabled": False})
    result = client.post(f"/api/trips/{tid}/proposals/{pid}/confirm", params={"member_id": mid})
    assert result.status_code == 400
    assert client.get(f"/api/trips/{tid}/items").json() == []


def test_ambiguous_geo_does_not_bind_place(setup):
    _, _, _, _, _, _, geo = setup
    geo.ambiguous = True
    result = plan(setup, geo_consent=True).json()
    assert all(p["poi"] is None for p in result["planning"]["places"])
    assert any("唯一" in w for w in result["planning"]["warnings"])


def test_existing_timed_activities_not_overlapped(setup):
    client, tid, mid, did, run, _, _ = setup
    # Modify before generating a fresh matching research fingerprint.
    client.post(
        f"/api/trips/{tid}/days/{did}/items",
        params={"member_id": mid},
        json={
            "title": "原有安排",
            "start_time": "09:00",
            "end_time": "12:00",
        },
    )
    research = client.app.state.research
    with client.app.state.db.cursor() as cur:
        cur.execute("UPDATE research_runs SET input_fingerprint=? WHERE id=?", (research.fingerprint(tid), run["id"]))
    response = plan(setup)
    assert response.status_code == 200, response.text
    assert all(p["start_time"] >= "13:00" for p in response.json()["planning"]["places"])


def test_wrong_day_and_idempotency_changes_rejected(setup):
    response = plan(setup, day_ids=["invented-day"])
    assert response.status_code == 404
    response = plan(setup)
    assert response.status_code == 200
    assert plan(setup, model_consent=False).status_code == 400
