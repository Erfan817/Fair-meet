"""阶段 E：变更历史 / 修订 / 更新 feed 测试（离线）。

注意：change_log / revisions 有 trip_id 外键，测试需先建真实旅程。
"""

from __future__ import annotations

from fairmeet.domain import ChangeType, FieldDiff, TripCreate
from fairmeet.history import HistoryService
from fairmeet.services import TripService


def _new_trip(trips: TripService) -> str:
    trip, _ = trips.create_trip(TripCreate(title="历史测试旅程"))
    return trip.id


def test_record_and_list(trips: TripService, hist: HistoryService):
    tid = _new_trip(trips)
    e1 = hist.record(tid, ChangeType.ITEM_CREATE, "新增 A", member_id="m1", target_ref="i1")
    e2 = hist.record(tid, ChangeType.ITEM_STATUS, "状态变更 B", member_id="m1")
    entries = hist.list_entries(tid)
    ids = [e.id for e in entries]
    assert e1.id in ids and e2.id in ids
    # 倒序（最近优先）
    assert entries[0].id == e2.id
    assert e1.diffs == []


def test_record_with_diffs(trips: TripService, hist: HistoryService):
    tid = _new_trip(trips)
    hist.record(
        tid,
        ChangeType.ITEM_UPDATE,
        "改费用",
        diffs=[FieldDiff(field="cost", before="100", after="250")],
    )
    e = hist.list_entries(tid)[0]
    assert e.diffs[0].field == "cost"
    assert e.diffs[0].before == "100" and e.diffs[0].after == "250"


def test_updates_since(trips: TripService, hist: HistoryService):
    tid = _new_trip(trips)
    first = hist.record(tid, ChangeType.ITEM_CREATE, "第一")
    second = hist.record(tid, ChangeType.ITEM_CREATE, "第二")
    # since 取一个早于两条记录的过去时间：应能取到全部两条
    got = hist.updates_since(tid, since="2000-01-01T00:00:00+00:00")
    assert {e.id for e in got} >= {first.id, second.id}
    # since=None 返回最近记录（含两条）
    all_entries = hist.updates_since(tid, since=None)
    assert {e.id for e in all_entries} >= {first.id, second.id}


def test_snapshot_and_revisions(trips: TripService, hist: HistoryService):
    tid = _new_trip(trips)
    snap = {
        "days": [{"id": "d1", "day_index": 1}],
        "items": [{"id": "i1", "title": "A"}, {"id": "i2", "title": "B"}],
        "transports": [],
    }
    rev = hist.snapshot(tid, "手动快照", snap, member_id="m1")
    revs = hist.list_revisions(tid)
    assert revs[0].id == rev.id
    assert revs[0].label == "手动快照"
    assert len(revs[0].snapshot["items"]) == 2
    assert revs[0].snapshot["days"][0]["day_index"] == 1
