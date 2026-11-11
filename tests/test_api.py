"""阶段 B HTTP 接口与页面测试（使用内存库，不联网）。"""

from __future__ import annotations

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


def _create(client: TestClient) -> dict:
    r = client.post(
        "/api/trips",
        json={"title": "成都三日游", "owner_name": "小林", "destination": "成都", "budget": 3000},
    )
    assert r.status_code == 200
    return r.json()


def test_health(client: TestClient):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_create_trip_api(client: TestClient):
    data = _create(client)
    assert data["trip"]["destination"] == "成都"
    assert data["invite_url"].startswith("/join/")
    assert data["owner"]["is_owner"] is True


def test_full_collab_flow(client: TestClient):
    data = _create(client)
    tid = data["trip"]["id"]
    owner = data["owner"]["id"]

    # 第二名成员通过 API 加入
    r = client.post(f"/api/trips/{tid}/members", json={"display_name": "阿May"})
    assert r.status_code == 200
    may = r.json()["id"]

    # owner 加硬约束
    r = client.post(
        f"/api/trips/{tid}/needs?member_id={owner}",
        json={"kind": "hard", "category": "出发日期", "value": "周日必须出发"},
    )
    assert r.status_code == 200

    # 成员阿May 加偏好与灵感
    r = client.post(
        f"/api/trips/{tid}/needs?member_id={may}",
        json={"kind": "preference", "category": "饮食", "value": "喜欢川菜"},
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/trips/{tid}/inspirations?member_id={may}",
        json={"title": "宽窄巷子", "place_name": "宽窄巷子", "source_url": "https://example.com/x"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending"

    # 全员可见一致状态
    assert len(client.get(f"/api/trips/{tid}/members").json()) == 2
    assert len(client.get(f"/api/trips/{tid}/needs").json()) == 2
    assert len(client.get(f"/api/trips/{tid}/inspirations").json()) == 1
    assert len(client.get(f"/api/trips/{tid}/places").json()) == 1


def test_unauthorized_need_rejected(client: TestClient):
    data = _create(client)
    tid = data["trip"]["id"]
    r = client.post(
        f"/api/trips/{tid}/needs?member_id=bogus",
        json={"kind": "hard", "category": "x", "value": "y"},
    )
    assert r.status_code == 403


def test_join_page_and_form(client: TestClient):
    data = _create(client)
    token = data["invite_url"].split("/")[-1]
    # 邀请页可直接打开
    r = client.get(f"/join/{token}")
    assert r.status_code == 200
    assert "成都三日游" in r.text
    # 表单加入 -> 303 重定向到旅程空间
    r = client.post(f"/join/{token}", data={"display_name": "阿May"}, follow_redirects=False)
    assert r.status_code == 303
    assert "/trip/" in r.headers["location"]


def test_invalid_join_page(client: TestClient):
    r = client.get("/join/nonexistent-token")
    assert r.status_code == 404


def test_ui_create_trip_redirects_to_trip_page(client: TestClient):
    r = client.post(
        "/ui/trips",
        data={"title": "成都三日游", "owner_name": "小林", "destination": "成都"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/trip/" in r.headers["location"]


def test_trip_page_renders(client: TestClient):
    data = _create(client)
    tid = data["trip"]["id"]
    owner = data["owner"]["id"]
    r = client.get(f"/trip/{tid}?member={owner}")
    assert r.status_code == 200
    assert "成员需求" in r.text
    assert "灵感箱" in r.text


def test_home_renders(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "创建一趟旅程" in r.text
