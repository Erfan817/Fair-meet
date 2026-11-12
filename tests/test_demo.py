"""Demo gate and quotas are deterministic and never call external services."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.ai.orchestrator import FakeLLM
from fairmeet.app import create_app
from fairmeet.db import Database
from fairmeet.demo import COOKIE, DemoSettings

SETTINGS = DemoSettings(enabled=True, access_code="sample-demo-code-123", session_secret="test-secret",
                        secure_cookie=False)


def make_client(settings=SETTINGS, database=None):
    return TestClient(create_app(database or Database(":memory:"), FakeGeoAdapter(), FakeLLM(), settings))


def login(client):
    response = client.post("/demo/login", data={"access_code": SETTINGS.access_code}, follow_redirects=False)
    assert response.status_code == 303
    return response


def trip(client):
    response = client.post("/api/trips", json={"title": "虚构旅程", "destination": "成都", "owner_name": "体验者"})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("path", ["/", "/join/example", "/trip/example", "/docs", "/openapi.json"])
def test_pages_require_gate(path):
    with make_client() as client:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/demo/login"


def test_health_and_login_assets_public_api_private():
    with make_client() as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/demo.css").status_code == 200
        assert client.get("/api/trips/anything").status_code == 401
        assert client.post("/api/trips", json={}).status_code == 401
        response = client.get("/demo/login")
        assert response.status_code == 200
        assert "体验码" in response.text
        assert SETTINGS.access_code not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_login_wrong_code_valid_cookie_and_logout():
    with make_client() as client:
        response = client.post("/demo/login", data={"access_code": "wrong"})
        assert response.status_code == 401
        assert "体验码不正确" in response.text
        response = login(client)
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert SETTINGS.access_code not in cookie
        assert client.get("/").status_code == 200
        trip(client)
        response = client.post("/demo/logout", follow_redirects=False)
        assert response.status_code == 303
        assert client.get("/api/trips/anything").status_code == 401


def test_tampered_expired_rotated_and_future_cookies(monkeypatch):
    from fairmeet import demo

    with make_client() as client:
        login(client)
        token = client.cookies.get(COOKIE)
        client.cookies.set(COOKIE, token + "x", domain="testserver.local", path="/")
        assert client.get("/api/sources").status_code == 401
        client.cookies.set(COOKIE, token, domain="testserver.local", path="/")
        now = demo.time.time()
        monkeypatch.setattr(demo.time, "time", lambda: now + 13 * 3600)
        assert client.get("/api/sources").status_code == 401
        monkeypatch.setattr(demo.time, "time", lambda: now - 60)
        assert client.get("/api/sources").status_code == 401
        monkeypatch.setattr(demo.time, "time", lambda: now)
        with make_client(replace(SETTINGS, access_code="rotated-code-456")) as rotated:
            rotated.cookies.set(COOKIE, token)
            assert rotated.get("/api/sources").status_code == 401


def test_secure_cookie_defaults_and_fail_closed_config():
    assert DemoSettings().secure_cookie
    with pytest.raises(ValueError, match="12"):
        DemoSettings(enabled=True, access_code="short")
    with pytest.raises(ValueError, match="正整数"):
        replace(SETTINGS, ai_daily_limit=0)
    with make_client(replace(SETTINGS, secure_cookie=True)) as client:
        assert "Secure" in login(client).headers["set-cookie"]


def test_cross_site_post_and_logout_rejected():
    with make_client() as client:
        login(client)
        for path in ["/api/trips", "/demo/logout", "/demo/login"]:
            assert client.post(path, headers={"origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/trips", headers={"sec-fetch-site": "cross-site"}).status_code == 403
        response = client.post("/api/trips", headers={"origin": "http://testserver"},
                               json={"title": "旅程", "destination": "成都", "owner_name": "体验者"})
        assert response.status_code == 200


def test_body_stream_limit_without_content_length():
    with make_client(replace(SETTINGS, max_body_bytes=128)) as client:
        login(client)
        response = client.post("/api/trips", content=iter([b"x" * 80, b"x" * 80]))
        assert response.status_code == 413


def test_writes_throttled_and_reset(monkeypatch):
    from fairmeet import demo

    with make_client(replace(SETTINGS, writes_per_minute=2)) as client:
        login(client)
        trip(client)
        trip(client)
        response = client.post("/api/trips", json={})
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
        now = demo.time.monotonic()
        monkeypatch.setattr(demo.time, "monotonic", lambda: now + 61)
        trip(client)


def test_login_attempts_have_global_limit():
    with make_client() as client:
        for _ in range(10):
            assert client.post("/demo/login", data={"access_code": "no"}).status_code == 401
        assert client.post("/demo/login", data={"access_code": "no"}).status_code == 429


def chat(client, data):
    return client.post(f"/api/trips/{data['trip']['id']}/ai/chat?member_id={data['owner']['id']}",
                       json={"message": "检查预算"})


def test_ai_per_session_limit():
    with make_client(replace(SETTINGS, ai_per_minute=1)) as client:
        login(client)
        data = trip(client)
        assert chat(client, data).status_code == 200
        assert chat(client, data).status_code == 429
        # AI throttling does not prevent normal editing.
        trip(client)


def test_daily_ai_quota_shared_persisted_and_resets(tmp_path, monkeypatch):
    from fairmeet import demo

    path = str(tmp_path / "demo.db")
    settings = replace(SETTINGS, ai_daily_limit=1)
    database = Database(path)
    with make_client(settings, database) as client:
        login(client)
        data = trip(client)
        assert chat(client, data).status_code == 200
        assert chat(client, data).status_code == 429
    database.close()
    reopened = Database(path)
    with make_client(settings, reopened) as client:
        login(client)
        assert chat(client, data).status_code == 429

        class Tomorrow:
            @staticmethod
            def now(tz):
                from datetime import datetime, timedelta
                return datetime.now(tz) + timedelta(days=1)

        monkeypatch.setattr(demo, "datetime", Tomorrow)
        assert chat(client, data).status_code == 200
    reopened.close()


def test_local_mode_unchanged_and_message_length_limited():
    with make_client(DemoSettings()) as client:
        assert client.get("/").status_code == 200
        data = trip(client)
        response = client.post(f"/api/trips/{data['trip']['id']}/ai/chat?member_id={data['owner']['id']}",
                               json={"message": "字" * 4001})
        assert response.status_code == 400
