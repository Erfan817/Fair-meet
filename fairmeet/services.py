"""FairMeet 业务服务层（阶段 B）。

封装旅程/成员/需求/灵感/地点的业务规则。API 层与模板层都通过这里访问数据，
保证「AI 或任何调用方都不得绕过校验直接写库」的纪律（后续阶段 D 的受控工具也复用此处）。

关键规则：
- 每个旅程有不可猜的 invite_token；成员凭 token 加入。
- 访问某旅程的数据必须证明授权：owner 或已加入该旅程的成员（以 member_id 校验）。
- 硬约束（NeedKind.HARD）不允许被静默改写；查询时区分展示以便显式报冲突。
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid

from fairmeet.db import Database
from fairmeet.domain import (
    Inspiration,
    InspirationCreate,
    InspirationStatus,
    Member,
    MemberJoin,
    MemberStatus,
    Need,
    NeedCreate,
    NeedKind,
    Place,
    PlaceCreate,
    Trip,
    TripCreate,
    TripStatus,
)


class FairMeetError(Exception):
    """业务错误基类。"""


class NotFound(FairMeetError):
    pass


class Forbidden(FairMeetError):
    pass


def _new_id() -> str:
    return uuid.uuid4().hex


def _new_token() -> str:
    # 不可猜：32 字节 URL 安全随机串（43 字符）
    return secrets.token_urlsafe(32)


def _row_to_trip(row: sqlite3.Row) -> Trip:
    return Trip(
        id=row["id"],
        title=row["title"],
        destination=row["destination"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        budget=row["budget"],
        status=TripStatus(row["status"]),
        invite_token=row["invite_token"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _row_to_member(row: sqlite3.Row) -> Member:
    return Member(
        id=row["id"],
        trip_id=row["trip_id"],
        display_name=row["display_name"],
        status=MemberStatus(row["status"]),
        is_owner=bool(row["is_owner"]),
        created_at=row["created_at"],
    )


class TripService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- 创建旅程 ----
    def create_trip(self, payload: TripCreate) -> tuple[Trip, Member]:
        trip_id = _new_id()
        member_id = _new_id()
        token = _new_token()
        trip = Trip(
            id=trip_id,
            title=payload.title,
            destination=payload.destination,
            start_date=payload.start_date,
            end_date=payload.end_date,
            budget=payload.budget,
            status=TripStatus.DRAFT,
            invite_token=token,
            created_by=member_id,
        )
        owner = Member(
            id=member_id,
            trip_id=trip_id,
            display_name=payload.owner_name,
            status=MemberStatus.JOINED,
            is_owner=True,
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO trips("
                "id,title,destination,start_date,end_date,budget,status,invite_token,created_by,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    trip.id,
                    trip.title,
                    trip.destination,
                    trip.start_date,
                    trip.end_date,
                    trip.budget,
                    trip.status.value,
                    trip.invite_token,
                    trip.created_by,
                    trip.created_at,
                ),
            )
            cur.execute(
                "INSERT INTO members(id,trip_id,display_name,status,is_owner,created_at) VALUES(?,?,?,?,?,?)",
                (owner.id, owner.trip_id, owner.display_name, owner.status.value, 1, owner.created_at),
            )
        return trip, owner

    def get_trip(self, trip_id: str) -> Trip:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM trips WHERE id=?", (trip_id,))
            row = cur.fetchone()
        if row is None:
            raise NotFound("旅程不存在")
        return _row_to_trip(row)

    def get_trip_by_token(self, token: str) -> Trip:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM trips WHERE invite_token=?", (token,))
            row = cur.fetchone()
        if row is None:
            raise NotFound("邀请链接无效")
        return _row_to_trip(row)

    # ---- 授权校验 ----
    def require_member(self, trip_id: str, member_id: str | None) -> Member:
        """断言 member_id 属于该旅程，否则拒绝。用于所有旅程内数据访问。"""
        if not member_id:
            raise Forbidden("缺少成员身份，请先通过邀请链接加入")
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM members WHERE id=? AND trip_id=?", (member_id, trip_id))
            row = cur.fetchone()
        if row is None or row["status"] != MemberStatus.JOINED.value:
            raise Forbidden("无权访问该旅程，请确认成员仍已加入")
        return _row_to_member(row)

    # ---- 成员 ----
    def add_member(self, trip_id: str, payload: MemberJoin) -> Member:
        self.get_trip(trip_id)  # 确保旅程存在
        member = Member(
            id=_new_id(),
            trip_id=trip_id,
            display_name=payload.display_name,
            status=MemberStatus.JOINED,
            is_owner=False,
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO members(id,trip_id,display_name,status,is_owner,created_at) VALUES(?,?,?,?,?,?)",
                (member.id, member.trip_id, member.display_name, member.status.value, 0, member.created_at),
            )
        return member

    def list_members(self, trip_id: str) -> list[Member]:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM members WHERE trip_id=? ORDER BY created_at", (trip_id,))
            return [_row_to_member(r) for r in cur.fetchall()]

    # ---- 需求 ----
    def add_need(self, trip_id: str, member_id: str, payload: NeedCreate) -> Need:
        self.require_member(trip_id, member_id)
        need = Need(
            id=_new_id(),
            member_id=member_id,
            trip_id=trip_id,
            kind=payload.kind,
            category=payload.category,
            value=payload.value,
            source=payload.source,
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO needs(id,member_id,trip_id,kind,category,value,source,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    need.id,
                    need.member_id,
                    need.trip_id,
                    need.kind.value,
                    need.category,
                    need.value,
                    need.source,
                    need.created_at,
                ),
            )
        return need

    def list_needs(self, trip_id: str) -> list[Need]:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM needs WHERE trip_id=? ORDER BY created_at", (trip_id,))
            return [
                Need(
                    id=r["id"],
                    member_id=r["member_id"],
                    trip_id=r["trip_id"],
                    kind=NeedKind(r["kind"]),
                    category=r["category"],
                    value=r["value"],
                    source=r["source"],
                    created_at=r["created_at"],
                )
                for r in cur.fetchall()
            ]

    # ---- 地点 ----
    def add_place(self, trip_id: str, member_id: str, payload: PlaceCreate) -> Place:
        self.require_member(trip_id, member_id)
        place = Place(
            id=_new_id(),
            trip_id=trip_id,
            name=payload.name,
            category=payload.category,
            lng=payload.lng,
            lat=payload.lat,
            source=payload.source or "manual",
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO places(id,trip_id,name,category,lng,lat,source,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    place.id,
                    place.trip_id,
                    place.name,
                    place.category,
                    place.lng,
                    place.lat,
                    place.source,
                    place.created_at,
                ),
            )
        return place

    def list_places(self, trip_id: str) -> list[Place]:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM places WHERE trip_id=? ORDER BY created_at", (trip_id,))
            return [
                Place(
                    id=r["id"],
                    trip_id=r["trip_id"],
                    name=r["name"],
                    category=r["category"],
                    lng=r["lng"],
                    lat=r["lat"],
                    source=r["source"],
                    created_at=r["created_at"],
                )
                for r in cur.fetchall()
            ]

    # ---- 灵感箱 ----
    def add_inspiration(self, trip_id: str, member_id: str, payload: InspirationCreate) -> Inspiration:
        self.require_member(trip_id, member_id)
        place_id = payload.place_id
        if place_id:
            with self.db.cursor() as cur:
                cur.execute("SELECT id FROM places WHERE id=? AND trip_id=?", (place_id, trip_id))
                if cur.fetchone() is None:
                    raise Forbidden("地点不属于该旅程")
        if payload.place_name:
            place = self.add_place(
                trip_id, member_id, PlaceCreate(name=payload.place_name, source=payload.source_url or "manual")
            )
            place_id = place.id
        insp = Inspiration(
            id=_new_id(),
            trip_id=trip_id,
            member_id=member_id,
            title=payload.title,
            note=payload.note,
            source_url=payload.source_url,
            source=payload.source,
            place_id=place_id,
            # 阶段 B 未接外部解析：手动/粘贴链接均视为「待确认」
            status=InspirationStatus.PENDING,
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO inspirations(id,trip_id,member_id,title,note,source_url,source,place_id,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    insp.id,
                    insp.trip_id,
                    insp.member_id,
                    insp.title,
                    insp.note,
                    insp.source_url,
                    insp.source,
                    insp.place_id,
                    insp.status.value,
                    insp.created_at,
                ),
            )
        return insp

    def list_inspirations(self, trip_id: str) -> list[Inspiration]:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM inspirations WHERE trip_id=? ORDER BY created_at", (trip_id,))
            return [
                Inspiration(
                    id=r["id"],
                    trip_id=r["trip_id"],
                    member_id=r["member_id"],
                    title=r["title"],
                    note=r["note"],
                    source_url=r["source_url"],
                    source=r["source"],
                    place_id=r["place_id"],
                    status=InspirationStatus(r["status"]),
                    created_at=r["created_at"],
                )
                for r in cur.fetchall()
            ]

    def set_inspiration_status(
        self, trip_id: str, member_id: str, inspiration_id: str, status: InspirationStatus
    ) -> Inspiration:
        self.require_member(trip_id, member_id)
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE inspirations SET status=? WHERE id=? AND trip_id=?",
                (status.value, inspiration_id, trip_id),
            )
            if cur.rowcount == 0:
                raise NotFound("灵感不存在")
            cur.execute("SELECT * FROM inspirations WHERE id=?", (inspiration_id,))
            r = cur.fetchone()
        return Inspiration(
            id=r["id"],
            trip_id=r["trip_id"],
            member_id=r["member_id"],
            title=r["title"],
            note=r["note"],
            source_url=r["source_url"],
            source=r["source"],
            place_id=r["place_id"],
            status=InspirationStatus(r["status"]),
            created_at=r["created_at"],
        )
