"""阶段 B 服务层测试（纯本地，不联网）。

覆盖：
- 创建旅程生成不可猜邀请令牌，owner 为发起人
- 凭令牌加入成员
- 需求区分硬约束/偏好/可协商并持久化
- 灵感手动/粘贴链接入库，带来源与状态；带地点名自动入地点库
- 授权规则：非成员不得读写他旅程数据
"""

from __future__ import annotations

import pytest

from fairmeet.db import Database
from fairmeet.domain import (
    InspirationCreate,
    InspirationStatus,
    MemberJoin,
    NeedCreate,
    NeedKind,
    PlaceCreate,
    TripCreate,
)
from fairmeet.services import Forbidden, NotFound, TripService


@pytest.fixture()
def service() -> TripService:
    return TripService(Database(":memory:"))


def test_create_trip_generates_token_and_owner(service: TripService):
    trip, owner = service.create_trip(TripCreate(title="成都三日游", owner_name="小林"))
    assert trip.title == "成都三日游"
    assert trip.destination == "成都"
    assert len(trip.invite_token) >= 32  # 不可猜
    assert trip.created_by == owner.id
    assert owner.is_owner is True
    assert len(service.list_members(trip.id)) == 1


def test_invite_tokens_are_unique(service: TripService):
    t1, _ = service.create_trip(TripCreate(title="A"))
    t2, _ = service.create_trip(TripCreate(title="B"))
    assert t1.invite_token != t2.invite_token


def test_join_by_token(service: TripService):
    trip, _ = service.create_trip(TripCreate(title="成都三日游"))
    resolved = service.get_trip_by_token(trip.invite_token)
    assert resolved.id == trip.id
    member = service.add_member(trip.id, MemberJoin(display_name="阿May"))
    assert member.status.value == "joined"
    assert len(service.list_members(trip.id)) == 2


def test_invalid_token_raises(service: TripService):
    with pytest.raises(NotFound):
        service.get_trip_by_token("not-a-real-token")


def test_needs_distinguish_kind(service: TripService):
    trip, owner = service.create_trip(TripCreate(title="成都三日游"))
    service.add_need(trip.id, owner.id, NeedCreate(kind=NeedKind.HARD, category="出发日期", value="周日必须出发"))
    service.add_need(trip.id, owner.id, NeedCreate(kind=NeedKind.PREFERENCE, category="饮食", value="喜欢川菜"))
    service.add_need(trip.id, owner.id, NeedCreate(kind=NeedKind.NEGOTIABLE, category="住宿", value="可接受青旅"))
    needs = service.list_needs(trip.id)
    kinds = {n.kind for n in needs}
    assert kinds == {NeedKind.HARD, NeedKind.PREFERENCE, NeedKind.NEGOTIABLE}


def test_non_member_cannot_add_need(service: TripService):
    trip, _ = service.create_trip(TripCreate(title="成都三日游"))
    with pytest.raises(Forbidden):
        service.add_need(trip.id, "someone-else", NeedCreate(kind=NeedKind.HARD, category="x", value="y"))


def test_cross_trip_authorization_rejected(service: TripService):
    trip_a, owner_a = service.create_trip(TripCreate(title="A"))
    trip_b, _ = service.create_trip(TripCreate(title="B"))
    # owner_a 不能写 trip_b
    with pytest.raises(Forbidden):
        service.add_need(trip_b.id, owner_a.id, NeedCreate(kind=NeedKind.HARD, category="x", value="y"))


def test_inspiration_with_place_url_and_note(service: TripService):
    trip, owner = service.create_trip(TripCreate(title="成都三日游"))
    insp = service.add_inspiration(
        trip.id,
        owner.id,
        InspirationCreate(
            title="宽窄巷子攻略",
            note="周末人多，建议工作日去",
            source_url="https://example.com/xhs/123",
            place_name="宽窄巷子",
        ),
    )
    assert insp.status == InspirationStatus.PENDING
    assert insp.source_url == "https://example.com/xhs/123"
    assert insp.place_id is not None
    places = service.list_places(trip.id)
    assert len(places) == 1
    assert places[0].name == "宽窄巷子"


def test_inspiration_status_transition(service: TripService):
    trip, owner = service.create_trip(TripCreate(title="成都三日游"))
    insp = service.add_inspiration(trip.id, owner.id, InspirationCreate(title="某地点"))
    updated = service.set_inspiration_status(trip.id, owner.id, insp.id, InspirationStatus.VERIFIED)
    assert updated.status == InspirationStatus.VERIFIED


def test_manual_place_without_coordinates(service: TripService):
    trip, owner = service.create_trip(TripCreate(title="成都三日游"))
    place = service.add_place(trip.id, owner.id, PlaceCreate(name="春熙路"))
    assert place.lng is None and place.lat is None
    assert place.source == "manual"
