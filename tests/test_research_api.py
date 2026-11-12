"""Offline research adoption, authorization, idempotency and proposal baselines."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.ai.orchestrator import FakeLLM
from fairmeet.app import create_app
from fairmeet.db import Database
from fairmeet.demo import DemoSettings
from fairmeet.domain import InspirationCreate, PlaceCreate, TripCreate
from fairmeet.services import Forbidden, TripService


@pytest.fixture()
def client():
    db = Database(":memory:")
    app = create_app(db, geo=FakeGeoAdapter(), llm=FakeLLM(), demo_settings=DemoSettings())
    with TestClient(app) as client:
        yield client
    db.close()


def trip(client, title="成都研究旅程"):
    response = client.post("/api/trips", json={"title": title, "destination": "成都"})
    assert response.status_code == 200
    data = response.json()
    return data["trip"]["id"], data["owner"]["id"]


def start(client, tid, mid, key="discover-1", **extra):
    data = {"message": "发现成都适合多人活动的候选", "enabled_sources": ["fake"],
            "idempotency_key": key, **extra}
    response = client.post(f"/api/trips/{tid}/research", params={"member_id": mid}, json=data)
    assert response.status_code == 202, response.text
    return response.json()


def finished(client, tid, mid, run):
    for _ in range(200):
        response = client.get(f"/api/trips/{tid}/research/{run['id']}", params={"member_id": mid})
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] not in {"queued", "running"}:
            assert result["status"] in {"succeeded", "partial"}, result
            return result
        time.sleep(.01)
    pytest.fail("offline research did not finish")


def select(client, tid, mid, run, action, key, **extra):
    return client.post(f"/api/trips/{tid}/research/{run['id']}/selections",
                       params={"member_id": mid}, json={"candidate_id": run["candidates"][0]["id"],
                                                        "action": action, "idempotency_key": key, **extra})


def day(client, tid, mid):
    response = client.post(f"/api/trips/{tid}/days", params={"member_id": mid},
                           json={"day_index": 1, "date": "2026-12-01"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_research_has_no_business_writes_and_is_explicitly_simulated(client):
    tid, mid = trip(client)
    run = finished(client, tid, mid, start(client, tid, mid))
    assert run["is_simulated"] is True
    assert len(run["candidates"]) >= 1
    ids = {d["id"] for d in run["documents"]}
    assert all(c["evidence_ids"] and set(c["evidence_ids"]) <= ids for c in run["candidates"])
    assert all(d["is_simulated"] for d in run["documents"])
    for resource in ["inspirations", "places", "items", "proposals"]:
        assert client.get(f"/api/trips/{tid}/{resource}").json() == []


def test_research_and_sources_require_current_same_trip_member(client):
    tid, mid = trip(client)
    other_tid, other_mid = trip(client, "其他旅程")
    run = finished(client, tid, mid, start(client, tid, mid))
    base = f"/api/trips/{tid}"
    for path in ["research-sources", f"research/{run['id']}"]:
        assert client.get(f"{base}/{path}", params={"member_id": other_mid}).status_code == 403
    assert client.get(f"/api/trips/{other_tid}/research/{run['id']}",
                      params={"member_id": other_mid}).status_code in {403, 404}
    with client.app.state.db.cursor() as cur:
        cur.execute("UPDATE members SET status='declined' WHERE id=?", (mid,))
    assert client.get(f"{base}/research-sources", params={"member_id": mid}).status_code == 403
    assert select(client, tid, mid, run, "save", "departed").status_code == 403


def test_save_is_idempotent_preserves_evidence_and_no_duplicate_place(client):
    tid, mid = trip(client)
    run = finished(client, tid, mid, start(client, tid, mid))
    first = select(client, tid, mid, run, "save", "save-1")
    assert first.status_code == 200, first.text
    retry = select(client, tid, mid, run, "save", "save-1")
    assert retry.json() == first.json()
    another_key = select(client, tid, mid, run, "save", "save-2")
    assert another_key.json() == first.json()
    data = client.get(f"/api/trips/{tid}/inspirations").json()
    assert len(data) == 1 and data[0]["status"] == "pending"
    assert "模拟" in data[0]["note"]
    assert client.get(f"/api/trips/{tid}/places").json() == []
    with client.app.state.db.cursor() as cur:
        cur.execute("SELECT snapshot FROM research_evidence_links WHERE target_id=?", (data[0]["id"],))
        assert run["documents"][0]["id"] in cur.fetchone()["snapshot"]
    conflicting = select(client, tid, mid, run, "dismiss", "save-1")
    assert conflicting.status_code == 400


def test_note_proposal_confirm_only_then_item_and_evidence(client):
    tid, mid = trip(client)
    did = day(client, tid, mid)
    run = finished(client, tid, mid, start(client, tid, mid))
    response = select(client, tid, mid, run, "build_proposal", "propose", day_id=did)
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    assert proposal["status"] == "pending"
    assert client.get(f"/api/trips/{tid}/items").json() == []
    result = client.post(f"/api/trips/{tid}/proposals/{proposal['id']}/confirm",
                         params={"member_id": mid})
    assert result.status_code == 200, result.text
    assert not result.json()["skipped"]
    items = client.get(f"/api/trips/{tid}/items").json()
    assert len(items) == 1 and items[0]["item_type"] == "note"
    assert items[0]["cost"] is None and items[0]["place_id"] is None
    assert "模拟" in items[0]["title"]
    with client.app.state.db.cursor() as cur:
        cur.execute("SELECT snapshot FROM research_evidence_links WHERE target_id=?", (items[0]["id"],))
        assert cur.fetchone() is not None
    assert client.post(f"/api/trips/{tid}/proposals/{proposal['id']}/confirm",
                       params={"member_id": mid}).status_code == 400


@pytest.mark.parametrize("change", ["budget", "need", "item"])
def test_context_changes_reject_stale_proposal(client, change):
    tid, mid = trip(client)
    did = day(client, tid, mid)
    run = finished(client, tid, mid, start(client, tid, mid))
    result = select(client, tid, mid, run, "build_proposal", "propose", day_id=did)
    assert result.status_code == 200, result.text
    pid = result.json()["proposal"]["id"]
    if change == "budget":
        with client.app.state.db.cursor() as cur:
            cur.execute("UPDATE trips SET budget=200 WHERE id=?", (tid,))
    elif change == "need":
        client.post(f"/api/trips/{tid}/needs", params={"member_id": mid},
                    json={"kind": "hard", "category": "dietary", "value": "花生过敏"})
    else:
        client.post(f"/api/trips/{tid}/days/{did}/items", params={"member_id": mid},
                    json={"title": "新安排"})
    result = client.post(f"/api/trips/{tid}/proposals/{pid}/confirm", params={"member_id": mid})
    assert result.status_code == 400 and "变化" in result.json()["detail"]
    assert all("模拟" not in i["title"] for i in client.get(f"/api/trips/{tid}/items").json())


def test_invalid_candidate_foreign_day_and_foreign_proposal(client):
    tid, mid = trip(client)
    other_tid, other_mid = trip(client, "别的旅程")
    foreign_day = day(client, other_tid, other_mid)
    run = finished(client, tid, mid, start(client, tid, mid))
    assert select(client, tid, mid, run, "save", "bad", candidate_id="forged").status_code == 404
    assert select(client, tid, mid, run, "build_proposal", "wrong-day", day_id=foreign_day).status_code == 404
    did = day(client, tid, mid)
    run = finished(client, tid, mid, start(client, tid, mid, key="after-day"))
    result = select(client, tid, mid, run, "build_proposal", "proposal", day_id=did)
    assert result.status_code == 200
    pid = result.json()["proposal"]["id"]
    response = client.post(f"/api/trips/{other_tid}/proposals/{pid}/confirm", params={"member_id": other_mid})
    assert response.status_code == 404


def test_sources_network_disabled_and_legacy_chat_untouched(client):
    tid, mid = trip(client)
    data = client.get(f"/api/trips/{tid}/research-sources", params={"member_id": mid}).json()
    assert data["consent_enabled"] is False
    assert any(s.get("id", s.get("source")) == "fake" for s in data["sources"])
    result = client.post(f"/api/trips/{tid}/ai/chat", params={"member_id": mid},
                         json={"message": "检查现有预算冲突"})
    assert result.status_code == 200 and result.json()["mode"] == "explain"
    with client.app.state.db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM research_runs")
        assert cur.fetchone()["n"] == 0


def test_expired_run_returns_gone_and_selection_state_survives_reload(client):
    tid, mid = trip(client)
    run = finished(client, tid, mid, start(client, tid, mid))
    assert select(client, tid, mid, run, "dismiss", "dismiss").status_code == 200
    response = client.get(f"/api/trips/{tid}/research/{run['id']}", params={"member_id": mid})
    assert response.json()["candidates"][0]["status"] == "dismissed"
    with client.app.state.db.cursor() as cur:
        cur.execute("UPDATE research_runs SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (run["id"],))
    response = client.get(f"/api/trips/{tid}/research/{run['id']}", params={"member_id": mid})
    assert response.status_code == 410


def test_inspiration_can_reference_existing_place_without_duplicating():
    db = Database(":memory:")
    service = TripService(db)
    t, m = service.create_trip(TripCreate(title="成都"))
    place = service.add_place(t.id, m.id, PlaceCreate(name="同分店", lng=104.05, lat=30.66))
    inspiration = service.add_inspiration(t.id, m.id, InspirationCreate(title="候选", place_id=place.id))
    assert inspiration.place_id == place.id and len(service.list_places(t.id)) == 1
    other, _ = service.create_trip(TripCreate(title="另一趟"))
    with pytest.raises(Forbidden):
        service.add_inspiration(other.id, other.created_by, InspirationCreate(title="错误", place_id=place.id))
    db.close()


def test_nested_database_transaction_rollback_and_thread_serialization():
    db = Database(":memory:")
    with db.cursor() as cur:
        cur.execute("CREATE TABLE transaction_test (n INTEGER)")
    with pytest.raises(RuntimeError):
        with db.cursor() as cur:
            cur.execute("INSERT INTO transaction_test VALUES(1)")
            with db.cursor() as nested:
                nested.execute("INSERT INTO transaction_test VALUES(2)")
            raise RuntimeError("rollback outer transaction")
    with db.cursor() as cur:
        assert cur.execute("SELECT COUNT(*) AS n FROM transaction_test").fetchone()["n"] == 0
    def write(n):
        with db.cursor() as cur:
            cur.execute("INSERT INTO transaction_test VALUES(?)", (n,))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(30)))
    with db.cursor() as cur:
        assert cur.execute("SELECT COUNT(*) AS n FROM transaction_test").fetchone()["n"] == 30
    db.close()
