"""阶段 C：检查与预算测试（内存库 + FakeGeoAdapter，不联网）。

覆盖：
- budget()：累计 cost、按 provenance 分类、over_budget 判定、含 estimated/unknown 时 note 非空。
- check()：时间重叠 -> warning time_overlap；交通段 unknown -> info transport_unknown；
  硬约束「预算」+ 超预算 -> hard hard_budget_conflict 且 ok=False。
- 硬约束不被静默忽略：确认 check() 对硬约束至少有 info/hard 提示。
- 已取消(cancelled)条目不计入预算。
"""

from __future__ import annotations

import pytest

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.checks import CheckService
from fairmeet.db import Database
from fairmeet.domain import (
    DayCreate,
    ItemCreate,
    ItemStatus,
    NeedCreate,
    NeedKind,
    ProvenanceKind,
    TransportCreate,
    TransportMode,
    TripCreate,
)
from fairmeet.itinerary import ItineraryService
from fairmeet.services import TripService

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def db() -> Database:
    return Database(":memory:")


@pytest.fixture()
def trips(db: Database) -> TripService:
    return TripService(db)


@pytest.fixture()
def itin(db: Database, trips: TripService) -> ItineraryService:
    return ItineraryService(db, trips, FakeGeoAdapter())


@pytest.fixture()
def checks(trips: TripService, itin: ItineraryService) -> CheckService:
    return CheckService(trips, itin)


def _codes(result) -> set[str]:
    return {c.code for c in result.conflicts}


# ===========================================================================
# budget()
# ===========================================================================


def test_budget_accumulates_and_classifies_by_provenance(
    itin: ItineraryService, checks: CheckService, trips: TripService
):
    trip, owner = trips.create_trip(TripCreate(title="预算游", budget=1000.0))
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    itin.add_item(trip.id, owner.id, day.id,
                  ItemCreate(title="门票", cost=100.0, cost_provenance_kind=ProvenanceKind.VERIFIED))
    itin.add_item(trip.id, owner.id, day.id,
                  ItemCreate(title="餐饮估算", cost=200.0, cost_provenance_kind=ProvenanceKind.ESTIMATED))
    itin.add_item(trip.id, owner.id, day.id,
                  ItemCreate(title="未知花费", cost=50.0, cost_provenance_kind=ProvenanceKind.UNKNOWN))

    b = checks.budget(trip.id)
    assert b.trip_budget == 1000.0
    assert b.estimated_total == pytest.approx(350.0)
    assert b.by_provenance["verified"] == pytest.approx(100.0)
    assert b.by_provenance["estimated"] == pytest.approx(200.0)
    assert b.by_provenance["unknown"] == pytest.approx(50.0)
    assert b.over_budget is False
    # 含 estimated/unknown，note 必须非空
    assert b.note != ""


def test_budget_over_budget(itin: ItineraryService, checks: CheckService, trips: TripService):
    trip, owner = trips.create_trip(TripCreate(title="超支游", budget=100.0))
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    itin.add_item(trip.id, owner.id, day.id,
                  ItemCreate(title="大额", cost=300.0, cost_provenance_kind=ProvenanceKind.VERIFIED))
    b = checks.budget(trip.id)
    assert b.estimated_total == pytest.approx(300.0)
    assert b.over_budget is True


def test_budget_cancelled_item_excluded(itin: ItineraryService, checks: CheckService, trips: TripService):
    trip, owner = trips.create_trip(TripCreate(title="取消项", budget=1000.0))
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    keep = itin.add_item(trip.id, owner.id, day.id,
                         ItemCreate(title="保留", cost=100.0, cost_provenance_kind=ProvenanceKind.VERIFIED))
    drop = itin.add_item(trip.id, owner.id, day.id,
                         ItemCreate(title="取消项", cost=500.0, cost_provenance_kind=ProvenanceKind.VERIFIED))
    itin.set_item_status(trip.id, owner.id, drop.id, ItemStatus.CANCELLED)

    b = checks.budget(trip.id)
    # 仅保留项计入
    assert b.estimated_total == pytest.approx(100.0)
    assert _item_status(itin, trip.id, drop.id) == ItemStatus.CANCELLED
    assert keep.cost == 100.0


def _item_status(itin: ItineraryService, trip_id: str, item_id: str) -> ItemStatus:
    return itin.get_item(trip_id, item_id).status


# ===========================================================================
# check()：时间重叠
# ===========================================================================


def test_check_time_overlap_warning(itin: ItineraryService, checks: CheckService, trips: TripService):
    trip, owner = trips.create_trip(TripCreate(title="重叠游"))
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    itin.add_item(trip.id, owner.id, day.id,
                  ItemCreate(title="A", start_time="09:00", end_time="11:00"))
    itin.add_item(trip.id, owner.id, day.id,
                  ItemCreate(title="B", start_time="10:00", end_time="12:00"))
    result = checks.check(trip.id)
    overlaps = [c for c in result.conflicts if c.code == "time_overlap"]
    assert overlaps, "应出现 time_overlap"
    assert overlaps[0].severity == "warning"


# ===========================================================================
# check()：交通未知
# ===========================================================================


def test_check_transport_unknown_info(itin: ItineraryService, checks: CheckService, trips: TripService):
    trip, owner = trips.create_trip(TripCreate(title="交通游"))
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    # 显式不给时长、provenance=unknown 的交通段（compute=False）
    itin.add_transport(
        trip.id, owner.id, day.id,
        TransportCreate(mode=TransportMode.WALK, duration_provenance_kind=ProvenanceKind.UNKNOWN),
        compute=False,
    )
    result = checks.check(trip.id)
    unknown = [c for c in result.conflicts if c.code == "transport_unknown"]
    assert unknown, "应出现 transport_unknown"
    assert unknown[0].severity == "info"


# ===========================================================================
# check()：硬约束预算 + 超预算 -> hard，ok=False
# ===========================================================================


def test_check_hard_budget_conflict(itin: ItineraryService, checks: CheckService, trips: TripService):
    trip, owner = trips.create_trip(TripCreate(title="硬预算游", budget=100.0))
    # 添加硬约束「预算」
    trips.add_need(trip.id, owner.id, NeedCreate(kind=NeedKind.HARD, category="预算", value="不超过 100 元"))
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    itin.add_item(trip.id, owner.id, day.id,
                  ItemCreate(title="大额", cost=500.0, cost_provenance_kind=ProvenanceKind.VERIFIED))

    result = checks.check(trip.id)
    assert result.ok is False
    hard = [c for c in result.conflicts if c.code == "hard_budget_conflict"]
    assert hard, "应出现 hard_budget_conflict"
    assert hard[0].severity == "hard"


def test_check_hard_need_not_silently_ignored(itin: ItineraryService, checks: CheckService, trips: TripService):
    """硬约束即便不被覆盖，也必须至少有 info/hard 提示，绝不静默忽略。"""
    trip, owner = trips.create_trip(TripCreate(title="无障碍游"))
    trips.add_need(trip.id, owner.id, NeedCreate(kind=NeedKind.HARD, category="无障碍", value="全程无台阶"))
    # 完全没有相关行程条目
    result = checks.check(trip.id)
    hard_need_hints = [c for c in result.conflicts if c.code.startswith("hard_")]
    assert hard_need_hints, "硬约束必须有提示（不得静默忽略）"
    assert all(c.severity in ("info", "hard") for c in hard_need_hints)
