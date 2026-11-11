"""阶段 D：对话编排（Orchestrator）测试（内存库 + FakeGeoAdapter + FakeLLM，不联网）。

覆盖：
- FakeLLM(canned=无 operations 的 JSON)：handle 返回 mode='chat'，不产生提案。
- FakeLLM 返回带 operations 的 JSON：生成 1 个 pending 提案，未执行（库存量不变）。
- FakeLLM 返回非法 JSON / 缺字段：mode='invalid'，不落任何提案。
- 含「冲突」关键词的消息走确定性 explain 路径（mode='explain'），不调用 LLM。
- execute 编排兼容裸 op 形态 {"op": "add_need", ...}。
- 非成员 handle 抛 Forbidden / 经 API 返回 403。
"""

from __future__ import annotations

import json

import pytest

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.ai.explain import Explainer
from fairmeet.ai.orchestrator import FakeLLM, Orchestrator
from fairmeet.ai.proposal import ProposalService
from fairmeet.ai.sources import SourceRegistry
from fairmeet.ai.tools import Toolbox
from fairmeet.checks import CheckService
from fairmeet.db import Database
from fairmeet.domain import TripCreate
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


def _build_orchestrator(db, trips, itin, checks, llm):
    def factory(trip_id: str, member_id: str) -> Toolbox:
        return Toolbox(trips, itin, checks, trip_id, member_id)

    proposals = ProposalService(db, factory)
    explainer = Explainer(trips, checks)
    sources = SourceRegistry(db)
    return Orchestrator(trips, proposals, explainer, sources, llm), proposals


# ---------------------------------------------------------------------------
# chat：无 operations
# ---------------------------------------------------------------------------
def test_chat_without_operations(db, trips, itin, checks, trip_and_owner):
    trip, owner = trip_and_owner
    canned = json.dumps(
        {"reply": "请补充出行人数。", "clarification_questions": ["几个人？"], "operations": [], "sources": []}
    )
    llm = FakeLLM(canned=canned)
    orch, proposals = _build_orchestrator(db, trips, itin, checks, llm)

    result = orch.handle(trip.id, owner.id, "帮我规划一下")
    assert result["mode"] == "chat"
    assert result["reply"] == "请补充出行人数。"
    assert result["proposals"] == []
    assert proposals.list(trip.id) == []  # 无提案


# ---------------------------------------------------------------------------
# chat：带 operations -> 生成 pending 提案但未执行
# ---------------------------------------------------------------------------
def test_chat_with_operations_creates_pending_not_executed(db, trips, itin, checks, trip_and_owner):
    trip, owner = trip_and_owner
    before = len(trips.list_needs(trip.id))
    canned = json.dumps(
        {
            "reply": "我建议加一条硬约束。",
            "clarification_questions": [],
            "operations": [
                {"operation": {"op": "add_need", "kind": "hard", "category": "预算", "value": "3000 元"}}
            ],
            "sources": [],
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(canned=canned)
    orch, proposals = _build_orchestrator(db, trips, itin, checks, llm)

    result = orch.handle(trip.id, owner.id, "把预算定为 3000")
    assert result["mode"] == "chat"
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["status"] == "pending"

    # 落库为 pending，但未执行：需求数量不变
    assert len(proposals.list(trip.id)) == 1
    assert len(trips.list_needs(trip.id)) == before


# ---------------------------------------------------------------------------
# invalid：非法 JSON / 缺字段
# ---------------------------------------------------------------------------
def test_malformed_json_returns_invalid_no_proposal(db, trips, itin, checks, trip_and_owner):
    """非法 JSON 应返回 mode='invalid' 且不落提案，而非抛异常（已修复 JSONDecodeError 捕获）。"""
    trip, owner = trip_and_owner
    llm = FakeLLM(canned="这不是 JSON {{{")
    orch, proposals = _build_orchestrator(db, trips, itin, checks, llm)

    result = orch.handle(trip.id, owner.id, "随便说点什么")
    assert result["mode"] == "invalid"
    assert result["proposals"] == []
    assert proposals.list(trip.id) == []  # 不落任何提案


def test_schema_invalid_json_returns_invalid_no_proposal(db, trips, itin, checks, trip_and_owner):
    """合法 JSON 但违反 AIResponse schema（如 reply 类型错误）-> mode='invalid' 且不落提案。"""
    trip, owner = trip_and_owner
    # 能解析为 JSON，但 reply 不是字符串 -> ValidationError
    llm = FakeLLM(canned=json.dumps({"reply": 123, "operations": [], "sources": []}))
    orch, proposals = _build_orchestrator(db, trips, itin, checks, llm)

    result = orch.handle(trip.id, owner.id, "随便说点什么")
    assert result["mode"] == "invalid"
    assert result["proposals"] == []
    assert proposals.list(trip.id) == []  # 不落任何提案


def test_missing_reply_field_returns_invalid(db, trips, itin, checks, trip_and_owner):
    trip, owner = trip_and_owner
    # 缺 reply 字段（reply 必填）
    canned = json.dumps({"clarification_questions": [], "operations": [], "sources": []})
    llm = FakeLLM(canned=canned)
    orch, proposals = _build_orchestrator(db, trips, itin, checks, llm)

    result = orch.handle(trip.id, owner.id, "你好")
    assert result["mode"] == "invalid"
    assert proposals.list(trip.id) == []


# ---------------------------------------------------------------------------
# explain 确定性路径：不调用 LLM
# ---------------------------------------------------------------------------
def test_conflict_keyword_uses_explain_without_llm(db, trips, itin, checks, trip_and_owner):
    trip, owner = trip_and_owner
    llm = FakeLLM(canned=json.dumps({"reply": "不应被调用"}))
    orch, _proposals = _build_orchestrator(db, trips, itin, checks, llm)
    calls_before = len(llm.calls)

    result = orch.handle(trip.id, owner.id, "帮我检查一下有没有冲突")
    assert result["mode"] == "explain"
    assert "hard_conflicts" in result
    # LLM 未被调用
    assert len(llm.calls) == calls_before


# ---------------------------------------------------------------------------
# 裸 op 形态兼容
# ---------------------------------------------------------------------------
def test_bare_op_form_supported(db, trips, itin, checks, trip_and_owner):
    trip, owner = trip_and_owner
    canned = json.dumps(
        {
            "reply": "加一条偏好。",
            "operations": [
                {"op": "add_need", "kind": "preference", "category": "饮食", "value": "喜欢川菜"}
            ],
            "sources": [],
        },
        ensure_ascii=False,
    )
    llm = FakeLLM(canned=canned)
    orch, proposals = _build_orchestrator(db, trips, itin, checks, llm)

    result = orch.handle(trip.id, owner.id, "我们都可以吃辣")
    assert result["mode"] == "chat"
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["status"] == "pending"


# ---------------------------------------------------------------------------
# 非成员
# ---------------------------------------------------------------------------
def test_non_member_handle_raises_forbidden(db, trips, itin, checks, trip_and_owner):
    trip, _owner = trip_and_owner
    llm = FakeLLM()
    orch, _proposals = _build_orchestrator(db, trips, itin, checks, llm)

    with pytest.raises(Forbidden):
        orch.handle(trip.id, "not-a-member", "帮我规划")


def test_non_member_via_api_returns_403(db, trips, itin, checks, trip_and_owner):
    from fastapi.testclient import TestClient

    from fairmeet.app import create_app

    trip, _owner = trip_and_owner
    # 用同一个 db 构造 app，复用已有旅程
    app = create_app(db=db, geo=FakeGeoAdapter(), llm=FakeLLM())
    client = TestClient(app)

    r = client.post(
        f"/api/trips/{trip.id}/ai/chat?member_id=bogus",
        json={"message": "帮我规划"},
    )
    assert r.status_code == 403
