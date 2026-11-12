"""研究核心全离线单测；不访问真实搜索、模型或地理服务。"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from fairmeet.content import (
    FakeContentProvider,
    ResearchError,
    ResearchRequest,
    ResearchService,
)
from fairmeet.content.service import MAX_ACTIVE_RUNS
from fairmeet.db import Database
from fairmeet.domain import DayCreate, ItemCreate, MemberJoin, NeedCreate, NeedKind, PlaceCreate, TripCreate
from fairmeet.services import FairMeetError, Forbidden, NotFound, TripService

TERMINAL = {"succeeded", "partial", "failed", "cancelled", "interrupted"}


def request(key="request-1", **kwargs):
    return ResearchRequest(message="合成测试：想看看玩法", idempotency_key=key, **kwargs)


def wait_run(service, trip_id, member_id, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = service.get(trip_id, member_id, run_id)
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.005)
    pytest.fail("离线 worker 未在限时内结束")


@pytest.fixture()
def context(db, trips, itin):
    trip, owner = trips.create_trip(TripCreate(title="合成成都测试", destination="成都"))
    service = ResearchService(db, trips, itin)
    yield service, trip, owner
    service.close()


class BlockedFake(FakeContentProvider):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def search(self, destination, scope=""):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("离线测试未释放")
        return super().search(destination, scope)


class OfflineBrave:
    source = "brave"
    is_simulated = False
    daily_limit = 2

    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    def available(self):
        return True

    def search(self, destination, scope=""):
        self.calls.append((destination, scope))
        if self.error:
            raise self.error
        from fairmeet.content import ContentHit
        return [ContentHit(
            title="合成资料标题不是商家", url="https://example.org/info?token=private#secret", excerpt="合成摘要",
        )]


def test_request_contract():
    first, second = request(), request()
    assert first.intent == "discover" and first.scope == ""
    assert first.enabled_sources == ["fake"] and first.network_consent is False
    assert first.consent_version == "v1"
    assert request(enabled_sources=["public_web"]).enabled_sources == ["public_web"]
    first.enabled_sources.append("brave")
    assert second.enabled_sources == ["fake"]
    assert isinstance(ResearchError("固定错误"), FairMeetError)
    invalid = [
        {"message": "x" * 4001}, {"scope": "x" * 201}, {"intent": "execute"},
        {"idempotency_key": ""}, {"idempotency_key": "x" * 81}, {"extra": True},
        {"enabled_sources": ["xhs"]}, {"enabled_sources": ["fake", "brave", "fake"]},
        {"enabled_sources": ["fake", "fake"]}, {"network_consent": "true"},
    ]
    for patch in invalid:
        with pytest.raises(ValidationError):
            ResearchRequest(**({"message": "", "idempotency_key": "key"} | patch))


def test_offline_fake_complete_no_domain_writes(context, db, trips, itin):
    service, trip, owner = context
    before = service.fingerprint(trip.id)
    run = service.create(trip.id, owner.id, request())
    assert run["status"] == "queued"
    got = wait_run(service, trip.id, owner.id, run["id"])
    required = {"id", "trip_id", "member_id", "status", "stage", "is_simulated", "reply", "candidates",
                "documents", "errors", "usage", "input_fingerprint", "created_at", "expires_at"}
    assert required <= got.keys()
    assert got["status"] == "succeeded" and got["is_simulated"]
    assert got["enabled_sources"] == ["fake"]
    assert "虚构" in got["reply"] and "未生成 AI 行程" in got["reply"]
    assert len(got["candidates"]) == len(got["documents"]) == 3
    evidence = {doc["id"] for doc in got["documents"]}
    for candidate in got["candidates"]:
        assert set(candidate["evidence_ids"]) <= evidence
        assert candidate["status"] == "pending" and candidate["match_status"] == "simulated"
        assert all(candidate[key] is None for key in ("place_id", "lat", "lng", "cost"))
        assert candidate["source_url"].startswith("https://example.com/")
    for doc in got["documents"]:
        assert doc["kind"] == "unknown" and doc["is_simulated"]
        assert len(doc["excerpt"]) <= 1000 and "虚构" in doc["title"]
    assert got["usage"]["searches"] == 1 and got["usage"]["reads"] == 0
    assert service.fingerprint(trip.id) == before
    assert trips.list_places(trip.id) == trips.list_inspirations(trip.id) == itin.list_days(trip.id) == []
    with db.cursor() as cur:
        assert cur.execute("SELECT COUNT(*) FROM change_proposals").fetchone()[0] == 0
    lifetime = datetime.fromisoformat(got["expires_at"]) - datetime.fromisoformat(got["created_at"])
    assert lifetime == timedelta(days=7)


@pytest.mark.parametrize("destination", ["北京", "成都附近的北京", "重庆", "成都/北京"])
def test_fake_never_moves_chengdu_examples(db, trips, itin, destination):
    trip, owner = trips.create_trip(TripCreate(title="合成测试", destination=destination))
    service = ResearchService(db, trips, itin)
    try:
        run = service.create(trip.id, owner.id, request())
        got = wait_run(service, trip.id, owner.id, run["id"])
        assert got["candidates"] == got["documents"] == []
        assert "没有对应离线样例" in got["reply"]
    finally:
        service.close()


def test_destination_required(context, db):
    service, trip, owner = context
    with db.cursor() as cur:
        cur.execute("UPDATE trips SET destination=' ' WHERE id=?", (trip.id,))
    with pytest.raises(ResearchError) as error:
        service.create(trip.id, owner.id, request())
    assert error.value.code == "missing_destination"
    with db.cursor() as cur:
        assert cur.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0


def test_idempotency_request_body_not_stored(context, db):
    service, trip, owner = context
    payload = ResearchRequest(
        message="测试敏感正文不入库", idempotency_key="same-key",
    )
    first = service.create(trip.id, owner.id, payload)
    wait_run(service, trip.id, owner.id, first["id"])
    assert service.create(trip.id, owner.id, payload)["id"] == first["id"]
    with pytest.raises(ResearchError) as error:
        service.create(trip.id, owner.id, payload.model_copy(update={"message": "另一正文"}))
    assert error.value.code == "idempotency_conflict"
    with db.cursor() as cur:
        rows = cur.execute("SELECT * FROM research_runs").fetchall()
        assert len(rows) == 1
        assert "测试敏感正文" not in json.dumps([dict(row) for row in rows], ensure_ascii=False)


def test_authorization_and_joined(context, db, trips):
    service, trip, owner = context
    run = service.create(trip.id, owner.id, request())
    wait_run(service, trip.id, owner.id, run["id"])
    other = trips.add_member(trip.id, MemberJoin(display_name="合成其他成员"))
    alien_trip, alien = trips.create_trip(TripCreate(title="其他合成旅程"))
    for method in (service.get, service.cancel):
        with pytest.raises(Forbidden):
            method(trip.id, other.id, run["id"])
        with pytest.raises(Forbidden):
            method(trip.id, alien.id, run["id"])
        with pytest.raises(NotFound):
            method(alien_trip.id, alien.id, run["id"])
    for status in ("invited", "declined"):
        with db.cursor() as cur:
            cur.execute("UPDATE members SET status=? WHERE id=?", (status, owner.id))
        for method, args in (
            (service.sources, (trip.id, owner.id)),
            (service.consent_enabled, (trip.id, owner.id)),
            (service.set_consent, (trip.id, owner.id, True)),
            (service.get, (trip.id, owner.id, run["id"])),
            (service.cancel, (trip.id, owner.id, run["id"])),
            (service.create, (trip.id, owner.id, request("no-access"))),
        ):
            with pytest.raises(Forbidden):
                method(*args)


@pytest.mark.parametrize("source", ["brave", "public_web"])
def test_network_requires_both_consents_per_member(context, trips, itin, db, source):
    old, trip, owner = context
    old.close()
    provider = OfflineBrave()
    provider.source = source
    service = ResearchService(db, trips, itin, provider)
    other = trips.add_member(trip.id, MemberJoin(display_name="合成同行人"))
    try:
        payload = request(enabled_sources=[source], network_consent=True)
        with pytest.raises(ResearchError, match="授权"):
            service.create(trip.id, owner.id, payload)
        assert not provider.calls
        service.set_consent(trip.id, owner.id, True)
        assert service.consent_enabled(trip.id, owner.id)
        assert not service.consent_enabled(trip.id, other.id)
        with pytest.raises(ResearchError, match="授权"):
            service.create(trip.id, owner.id, payload.model_copy(update={"network_consent": False}))
        with pytest.raises(ResearchError, match="授权"):
            service.create(trip.id, other.id, payload)
        run = service.create(trip.id, owner.id, payload)
        got = wait_run(service, trip.id, owner.id, run["id"])
        assert got["status"] == "succeeded" and not got["is_simulated"]
        assert got["enabled_sources"] == [source] and got["usage"]["max_searches"] == 1
        assert provider.calls == [("成都", "")]
        candidate = got["candidates"][0]
        assert candidate["match_status"] == "unresolved" and candidate["lat"] is None
        assert candidate["cost"] is None and candidate["source_url"] == "https://example.org/info"
        assert got["documents"][0]["kind"] == "external"
        assert got["documents"][0]["content_scope"] == "snippet"
        assert "未读取正文" in got["reply"]
    finally:
        service.close()


def test_polling_never_repeats_network_search(context, db, trips, itin):
    old, trip, owner = context
    old.close()
    provider = OfflineBrave()
    service = ResearchService(db, trips, itin, provider)
    try:
        service.set_consent(trip.id, owner.id, True)
        req = ResearchRequest(
            message="合成隐私名字和过敏需求，想吃餐厅美食", scope="武侯区",
            intent="plan", enabled_sources=["brave"], network_consent=True, idempotency_key="privacy",
        )
        run = service.create(trip.id, owner.id, req)
        got = wait_run(service, trip.id, owner.id, run["id"])
        assert got["topic"] == "dining" and got["intent"] == "plan"
        assert got["candidates"][0]["category"] == "meal"
        for _ in range(10):
            assert service.get(trip.id, owner.id, run["id"])["status"] == "succeeded"
        assert provider.calls == [("成都", "武侯区")]
        assert "合成隐私" not in json.dumps(got, ensure_ascii=False)
        assert "未生成 AI 行程" in got["reply"]
    finally:
        service.close()


def test_fake_does_not_touch_geo(context):
    service, trip, owner = context

    class NoGeo:
        def __getattr__(self, name):
            pytest.fail("研究核心不应调用 Geo")

    service.itinerary.geo = NoGeo()
    run = service.create(trip.id, owner.id, request())
    assert wait_run(service, trip.id, owner.id, run["id"])["status"] == "succeeded"


def test_trip_atomic_reserve_and_cancellation(db, trips, itin):
    trip, owner = trips.create_trip(TripCreate(title="合成阻塞测试"))
    blocked = BlockedFake()
    service = ResearchService(db, trips, itin, blocked)
    try:
        run = service.create(trip.id, owner.id, request())
        assert blocked.entered.wait(2)
        with pytest.raises(ResearchError) as error:
            service.create(trip.id, owner.id, request("second"))
        assert error.value.code == "trip_busy"
        assert service.cancel(trip.id, owner.id, run["id"])["status"] == "cancelled"
        assert service.cancel(trip.id, owner.id, run["id"])["status"] == "cancelled"
        blocked.release.set()
    finally:
        blocked.release.set()
        service.close()
    got = service.get(trip.id, owner.id, run["id"])
    assert got["status"] == "cancelled" and got["candidates"] == []
    assert blocked.calls == 1


def test_same_key_concurrent_reuses_one_run(db, trips, itin):
    trip, owner = trips.create_trip(TripCreate(title="合成并发测试"))
    blocked = BlockedFake()
    one = ResearchService(db, trips, itin, blocked)
    two = ResearchService(db, trips, itin, blocked)
    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            runs = list(executor.map(lambda i: (one if i % 2 else two).create(trip.id, owner.id, request()), range(6)))
        assert len({run["id"] for run in runs}) == 1
        assert blocked.entered.wait(2)
        assert blocked.calls == 1
    finally:
        blocked.release.set()
        one.close()
        two.close()


def test_per_member_trip_rate_limit(context):
    service, trip, owner = context
    for i in range(5):
        run = service.create(trip.id, owner.id, request(f"key-{i}"))
        wait_run(service, trip.id, owner.id, run["id"])
    assert service.create(trip.id, owner.id, request("key-0"))["id"]
    with pytest.raises(ResearchError) as error:
        service.create(trip.id, owner.id, request("sixth"))
    assert error.value.code == "rate_limited"


def test_global_queue_bound_and_two_workers(db, trips, itin):
    blocked = BlockedFake()
    service = ResearchService(db, trips, itin, blocked)
    try:
        runs = []
        for i in range(MAX_ACTIVE_RUNS):
            trip, owner = trips.create_trip(TripCreate(title=f"合成并发 {i}"))
            runs.append(service.create(trip.id, owner.id, request()))
        assert blocked.entered.wait(2)
        time.sleep(0.03)
        assert blocked.calls <= 2
        trip, owner = trips.create_trip(TripCreate(title="合成超额"))
        with pytest.raises(ResearchError) as error:
            service.create(trip.id, owner.id, request())
        assert error.value.code == "queue_full"
    finally:
        blocked.release.set()
        service.close()


def test_trip_reserve_atomic_across_database_connections(tmp_path):
    path = str(tmp_path / "trip-reserve.sqlite3")
    db1, db2 = Database(path), Database(path)
    trips1, trips2 = TripService(db1), TripService(db2)
    trip, owner = trips1.create_trip(TripCreate(title="合成双连接占位"))
    blocked = BlockedFake()
    one = ResearchService(db1, trips1, None, blocked)
    two = ResearchService(db2, trips2, None, blocked)
    try:
        def create_one(pair):
            service, key = pair
            try:
                return service.create(trip.id, owner.id, request(key))
            except ResearchError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create_one, [(one, "one"), (two, "two")]))
        assert len([result for result in results if isinstance(result, dict)]) == 1
        assert "trip_busy" in results
        assert blocked.entered.wait(2) and blocked.calls == 1
    finally:
        blocked.release.set()
        one.close()
        two.close()
        db1.close()
        db2.close()


def test_process_queue_bound_across_independent_databases():
    blocked = BlockedFake()
    databases, services = [], []
    try:
        for i in range(MAX_ACTIVE_RUNS + 1):
            db = Database(":memory:")
            databases.append(db)
            trips = TripService(db)
            trip, owner = trips.create_trip(TripCreate(title=f"独立数据库 {i}"))
            service = ResearchService(db, trips, None, blocked)
            services.append(service)
            if i < MAX_ACTIVE_RUNS:
                service.create(trip.id, owner.id, request())
            else:
                with pytest.raises(ResearchError) as error:
                    service.create(trip.id, owner.id, request())
                assert error.value.code == "queue_full"
        assert blocked.entered.wait(2)
        time.sleep(0.02)
        assert blocked.calls <= 2
    finally:
        blocked.release.set()
        for service in services:
            service.close()
        for db in databases:
            db.close()


@pytest.mark.parametrize("source", ["brave", "public_web"])
def test_daily_quota_persistent_atomic_across_connections(tmp_path, source):
    path = str(tmp_path / "content.sqlite3")
    db1, db2 = Database(path), Database(path)
    trips1, trips2 = TripService(db1), TripService(db2)
    provider1, provider2 = OfflineBrave(), OfflineBrave()
    provider1.source = provider2.source = source
    provider1.daily_limit = provider2.daily_limit = 1
    one, two = ResearchService(db1, trips1, None, provider1), ResearchService(db2, trips2, None, provider2)
    trip1, owner1 = trips1.create_trip(TripCreate(title="合成并发配额 1"))
    trip2, owner2 = trips2.create_trip(TripCreate(title="合成并发配额 2"))
    try:
        one.set_consent(trip1.id, owner1.id, True)
        two.set_consent(trip2.id, owner2.id, True)
        req = request(enabled_sources=[source], network_consent=True)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(one.create, trip1.id, owner1.id, req),
                       executor.submit(two.create, trip2.id, owner2.id, req)]
            a, b = [future.result() for future in futures]
        result1 = wait_run(one, trip1.id, owner1.id, a["id"])
        result2 = wait_run(two, trip2.id, owner2.id, b["id"])
        assert sorted([result1["status"], result2["status"]]) == ["failed", "succeeded"]
        assert len(provider1.calls) + len(provider2.calls) == 1
        with db1.cursor() as cur:
            assert cur.execute("SELECT used FROM research_daily_usage WHERE source=?", (source,)).fetchone()[0] == 1
    finally:
        one.close()
        two.close()
        db1.close()
        db2.close()


def test_failure_error_redaction(context, db, trips, itin):
    old, trip, owner = context
    old.close()
    provider = OfflineBrave(error=RuntimeError("secret-key response-body https://private.invalid/?token=secret"))
    service = ResearchService(db, trips, itin, provider)
    try:
        service.set_consent(trip.id, owner.id, True)
        run = service.create(trip.id, owner.id, request(enabled_sources=["brave"], network_consent=True))
        got = wait_run(service, trip.id, owner.id, run["id"])
        assert got["status"] == "failed" and not got["candidates"]
        assert got["errors"][0]["code"] == "provider_failure"
        assert "secret" not in json.dumps(got) and "response-body" not in json.dumps(got)
        assert got["usage"]["searches"] == 1 and got["usage"]["reads"] == 0
    finally:
        service.close()


def test_revoking_consent_discards_inflight_result(db, trips, itin):
    trip, owner = trips.create_trip(TripCreate(title="合成撤销测试"))
    blocked = BlockedFake()
    blocked.source, blocked.is_simulated, blocked.daily_limit = "brave", False, 3
    service = ResearchService(db, trips, itin, blocked)
    try:
        service.set_consent(trip.id, owner.id, True)
        run = service.create(trip.id, owner.id, request(enabled_sources=["brave"], network_consent=True))
        assert blocked.entered.wait(2)
        service.set_consent(trip.id, owner.id, False)
        assert service.get(trip.id, owner.id, run["id"])["status"] == "cancelled"
    finally:
        blocked.release.set()
        service.close()
    assert blocked.calls == 1
    assert not service.get(trip.id, owner.id, run["id"])["documents"]


def test_close_cancels_and_refuses_new_requests(db, trips, itin):
    trip, owner = trips.create_trip(TripCreate(title="合成关闭测试"))
    blocked = BlockedFake()
    service = ResearchService(db, trips, itin, blocked)
    run = service.create(trip.id, owner.id, request())
    assert blocked.entered.wait(2)
    blocked.release.set()
    service.close()
    service.close()
    assert service.get(trip.id, owner.id, run["id"])["status"] in {"cancelled", "succeeded"}
    with pytest.raises(ResearchError) as error:
        service.create(trip.id, owner.id, request("after-close"))
    assert error.value.code == "closed"


def test_new_instance_does_not_interrupt_active_instance(db, trips, itin):
    trip, owner = trips.create_trip(TripCreate(title="合成多实例"))
    blocked = BlockedFake()
    one = ResearchService(db, trips, itin, blocked)
    two = None
    try:
        run = one.create(trip.id, owner.id, request())
        assert blocked.entered.wait(2)
        two = ResearchService(db, trips, itin)
        assert two.get(trip.id, owner.id, run["id"])["status"] == "running"
    finally:
        blocked.release.set()
        one.close()
        if two:
            two.close()


def test_startup_interrupts_orphan_without_replay(context, db, trips, itin):
    service, trip, owner = context
    run = service.create(trip.id, owner.id, request())
    wait_run(service, trip.id, owner.id, run["id"])
    service.close()
    with db.cursor() as cur:
        cur.execute("UPDATE research_runs SET status='running',worker_pid=NULL WHERE id=?", (run["id"],))
    restarted = ResearchService(db, trips, itin)
    try:
        got = restarted.get(trip.id, owner.id, run["id"])
        assert got["status"] == "interrupted" and "未自动重放" in got["reply"]
        assert restarted.create(trip.id, owner.id, request())["status"] == "interrupted"
    finally:
        restarted.close()


def test_expiry_clears_evidence_and_preserves_tombstone(context, db):
    service, trip, owner = context
    run = service.create(trip.id, owner.id, request())
    wait_run(service, trip.id, owner.id, run["id"])
    with db.cursor() as cur:
        cur.execute("UPDATE research_runs SET expires_at=? WHERE id=?",
                    ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), run["id"]))
    with pytest.raises(ResearchError) as error:
        service.get(trip.id, owner.id, run["id"])
    assert error.value.code == "expired"
    with db.cursor() as cur:
        row = cur.execute("SELECT payload FROM research_runs WHERE id=?", (run["id"],)).fetchone()
        payload = json.loads(row[0])
        assert not payload["documents"] and not payload["candidates"]
    with pytest.raises(ResearchError) as error:
        service.create(trip.id, owner.id, request())
    assert error.value.code == "expired"


def test_fingerprint_snapshot_coverage_and_nested_transaction(context, db, trips, itin):
    service, trip, owner = context
    previous = service.fingerprint(trip.id)

    def changed():
        nonlocal previous
        current = service.fingerprint(trip.id)
        assert current != previous
        previous = current

    with db.cursor() as cur:
        cur.execute("UPDATE trips SET budget=123 WHERE id=?", (trip.id,))
        changed()
    with db.cursor() as cur:
        cur.execute("UPDATE trips SET start_date='2027-01-01',end_date='2027-01-03' WHERE id=?", (trip.id,))
    changed()
    with db.cursor() as cur:
        cur.execute("UPDATE trips SET destination='北京' WHERE id=?", (trip.id,))
    changed()
    member = trips.add_member(trip.id, MemberJoin(display_name="合成成员"))
    changed()
    with db.cursor() as cur:
        cur.execute("UPDATE members SET status='declined' WHERE id=?", (member.id,))
    changed()
    trips.add_need(trip.id, owner.id, NeedCreate(kind=NeedKind.HARD, category="dietary", value="合成约束"))
    changed()
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    changed()
    itin.add_item(trip.id, owner.id, day.id, ItemCreate(title="合成安排"))
    changed()
    trips.add_place(trip.id, owner.id, PlaceCreate(name="合成地点"))
    changed()
    service.set_consent(trip.id, owner.id, True)
    assert service.fingerprint(trip.id) == previous
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inspirations(id,trip_id,title,status,created_at) "
            "VALUES('fixture',?,'合成资料','pending','now')", (trip.id,),
        )
    assert service.fingerprint(trip.id) == previous
    with pytest.raises(RuntimeError), db.cursor() as cur:
        cur.execute("UPDATE trips SET budget=999 WHERE id=?", (trip.id,))
        assert service.fingerprint(trip.id) != previous
        raise RuntimeError("合成回滚")
    assert service.fingerprint(trip.id) == previous


@pytest.mark.parametrize("message,topic,category", [
    ("想吃餐厅美食", "dining", "meal"), ("想看博物馆文化展览", "culture", "activity"),
    ("公园散步自然风景", "outdoors", "activity"),
])
def test_local_topic_filters_fake_without_raw_message(context, message, topic, category):
    service, trip, owner = context
    req = ResearchRequest(message=message, idempotency_key="topic")
    run = service.create(trip.id, owner.id, req)
    got = wait_run(service, trip.id, owner.id, run["id"])
    assert got["topic"] == topic and len(got["candidates"]) == 1
    assert got["candidates"][0]["category"] == category


@pytest.mark.parametrize("sources", [["fake", "brave"], ["public_web", "brave"], ["fake", "public_web"]])
def test_mixed_sources_refused_before_any_request(context, sources):
    service, trip, owner = context
    with pytest.raises(ResearchError) as error:
        service.create(trip.id, owner.id, request(enabled_sources=sources))
    assert error.value.code == "mixed_sources"


def test_service_deadline_preserves_partial_results(context, db):
    service, trip, owner = context
    run = service.create(trip.id, owner.id, request())
    got = wait_run(service, trip.id, owner.id, run["id"])
    with db.cursor() as cur:
        cur.execute("UPDATE research_runs SET status='running',created_at=? WHERE id=?", (
            (datetime.now(timezone.utc) - timedelta(seconds=121)).isoformat(), run["id"],
        ))
    assert not service._stage(run["id"], "不应继续")
    got = service.get(trip.id, owner.id, run["id"])
    assert got["status"] == "partial" and got["candidates"]
    assert got["errors"][0]["code"] == "timeout"


def test_member_leaving_stops_pending_result(db, trips, itin):
    trip, owner = trips.create_trip(TripCreate(title="合成退出成员"))
    blocked = BlockedFake()
    service = ResearchService(db, trips, itin, blocked)
    try:
        run = service.create(trip.id, owner.id, request())
        assert blocked.entered.wait(2)
        with db.cursor() as cur:
            cur.execute("UPDATE members SET status='declined' WHERE id=?", (owner.id,))
        blocked.release.set()
    finally:
        blocked.release.set()
        service.close()
    with db.cursor() as cur:
        got = json.loads(cur.execute("SELECT payload FROM research_runs WHERE id=?", (run["id"],)).fetchone()[0])
        assert got["status"] == "cancelled" and not got["documents"]


def test_source_metadata_and_default_availability(context, monkeypatch):
    service, trip, owner = context
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("FAIRMEET_PUBLIC_SEARCH_ENABLED", raising=False)
    monkeypatch.delenv("FAIRMEET_PUBLIC_SEARCH_DAILY_LIMIT", raising=False)
    info = {row["source"]: row for row in service.sources(trip.id, owner.id)}
    assert set(info) == {"fake", "brave", "public_web", "amap_poi"}
    assert info["fake"]["available"] and info["fake"]["is_simulated"]
    assert not info["brave"]["available"] and info["brave"]["requires_network_consent"]
    assert not info["brave"]["read"] and not info["brave"]["consent_enabled"]
    public = info["public_web"]
    assert public["available"] and not public["is_simulated"] and public["daily_limit"] == 20
    assert public["requires_network_consent"] and not public["consent_enabled"] and not public["read"]
    assert "DuckDuckGo" in public["name"] and "免密钥" in public["detail"] and "链接" in public["detail"]
    service.set_consent(trip.id, owner.id, True)
    with pytest.raises(ResearchError) as error:
        service.create(trip.id, owner.id, request(enabled_sources=["brave"], network_consent=True))
    assert error.value.code == "source_disabled"


def test_server_assigns_evidence_ids_and_never_accepts_provider_locations(context, db, trips, itin):
    from fairmeet.content import ContentHit

    old, trip, owner = context
    old.close()

    class MaliciousFake(FakeContentProvider):
        def search(self, destination, scope=""):
            return [ContentHit(
                title="<script>合成不可信标题</script>", url="https://www.xiaohongshu.com/real-person",
                excerpt="token=secret https://example.org/?sign=secret " + "合成正文" * 500,
                category="not-a-category",
            )]

    service = ResearchService(db, trips, itin, MaliciousFake())
    try:
        run = service.create(trip.id, owner.id, request())
        got = wait_run(service, trip.id, owner.id, run["id"])
        assert got["documents"][0]["url"] is None
        assert len(got["documents"][0]["excerpt"]) <= 1000
        assert "secret" not in json.dumps(got) and "<script>" not in json.dumps(got)
        assert got["candidates"][0]["category"] == "activity"
        assert got["candidates"][0]["evidence_ids"] == [got["documents"][0]["id"]]
        assert "xiaohongshu" not in json.dumps(got)
    finally:
        service.close()


def test_public_html_pipeline_keeps_scopes_and_minimal_query(context, db, trips, itin):
    import httpx

    from fairmeet.content import PublicWebContentProvider
    from fairmeet.content.safety import public_query

    old, trip, owner = context
    old.close()
    calls = []

    def handler(req):
        calls.append(req.url.params["q"])
        return httpx.Response(200, text=(
            '<a class="result__a" href="https://example.org/museum">合成博物馆</a>'
            '<div class="result__snippet">合成摘要</div>'
            '<a class="result__a" href="https://example.org/park">合成公园</a>'
        ), headers={"content-type": "text/html"})

    provider = PublicWebContentProvider(transport=httpx.MockTransport(handler))
    service = ResearchService(db, trips, itin, provider)
    try:
        trips.add_need(trip.id, owner.id, NeedCreate(kind=NeedKind.HARD, category="dietary", value="私密硬约束"))
        service.set_consent(trip.id, owner.id, True)
        req = ResearchRequest(message="私密原话：想看博物馆展览", scope="武侯区", enabled_sources=["public_web"],
                              network_consent=True, idempotency_key="html-integration")
        run = service.create(trip.id, owner.id, req)
        got = wait_run(service, trip.id, owner.id, run["id"])
        assert got["status"] == "succeeded" and got["enabled_sources"] == ["public_web"]
        assert calls == [public_query("成都", "武侯区", "culture")]
        assert [d["content_scope"] for d in got["documents"]] == ["snippet", "link"]
        assert all(d["kind"] == "external" and not d["is_simulated"] for d in got["documents"])
        assert got["documents"][1]["excerpt"] == "" and "仅取得搜索链接" in got["candidates"][1]["risks"][0]
        assert got["usage"]["searches"] == got["usage"]["max_searches"] == 1
        assert got["usage"]["reads"] == 0 and len(got["candidates"]) <= 5
    finally:
        service.close()


@pytest.mark.parametrize("status,code", [(202, "blocked"), (429, "rate_limited")])
def test_public_search_latch_exposed_in_sources_and_stops_new_runs(context, db, trips, itin, status, code):
    import httpx

    from fairmeet.content import PublicWebContentProvider

    old, trip, owner = context
    old.close()
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status)

    provider = PublicWebContentProvider(transport=httpx.MockTransport(handler))
    service = ResearchService(db, trips, itin, provider)
    try:
        service.set_consent(trip.id, owner.id, True)
        payload = request(enabled_sources=["public_web"], network_consent=True)
        run = service.create(trip.id, owner.id, payload)
        got = wait_run(service, trip.id, owner.id, run["id"])
        assert got["status"] == "failed" and got["errors"][0]["code"] == code
        public = next(row for row in service.sources(trip.id, owner.id) if row["source"] == "public_web")
        assert not public["available"] and public["blocked_code"] == code and "已停止" in public["detail"]
        with pytest.raises(ResearchError) as error:
            service.create(trip.id, owner.id, payload.model_copy(update={"idempotency_key": "again"}))
        assert error.value.code == code and len(calls) == 1
    finally:
        service.close()


def test_trip_deletion_cascades_research(context, db):
    service, trip, owner = context
    service.set_consent(trip.id, owner.id, True)
    run = service.create(trip.id, owner.id, request())
    wait_run(service, trip.id, owner.id, run["id"])
    service.close()
    with db.cursor() as cur:
        cur.execute("DELETE FROM trips WHERE id=?", (trip.id,))
        assert cur.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0] == 0
        assert cur.execute("SELECT COUNT(*) FROM research_preferences").fetchone()[0] == 0
