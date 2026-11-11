"""阶段 D：修改提案（ChangeProposal）构建与确认。

流程（对应任务书 §3.8、§5、§9.7）：
  operations --plan--> impacts --build--> ChangeProposal(pending)
  若有任何操作触及已确认项或有写操作，则 requires_confirmation=True。
  confirm(): 仅 pending 且（无需确认或已被显式确认）才 execute；执行仍走 tools → services。
  reject(): 标记 rejected，不执行。

要点：
- **不静默修改已确认计划**：提案不会自动执行，必须显式 confirm。
- **差异与影响可见**：每个 impact 带 diffs 与 impact_notes，确认前可读。
- 校验/授权失败的操作（blocked_reason 非空）在构建时标为不可执行，确认时跳过并回报。
"""

from __future__ import annotations

import json
import uuid

from fairmeet.ai.contracts import (
    ChangeProposal,
    OperationImpact,
    ProposedOperationEnvelope,
)
from fairmeet.ai.tools import Toolbox
from fairmeet.db import Database
from fairmeet.domain import utcnow


class ProposalError(Exception):
    pass


class ProposalService:
    def __init__(self, db: Database, toolbox_factory) -> None:
        """toolbox_factory(trip_id, member_id) -> Toolbox"""
        self.db = db
        self.toolbox_factory = toolbox_factory

    def build(
        self,
        trip_id: str,
        member_id: str,
        summary: str,
        operations: list[ProposedOperationEnvelope],
    ) -> ChangeProposal:
        toolbox: Toolbox = self.toolbox_factory(trip_id, member_id)
        impacts: list[OperationImpact] = [toolbox.plan(op) for op in operations]
        # 只要存在写操作就需要确认（写操作一律走确认，避免静默落库）；
        # 触及已确认项的操作同样需要确认。
        has_writable = any(i.blocked_reason is None for i in impacts)
        affects_confirmed = any(i.affects_confirmed for i in impacts)
        requires_confirmation = bool(has_writable or affects_confirmed)

        proposal = ChangeProposal(
            id=uuid.uuid4().hex,
            trip_id=trip_id,
            member_id=member_id,
            summary=summary,
            impacts=impacts,
            requires_confirmation=requires_confirmation,
            status="pending",
            created_at=utcnow(),
        )
        self._persist(proposal)
        return proposal

    def _persist(self, p: ChangeProposal) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO change_proposals("
                "id,trip_id,member_id,summary,payload,requires_confirmation,status,created_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    p.id,
                    p.trip_id,
                    p.member_id,
                    p.summary,
                    json.dumps(p.model_dump(mode="json"), ensure_ascii=False),
                    1 if p.requires_confirmation else 0,
                    p.status,
                    p.created_at,
                ),
            )

    def get(self, proposal_id: str) -> ChangeProposal:
        with self.db.cursor() as cur:
            cur.execute("SELECT payload FROM change_proposals WHERE id=?", (proposal_id,))
            row = cur.fetchone()
        if row is None:
            raise ProposalError("提案不存在")
        return ChangeProposal.model_validate_json(row["payload"])

    def list(self, trip_id: str) -> list[ChangeProposal]:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT payload FROM change_proposals WHERE trip_id=? ORDER BY created_at DESC", (trip_id,)
            )
            return [ChangeProposal.model_validate_json(r["payload"]) for r in cur.fetchall()]

    def confirm(self, proposal_id: str, member_id: str) -> dict:
        """确认并执行提案。返回执行结果与跳过项。"""
        p = self.get(proposal_id)
        if p.status != "pending":
            raise ProposalError(f"提案状态为 {p.status}，不能确认")
        if member_id != p.member_id:
            raise ProposalError("只有提案发起者可确认")
        if p.requires_confirmation and member_id != p.member_id:
            raise ProposalError("需要确认")

        toolbox: Toolbox = self.toolbox_factory(p.trip_id, p.member_id)
        executed, skipped = [], []
        for impact in p.impacts:
            if impact.blocked_reason is not None:
                skipped.append({"reason": impact.blocked_reason, "impact": impact.model_dump(mode="json")})
                continue
            try:
                executed.append(toolbox.execute(impact.op))
            except Exception as exc:  # 执行期错误：记录但不回滚已执行项（保持简单、可追溯）
                skipped.append({"reason": str(exc), "impact": impact.model_dump(mode="json")})

        p.status = "confirmed"
        self._update_status(p)
        return {"executed": executed, "skipped": skipped, "proposal": p.model_dump(mode="json")}

    def reject(self, proposal_id: str, member_id: str) -> ChangeProposal:
        p = self.get(proposal_id)
        if p.status != "pending":
            raise ProposalError(f"提案状态为 {p.status}，不能拒绝")
        p.status = "rejected"
        self._update_status(p)
        return p

    def _update_status(self, p: ChangeProposal) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE change_proposals SET status=?, payload=? WHERE id=?",
                (p.status, json.dumps(p.model_dump(mode="json"), ensure_ascii=False), p.id),
            )
