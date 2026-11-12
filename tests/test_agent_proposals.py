"""阶段 G：agent 提案、expected_revision 并发控制与限定范围自动确认。"""

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


def _setup(
    client: TestClient,
    *,
    budget: float | None = 3000,
    auto_confirm: bool = False,
    max_cost: float | None = None,
    scopes: list[str] | None = None,
) -> dict:
    data = client.post(
        "/api/trips",
        json={"title": "G 阶段旅程", "owner_name": "小林", "destination": "成都", "budget": budget},
    ).json()
    trip_id = data["trip"]["id"]
    owner = data["owner"]["id"]
    may = client.post(f"/api/trips/{trip_id}/members", json={"display_name": "阿May"}).json()["id"]
    body: dict = {"agent_name": "Test Agent", "expires_in_hours": 168}
    if auto_confirm:
        body["auto_confirm"] = True
        body["auto_confirm_max_cost"] = max_cost
    if scopes is not None:
        body["scopes"] = scopes
    delegation = client.post(
        f"/api/trips/{trip_id}/agent-delegations?member_id={may}", json=body
    ).json()
    return {
        "trip_id": trip_id,
        "owner": owner,
        "may": may,
        "token": delegation["token"],
        "delegation": delegation["delegation"],
    }


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _add_day(client: TestClient, trip_id: str, member_id: str, index: int = 1) -> dict:
    response = client.post(
        f"/api/trips/{trip_id}/days?member_id={member_id}", json={"day_index": index}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _revision(client: TestClient, token: str) -> str:
    return client.get("/api/agent/trip", headers=_auth(token)).json()["revision"]


def _propose(client: TestClient, token: str, revision: str, operations: list[dict], summary="提案"):
    return client.post(
        "/api/agent/proposals",
        headers=_auth(token),
        json={"summary": summary, "expected_revision": revision, "operations": operations},
    )


def _err(response) -> dict:
    return response.json()["error"]


def test_agent_proposal_is_pending_with_origin_and_audit(client: TestClient):
    env = _setup(client)
    day = _add_day(client, env["trip_id"], env["may"])
    revision = _revision(client, env["token"])

    response = _propose(
        client,
        env["token"],
        revision,
        [{"op": "add_item", "day_id": day["id"], "title": "熊猫基地", "cost": 100}],
        summary="加入熊猫基地",
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["auto_confirmed"] is False
    assert payload["auto_confirm_skipped_reason"] == "auto_confirm_disabled"
    proposal = payload["proposal"]
    assert proposal["status"] == "pending"
    assert proposal["origin"] == "agent:Test Agent"
    assert proposal["delegation_id"] == env["delegation"]["id"]
    assert proposal["impacts"][0]["impact_notes"]

    # 行程没有被修改
    assert client.get(f"/api/trips/{env['trip_id']}/items").json() == []

    listed = client.get(
        "/api/agent/proposals?status=pending", headers=_auth(env["token"])
    ).json()
    assert [p["id"] for p in listed] == [proposal["id"]]

    audit = client.get(
        f"/api/trips/{env['trip_id']}/agent-audit?member_id={env['may']}"
    ).json()
    actions = {(entry["action"], entry["outcome"]) for entry in audit}
    assert ("proposal.submit", "applied") in actions
    assert ("proposal.auto_confirm_skipped", "denied") in actions


def test_expected_revision_conflict(client: TestClient):
    env = _setup(client)
    day = _add_day(client, env["trip_id"], env["may"])
    stale_revision = _revision(client, env["token"])

    # 另一名成员在提案前修改了行程
    client.post(
        f"/api/trips/{env['trip_id']}/days/{day['id']}/items?member_id={env['owner']}",
        json={"title": "宽窄巷子", "cost": 0},
    )

    response = _propose(
        client,
        env["token"],
        stale_revision,
        [{"op": "add_day", "day_index": 2, "title": "第二天"}],
    )
    assert response.status_code == 409
    error = _err(response)
    assert error["code"] == "revision_conflict"
    assert error["details"]["expected"] == stale_revision
    assert error["details"]["current"] != stale_revision

    audit = client.get(
        f"/api/trips/{env['trip_id']}/agent-audit?member_id={env['may']}"
    ).json()
    assert audit[0]["action"] == "proposal.submit"
    assert audit[0]["outcome"] == "denied"


def test_proposal_rejects_hard_need_and_sourceless_inspiration(client: TestClient):
    env = _setup(client)
    revision = _revision(client, env["token"])

    hard = _propose(
        client, env["token"], revision, [{"op": "add_need", "kind": "hard", "category": "日期", "value": "周日"}]
    )
    assert hard.status_code == 403
    assert _err(hard)["code"] == "hard_constraint_requires_member"

    no_source = _propose(
        client, env["token"], revision, [{"op": "add_inspiration", "title": "某火锅店"}]
    )
    assert no_source.status_code == 422
    assert _err(no_source)["code"] == "source_required"

    invalid = _propose(client, env["token"], revision, [{"op": "drop_database"}])
    assert invalid.status_code == 422
    assert _err(invalid)["code"] == "invalid_operation"


def test_auto_confirm_requires_cost_limit(client: TestClient):
    data = client.post(
        "/api/trips", json={"title": "无上限", "owner_name": "小林", "destination": "成都"}
    ).json()
    response = client.post(
        f"/api/trips/{data['trip']['id']}/agent-delegations?member_id={data['owner']['id']}",
        json={"agent_name": "Auto", "auto_confirm": True},
    )
    assert response.status_code == 422
    assert _err(response)["code"] == "invalid_auto_confirm_policy"


def test_auto_confirm_applies_within_policy(client: TestClient):
    env = _setup(client, auto_confirm=True, max_cost=200)
    day = _add_day(client, env["trip_id"], env["may"])
    revision = _revision(client, env["token"])

    response = _propose(
        client,
        env["token"],
        revision,
        [{"op": "add_item", "day_id": day["id"], "title": "熊猫基地", "cost": 100}],
        summary="自动确认的追加",
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["auto_confirmed"] is True
    assert payload["proposal"]["status"] == "confirmed"

    items = client.get(f"/api/trips/{env['trip_id']}/items").json()
    assert [item["title"] for item in items] == ["熊猫基地"]

    audit = client.get(
        f"/api/trips/{env['trip_id']}/agent-audit?member_id={env['may']}"
    ).json()
    actions = {(entry["action"], entry["outcome"]) for entry in audit}
    assert ("proposal.auto_confirmed", "applied") in actions

    history = client.get(f"/api/trips/{env['trip_id']}/history").json()
    assert any("自动确认 agent 提案" in entry["summary"] for entry in history)


def test_auto_confirm_over_cost_stays_pending(client: TestClient):
    env = _setup(client, auto_confirm=True, max_cost=50)
    day = _add_day(client, env["trip_id"], env["may"])
    revision = _revision(client, env["token"])

    payload = _propose(
        client,
        env["token"],
        revision,
        [{"op": "add_item", "day_id": day["id"], "title": "贵餐厅", "cost": 300}],
    ).json()
    assert payload["auto_confirmed"] is False
    assert payload["auto_confirm_skipped_reason"] == "cost_over_limit"
    assert payload["proposal"]["status"] == "pending"
    assert client.get(f"/api/trips/{env['trip_id']}/items").json() == []


def test_auto_confirm_over_budget_stays_pending(client: TestClient):
    env = _setup(client, budget=100, auto_confirm=True, max_cost=500)
    day = _add_day(client, env["trip_id"], env["may"])
    revision = _revision(client, env["token"])

    payload = _propose(
        client,
        env["token"],
        revision,
        [{"op": "add_item", "day_id": day["id"], "title": "超预算", "cost": 200}],
    ).json()
    assert payload["auto_confirmed"] is False
    assert payload["auto_confirm_skipped_reason"] == "budget_over"


def test_auto_confirm_refuses_modification_operations(client: TestClient):
    env = _setup(client, auto_confirm=True, max_cost=500)
    day = _add_day(client, env["trip_id"], env["may"])
    item = client.post(
        f"/api/trips/{env['trip_id']}/days/{day['id']}/items?member_id={env['may']}",
        json={"title": "原始条目", "cost": 10},
    ).json()
    revision = _revision(client, env["token"])

    payload = _propose(
        client,
        env["token"],
        revision,
        [{"op": "update_item", "item_id": item["id"], "title": "改名"}],
    ).json()
    assert payload["auto_confirmed"] is False
    assert payload["auto_confirm_skipped_reason"] == "operation_not_append_only"
    assert payload["proposal"]["status"] == "pending"


def test_auto_confirm_blocked_by_any_hard_constraint(client: TestClient):
    env = _setup(client, auto_confirm=True, max_cost=500)
    day = _add_day(client, env["trip_id"], env["may"])
    client.post(
        f"/api/trips/{env['trip_id']}/needs?member_id={env['owner']}",
        json={"kind": "hard", "category": "饮食", "value": "不吃辣"},
    )
    revision = _revision(client, env["token"])

    payload = _propose(
        client,
        env["token"],
        revision,
        [{"op": "add_item", "day_id": day["id"], "title": "火锅", "cost": 50}],
    ).json()
    assert payload["auto_confirmed"] is False
    assert payload["auto_confirm_skipped_reason"] == "hard_constraint_present"


def test_proposal_scope_is_enforced(client: TestClient):
    env = _setup(client, scopes=["trip:read"])
    revision = _revision(client, env["token"])

    response = _propose(client, env["token"], revision, [{"op": "add_day", "day_index": 1}])
    assert response.status_code == 403
    assert _err(response)["code"] == "insufficient_scope"


def test_agent_confirm_denied_keeps_proposal_pending(client: TestClient):
    env = _setup(client)
    day = _add_day(client, env["trip_id"], env["may"])
    revision = _revision(client, env["token"])
    proposal = _propose(
        client,
        env["token"],
        revision,
        [{"op": "add_item", "day_id": day["id"], "title": "待确认条目"}],
    ).json()["proposal"]

    denied = client.post(
        f"/api/agent/proposals/{proposal['id']}/confirm", headers=_auth(env["token"])
    )
    assert denied.status_code == 403
    assert _err(denied)["code"] == "confirmation_requires_member"

    still_pending = client.get(
        "/api/agent/proposals?status=pending", headers=_auth(env["token"])
    ).json()
    assert any(p["id"] == proposal["id"] for p in still_pending)

    # 成员本人可以在网页/API 确认（现有确认链路）
    confirmed = client.post(
        f"/api/trips/{env['trip_id']}/proposals/{proposal['id']}/confirm?member_id={env['may']}"
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["proposal"]["status"] == "confirmed"


def test_agent_reads_proposal_detail(client: TestClient):
    env = _setup(client)
    revision = _revision(client, env["token"])
    proposal = _propose(
        client, env["token"], revision, [{"op": "add_day", "day_index": 1}]
    ).json()["proposal"]

    detail = client.get(f"/api/agent/proposals/{proposal['id']}", headers=_auth(env["token"]))
    assert detail.status_code == 200
    assert detail.json()["id"] == proposal["id"]

    missing = client.get("/api/agent/proposals/nope", headers=_auth(env["token"]))
    assert missing.status_code == 404
    assert _err(missing)["code"] == "not_found"
