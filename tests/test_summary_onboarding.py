"""阶段 E：摘要页 / 途中页 / 历史页 / replan & revisions API 集成测试（离线）。"""

from __future__ import annotations

import warnings

import pytest

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from fairmeet.app import create_app  # noqa: E402
from fairmeet.db import Database  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(Database(":memory:")))


def _seed(client: TestClient):
    d = client.post("/api/trips", json={"title": "成都三日游", "owner_name": "小林", "budget": 2000}).json()
    tid = d["trip"]["id"]
    owner = d["owner"]["id"]
    day = client.post(f"/api/trips/{tid}/days?member_id={owner}", json={"day_index": 1, "date": "2026-11-20"}).json()
    did = day["id"]
    i1 = client.post(
        f"/api/trips/{tid}/days/{did}/items?member_id={owner}",
        json={"title": "A", "cost": 100, "cost_provenance_kind": "estimated"},
    ).json()
    i2 = client.post(
        f"/api/trips/{tid}/days/{did}/items?member_id={owner}",
        json={"title": "B", "cost": 50, "cost_provenance_kind": "estimated"},
    ).json()
    return tid, owner, did, i1, i2


def test_summary_api_and_page(client: TestClient):
    tid, owner, did, i1, i2 = _seed(client)
    r = client.get(f"/api/trips/{tid}/summary")
    assert r.status_code == 200
    data = r.json()
    assert data["days"][0]["items"]
    assert data["budget"]["estimated_total"] == 150.0
    assert client.get(f"/trip/{tid}/summary").status_code == 200


def test_ontrip_and_history_pages(client: TestClient):
    tid, owner, did, i1, i2 = _seed(client)
    assert client.get(f"/trip/{tid}/ontrip?member={owner}").status_code == 200
    assert client.get(f"/trip/{tid}/history").status_code == 200


def test_replan_api_records_history_and_revision(client: TestClient):
    tid, owner, did, i1, i2 = _seed(client)
    r = client.post(
        f"/api/trips/{tid}/replan?member_id={owner}",
        json={"day_id": did, "new_order": [i2["id"], i1["id"]], "reason": "换序"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["day_id"] == did

    # 历史里能看到 replan 记录
    hist = client.get(f"/api/trips/{tid}/history").json()
    assert any(e["change_type"] == "replan" for e in hist)
    # 重排后保存了版本快照
    revs = client.get(f"/api/trips/{tid}/revisions").json()
    assert len(revs) >= 1


def test_updates_feed(client: TestClient):
    tid, owner, did, i1, i2 = _seed(client)
    client.post(f"/api/trips/{tid}/replan?member_id={owner}", json={"day_id": did, "reason": "x"})
    feed = client.get(f"/api/trips/{tid}/updates").json()
    assert feed["count"] >= 1


def test_manual_snapshot(client: TestClient):
    tid, owner, did, i1, i2 = _seed(client)
    r = client.post(f"/api/trips/{tid}/revisions?member_id={owner}&label=手动")
    assert r.status_code == 200
    assert r.json()["label"] == "手动"


def test_replan_unauthorized(client: TestClient):
    tid, owner, did, i1, i2 = _seed(client)
    r = client.post(f"/api/trips/{tid}/replan?member_id=bogus", json={"day_id": did})
    assert r.status_code == 403
