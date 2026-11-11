"""阶段 D：对话协调编排。

流程（任务书 §5、§6 阶段 D）：
  user message + 旅程上下文 → LLMClient.complete(结构化) → AIResponse（校验）
    → 若含 operations：经 ProposalService.build 生成**待确认提案**（不自动执行）
    → 返回 reply + clarification_questions + 提案 + 来源/可用性标示

- LLM 通过 Protocol 注入；测试用 FakeLLM，不联网、不依赖真实模型。
- 输出必须通过 AIResponse 的 Pydantic 校验，未通过则不落任何提案（返回安全回退）。
- 「解释冲突」「还需要补充什么」类请求可确定性回答（不依赖 LLM），保证无模型时也能用。
"""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import ValidationError

from fairmeet.ai.contracts import AIResponse, ChangeProposal, ProposedOperationEnvelope
from fairmeet.ai.explain import Explainer
from fairmeet.ai.proposal import ProposalError, ProposalService
from fairmeet.ai.sources import SourceRegistry
from fairmeet.services import TripService


class LLMClient(Protocol):
    """LLM 端口：给定系统提示与用户消息，返回 JSON 字符串（待校验为 AIResponse）。"""

    def complete(self, system: str, user: str) -> str: ...


class FakeLLM:
    """测试/离线用假 LLM：按预设返回固定 JSON，或回显一个安全的默认响应。"""

    def __init__(self, canned: str | None = None) -> None:
        self.canned = canned
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.canned is not None:
            return self.canned
        # 默认：不产生任何操作，只给出文本 + 澄清问题
        return json.dumps(
            {
                "reply": "我理解你的请求。为了更准确地建议，请补充一些关键信息。",
                "clarification_questions": ["大概几个人同行？", "期望的出行日期是？"],
                "operations": [],
                "sources": [],
            },
            ensure_ascii=False,
        )


_SYSTEM_PROMPT = (
    "你是 FairMeet 的出行规划协调助手。你只能通过结构化提案影响系统，不能直接修改数据。"
    "请严格只输出一个 JSON 对象，顶层字段：reply(string)、clarification_questions(string[])、"
    "operations(array)、sources(array，每项 {label,kind,detail?}，kind∈verified/external/estimated/unknown)。\n"
    "operations 数组每项必须是 {\"operation\": {op 及该 op 的字段}}，"
    "op 只能取以下之一，字段必须严格匹配（不要自造 op 或字段）：\n"
    "1) add_need: {op, kind(hard|preference|negotiable), category, value}\n"
    "2) add_inspiration: {op, title, note?, source_url?, place_name?}\n"
    "3) add_place: {op, name, category?}\n"
    "4) add_day: {op, day_index(int>=1), date?(YYYY-MM-DD), title?}\n"
    "5) add_item: {op, day_id, item_type(activity|meal|lodging|meeting_point|transit|free_time|note), title,"
    " place_id?, start_time?(HH:MM), end_time?(HH:MM), stay_minutes?, cost?,"
    " cost_provenance_kind(verified|external|estimated|unknown), note?}\n"
    "6) update_item: {op, item_id, title?, start_time?, end_time?, stay_minutes?, cost?, note?}\n"
    "7) set_item_status: {op, item_id, status(draft|discussing|pending|confirmed|done|cancelled)}\n"
    "示例：新增午餐 -> {\"operations\":[{\"operation\":{\"op\":\"add_item\","
    "\"day_id\":\"<上下文里的真实day_id>\","
    "\"item_type\":\"meal\",\"title\":\"川菜午餐\",\"cost\":120,\"cost_provenance_kind\":\"estimated\"}}]}\n"
    "规则：day_id/item_id 必须使用 context 中真实存在的 id，不得编造；"
    "无法确定时在 clarification_questions 追问而不生成操作；"
    "硬约束冲突必须显式指出、不得擅自放宽；不得编造精确数字，估算费用必须标 cost_provenance_kind=estimated。"
)


class Orchestrator:
    def __init__(
        self,
        trips: TripService,
        proposals: ProposalService,
        explainer: Explainer,
        sources: SourceRegistry,
        llm: LLMClient,
        itinerary: "object | None" = None,
    ) -> None:
        self.trips = trips
        self.proposals = proposals
        self.explainer = explainer
        self.sources = sources
        self.llm = llm
        self.itinerary = itinerary

    # ------------------------------------------------------------------
    def _context(self, trip_id: str) -> dict:
        trip = self.trips.get_trip(trip_id)
        days = []
        items = []
        if self.itinerary is not None:
            days = [
                {"id": d.id, "day_index": d.day_index, "date": d.date, "title": d.title}
                for d in self.itinerary.list_days(trip_id)
            ]
            items = [
                {
                    "id": i.id,
                    "day_id": i.day_id,
                    "title": i.title,
                    "item_type": i.item_type.value,
                    "status": i.status.value,
                }
                for i in self.itinerary.list_items(trip_id)
            ]
        return {
            "trip": {"title": trip.title, "destination": trip.destination, "budget": trip.budget},
            "needs": [n.model_dump(mode="json") for n in self.trips.list_needs(trip_id)],
            "inspirations": [i.model_dump(mode="json") for i in self.trips.list_inspirations(trip_id)],
            "days": days,
            "items": items,
        }

    def handle(self, trip_id: str, member_id: str, message: str) -> dict:
        """处理一条用户消息。返回 reply/澄清/提案/来源/可用性摘要。"""
        # 先做成员授权（没有权限的成员不能用 AI）
        self.trips.require_member(trip_id, member_id)

        # 确定性快捷路径：解释冲突（无需 LLM）
        if any(k in message for k in ("冲突", "矛盾", "问题", "预算够", "还差什么", "检查")):
            expl = self.explainer.explain_conflicts(trip_id)
            return {
                "mode": "explain",
                "reply": expl["text"],
                "clarification_questions": [],
                "proposals": [],
                "sources": [],
                "hard_conflicts": expl["hard_conflicts"],
            }

        # 走 LLM（结构化输出 → 校验）
        user_payload = json.dumps({"message": message, "context": self._context(trip_id)}, ensure_ascii=False)
        raw = self.llm.complete(_SYSTEM_PROMPT, user_payload)
        try:
            parsed = self._parse(raw)
        except (ValidationError, json.JSONDecodeError) as exc:
            # LLM 输出既可能是非法 JSON，也可能是合法 JSON 但违反 schema。
            # 两者都必须安全拒绝：不落任何提案，返回统一回退（mode='invalid'）。
            return {
                "mode": "invalid",
                "reply": "抱歉，我这次生成的结构化结果未通过校验，未做任何修改。请换一种说法或补充信息。",
                "clarification_questions": [],
                "proposals": [],
                "sources": [],
                "error": str(exc),
            }

        proposals: list[dict] = []
        if parsed.operations:
            try:
                proposal: ChangeProposal = self.proposals.build(
                    trip_id,
                    member_id,
                    "AI 提议的修改",
                    parsed.operations,
                )
                proposals.append(proposal.model_dump(mode="json"))
            except ProposalError as exc:
                return {
                    "mode": "error",
                    "reply": f"生成提案失败：{exc}",
                    "clarification_questions": [],
                    "proposals": [],
                    "sources": [s.model_dump() for s in parsed.sources],
                }

        availability = self.sources.list()
        return {
            "mode": "chat",
            "reply": parsed.reply,
            "clarification_questions": parsed.clarification_questions,
            "proposals": proposals,
            "sources": [s.model_dump() for s in parsed.sources],
            "source_availability": availability,
        }

    def _parse(self, raw: str) -> AIResponse:
        """把 LLM 的 JSON 字符串解析为 AIResponse；并规范化 operations 的包装。

        兼容两种 operation 形态：
        - {"operation": {"op": "add_need", ...}}  (envelope)
        - {"op": "add_need", ...}                 (裸操作)
        """
        data = json.loads(raw)
        ops = data.get("operations") or []
        normalized = []
        for item in ops:
            if isinstance(item, dict) and "operation" in item:
                normalized.append(item)
            elif isinstance(item, dict) and "op" in item:
                normalized.append({"operation": item})
            else:
                raise ValidationError.from_exception_data(
                    "AIResponse", [{"type": "value_error", "loc": ("operations",), "input": item}]
                )
        data["operations"] = normalized
        return AIResponse.model_validate(data)

    # 供测试直接构造 envelope
    @staticmethod
    def envelope(**op_fields) -> ProposedOperationEnvelope:
        return ProposedOperationEnvelope.model_validate({"operation": op_fields})
