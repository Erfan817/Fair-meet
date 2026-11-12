"""阶段 F：agent 委托、只读上下文、受控写入与审计。"""

from __future__ import annotations

import json
import warnings

import pytest

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from fairmeet.app import create_app  # noqa: E402
from fairmeet.db import Database  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    app = create_app(Database(":memory:"))
    return TestClient(app)


def _setup(client: TestClient) -> tuple[str, str, str]:
    data = client.post(
        "/api/trips",
        json={"title": "成都三日游", "owner_name": "小林", "destination": "成都", "budget": 3000},
    ).json()
    trip_id = data["trip"]["id"]
    owner = data["owner"]["id"]
    may = client.post(f"/api/trips/{trip_id}/members", json={"display_name": "阿May"}).json()["id"]
    return trip_id, owner, may


def _delegate(
    client: TestClient,
    trip_id: str,
    member_id: str,
    *,
    agent_name: str = "Test Agent",
    scopes: list[str] | None = None,
    hours: int = 168,
) -> dict:
    body: dict = {"agent_name": agent_name, "expires_in_hours": hours}
    if scopes is not None:
        body["scopes"] = scopes
    response = client.post(
        f"/api/trips/{trip_id}/agent-delegations?member_id={member_id}", json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _err(response) -> dict:
    return response.json()["error"]


def test_create_delegation_returns_token_once(client: TestClient):
    trip_id, owner, _ = _setup(client)
    data = _delegate(client, trip_id, owner)

    assert data["token"].startswith("fm_agent_")
    assert data["delegation"]["agent_name"] == "Test Agent"
    assert set(data["delegation"]["scopes"]) == {
        "trip:read",
        "checks:read",
        "need:submit",
        "inspiration:submit",
        "proposal:submit",
        "proposal:read",
    }
    assert "token" not in data["delegation"]

    listed = client.get(f"/api/trips/{trip_id}/agent-delegations?member_id={owner}").json()
    assert len(listed) == 1
    assert "token_hash" not in json.dumps(listed)

    with client.app.state.db.cursor() as cur:
        cur.execute("SELECT token_hash FROM agent_delegations WHERE id=?", (listed[0]["id"],))
        stored = cur.fetchone()["token_hash"]
    assert stored != data["token"]
    assert len(stored) == 64


def test_agent_reads_context_without_invite_token(client: TestClient):
    trip_id, owner, may = _setup(client)
    token = _delegate(client, trip_id, may)["token"]

    response = client.get("/api/agent/trip", headers=_auth(token))
    assert response.status_code == 200
    context = response.json()

    assert context["trip"]["id"] == trip_id
    assert context["member"]["display_name"] == "阿May"
    assert "itinerary" in context and "checks" in context and "budget" in context
    assert "invite_token" not in json.dumps(context)


def test_other_hard_constraints_are_counted_not_exposed(client: TestClient):
    trip_id, owner, may = _setup(client)
    secret = "不吃辣的小林"
    client.post(
        f"/api/trips/{trip_id}/needs?member_id={owner}",
        json={"kind": "hard", "category": "饮食", "value": secret},
    )
    client.post(
        f"/api/trips/{trip_id}/needs?member_id={owner}",
        json={"kind": "preference", "category": "兴趣", "value": "想看熊猫"},
    )
    token = _delegate(client, trip_id, may)["token"]

    context = client.get("/api/agent/trip", headers=_auth(token)).json()
    dumped = json.dumps(context, ensure_ascii=False)

    assert context["needs"]["other_hard_constraint_count"] == 1
    assert secret not in dumped
    assert "想看熊猫" in dumped  # 他人的偏好可见
    # 校验结果中涉及他人硬约束的条目被改写为「仅提示冲突存在」
    hard_checks = [c for c in context["checks"]["conflicts"] if c["code"].startswith("hard_")]
    assert hard_checks
    for conflict in hard_checks:
        assert secret not in conflict["message"]
        assert conflict["ref"] is None


def test_agent_submits_preference_need_with_source(client: TestClient):
    trip_id, _, may = _setup(client)
    token = _delegate(client, trip_id, may, agent_name="May Bot")["token"]

    response = client.post(
        "/api/agent/needs",
        headers=_auth(token),
        json={"kind": "preference", "category": "兴趣", "value": "想去博物馆"},
    )
    assert response.status_code == 200, response.text
    need = response.json()
    assert need["source"] == "agent:May Bot"

    needs = client.get(f"/api/trips/{trip_id}/needs").json()
    assert any(n["id"] == need["id"] and n["member_id"] == may for n in needs)


def test_agent_cannot_declare_hard_constraint(client: TestClient):
    trip_id, _, may = _setup(client)
    token = _delegate(client, trip_id, may)["token"]

    response = client.post(
        "/api/agent/needs",
        headers=_auth(token),
        json={"kind": "hard", "category": "日期", "value": "周日必须返程"},
    )
    assert response.status_code == 403
    error = _err(response)
    assert error["code"] == "hard_constraint_requires_member"
    assert error["details"]["suggested_kind"] == "preference"

    assert client.get(f"/api/trips/{trip_id}/needs").json() == []
    audit = client.get(f"/api/trips/{trip_id}/agent-audit?member_id={may}").json()
    assert audit[0]["action"] == "need.submit"
    assert audit[0]["outcome"] == "denied"


def test_agent_inspiration_requires_source(client: TestClient):
    trip_id, _, may = _setup(client)
    token = _delegate(client, trip_id, may)["token"]

    rejected = client.post(
        "/api/agent/inspirations", headers=_auth(token), json={"title": "某家火锅店"}
    )
    assert rejected.status_code == 422
    assert _err(rejected)["code"] == "source_required"

    accepted = client.post(
        "/api/agent/inspirations",
        headers=_auth(token),
        json={"title": "某家火锅店", "source_url": "https://example.com/poi/1"},
    )
    assert accepted.status_code == 200, accepted.text
    insp = accepted.json()
    assert insp["status"] == "pending"
    assert insp["source"] == "agent:Test Agent"


def test_revoked_token_is_rejected(client: TestClient):
    trip_id, owner, _ = _setup(client)
    data = _delegate(client, trip_id, owner)
    delegation_id = data["delegation"]["id"]

    response = client.delete(
        f"/api/trips/{trip_id}/agent-delegations/{delegation_id}?member_id={owner}"
    )
    assert response.status_code == 200
    assert response.json()["revoked_at"] is not None

    rejected = client.get("/api/agent/trip", headers=_auth(data["token"]))
    assert rejected.status_code == 401
    assert _err(rejected)["code"] == "token_revoked"


def test_expired_token_is_rejected(client: TestClient):
    trip_id, owner, _ = _setup(client)
    data = _delegate(client, trip_id, owner)
    with client.app.state.db.cursor() as cur:
        cur.execute(
            "UPDATE agent_delegations SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (data["delegation"]["id"],),
        )

    rejected = client.get("/api/agent/trip", headers=_auth(data["token"]))
    assert rejected.status_code == 401
    assert _err(rejected)["code"] == "token_expired"


def test_missing_and_invalid_tokens(client: TestClient):
    missing = client.get("/api/agent/trip")
    assert missing.status_code == 401
    assert _err(missing)["code"] == "missing_token"

    invalid = client.get("/api/agent/trip", headers=_auth("fm_agent_not-a-real-token"))
    assert invalid.status_code == 401
    assert _err(invalid)["code"] == "invalid_token"


def test_scope_is_enforced(client: TestClient):
    trip_id, _, may = _setup(client)
    token = _delegate(client, trip_id, may, scopes=["trip:read"])["token"]

    read_ok = client.get("/api/agent/trip", headers=_auth(token))
    assert read_ok.status_code == 200

    checks_denied = client.get("/api/agent/checks", headers=_auth(token))
    assert checks_denied.status_code == 403
    assert _err(checks_denied)["code"] == "insufficient_scope"

    write_denied = client.post(
        "/api/agent/needs",
        headers=_auth(token),
        json={"kind": "preference", "category": "兴趣", "value": "想看熊猫"},
    )
    assert write_denied.status_code == 403


def test_unknown_scope_is_rejected(client: TestClient):
    trip_id, owner, _ = _setup(client)
    response = client.post(
        f"/api/trips/{trip_id}/agent-delegations?member_id={owner}",
        json={"agent_name": "Bad", "scopes": ["admin:all"]},
    )
    assert response.status_code == 422
    assert _err(response)["code"] == "invalid_scope"


def test_agent_cannot_confirm_proposals(client: TestClient):
    trip_id, _, may = _setup(client)
    token = _delegate(client, trip_id, may)["token"]

    response = client.post("/api/agent/proposals/whatever/confirm", headers=_auth(token))
    assert response.status_code == 403
    assert _err(response)["code"] == "confirmation_requires_member"

    audit = client.get(f"/api/trips/{trip_id}/agent-audit?member_id={may}").json()
    assert audit[0]["action"] == "proposal.confirm_denied"


def test_revocation_permissions(client: TestClient):
    trip_id, owner, may = _setup(client)
    owner_delegation = _delegate(client, trip_id, owner)

    denied = client.delete(
        f"/api/trips/{trip_id}/agent-delegations/{owner_delegation['delegation']['id']}"
        f"?member_id={may}"
    )
    assert denied.status_code == 403
    assert _err(denied)["code"] == "forbidden"

    allowed = client.delete(
        f"/api/trips/{trip_id}/agent-delegations/{owner_delegation['delegation']['id']}"
        f"?member_id={owner}"
    )
    assert allowed.status_code == 200


def test_delegation_requires_current_membership(client: TestClient):
    trip_id, _, _ = _setup(client)
    response = client.post(
        f"/api/trips/{trip_id}/agent-delegations?member_id=not-a-member",
        json={"agent_name": "Stranger"},
    )
    assert response.status_code == 403
    assert _err(response)["code"] == "forbidden"


def test_checks_endpoint_returns_redacted_view(client: TestClient):
    trip_id, owner, may = _setup(client)
    secret = "行动不便需要无障碍"
    client.post(
        f"/api/trips/{trip_id}/needs?member_id={owner}",
        json={"kind": "hard", "category": "无障碍", "value": secret},
    )
    token = _delegate(client, trip_id, may)["token"]

    response = client.get("/api/agent/checks", headers=_auth(token))
    assert response.status_code == 200
    payload = response.json()
    assert "check" in payload and "budget" in payload
    assert secret not in json.dumps(payload, ensure_ascii=False)
