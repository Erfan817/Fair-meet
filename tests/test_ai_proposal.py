"""阶段 D：修改提案构建与确认流程测试（内存库 + FakeGeoAdapter，不联网）。

覆盖：
- build 生成 pending 且不执行（库里无变化）；requires_confirmation 逻辑。
- 改已确认项：requires_confirmation=True、affects_confirmed=True、diffs 展示 before/after。
- confirm 后才执行（before/after 库状态对比）；只有发起者能 confirm（他人 ProposalError）。
- reject 后不执行、status=rejected；对已 confirmed 的提案再 confirm 抛 ProposalError。
"""

from __future__ import annotations

import pytest

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.ai.contracts import ProposedOperationEnvelope
from fairmeet.ai.proposal import ProposalError, ProposalService
from fairmeet.ai.tools import Toolbox
from fairmeet.checks import CheckService
from fairmeet.db import Database
from fairmeet.domain import DayCreate, ItemCreate, ItemStatus, MemberJoin, TripCreate
from fairmeet.itinerary import ItineraryService
from fairmeet.services import TripService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
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


@pytest.fixture()
def trip_and_owner(trips: TripService):
    return trips.create_trip(TripCreate(title="成都三日游", owner_name="小林", budget=3000))


@pytest.fixture()
def proposals(db: Database, trips, itin, checks) -> ProposalService:
    def factory(trip_id: str, member_id: str) -> Toolbox:
        return Toolbox(trips, itin, checks, trip_id, member_id)

    return ProposalService(db, factory)


def _env(op: dict) -> ProposedOperationEnvelope:
    return ProposedOperationEnvelope.model_validate({"operation": op})


def _make_item(itin, trip, owner, *, title="原条目", cost=50.0, confirm=False):
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    item = itin.add_item(trip.id, owner.id, day.id, ItemCreate(title=title, cost=cost))
    if confirm:
        for st in (ItemStatus.DISCUSSING, ItemStatus.PENDING, ItemStatus.CONFIRMED):
            item = itin.set_item_status(trip.id, owner.id, item.id, st)
    return day, item


# ---------------------------------------------------------------------------
# build：pending 且不执行
# ---------------------------------------------------------------------------
def test_build_is_pending_and_does_not_execute(proposals, trips, trip_and_owner):
    trip, owner = trip_and_owner
    before = len(trips.list_needs(trip.id))

    proposal = proposals.build(
        trip.id, owner.id, "加一条硬约束",
        [_env({"op": "add_need", "kind": "hard", "category": "预算", "value": "3000 元"})],
    )
    assert proposal.status == "pending"
    assert proposal.requires_confirmation is True  # 含写操作 -> 需确认

    # 未执行：库里需求数量不变
    assert len(trips.list_needs(trip.id)) == before


def test_build_persisted_and_listed(proposals, trip_and_owner):
    trip, owner = trip_and_owner
    proposal = proposals.build(
        trip.id, owner.id, "加地点",
        [_env({"op": "add_place", "name": "人民公园"})],
    )
    got = proposals.get(proposal.id)
    assert got.id == proposal.id
    assert got.status == "pending"
    assert any(p.id == proposal.id for p in proposals.list(trip.id))


def test_build_all_blocked_requires_no_confirmation(trips, itin, checks, proposals, trip_and_owner):
    """所有操作都被 block（如非成员）时，无可写操作 -> requires_confirmation=False。"""
    trip, _owner = trip_and_owner
    proposal = proposals.build(
        trip.id, "not-a-member", "非法操作",
        [_env({"op": "add_place", "name": "X"})],
    )
    assert proposal.requires_confirmation is False
    assert all(i.blocked_reason is not None for i in proposal.impacts)


# ---------------------------------------------------------------------------
# 改已确认项
# ---------------------------------------------------------------------------
def test_build_update_confirmed_item_shows_diffs(proposals, itin, trip_and_owner):
    trip, owner = trip_and_owner
    _day, item = _make_item(itin, trip, owner, title="已确认景点", cost=120.0, confirm=True)

    proposal = proposals.build(
        trip.id, owner.id, "改费用",
        [_env({"op": "update_item", "item_id": item.id, "cost": 150.0})],
    )
    assert proposal.requires_confirmation is True
    impact = proposal.impacts[0]
    assert impact.affects_confirmed is True
    diff = {d.field: (d.before, d.after) for d in impact.diffs}
    assert diff["cost"] == ("120.0", "150.0")

    # 构建不改库
    assert itin.get_item(trip.id, item.id).cost == 120.0


# ---------------------------------------------------------------------------
# confirm 后才执行
# ---------------------------------------------------------------------------
def test_confirm_executes_after_build(proposals, itin, trip_and_owner):
    trip, owner = trip_and_owner
    _day, item = _make_item(itin, trip, owner, title="景点", cost=120.0, confirm=True)

    proposal = proposals.build(
        trip.id, owner.id, "改费用",
        [_env({"op": "update_item", "item_id": item.id, "cost": 150.0})],
    )
    before = itin.get_item(trip.id, item.id)
    assert before.cost == 120.0  # confirm 前

    result = proposals.confirm(proposal.id, owner.id)
    assert result["proposal"]["status"] == "confirmed"
    assert len(result["skipped"]) == 0

    after = itin.get_item(trip.id, item.id)
    assert after.cost == 150.0  # confirm 后


def test_confirm_add_need_executes(proposals, trips, trip_and_owner):
    trip, owner = trip_and_owner
    before = len(trips.list_needs(trip.id))
    proposal = proposals.build(
        trip.id, owner.id, "加硬约束",
        [_env({"op": "add_need", "kind": "hard", "category": "预算", "value": "3000 元"})],
    )
    proposals.confirm(proposal.id, owner.id)
    assert len(trips.list_needs(trip.id)) == before + 1


# ---------------------------------------------------------------------------
# 只有发起者能 confirm
# ---------------------------------------------------------------------------
def test_only_initiator_can_confirm(proposals, trips, trip_and_owner):
    trip, owner = trip_and_owner
    other = trips.add_member(trip.id, MemberJoin(display_name="阿May"))

    proposal = proposals.build(
        trip.id, owner.id, "加地点",
        [_env({"op": "add_place", "name": "人民公园"})],
    )
    with pytest.raises(ProposalError):
        proposals.confirm(proposal.id, other.id)

    # 仍为 pending，未执行
    assert proposals.get(proposal.id).status == "pending"


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------
def test_reject_does_not_execute(proposals, trips, trip_and_owner):
    trip, owner = trip_and_owner
    before = len(trips.list_needs(trip.id))
    proposal = proposals.build(
        trip.id, owner.id, "加硬约束",
        [_env({"op": "add_need", "kind": "hard", "category": "预算", "value": "x"})],
    )
    rejected = proposals.reject(proposal.id, owner.id)
    assert rejected.status == "rejected"
    assert len(trips.list_needs(trip.id)) == before  # 未执行


def test_confirm_after_confirmed_raises(proposals, trip_and_owner):
    trip, owner = trip_and_owner
    proposal = proposals.build(
        trip.id, owner.id, "加地点",
        [_env({"op": "add_place", "name": "人民公园"})],
    )
    proposals.confirm(proposal.id, owner.id)
    with pytest.raises(ProposalError):
        proposals.confirm(proposal.id, owner.id)


def test_confirm_after_rejected_raises(proposals, trip_and_owner):
    trip, owner = trip_and_owner
    proposal = proposals.build(
        trip.id, owner.id, "加地点",
        [_env({"op": "add_place", "name": "人民公园"})],
    )
    proposals.reject(proposal.id, owner.id)
    with pytest.raises(ProposalError):
        proposals.confirm(proposal.id, owner.id)
