"""阶段 D：AI 结构化 I/O 契约测试（纯 Pydantic 校验，不联网）。

覆盖：
- ProposedOperationEnvelope 按 op 判别还原各具体操作；非法 op 抛 ValidationError。
- AIResponse 最小合法构造；缺 reply 抛 ValidationError。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fairmeet.ai.contracts import (
    AIResponse,
    OpAddDay,
    OpAddInspiration,
    OpAddItem,
    OpAddNeed,
    OpAddPlace,
    OpSetItemStatus,
    OpUpdateItem,
    ProposedOperationEnvelope,
)


# ---------------------------------------------------------------------------
# Envelope 判别还原
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload, expected_type",
    [
        ({"op": "add_need", "kind": "hard", "category": "预算", "value": "3000 元"}, OpAddNeed),
        ({"op": "add_inspiration", "title": "宽窄巷子"}, OpAddInspiration),
        ({"op": "add_place", "name": "人民公园"}, OpAddPlace),
        ({"op": "add_day", "day_index": 1}, OpAddDay),
        ({"op": "add_item", "day_id": "d1", "title": "早餐"}, OpAddItem),
        ({"op": "update_item", "item_id": "i1", "cost": 88.0}, OpUpdateItem),
        ({"op": "set_item_status", "item_id": "i1", "status": "confirmed"}, OpSetItemStatus),
    ],
)
def test_envelope_discriminates_each_op(payload, expected_type):
    env = ProposedOperationEnvelope.model_validate({"operation": payload})
    assert isinstance(env.operation, expected_type)
    # 判别字段 op 被原样保留
    assert env.operation.op == payload["op"]


def test_envelope_illegal_op_raises():
    with pytest.raises(ValidationError):
        ProposedOperationEnvelope.model_validate({"operation": {"op": "delete_everything"}})


def test_envelope_missing_op_raises():
    # 缺判别字段 op 无法还原
    with pytest.raises(ValidationError):
        ProposedOperationEnvelope.model_validate({"operation": {"title": "no op field"}})


# ---------------------------------------------------------------------------
# AIResponse
# ---------------------------------------------------------------------------
def test_ai_response_minimal_valid():
    resp = AIResponse(reply="好的，我来帮你规划。")
    assert resp.reply == "好的，我来帮你规划。"
    # 其余字段有安全默认
    assert resp.clarification_questions == []
    assert resp.operations == []
    assert resp.sources == []


def test_ai_response_missing_reply_raises():
    with pytest.raises(ValidationError):
        AIResponse()  # 缺 reply


def test_ai_response_empty_reply_raises():
    with pytest.raises(ValidationError):
        AIResponse(reply="")  # min_length=1


def test_ai_response_with_operations():
    resp = AIResponse(
        reply="我建议加一个硬约束。",
        operations=[
            ProposedOperationEnvelope.model_validate(
                {"operation": {"op": "add_need", "kind": "hard", "category": "预算", "value": "3000 元"}}
            )
        ],
    )
    assert len(resp.operations) == 1
    assert isinstance(resp.operations[0].operation, OpAddNeed)
