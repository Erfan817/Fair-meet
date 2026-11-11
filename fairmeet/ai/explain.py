"""阶段 D：冲突/取舍解释（基于真实约束与检查数据，禁止编造）。

- 解释内容全部来自 CheckService 的确定性结果（conflicts/budget）与旅程/需求数据。
- 不引入模型臆测的精确数字；若数据未知/估算，明确标注。
"""

from __future__ import annotations

from fairmeet.checks import CheckService
from fairmeet.domain import NeedKind
from fairmeet.services import TripService


class Explainer:
    def __init__(self, trips: TripService, checks: CheckService) -> None:
        self.trips = trips
        self.checks = checks

    def explain_conflicts(self, trip_id: str) -> dict:
        """返回结构化解释：硬约束冲突、警告、预算情况、以及待成员处理的项。

        所有 message 直接来自 CheckService（规则生成），这里只做组织，不新增事实。
        """
        result = self.checks.check(trip_id)
        budget = self.checks.budget(trip_id)
        needs = self.trips.list_needs(trip_id)

        hard_issues = [c for c in result.conflicts if c.severity == "hard"]
        warnings = [c for c in result.conflicts if c.severity == "warning"]
        infos = [c for c in result.conflicts if c.severity == "info"]

        lines: list[str] = []
        if not result.conflicts:
            lines.append("当前未发现冲突或风险（基于现有行程与需求数据）。")
        else:
            if hard_issues:
                lines.append(f"存在 {len(hard_issues)} 项硬约束冲突，必须由成员处理，系统不会自动放宽：")
                lines.extend(f"  - {c.message}" for c in hard_issues)
            if warnings:
                lines.append(f"有 {len(warnings)} 项警告需关注：")
                lines.extend(f"  - {c.message}" for c in warnings)
            if infos:
                lines.append(f"另有 {len(infos)} 项待核对信息：")
                lines.extend(f"  - {c.message}" for c in infos)

        # 硬约束清单（原样呈现，标注约束强度）
        hard_needs = [
            {"category": n.category, "value": n.value, "kind": n.kind.value}
            for n in needs
            if n.kind == NeedKind.HARD
        ]

        budget_line = (
            f"预算：上限 {budget.trip_budget if budget.trip_budget is not None else '未设置'} 元，"
            f"估算合计 {budget.estimated_total} 元"
            + ("（已超预算）" if budget.over_budget else "")
            + (f"。{budget.note}" if budget.note else "")
        )

        return {
            "text": "\n".join(lines + [budget_line]),
            "hard_conflicts": [c.model_dump() for c in hard_issues],
            "warnings": [c.model_dump() for c in warnings],
            "infos": [c.model_dump() for c in infos],
            "hard_needs": hard_needs,
            "budget": budget.model_dump(),
            "ok": result.ok,
        }
