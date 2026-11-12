"""阶段 D：受控业务工具层。

AI **只能**通过这些工具影响系统：
- 工具复用 TripService / ItineraryService 的既有业务入口与授权（require_member）。
- 工具分两步：`plan_*`（只计算影响/差异，不写库，用于生成提案）与 `execute_*`（真正写入，仍经服务校验）。
- 任何操作都以 contracts 里的 ProposedOperation 表达，经 Pydantic 校验。
- 工具没有绕过权限的路径：所有写操作都传 member_id，服务内部 require_member 把关。

这样即满足任务书「AI 不得绕过权限或直接写数据库；应通过经过校验的业务操作提出修改」。
"""

from __future__ import annotations

from fairmeet.ai.contracts import (
    ChangeKind,
    DiffEntry,
    OpAddDay,
    OpAddInspiration,
    OpAddItem,
    OpAddNeed,
    OpAddPlace,
    OperationImpact,
    OpSetItemStatus,
    OpUpdateItem,
    ProposedOperationEnvelope,
)
from fairmeet.checks import CheckService
from fairmeet.domain import (
    DayCreate,
    InspirationCreate,
    ItemCreate,
    ItemStatus,
    ItemUpdate,
    NeedCreate,
    PlaceCreate,
    ProvenanceKind,
)
from fairmeet.itinerary import ItineraryService
from fairmeet.services import FairMeetError, NotFound, TripService


def _s(v: object) -> str | None:
    return None if v is None else str(v)


class Toolbox:
    """为某个（trip_id, member_id）绑定的受控工具集。"""

    def __init__(
        self,
        trips: TripService,
        itinerary: ItineraryService,
        checks: CheckService,
        trip_id: str,
        member_id: str,
    ) -> None:
        self.trips = trips
        self.itinerary = itinerary
        self.checks = checks
        self.trip_id = trip_id
        self.member_id = member_id

    # ------------------------------------------------------------------
    # 授权前置校验：进入工具前先确认成员确实属于该旅程
    # ------------------------------------------------------------------
    def authorize(self) -> None:
        self.trips.require_member(self.trip_id, self.member_id)

    # ==================================================================
    # 计划（dry-run）：计算单操作的变更类型、是否触碰已确认项、差异、影响
    # 不写库。校验/授权失败写入 blocked_reason。
    # ==================================================================
    def plan(self, envelope: ProposedOperationEnvelope) -> OperationImpact:
        op = envelope.operation
        try:
            self.authorize()
        except FairMeetError as exc:
            return OperationImpact(op=envelope, change_kind=ChangeKind.UPDATE, blocked_reason=str(exc))

        try:
            if isinstance(op, (OpAddNeed, OpAddInspiration, OpAddPlace, OpAddDay)):
                kind = ChangeKind.CREATE
                notes = self._create_notes(op)
                return OperationImpact(op=envelope, change_kind=kind, impact_notes=notes)
            if isinstance(op, OpAddItem):
                return OperationImpact(
                    op=envelope,
                    change_kind=ChangeKind.CREATE,
                    impact_notes=self._add_item_notes(op),
                )
            if isinstance(op, OpUpdateItem):
                return self._plan_update_item(envelope, op)
            if isinstance(op, OpSetItemStatus):
                return self._plan_set_status(envelope, op)
        except FairMeetError as exc:
            return OperationImpact(op=envelope, change_kind=ChangeKind.UPDATE, blocked_reason=str(exc))
        return OperationImpact(op=envelope, change_kind=ChangeKind.UPDATE, blocked_reason="未知操作")

    def _create_notes(self, op) -> list[str]:
        return [f"新增：{self._describe(op)}"]

    def _add_item_notes(self, op: OpAddItem) -> list[str]:
        notes = [f"在第 {op.day_id[:8]}… 天新增行程条目「{op.title}」"]
        if op.cost is not None:
            notes.append(f"将影响预算预估：+{op.cost} 元（来源：{op.cost_provenance_kind.value}）")
        return notes

    def _plan_update_item(self, envelope: ProposedOperationEnvelope, op: OpUpdateItem) -> OperationImpact:
        try:
            existing = self.itinerary.get_item(self.trip_id, op.item_id)
        except NotFound as exc:
            return OperationImpact(op=envelope, change_kind=ChangeKind.UPDATE, blocked_reason=str(exc))

        diffs: list[DiffEntry] = []
        pairs = {
            "title": (existing.title, op.title),
            "start_time": (existing.start_time, op.start_time),
            "end_time": (existing.end_time, op.end_time),
            "stay_minutes": (existing.stay_minutes, op.stay_minutes),
            "cost": (existing.cost, op.cost),
            "note": (existing.note, op.note),
        }
        for field, (before, after) in pairs.items():
            # 仅当调用方显式提供了该字段（非 None）才计入差异
            if after is not None and str(before) != str(after):
                diffs.append(DiffEntry(field=field, before=_s(before), after=_s(after)))

        affects_confirmed = existing.status == ItemStatus.CONFIRMED
        notes: list[str] = []
        if affects_confirmed:
            notes.append("⚠ 该条目已确认：修改需展示差异并等待确认后才生效。")
        if op.cost is not None and existing.cost is not None:
            delta = op.cost - existing.cost
            notes.append(f"预算变动：{'+' if delta >= 0 else ''}{round(delta, 2)} 元")
        return OperationImpact(
            op=envelope,
            change_kind=ChangeKind.UPDATE,
            target_ref=op.item_id,
            affects_confirmed=affects_confirmed,
            diffs=diffs,
            impact_notes=notes,
        )

    def _plan_set_status(self, envelope: ProposedOperationEnvelope, op: OpSetItemStatus) -> OperationImpact:
        try:
            existing = self.itinerary.get_item(self.trip_id, op.item_id)
        except NotFound as exc:
            return OperationImpact(op=envelope, change_kind=ChangeKind.STATUS, blocked_reason=str(exc))
        diffs = [DiffEntry(field="status", before=existing.status.value, after=op.status.value)]
        # 从「已确认」转出视为触碰已确认项
        affects_confirmed = existing.status == ItemStatus.CONFIRMED
        return OperationImpact(
            op=envelope,
            change_kind=ChangeKind.STATUS,
            target_ref=op.item_id,
            affects_confirmed=affects_confirmed,
            diffs=diffs,
            impact_notes=["状态变更"] + (["⚠ 从已确认项转出，需确认。"] if affects_confirmed else []),
        )

    # ==================================================================
    # 执行：真正写入。仅在提案被确认（或无需确认）后由 proposal 层调用。
    # ==================================================================
    def execute(self, envelope: ProposedOperationEnvelope) -> dict:
        op = envelope.operation
        self.authorize()
        if isinstance(op, OpAddNeed):
            n = self.trips.add_need(
                self.trip_id, self.member_id, NeedCreate(kind=op.kind, category=op.category, value=op.value)
            )
            return {"kind": "need", "id": n.id}
        if isinstance(op, OpAddInspiration):
            i = self.trips.add_inspiration(
                self.trip_id,
                self.member_id,
                InspirationCreate(
                    title=op.title,
                    note=op.note,
                    source_url=op.source_url,
                    source=op.source,
                    place_name=op.place_name,
                ),
            )
            return {"kind": "inspiration", "id": i.id}
        if isinstance(op, OpAddPlace):
            p = self.trips.add_place(self.trip_id, self.member_id, PlaceCreate(name=op.name, category=op.category))
            return {"kind": "place", "id": p.id}
        if isinstance(op, OpAddDay):
            d = self.itinerary.add_day(
                self.trip_id, self.member_id, DayCreate(day_index=op.day_index, date=op.date, title=op.title)
            )
            return {"kind": "day", "id": d.id}
        if isinstance(op, OpAddItem):
            it = self.itinerary.add_item(
                self.trip_id,
                self.member_id,
                op.day_id,
                ItemCreate(
                    item_type=op.item_type,
                    title=op.title,
                    place_id=op.place_id,
                    start_time=op.start_time,
                    end_time=op.end_time,
                    stay_minutes=op.stay_minutes,
                    cost=op.cost,
                    cost_provenance_kind=ProvenanceKind(op.cost_provenance_kind),
                    note=op.note,
                ),
            )
            return {"kind": "item", "id": it.id}
        if isinstance(op, OpUpdateItem):
            # 只传「显式提供（非 None）」的字段，避免把未涉及字段置空
            update_fields = {
                k: v
                for k, v in {
                    "title": op.title,
                    "start_time": op.start_time,
                    "end_time": op.end_time,
                    "stay_minutes": op.stay_minutes,
                    "cost": op.cost,
                    "note": op.note,
                }.items()
                if v is not None
            }
            it = self.itinerary.update_item(self.trip_id, self.member_id, op.item_id, ItemUpdate(**update_fields))
            return {"kind": "item", "id": it.id}
        if isinstance(op, OpSetItemStatus):
            it = self.itinerary.set_item_status(self.trip_id, self.member_id, op.item_id, op.status)
            return {"kind": "item", "id": it.id, "status": it.status.value}
        raise FairMeetError("未知操作")

    @staticmethod
    def _describe(op) -> str:
        for attr in ("title", "name", "value", "category"):
            v = getattr(op, attr, None)
            if v:
                return f"{op.op}: {v}"
        return op.op
