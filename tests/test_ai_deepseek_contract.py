"""DeepSeek 集成契约测试：验证「真实模型返回合法操作」时 orchestrator 能正确解析为提案。

不联网：用 FakeLLM 模拟 DeepSeek 返回的合法 JSON（覆盖 prompt 修复后的真实形态），
确保操作解析、提案生成、确认执行链路正确。
"""

from __future__ import annotations

import json

from fairmeet.ai.orchestrator import FakeLLM, Orchestrator
from fairmeet.ai.proposal import ProposalService
from fairmeet.ai.sources import SourceRegistry
from fairmeet.ai.tools import Toolbox
from fairmeet.domain import DayCreate, TripCreate


def _build(db, trips, itin, checks):
    src = SourceRegistry(db)

    def factory(tid, mid):
        return Toolbox(trips, itin, checks, tid, mid)

    props = ProposalService(db, factory)
    return props, src


def test_deepseek_style_add_item_parsed_to_pending_proposal(db, trips, itin, checks):
    trip, owner = trips.create_trip(TripCreate(title="成都两日游", budget=3000))
    day = itin.add_day(trip.id, owner.id, DayCreate(day_index=1))

    canned = json.dumps(
        {
            "reply": "已生成午餐提案，需确认后生效。",
            "clarification_questions": [],
            "operations": [
                {
                    "operation": {
                        "op": "add_item",
                        "day_id": day.id,
                        "item_type": "meal",
                        "title": "川菜午餐",
                        "cost": 120,
                        "cost_provenance_kind": "estimated",
                    }
                }
            ],
            "sources": [],
        },
        ensure_ascii=False,
    )
    props, src = _build(db, trips, itin, checks)
    from fairmeet.ai.explain import Explainer

    orch = Orchestrator(trips, props, Explainer(trips, checks), src, FakeLLM(canned=canned), itinerary=itin)
    out = orch.handle(trip.id, owner.id, "帮我加午餐")
    assert out["mode"] == "chat"
    assert len(out["proposals"]) == 1
    p = out["proposals"][0]
    assert p["requires_confirmation"] is True
    # 未执行
    assert len(itin.list_items(trip.id)) == 0
    # 确认后执行
    res = props.confirm(p["id"], owner.id)
    assert len(res["executed"]) == 1
    items = itin.list_items(trip.id)
    assert len(items) == 1
    assert items[0].title == "川菜午餐" and items[0].cost == 120.0


def test_self_invented_op_is_rejected_safely(db, trips, itin, checks):
    """模拟模型自造 op（如 add_activity）→ 校验失败 mode=invalid、不落提案、无写入。"""
    trip, owner = trips.create_trip(TripCreate(title="成都两日游"))
    canned = json.dumps(
        {
            "reply": "ok",
            "clarification_questions": [],
            "operations": [{"operation": {"op": "add_activity", "title": "x"}}],
            "sources": [],
        },
        ensure_ascii=False,
    )
    props, src = _build(db, trips, itin, checks)
    from fairmeet.ai.explain import Explainer

    orch = Orchestrator(trips, props, Explainer(trips, checks), src, FakeLLM(canned=canned), itinerary=itin)
    out = orch.handle(trip.id, owner.id, "x")
    assert out["mode"] == "invalid"
    assert out["proposals"] == []
    assert props.list(trip.id) == []
