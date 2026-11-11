"""阶段 D：受控工具层工具测试（内存库 + FakeGeoAdapter，不联网、无真实 LLM）。

覆盖：
- plan() 是 dry-run：对 update_item 计划后库里数据不变。
- 授权：非成员 plan 返回 blocked_reason（不抛）；execute 抛 Forbidden。
- update_item plan：对已确认项 affects_confirmed=True、diffs 正确；确认前不改库。
- execute update_item 只改提供的字段（关键回归：只改 cost 时 title 不被置空）。
- add_item/add_need/add_day execute 正常。
"""

from __future__ import annotations

import pytest

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.ai.contracts import (
    ChangeKind,
    ProposedOperationEnvelope,
)
from fairmeet.ai.tools import Toolbox
from fairmeet.checks import CheckService
from fairmeet.db import Database
from fairmeet.domain import (
    DayCreate,
    ItemCreate,
    ItemStatus,
    TripCreate,
)
from fairmeet.itinerary import ItineraryService
from fairmeet.services import Forbidden, TripService


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
def toolbox(trips, itin, checks, trip_and_owner) -> Toolbox:
    trip, owner = trip_and_owner
    return Toolbox(trips, itin, checks, trip.id, owner.id)


def _env(op: dict) -> ProposedOperationEnvelope:
    return ProposedOperationEnvelope.model_validate({"operation": op})


def _make_item(itin, trips, trip, owner, *, title="原条目", cost=50.0,
               start="09:00", end="10:00", stay=60, note="原备注", confirm=False):
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    item = itin.add_item(
        trip.id, owner.id, day.id,
        ItemCreate(title=title, cost=cost, start_time=start, end_time=end, stay_minutes=stay, note=note),
    )
    if confirm:
        for st in (ItemStatus.DISCUSSING, ItemStatus.PENDING, ItemStatus.CONFIRMED):
            item = itin.set_item_status(trip.id, owner.id, item.id, st)
    return day, item


# ---------------------------------------------------------------------------
# plan 是 dry-run
# ---------------------------------------------------------------------------
def test_plan_update_item_is_dry_run(toolbox: Toolbox, itin, trips, trip_and_owner):
    trip, owner = trip_and_owner
    _day, item = _make_item(itin, trips, trip, owner, title="旧标题", cost=50.0)

    impact = toolbox.plan(_env({"op": "update_item", "item_id": item.id, "title": "新标题", "cost": 99.0}))
    assert impact.blocked_reason is None
    assert impact.change_kind == ChangeKind.UPDATE

    # dry-run：库里数据不变
    after = itin.get_item(trip.id, item.id)
    assert after.title == "旧标题"
    assert after.cost == 50.0


# ---------------------------------------------------------------------------
# 授权
# ---------------------------------------------------------------------------
def test_non_member_plan_blocked_not_raise(trips, itin, checks, trip_and_owner):
    trip, _owner = trip_and_owner
    outsider = Toolbox(trips, itin, checks, trip.id, "not-a-member")
    impact = outsider.plan(_env({"op": "add_need", "kind": "hard", "category": "预算", "value": "x"}))
    assert impact.blocked_reason is not None  # 不抛，写入 blocked_reason


def test_non_member_execute_raises_forbidden(trips, itin, checks, trip_and_owner):
    trip, _owner = trip_and_owner
    outsider = Toolbox(trips, itin, checks, trip.id, "not-a-member")
    with pytest.raises(Forbidden):
        outsider.execute(_env({"op": "add_need", "kind": "hard", "category": "预算", "value": "x"}))


# ---------------------------------------------------------------------------
# update_item plan：已确认项
# ---------------------------------------------------------------------------
def test_plan_update_confirmed_item_affects_confirmed_and_diffs(
    toolbox: Toolbox, itin, trips, trip_and_owner
):
    trip, owner = trip_and_owner
    _day, item = _make_item(itin, trips, trip, owner, title="已确认景点", cost=120.0, confirm=True)
    assert itin.get_item(trip.id, item.id).status == ItemStatus.CONFIRMED

    impact = toolbox.plan(_env({"op": "update_item", "item_id": item.id, "cost": 150.0}))
    assert impact.affects_confirmed is True
    # diffs 只包含显式提供且发生变化的字段
    diff_map = {d.field: (d.before, d.after) for d in impact.diffs}
    assert "cost" in diff_map
    assert diff_map["cost"][1] == "150.0"
    assert "title" not in diff_map  # 未提供 title

    # 确认前不改库
    assert itin.get_item(trip.id, item.id).cost == 120.0


# ---------------------------------------------------------------------------
# execute update_item：只改提供字段（关键回归）
# ---------------------------------------------------------------------------
def test_execute_update_item_only_changes_cost_title_preserved(
    toolbox: Toolbox, itin, trips, trip_and_owner
):
    trip, owner = trip_and_owner
    _day, item = _make_item(itin, trips, trip, owner, title="保留的标题", cost=50.0, note="保留备注")

    toolbox.execute(_env({"op": "update_item", "item_id": item.id, "cost": 88.0}))

    after = itin.get_item(trip.id, item.id)
    assert after.cost == 88.0
    # 关键回归：未提供的 title 不得被置空
    assert after.title == "保留的标题"
    assert after.note == "保留备注"
    assert after.start_time == "09:00"


# ---------------------------------------------------------------------------
# execute add_*
# ---------------------------------------------------------------------------
def test_execute_add_need(toolbox: Toolbox, trips, trip_and_owner):
    trip, _owner = trip_and_owner
    result = toolbox.execute(
        _env({"op": "add_need", "kind": "hard", "category": "预算", "value": "3000 元以内"})
    )
    assert result["kind"] == "need"
    needs = trips.list_needs(trip.id)
    assert any(n.value == "3000 元以内" for n in needs)


def test_execute_add_day(toolbox: Toolbox, itin, trip_and_owner):
    trip, _owner = trip_and_owner
    result = toolbox.execute(_env({"op": "add_day", "day_index": 1, "date": "2026-11-11", "title": "抵达"}))
    assert result["kind"] == "day"
    days = itin.list_days(trip.id)
    assert any(d.day_index == 1 and d.title == "抵达" for d in days)


def test_execute_add_item(toolbox: Toolbox, itin, trips, trip_and_owner):
    trip, owner = trip_and_owner
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    result = toolbox.execute(
        _env({"op": "add_item", "day_id": day.id, "title": "锦里", "cost": 60.0})
    )
    assert result["kind"] == "item"
    items = itin.list_items(trip.id, day.id)
    assert any(i.title == "锦里" for i in items)


# ---------------------------------------------------------------------------
# plan 各操作类型的基础分支（补充覆盖）
# ---------------------------------------------------------------------------
def test_plan_add_need_is_create(toolbox: Toolbox):
    impact = toolbox.plan(_env({"op": "add_need", "kind": "preference", "category": "饮食", "value": "川菜"}))
    assert impact.blocked_reason is None
    assert impact.change_kind == ChangeKind.CREATE


def test_plan_update_missing_item_blocked(toolbox: Toolbox):
    impact = toolbox.plan(_env({"op": "update_item", "item_id": "does-not-exist", "cost": 1.0}))
    assert impact.blocked_reason is not None
