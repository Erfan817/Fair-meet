"""阶段 E：局部重排（replan）。

核心约束（任务书 §3.8、§9.8）：
- **只改受影响项**：replan 仅作用于指定 day 的指定条目（或当天全部），不触碰其他天。
- **保留其他已确认项**：未在被重排范围内的已确认条目一律不动，且结果中显式列出「保留项」。
- **展示差异**：产出 FieldDiff（字段前后）、受影响条目、预算增减、影响说明。
- **写入变更历史**：整次重排记录为一条 ChangeLogEntry（ChangeType.REPLAN）。

重排语义（阶段 E 的确定性实现，不依赖 LLM）：
- 按 `new_order` 重排 `sort_order`；未列出的条目保持相对顺序并排在其后。
- 可选：若请求提供了逐条目标字段（这里用 sort_order 重排为主），仅改动 sort_order 或指定字段。
- 不改动任何未传参字段；不删除、不改状态、不改其他天。
"""

from __future__ import annotations

from fairmeet.domain import (
    ChangeType,
    FieldDiff,
    ItemStatus,
    ItemUpdate,
    ReplanRequest,
    ReplanResult,
)
from fairmeet.history import HistoryService
from fairmeet.itinerary import ItineraryService
from fairmeet.services import TripService


class ReplanService:
    def __init__(self, trips: TripService, itinerary: ItineraryService, history: HistoryService) -> None:
        self.trips = trips
        self.itinerary = itinerary
        self.history = history

    def replan_day(self, trip_id: str, member_id: str, req: ReplanRequest) -> ReplanResult:
        self.trips.require_member(trip_id, member_id)
        day = self.itinerary.get_day(trip_id, req.day_id)  # 校验 day 属于该 trip
        all_items = self.itinerary.list_items(trip_id, req.day_id)
        by_id = {i.id: i for i in all_items}

        # 确定重排范围：显式 item_ids，或当天全部；只允许范围在本天内
        if req.item_ids:
            scope = [i for i in all_items if i.id in set(req.item_ids)]
            missing = set(req.item_ids) - set(by_id)
            if missing:
                raise ValueError(f"以下条目不属于该天：{sorted(missing)}")
        else:
            scope = list(all_items)

        scope_ids = [i.id for i in scope]
        # 计算目标顺序：按 new_order 中出现的顺序，其余保持原序追加
        ordered_ids: list[str] = []
        for iid in req.new_order:
            if iid in scope_ids and iid not in ordered_ids:
                ordered_ids.append(iid)
        for i in sorted(scope, key=lambda x: x.sort_order):
            if i.id not in ordered_ids:
                ordered_ids.append(i.id)

        # 应用 sort_order（只改 sort_order，其他字段/状态/其他天不动）
        diffs: list[FieldDiff] = []
        affected: list[str] = []
        for idx, iid in enumerate(ordered_ids):
            cur = by_id[iid]
            if cur.sort_order != idx:
                diffs.append(FieldDiff(field=f"sort_order[{cur.title}]", before=str(cur.sort_order), after=str(idx)))
                affected.append(iid)
            # 直接用 itinerary.update_item 局部更新（保持授权与一致性）
            # 只传 sort_order，确保不误改其他字段
            self.itinerary.update_item(trip_id, member_id, iid, ItemUpdate(sort_order=idx))

        # 预算增减：排序不改金额，故 delta=0；但若范围含已确认项，仍保留不变
        budget_delta = 0.0

        # 保留项 = 全旅程范围内、不在重排范围内的条目（含其他天所有条目、含已确认项）
        # 这些条目一律不动，显式列出以证明「局部重排不无故覆盖其他已确认安排」。
        trip_all_items = self.itinerary.list_items(trip_id)
        scope_set = set(scope_ids)
        preserved = [i.id for i in trip_all_items if i.id not in scope_set]
        # 范围内已确认项：顺序可能变，但内容/状态不改，单列以便审计说明
        preserved_confirmed = [i.id for i in all_items if i.id in scope_set and i.status == ItemStatus.CONFIRMED]

        notes: list[str] = [f"仅重排第 {day.day_index} 天的 {len(scope_ids)} 个条目的顺序。"]
        if preserved_confirmed:
            notes.append(f"其中 {len(preserved_confirmed)} 个已确认条目内容/状态未改动。")
        notes.append("其他天与其他条目未受影响。")

        entry = self.history.record(
            trip_id=trip_id,
            change_type=ChangeType.REPLAN,
            summary=f"局部重排：第 {day.day_index} 天（{req.reason}）",
            member_id=member_id,
            target_ref=req.day_id,
            diffs=diffs,
        )

        return ReplanResult(
            day_id=req.day_id,
            changed=diffs,
            affected_item_ids=affected,
            preserved_item_ids=preserved,
            budget_delta=budget_delta,
            impact_notes=notes,
            change_log_id=entry.id,
        )
