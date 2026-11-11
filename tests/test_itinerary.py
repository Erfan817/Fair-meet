"""阶段 C：逐日行程服务测试（内存库 + FakeGeoAdapter，不联网）。

覆盖：
- 天：创建、重复 day_index 冲突（Forbidden）、get_day/list_days 排序。
- 条目：添加、sort_order 递增、list_items 按天过滤。
- 状态机：合法 draft->discussing->pending->confirmed->done；非法 draft->confirmed 抛 Forbidden。
- update_item 局部更新只改指定字段。
- 回归：局部调整不影响其他已确认项。
- 授权：非成员写操作抛 Forbidden。
- add_transport(compute=True)：双端有坐标 -> duration+estimated；一端无坐标 -> None+unknown；
  geo 不可用 -> 降级 unknown 不抛。
"""

from __future__ import annotations

import pytest

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.db import Database
from fairmeet.domain import (
    DayCreate,
    ItemCreate,
    ItemStatus,
    ItemUpdate,
    PlaceCreate,
    ProvenanceKind,
    TransportCreate,
    TransportMode,
    TripCreate,
)
from fairmeet.itinerary import ItineraryService
from fairmeet.services import Forbidden, TripService

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
def trip_and_owner(trips: TripService):
    trip, owner = trips.create_trip(TripCreate(title="成都三日游", owner_name="小林"))
    return trip, owner


# ===========================================================================
# 天
# ===========================================================================


def test_add_day_and_get(itin: ItineraryService, trip_and_owner):
    trip, owner = trip_and_owner
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1, date="2026-11-11", title="抵达"))
    assert day.trip_id == trip.id
    assert day.day_index == 1
    got = itin.get_day(trip.id, day.id)
    assert got.id == day.id
    assert got.title == "抵达"


def test_duplicate_day_index_raises_forbidden(itin: ItineraryService, trip_and_owner):
    trip, owner = trip_and_owner
    itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    with pytest.raises(Forbidden):
        itin.add_day(trip.id, owner.id, DayCreate(day_index=1))


def test_list_days_sorted_by_day_index(itin: ItineraryService, trip_and_owner):
    trip, owner = trip_and_owner
    itin.add_day(trip.id, owner.id, DayCreate(day_index=3))
    itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    itin.add_day(trip.id, owner.id, DayCreate(day_index=2))
    days = itin.list_days(trip.id)
    assert [d.day_index for d in days] == [1, 2, 3]


# ===========================================================================
# 条目
# ===========================================================================


def test_add_item_increments_sort_order(itin: ItineraryService, trip_and_owner):
    trip, owner = trip_and_owner
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    i1 = itin.add_item(trip.id, owner.id, day.id, ItemCreate(title="早餐"))
    i2 = itin.add_item(trip.id, owner.id, day.id, ItemCreate(title="午餐"))
    i3 = itin.add_item(trip.id, owner.id, day.id, ItemCreate(title="晚餐"))
    assert [i1.sort_order, i2.sort_order, i3.sort_order] == [0, 1, 2]
    assert i1.status == ItemStatus.DRAFT


def test_list_items_filtered_by_day(itin: ItineraryService, trip_and_owner):
    trip, owner = trip_and_owner
    d1 = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    d2 = itin.add_day(trip.id, owner.id, DayCreate(day_index=2))
    itin.add_item(trip.id, owner.id, d1.id, ItemCreate(title="D1-A"))
    itin.add_item(trip.id, owner.id, d1.id, ItemCreate(title="D1-B"))
    itin.add_item(trip.id, owner.id, d2.id, ItemCreate(title="D2-A"))

    d1_items = itin.list_items(trip.id, d1.id)
    assert {i.title for i in d1_items} == {"D1-A", "D1-B"}
    assert len(itin.list_items(trip.id)) == 3  # 全部


# ===========================================================================
# 状态机
# ===========================================================================


def test_status_machine_legal_path(itin: ItineraryService, trip_and_owner):
    trip, owner = trip_and_owner
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    item = itin.add_item(trip.id, owner.id, day.id, ItemCreate(title="参观"))

    item = itin.set_item_status(trip.id, owner.id, item.id, ItemStatus.DISCUSSING)
    assert item.status == ItemStatus.DISCUSSING
    item = itin.set_item_status(trip.id, owner.id, item.id, ItemStatus.PENDING)
    assert item.status == ItemStatus.PENDING
    item = itin.set_item_status(trip.id, owner.id, item.id, ItemStatus.CONFIRMED)
    assert item.status == ItemStatus.CONFIRMED
    item = itin.set_item_status(trip.id, owner.id, item.id, ItemStatus.DONE)
    assert item.status == ItemStatus.DONE


def test_status_machine_illegal_transition_raises(itin: ItineraryService, trip_and_owner):
    trip, owner = trip_and_owner
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    item = itin.add_item(trip.id, owner.id, day.id, ItemCreate(title="参观"))
    # draft -> confirmed 非法
    with pytest.raises(Forbidden):
        itin.set_item_status(trip.id, owner.id, item.id, ItemStatus.CONFIRMED)
    # 状态保持不变
    assert itin.get_item(trip.id, item.id).status == ItemStatus.DRAFT


# ===========================================================================
# 局部更新
# ===========================================================================


def test_update_item_partial_only_changes_given_fields(itin: ItineraryService, trip_and_owner):
    trip, owner = trip_and_owner
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    item = itin.add_item(
        trip.id,
        owner.id,
        day.id,
        ItemCreate(title="原标题", start_time="09:00", end_time="10:00", stay_minutes=60, cost=50.0,
                   note="原备注"),
    )
    updated = itin.update_item(trip.id, owner.id, item.id, ItemUpdate(title="新标题"))
    assert updated.title == "新标题"
    # 其余字段不变
    assert updated.start_time == "09:00"
    assert updated.end_time == "10:00"
    assert updated.stay_minutes == 60
    assert updated.cost == 50.0
    assert updated.note == "原备注"


def test_update_item_does_not_affect_other_confirmed_item(itin: ItineraryService, trip_and_owner):
    """回归：局部调整一个条目，不得改动同一天里另一个已确认条目。"""
    trip, owner = trip_and_owner
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    confirmed = itin.add_item(
        trip.id, owner.id, day.id, ItemCreate(title="已确认景点", start_time="14:00", end_time="16:00", cost=120.0)
    )
    # 走状态机到 confirmed
    for st in (ItemStatus.DISCUSSING, ItemStatus.PENDING, ItemStatus.CONFIRMED):
        confirmed = itin.set_item_status(trip.id, owner.id, confirmed.id, st)
    assert confirmed.status == ItemStatus.CONFIRMED

    other = itin.add_item(trip.id, owner.id, day.id, ItemCreate(title="待调整条目", cost=30.0))

    # 更新另一个条目
    itin.update_item(trip.id, owner.id, other.id, ItemUpdate(title="已改名", cost=99.0))

    # 已确认那条必须完全不变
    after = itin.get_item(trip.id, confirmed.id)
    assert after.status == ItemStatus.CONFIRMED
    assert after.title == "已确认景点"
    assert after.start_time == "14:00"
    assert after.end_time == "16:00"
    assert after.cost == 120.0
    assert after.sort_order == confirmed.sort_order
    assert after.id == confirmed.id


# ===========================================================================
# 授权
# ===========================================================================


def test_non_member_write_raises_forbidden(itin: ItineraryService, trip_and_owner, trips: TripService):
    trip, _owner = trip_and_owner
    # 另一条旅程，用它的合法成员去操作本旅程 -> 跨旅程应被拒
    other_trip, other_owner = trips.create_trip(TripCreate(title="别人的旅程"))
    # token 无效 / 空
    with pytest.raises(Forbidden):
        itin.add_day(trip.id, "", DayCreate(day_index=1))
    with pytest.raises(Forbidden):
        itin.add_day(trip.id, "not-a-real-member", DayCreate(day_index=1))
    # 跨旅程成员
    with pytest.raises(Forbidden):
        itin.add_day(trip.id, other_owner.id, DayCreate(day_index=1))


# ===========================================================================
# 交通段 compute=True
# ===========================================================================


def _make_point_items(itin: ItineraryService, trips: TripService, trip_id: str, owner_id: str, with_coords: bool):
    """辅助：建两次条目，可选建立带坐标的地点并绑定。返回 (day, from_item, to_item)。"""
    day = itin.add_day(trip_id, owner_id, DayCreate(day_index=1))
    if with_coords:
        p1 = trips.add_place(trip_id, owner_id, PlaceCreate(name="起点", lng=104.06, lat=30.60))
        p2 = trips.add_place(trip_id, owner_id, PlaceCreate(name="终点", lng=104.08, lat=30.65))
        i1 = itin.add_item(trip_id, owner_id, day.id, ItemCreate(title="From", place_id=p1.id))
        i2 = itin.add_item(trip_id, owner_id, day.id, ItemCreate(title="To", place_id=p2.id))
    else:
        i1 = itin.add_item(trip_id, owner_id, day.id, ItemCreate(title="From"))
        i2 = itin.add_item(trip_id, owner_id, day.id, ItemCreate(title="To"))
    return day, i1, i2


def test_add_transport_compute_with_coords_estimated(itin: ItineraryService, trip_and_owner, trips: TripService):
    trip, owner = trip_and_owner
    day, i1, i2 = _make_point_items(itin, trips, trip.id, owner.id, with_coords=True)
    seg = itin.add_transport(
        trip.id, owner.id, day.id,
        TransportCreate(from_item_id=i1.id, to_item_id=i2.id, mode=TransportMode.WALK),
        compute=True,
    )
    assert seg.duration_minutes is not None
    assert seg.duration_minutes > 0
    assert seg.duration_provenance.kind == ProvenanceKind.ESTIMATED


def test_add_transport_compute_missing_coords_unknown(itin: ItineraryService, trip_and_owner, trips: TripService):
    trip, owner = trip_and_owner
    day, i1, i2 = _make_point_items(itin, trips, trip.id, owner.id, with_coords=False)
    seg = itin.add_transport(
        trip.id, owner.id, day.id,
        TransportCreate(from_item_id=i1.id, to_item_id=i2.id, mode=TransportMode.WALK),
        compute=True,
    )
    assert seg.duration_minutes is None
    assert seg.duration_provenance.kind == ProvenanceKind.UNKNOWN


def test_add_transport_compute_geo_unavailable_degrades(itin: ItineraryService, trip_and_owner, trips: TripService):
    """geo 不可用时 compute 应降级为 unknown，不抛异常。"""
    trip, owner = trip_and_owner
    itin.geo = FakeGeoAdapter(unavailable=True)
    day, i1, i2 = _make_point_items(itin, trips, trip.id, owner.id, with_coords=True)
    seg = itin.add_transport(
        trip.id, owner.id, day.id,
        TransportCreate(from_item_id=i1.id, to_item_id=i2.id, mode=TransportMode.WALK),
        compute=True,
    )
    assert seg.duration_minutes is None
    assert seg.duration_provenance.kind == ProvenanceKind.UNKNOWN
