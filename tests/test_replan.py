"""阶段 E：局部重排回归测试（核心：只改受影响项、保留其他已确认项）。"""

from __future__ import annotations

import pytest

from fairmeet.domain import (
    DayCreate,
    ItemCreate,
    ItemStatus,
    ProvenanceKind,
    ReplanRequest,
    TripCreate,
)
from fairmeet.history import HistoryService
from fairmeet.itinerary import ItineraryService
from fairmeet.replan import ReplanService
from fairmeet.services import Forbidden, TripService


def _confirm(itin: ItineraryService, trip_id: str, member_id: str, item_id: str) -> None:
    for s in (ItemStatus.DISCUSSING, ItemStatus.PENDING, ItemStatus.CONFIRMED):
        itin.set_item_status(trip_id, member_id, item_id, s)


def _item(itin: ItineraryService, trip_id: str, mid: str, day_id: str, title: str, cost: float):
    return itin.add_item(
        trip_id, mid, day_id, ItemCreate(title=title, cost=cost, cost_provenance_kind=ProvenanceKind.ESTIMATED)
    )


def _setup(trips: TripService, itin: ItineraryService):
    trip, owner = trips.create_trip(TripCreate(title="成都三日游"))
    d1 = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))
    d2 = itin.add_day(trip.id, owner.id, DayCreate(day_index=2))
    a = _item(itin, trip.id, owner.id, d1.id, "A", 100)
    b = _item(itin, trip.id, owner.id, d1.id, "B", 50)
    c = _item(itin, trip.id, owner.id, d1.id, "C", 80)
    _confirm(itin, trip.id, owner.id, c.id)  # day1 的 C 已确认
    x = _item(itin, trip.id, owner.id, d2.id, "X", 999)
    _confirm(itin, trip.id, owner.id, x.id)  # day2 的 X 已确认
    return trip, owner, d1, d2, a, b, c, x


def test_replan_preserves_other_day_confirmed(trips: TripService, itin: ItineraryService, replan: ReplanService):
    trip, owner, d1, d2, a, b, c, x = _setup(trips, itin)
    x_before = itin.get_item(trip.id, x.id)
    c_before = itin.get_item(trip.id, c.id)

    result = replan.replan_day(
        trip.id, owner.id, ReplanRequest(day_id=d1.id, new_order=[c.id, a.id, b.id], reason="C提前")
    )

    # ① day1 顺序变化，diffs 含 sort_order
    assert any("sort_order" in d.field for d in result.changed)
    order = [i.title for i in sorted(itin.list_items(trip.id, d1.id), key=lambda z: z.sort_order)]
    assert order == ["C", "A", "B"]

    # ② day2 已确认条目完全不变
    x_after = itin.get_item(trip.id, x.id)
    assert x_after.status == x_before.status
    assert x_after.cost == x_before.cost
    assert x_after.sort_order == x_before.sort_order

    # ③ day1 已确认 C：状态/费用不变（仅顺序可能变）
    c_after = itin.get_item(trip.id, c.id)
    assert c_after.status == c_before.status == ItemStatus.CONFIRMED
    assert c_after.cost == c_before.cost

    # ④ preserved 含 day2 的 X（跨天未受影响项被列出）
    assert x.id in result.preserved_item_ids


def test_replan_partial_scope_keeps_others_order(trips: TripService, itin: ItineraryService, replan: ReplanService):
    trip, owner, d1, d2, a, b, c, x = _setup(trips, itin)
    b_before = itin.get_item(trip.id, b.id)
    # 只重排 scope=[a]，b/c 顺序不应变
    result = replan.replan_day(trip.id, owner.id, ReplanRequest(day_id=d1.id, item_ids=[a.id]))
    assert itin.get_item(trip.id, b.id).sort_order == b_before.sort_order
    # b、c 属于范围外，应在 preserved 中
    assert b.id in result.preserved_item_ids
    assert c.id in result.preserved_item_ids


def test_replan_records_history(
    trips: TripService, itin: ItineraryService, replan: ReplanService, hist: HistoryService
):
    trip, owner, d1, d2, a, b, c, x = _setup(trips, itin)
    before = len(hist.list_entries(trip.id))
    result = replan.replan_day(trip.id, owner.id, ReplanRequest(day_id=d1.id, new_order=[c.id, a.id, b.id]))
    entries = hist.list_entries(trip.id)
    assert len(entries) == before + 1
    assert entries[0].id == result.change_log_id
    assert entries[0].change_type.value == "replan"


def test_replan_non_member_forbidden(trips: TripService, itin: ItineraryService, replan: ReplanService):
    trip, owner, d1, d2, a, b, c, x = _setup(trips, itin)
    with pytest.raises(Forbidden):
        replan.replan_day(trip.id, "bogus", ReplanRequest(day_id=d1.id))


def test_replan_item_from_other_day_rejected(trips: TripService, itin: ItineraryService, replan: ReplanService):
    trip, owner, d1, d2, a, b, c, x = _setup(trips, itin)
    with pytest.raises(ValueError):
        replan.replan_day(trip.id, owner.id, ReplanRequest(day_id=d1.id, item_ids=[x.id]))
