"""阶段 C：逐日行程服务。

职责：
- 创建/查询 逐日行程（ItineraryDay）、行程条目（ItineraryItem）、交通段（TransportSegment）。
- 条目状态流转（草案/待讨论/待确认/已确认/已完成/已取消替换）。
- **局部调整不无故覆盖其他已确认项**：本服务只改被指定的行；状态流转独立端点。
- 复用阶段 B 的授权规则（require_member）——所有写操作先校验成员归属。
- 通过注入的 GeoAdapter 计算交通段（外部不可用时降级为估算/未知，不崩溃）。

数据须区分来源：费用/时长/路线均带 Provenance（verified/external/estimated/unknown）。
"""

from __future__ import annotations

import sqlite3
import uuid

from fairmeet.adapters import GeoAdapter, GeoPoint, GeoUnavailable
from fairmeet.db import Database
from fairmeet.domain import (
    DayCreate,
    ItemCreate,
    ItemStatus,
    ItemType,
    ItemUpdate,
    ItineraryDay,
    ItineraryItem,
    Provenance,
    ProvenanceKind,
    TransportCreate,
    TransportMode,
    TransportSegment,
)
from fairmeet.services import Forbidden, NotFound, TripService


def _nid() -> str:
    return uuid.uuid4().hex


def _prov(kind: str, source: str | None, fetched: str | None) -> Provenance:
    return Provenance(kind=ProvenanceKind(kind), source=source, fetched_at=fetched)


class ItineraryService:
    def __init__(self, db: Database, trips: TripService | None = None, geo: GeoAdapter | None = None) -> None:
        self.db = db
        self.trips = trips or TripService(db)
        self.geo = geo

    # ---- 授权（复用阶段 B） ----
    def _require(self, trip_id: str, member_id: str | None) -> None:
        self.trips.require_member(trip_id, member_id)

    # ==================================================================
    # 天
    # ==================================================================
    def add_day(self, trip_id: str, member_id: str, payload: DayCreate) -> ItineraryDay:
        self._require(trip_id, member_id)
        day = ItineraryDay(
            id=_nid(),
            trip_id=trip_id,
            day_index=payload.day_index,
            date=payload.date,
            title=payload.title,
        )
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "INSERT INTO itinerary_days(id,trip_id,day_index,date,title,created_at) VALUES(?,?,?,?,?,?)",
                    (day.id, day.trip_id, day.day_index, day.date, day.title, day.created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise Forbidden("该旅程中此天序号已存在") from exc
        return day

    def list_days(self, trip_id: str) -> list[ItineraryDay]:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM itinerary_days WHERE trip_id=? ORDER BY day_index", (trip_id,))
            return [self._row_to_day(r) for r in cur.fetchall()]

    def get_day(self, trip_id: str, day_id: str) -> ItineraryDay:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM itinerary_days WHERE id=? AND trip_id=?", (day_id, trip_id))
            r = cur.fetchone()
        if r is None:
            raise NotFound("行程天不存在")
        return self._row_to_day(r)

    @staticmethod
    def _row_to_day(r: sqlite3.Row) -> ItineraryDay:
        return ItineraryDay(
            id=r["id"],
            trip_id=r["trip_id"],
            day_index=r["day_index"],
            date=r["date"],
            title=r["title"],
            created_at=r["created_at"],
        )

    # ==================================================================
    # 条目
    # ==================================================================
    def add_item(self, trip_id: str, member_id: str, day_id: str, payload: ItemCreate) -> ItineraryItem:
        self._require(trip_id, member_id)
        self.get_day(trip_id, day_id)  # 校验 day 属于该 trip
        # 计算 sort_order：排在当天最后一个之后
        with self.db.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM itinerary_items WHERE day_id=?", (day_id,))
            next_order = int(cur.fetchone()["m"]) + 1
        item = ItineraryItem(
            id=_nid(),
            trip_id=trip_id,
            day_id=day_id,
            item_type=payload.item_type,
            title=payload.title,
            place_id=payload.place_id,
            start_time=payload.start_time,
            end_time=payload.end_time,
            stay_minutes=payload.stay_minutes,
            cost=payload.cost,
            cost_provenance=Provenance(kind=payload.cost_provenance_kind, source="manual", fetched_at=None),
            status=ItemStatus.DRAFT,
            note=payload.note,
            sort_order=next_order,
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO itinerary_items("
                "id,trip_id,day_id,item_type,title,place_id,start_time,end_time,stay_minutes,cost,"
                "cost_provenance_kind,cost_provenance_source,cost_provenance_fetched_at,status,note,sort_order,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item.id,
                    item.trip_id,
                    item.day_id,
                    item.item_type.value,
                    item.title,
                    item.place_id,
                    item.start_time,
                    item.end_time,
                    item.stay_minutes,
                    item.cost,
                    item.cost_provenance.kind.value,
                    item.cost_provenance.source,
                    item.cost_provenance.fetched_at,
                    item.status.value,
                    item.note,
                    item.sort_order,
                    item.created_at,
                ),
            )
        return item

    def update_item(self, trip_id: str, member_id: str, item_id: str, payload: ItemUpdate) -> ItineraryItem:
        """局部更新条目字段（仅改被指定的列）。不触碰其他条目。"""
        self._require(trip_id, member_id)
        current = self.get_item(trip_id, item_id)
        data = payload.model_dump(exclude_unset=True)
        if not data:
            return current
        sets: list[str] = []
        args: list[object] = []
        for key, value in data.items():
            if key == "cost_provenance_kind":
                sets.append("cost_provenance_kind=?")
                args.append(value.value if isinstance(value, ProvenanceKind) else value)
            else:
                sets.append(f"{key}=?")
                args.append(value)
        args.extend([item_id, trip_id])
        with self.db.cursor() as cur:
            cur.execute(f"UPDATE itinerary_items SET {', '.join(sets)} WHERE id=? AND trip_id=?", args)
        return self.get_item(trip_id, item_id)

    _ALLOWED_TRANSITIONS: dict[ItemStatus, set[ItemStatus]] = {
        ItemStatus.DRAFT: {ItemStatus.DISCUSSING, ItemStatus.CANCELLED},
        ItemStatus.DISCUSSING: {ItemStatus.PENDING, ItemStatus.DRAFT, ItemStatus.CANCELLED},
        ItemStatus.PENDING: {ItemStatus.CONFIRMED, ItemStatus.DISCUSSING, ItemStatus.CANCELLED},
        ItemStatus.CONFIRMED: {ItemStatus.DONE, ItemStatus.CANCELLED},
        ItemStatus.DONE: set(),
        ItemStatus.CANCELLED: set(),
    }

    def set_item_status(self, trip_id: str, member_id: str, item_id: str, status: ItemStatus) -> ItineraryItem:
        self._require(trip_id, member_id)
        current = self.get_item(trip_id, item_id)
        if status not in self._ALLOWED_TRANSITIONS[current.status]:
            raise Forbidden(f"不允许从 {current.status.value} 流转到 {status.value}")
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE itinerary_items SET status=? WHERE id=? AND trip_id=?",
                (status.value, item_id, trip_id),
            )
        return self.get_item(trip_id, item_id)

    def get_item(self, trip_id: str, item_id: str) -> ItineraryItem:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM itinerary_items WHERE id=? AND trip_id=?", (item_id, trip_id))
            r = cur.fetchone()
        if r is None:
            raise NotFound("行程条目不存在")
        return self._row_to_item(r)

    def list_items(self, trip_id: str, day_id: str | None = None) -> list[ItineraryItem]:
        with self.db.cursor() as cur:
            if day_id:
                cur.execute(
                    "SELECT * FROM itinerary_items WHERE trip_id=? AND day_id=? ORDER BY sort_order",
                    (trip_id, day_id),
                )
            else:
                cur.execute(
                    "SELECT * FROM itinerary_items WHERE trip_id=? ORDER BY day_id, sort_order", (trip_id,)
                )
            return [self._row_to_item(r) for r in cur.fetchall()]

    @staticmethod
    def _row_to_item(r: sqlite3.Row) -> ItineraryItem:
        return ItineraryItem(
            id=r["id"],
            trip_id=r["trip_id"],
            day_id=r["day_id"],
            item_type=ItemType(r["item_type"]),
            title=r["title"],
            place_id=r["place_id"],
            start_time=r["start_time"],
            end_time=r["end_time"],
            stay_minutes=r["stay_minutes"],
            cost=r["cost"],
            cost_provenance=_prov(
                r["cost_provenance_kind"], r["cost_provenance_source"], r["cost_provenance_fetched_at"]
            ),
            status=ItemStatus(r["status"]),
            note=r["note"],
            sort_order=r["sort_order"],
            created_at=r["created_at"],
        )

    # ==================================================================
    # 交通段
    # ==================================================================
    def add_transport(
        self,
        trip_id: str,
        member_id: str,
        day_id: str,
        payload: TransportCreate,
        compute: bool = False,
    ) -> TransportSegment:
        """新增交通段。compute=True 时尝试用适配层计算时长；外部不可用则降级为 unknown。"""
        self._require(trip_id, member_id)
        self.get_day(trip_id, day_id)
        duration = payload.duration_minutes
        prov = _prov(payload.duration_provenance_kind.value, "manual", None)

        if compute and duration is None and self.geo is not None:
            duration, prov = self._compute_duration(trip_id, day_id, payload)

        seg = TransportSegment(
            id=_nid(),
            trip_id=trip_id,
            day_id=day_id,
            from_item_id=payload.from_item_id,
            to_item_id=payload.to_item_id,
            mode=payload.mode,
            duration_minutes=duration,
            duration_provenance=prov,
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO transport_segments("
                "id,trip_id,day_id,from_item_id,to_item_id,mode,duration_minutes,"
                "duration_provenance_kind,duration_provenance_source,duration_provenance_fetched_at,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seg.id,
                    seg.trip_id,
                    seg.day_id,
                    seg.from_item_id,
                    seg.to_item_id,
                    seg.mode.value,
                    seg.duration_minutes,
                    seg.duration_provenance.kind.value,
                    seg.duration_provenance.source,
                    seg.duration_provenance.fetched_at,
                    seg.created_at,
                ),
            )
        return seg

    def _compute_duration(
        self, trip_id: str, day_id: str, payload: TransportCreate
    ) -> tuple[int | None, Provenance]:
        """尝试通过适配层计算两个条目所在地点之间的时长；失败降级。"""
        assert self.geo is not None
        try:
            fromp = self._item_point(trip_id, payload.from_item_id)
            top = self._item_point(trip_id, payload.to_item_id)
            if fromp is None or top is None:
                return None, Provenance(kind=ProvenanceKind.UNKNOWN, source="no-place-coords")
            route = self.geo.route(fromp, top, payload.mode)
            if route.duration_minutes is None:
                return None, Provenance(kind=ProvenanceKind.UNKNOWN, source=route.source)
            return int(round(route.duration_minutes)), Provenance(kind=route.provenance, source=route.source)
        except GeoUnavailable as exc:
            return None, Provenance(kind=ProvenanceKind.UNKNOWN, source=f"unavailable:{exc}")

    def _item_point(self, trip_id: str, item_id: str | None) -> GeoPoint | None:
        if not item_id:
            return None
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT p.lng AS lng, p.lat AS lat FROM itinerary_items i "
                "JOIN places p ON p.id = i.place_id "
                "WHERE i.id=? AND i.trip_id=?",
                (item_id, trip_id),
            )
            r = cur.fetchone()
        if r is None or r["lng"] is None or r["lat"] is None:
            return None
        return GeoPoint(lng=r["lng"], lat=r["lat"])

    def list_transports(self, trip_id: str, day_id: str | None = None) -> list[TransportSegment]:
        with self.db.cursor() as cur:
            if day_id:
                cur.execute("SELECT * FROM transport_segments WHERE trip_id=? AND day_id=?", (trip_id, day_id))
            else:
                cur.execute("SELECT * FROM transport_segments WHERE trip_id=?", (trip_id,))
            return [self._row_to_seg(r) for r in cur.fetchall()]

    @staticmethod
    def _row_to_seg(r: sqlite3.Row) -> TransportSegment:
        return TransportSegment(
            id=r["id"],
            trip_id=r["trip_id"],
            day_id=r["day_id"],
            from_item_id=r["from_item_id"],
            to_item_id=r["to_item_id"],
            mode=TransportMode(r["mode"]),
            duration_minutes=r["duration_minutes"],
            duration_provenance=_prov(
                r["duration_provenance_kind"], r["duration_provenance_source"], r["duration_provenance_fetched_at"]
            ),
            created_at=r["created_at"],
        )
