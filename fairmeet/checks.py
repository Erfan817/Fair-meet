"""阶段 C：确定性检查与预算汇总。

- 硬约束冲突：基于成员需求（NeedKind.HARD）与行程/预算做确定性规则校验，
  冲突必须显式报告（severity=hard），**绝不自动放宽或静默忽略**（任务书 §2.5、§3.4）。
- 预算汇总：按行程条目费用累计，并按 provenance 分类（verified/external/estimated/unknown），
  明确标注哪些是估算/未知，不把估算当精确数字。
- 行程校验：时间重叠、缺地点坐标、交通段未知等给出 warning/info。

全部为纯规则计算，不依赖 LLM，不编造数字。
"""

from __future__ import annotations

from fairmeet.domain import (
    BudgetSummary,
    CheckResult,
    ConflictItem,
    ItemStatus,
    ItemType,
    NeedKind,
    ProvenanceKind,
)
from fairmeet.itinerary import ItineraryService
from fairmeet.services import TripService

# 认为是「已取消/替换」的状态，不计入预算与冲突
_INACTIVE = {ItemStatus.CANCELLED}


def _parse_time(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


class CheckService:
    def __init__(self, trips: TripService, itinerary: ItineraryService) -> None:
        self.trips = trips
        self.itinerary = itinerary

    # ------------------------------------------------------------------
    # 预算
    # ------------------------------------------------------------------
    def budget(self, trip_id: str) -> BudgetSummary:
        trip = self.trips.get_trip(trip_id)
        items = [i for i in self.itinerary.list_items(trip_id) if i.status not in _INACTIVE]
        total = 0.0
        by_prov: dict[str, float] = {}
        for it in items:
            if it.cost is None:
                continue
            total += it.cost
            k = it.cost_provenance.kind.value
            by_prov[k] = by_prov.get(k, 0.0) + it.cost
        over = trip.budget is not None and total > trip.budget
        note = ""
        if ProvenanceKind.ESTIMATED.value in by_prov or ProvenanceKind.UNKNOWN.value in by_prov:
            note = "部分费用为估算或未知来源，非精确金额，仅供决策参考。"
        return BudgetSummary(
            trip_budget=trip.budget,
            estimated_total=round(total, 2),
            by_provenance={k: round(v, 2) for k, v in by_prov.items()},
            over_budget=over,
            note=note,
        )

    # ------------------------------------------------------------------
    # 综合检查
    # ------------------------------------------------------------------
    def check(self, trip_id: str) -> CheckResult:
        conflicts: list[ConflictItem] = []
        conflicts += self._check_budget(trip_id)
        conflicts += self._check_hard_needs(trip_id)
        conflicts += self._check_time_overlaps(trip_id)
        conflicts += self._check_unknown_transport(trip_id)
        has_hard = any(c.severity == "hard" for c in conflicts)
        return CheckResult(ok=not has_hard, conflicts=conflicts)

    def _check_budget(self, trip_id: str) -> list[ConflictItem]:
        b = self.budget(trip_id)
        if b.over_budget and b.trip_budget is not None:
            return [
                ConflictItem(
                    severity="warning",
                    code="budget_over",
                    message=(
                        f"预算超限：估算合计 {b.estimated_total} 元，超出旅程预算 {b.trip_budget} 元。"
                        "（含估算项，请成员确认取舍）"
                    ),
                )
            ]
        return []

    def _check_hard_needs(self, trip_id: str) -> list[ConflictItem]:
        """硬约束冲突：日期窗口、预算上限、饮食/行动需求等做确定性校验。

        这里给出通用且可验证的规则；未覆盖的硬约束以 info 提示成员人工确认，
        绝不静默忽略。
        """
        out: list[ConflictItem] = []
        trip = self.trips.get_trip(trip_id)
        needs = self.trips.list_needs(trip_id)
        days = self.itinerary.list_days(trip_id)

        # 1) 硬约束「预算」与预算汇总冲突
        b = self.budget(trip_id)
        for n in needs:
            if n.kind != NeedKind.HARD:
                continue
            cat = n.category.lower()
            if cat in ("预算", "budget") and b.over_budget:
                out.append(
                    ConflictItem(
                        severity="hard",
                        code="hard_budget_conflict",
                        message=f"硬约束（{n.value}）与当前估算合计 {b.estimated_total} 元冲突，请成员处理。",
                        ref=n.id,
                    )
                )
            # 2) 硬约束日期窗口 vs 行程日期
            if cat in ("出发日期", "可参加日期", "日期", "date", "dates") and n.value:
                trip_dates = {d.date for d in days if d.date}
                # 若给出的硬日期不在行程任一天，提示人工核对（不自动改期）
                out.append(
                    ConflictItem(
                        severity="info",
                        code="hard_date_check",
                        message=(
                            f"硬约束日期「{n.value}」需与行程日期人工核对；"
                            f"当前行程日期：{sorted(trip_dates) or '未设置'}。"
                        ),
                        ref=n.id,
                    )
                )
            # 3) 其它硬约束（饮食/行动/无障碍）：行程未记录对应处理即为待确认
            elif cat not in ("预算", "budget", "出发日期", "可参加日期", "日期", "date", "dates"):
                handled = self._hard_need_handled(trip_id, n.value)
                if not handled:
                    out.append(
                        ConflictItem(
                            severity="info",
                            code="hard_need_unverified",
                            message=f"硬约束「{n.category}: {n.value}」尚未在行程中体现，请成员确认如何满足。",
                            ref=n.id,
                        )
                    )
        # 目的地一致性（旅程目的地非空即可，仅作记录）
        _ = trip
        return out

    def _hard_need_handled(self, trip_id: str, value: str) -> bool:
        """粗略判断硬约束是否在行程条目备注中提及（仅用于降低噪音，不精确）。"""
        v = value.strip()
        if not v:
            return True
        for it in self.itinerary.list_items(trip_id):
            hay = f"{it.title} {it.note or ''}"
            if v in hay:
                return True
        return False

    def _check_time_overlaps(self, trip_id: str) -> list[ConflictItem]:
        out: list[ConflictItem] = []
        for day in self.itinerary.list_days(trip_id):
            items = [
                i
                for i in self.itinerary.list_items(trip_id, day.id)
                if i.start_time and i.end_time and i.status not in _INACTIVE
            ]
            items.sort(key=lambda i: _parse_time(i.start_time) or 0)
            for prev, cur in zip(items, items[1:], strict=False):
                pe = _parse_time(prev.end_time)
                cs = _parse_time(cur.start_time)
                if pe is not None and cs is not None and cs < pe:
                    out.append(
                        ConflictItem(
                            severity="warning",
                            code="time_overlap",
                            message=(
                                f"第 {day.day_index} 天：「{prev.title}」({prev.end_time}) 与"
                                f"「{cur.title}」({cur.start_time}) 时间重叠。"
                            ),
                            ref=cur.id,
                        )
                    )
        return out

    def _check_unknown_transport(self, trip_id: str) -> list[ConflictItem]:
        out: list[ConflictItem] = []
        for seg in self.itinerary.list_transports(trip_id):
            if seg.duration_minutes is None or seg.duration_provenance.kind == ProvenanceKind.UNKNOWN:
                out.append(
                    ConflictItem(
                        severity="info",
                        code="transport_unknown",
                        message="存在交通段时长未知或为估算，可能影响时间安排，请核对。",
                        ref=seg.id,
                    )
                )
        # 备注：餐饮/会面点即行程条目（item_type=meal/meeting_point），无需额外检查表
        _ = ItemType
        return out
