"""阶段 F：面向外部 agent 的委托、只读上下文与受控写入。

设计见 docs/plan/agent-era-positioning.md §7 F 阶段与 §5.3（守账本）：

- 委托令牌由成员显式创建，可撤销、有期限；服务端只保存 SHA-256 哈希，
  明文令牌仅在创建时返回一次。
- agent 只能读成员可见范围内的旅程上下文；他人硬约束只暴露数量，
  具体值不进入 agent 上下文。
- agent 可以提交偏好/可协商需求与带来源的灵感，但**不能声明硬约束**，
  也不能确认提案——两者都返回明确错误码，便于 agent 自行纠正。
- 所有 agent 写入与拒绝尝试都写入审计表。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, ValidationError

from fairmeet.ai.contracts import (
    OpAddDay,
    OpAddInspiration,
    OpAddItem,
    OpAddNeed,
    OpAddPlace,
    ProposedOperationEnvelope,
)
from fairmeet.checks import CheckService
from fairmeet.db import Database
from fairmeet.domain import (
    ChangeType,
    Inspiration,
    InspirationCreate,
    Need,
    NeedCreate,
    NeedKind,
    Trip,
    utcnow,
)
from fairmeet.itinerary import ItineraryService
from fairmeet.services import Forbidden, NotFound, TripService

SCOPES = frozenset(
    {
        "trip:read",
        "checks:read",
        "need:submit",
        "inspiration:submit",
        "proposal:submit",
        "proposal:read",
    }
)
DEFAULT_SCOPES: tuple[str, ...] = (
    "trip:read",
    "checks:read",
    "need:submit",
    "inspiration:submit",
    "proposal:submit",
    "proposal:read",
)
TOKEN_PREFIX = "fm_agent_"

# 错误码是给 agent 看的契约，不要随意改名。
ERR_MISSING_TOKEN = "missing_token"
ERR_INVALID_TOKEN = "invalid_token"
ERR_TOKEN_REVOKED = "token_revoked"
ERR_TOKEN_EXPIRED = "token_expired"
ERR_INSUFFICIENT_SCOPE = "insufficient_scope"
ERR_INVALID_SCOPE = "invalid_scope"
ERR_MEMBER_INACTIVE = "member_inactive"
ERR_HARD_CONSTRAINT = "hard_constraint_requires_member"
ERR_SOURCE_REQUIRED = "source_required"
ERR_CONFIRMATION_REQUIRES_MEMBER = "confirmation_requires_member"
ERR_REVISION_CONFLICT = "revision_conflict"
ERR_INVALID_OPERATION = "invalid_operation"
ERR_EMPTY_PROPOSAL = "empty_proposal"
ERR_INVALID_AUTO_CONFIRM = "invalid_auto_confirm_policy"


class AgentError(Exception):
    """带机器可读错误码的 agent 接口错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class Delegation(BaseModel):
    id: str
    trip_id: str
    member_id: str
    agent_name: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    revoked_at: str | None = None
    created_at: str = Field(default_factory=utcnow)
    last_used_at: str | None = None
    auto_confirm: bool = False
    auto_confirm_max_cost: float | None = None


class DelegationCreate(BaseModel):
    agent_name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    expires_in_hours: int = Field(default=168, ge=1, le=720)
    # 默认关闭：成员可预先授权「仅追加、限额内、无硬约束」的提案自动生效。
    auto_confirm: bool = False
    auto_confirm_max_cost: float | None = Field(default=None, ge=0)


class AgentProposalCreate(BaseModel):
    summary: str = Field(min_length=1, max_length=200)
    expected_revision: str = Field(min_length=1, max_length=64)
    operations: list[dict] = Field(min_length=1, max_length=20)


class AuditEntry(BaseModel):
    id: str
    trip_id: str
    delegation_id: str
    member_id: str
    agent_name: str
    action: str
    outcome: str
    target_ref: str | None = None
    detail: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow)


def _new_id() -> str:
    return uuid.uuid4().hex


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _row_to_delegation(row) -> Delegation:
    return Delegation(
        id=row["id"],
        trip_id=row["trip_id"],
        member_id=row["member_id"],
        agent_name=row["agent_name"],
        scopes=json.loads(row["scopes"] or "[]"),
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        auto_confirm=bool(row["auto_confirm"]),
        auto_confirm_max_cost=row["auto_confirm_max_cost"],
    )


class AgentService:
    def __init__(
        self,
        db: Database,
        trips: TripService,
        itinerary: ItineraryService | None = None,
        checks: CheckService | None = None,
        proposals=None,
        history=None,
    ) -> None:
        self.db = db
        self.trips = trips
        self.itinerary = itinerary
        self.checks = checks
        self.proposals = proposals
        self.history = history

    # ---- 委托生命周期（由成员本人在网页/API 调用） ----
    def create_delegation(
        self, trip_id: str, member_id: str, payload: DelegationCreate
    ) -> tuple[Delegation, str]:
        self.trips.require_member(trip_id, member_id)
        requested = {s for s in payload.scopes}
        unknown = sorted(requested - SCOPES)
        if not requested or unknown:
            raise AgentError(
                ERR_INVALID_SCOPE,
                "委托作用域为空或包含未知项",
                422,
                {"unknown": unknown, "allowed": sorted(SCOPES)},
            )
        if payload.auto_confirm and payload.auto_confirm_max_cost is None:
            raise AgentError(
                ERR_INVALID_AUTO_CONFIRM,
                "开启自动确认时必须设置 auto_confirm_max_cost（成本上限）",
                422,
                {"required": "auto_confirm_max_cost"},
            )
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=payload.expires_in_hours)
        ).replace(microsecond=0)
        delegation = Delegation(
            id=_new_id(),
            trip_id=trip_id,
            member_id=member_id,
            agent_name=payload.agent_name,
            scopes=sorted(requested),
            expires_at=expires_at.isoformat(),
            auto_confirm=payload.auto_confirm,
            auto_confirm_max_cost=payload.auto_confirm_max_cost,
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_delegations("
                "id,trip_id,member_id,agent_name,scopes,token_hash,expires_at,revoked_at,created_at,last_used_at,"
                "auto_confirm,auto_confirm_max_cost"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delegation.id,
                    delegation.trip_id,
                    delegation.member_id,
                    delegation.agent_name,
                    json.dumps(delegation.scopes),
                    _hash_token(token),
                    delegation.expires_at,
                    None,
                    delegation.created_at,
                    None,
                    1 if delegation.auto_confirm else 0,
                    delegation.auto_confirm_max_cost,
                ),
            )
        return delegation, token

    def list_delegations(self, trip_id: str, member_id: str) -> list[Delegation]:
        requester = self.trips.require_member(trip_id, member_id)
        with self.db.cursor() as cur:
            if requester.is_owner:
                cur.execute(
                    "SELECT * FROM agent_delegations WHERE trip_id=? ORDER BY created_at",
                    (trip_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM agent_delegations WHERE trip_id=? AND member_id=? ORDER BY created_at",
                    (trip_id, member_id),
                )
            return [_row_to_delegation(r) for r in cur.fetchall()]

    def revoke_delegation(self, trip_id: str, member_id: str, delegation_id: str) -> Delegation:
        requester = self.trips.require_member(trip_id, member_id)
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT * FROM agent_delegations WHERE id=? AND trip_id=?",
                (delegation_id, trip_id),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound("委托不存在")
            if row["member_id"] != member_id and not requester.is_owner:
                raise Forbidden("只能撤销自己的委托")
            if row["revoked_at"] is None:
                cur.execute(
                    "UPDATE agent_delegations SET revoked_at=? WHERE id=?",
                    (utcnow(), delegation_id),
                )
                cur.execute("SELECT * FROM agent_delegations WHERE id=?", (delegation_id,))
                row = cur.fetchone()
        return _row_to_delegation(row)

    # ---- agent 鉴权 ----
    def resolve(self, token: str | None) -> Delegation:
        if not token:
            raise AgentError(ERR_MISSING_TOKEN, "缺少 Bearer 令牌", 401)
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM agent_delegations WHERE token_hash=?", (_hash_token(token),))
            row = cur.fetchone()
            if row is None:
                raise AgentError(ERR_INVALID_TOKEN, "令牌无效", 401)
            delegation = _row_to_delegation(row)
            if delegation.revoked_at:
                raise AgentError(ERR_TOKEN_REVOKED, "令牌已被撤销", 401)
            if delegation.expires_at and _parse_ts(delegation.expires_at) <= datetime.now(timezone.utc):
                raise AgentError(ERR_TOKEN_EXPIRED, "令牌已过期", 401)
            try:
                self.trips.require_member(delegation.trip_id, delegation.member_id)
            except Forbidden as exc:
                raise AgentError(ERR_MEMBER_INACTIVE, "委托对应的成员已不在该旅程", 403) from exc
            cur.execute(
                "UPDATE agent_delegations SET last_used_at=? WHERE id=?",
                (utcnow(), delegation.id),
            )
        delegation.last_used_at = utcnow()
        return delegation

    def require_scope(self, delegation: Delegation, scope: str) -> None:
        if scope not in delegation.scopes:
            raise AgentError(
                ERR_INSUFFICIENT_SCOPE,
                f"缺少作用域 {scope}",
                403,
                {"required": scope, "granted": delegation.scopes},
            )

    # ---- 受控写入 ----
    def submit_need(self, delegation: Delegation, payload: NeedCreate) -> Need:
        self.require_scope(delegation, "need:submit")
        if payload.kind is NeedKind.HARD:
            self.record(
                delegation,
                "need.submit",
                "denied",
                detail={"reason": ERR_HARD_CONSTRAINT, "category": payload.category},
            )
            raise AgentError(
                ERR_HARD_CONSTRAINT,
                "硬约束只能由成员本人确认；请提交 preference 或 negotiable，"
                "或请成员在页面自行添加",
                403,
                {"suggested_kind": NeedKind.PREFERENCE.value},
            )
        source = payload.source or f"agent:{delegation.agent_name}"
        need = self.trips.add_need(
            delegation.trip_id,
            delegation.member_id,
            NeedCreate(
                kind=payload.kind,
                category=payload.category,
                value=payload.value,
                source=source,
            ),
        )
        self.record(
            delegation,
            "need.submit",
            "applied",
            target_ref=need.id,
            detail={"kind": need.kind.value, "category": need.category},
        )
        return need

    def submit_inspiration(self, delegation: Delegation, payload: InspirationCreate) -> Inspiration:
        self.require_scope(delegation, "inspiration:submit")
        if not (payload.source_url or payload.source or payload.place_id or payload.place_name):
            self.record(
                delegation,
                "inspiration.submit",
                "denied",
                detail={"reason": ERR_SOURCE_REQUIRED, "title": payload.title},
            )
            raise AgentError(
                ERR_SOURCE_REQUIRED,
                "灵感必须带来源：请提供 source_url、source、place_id 或 place_name",
                422,
                {"accepted": ["source_url", "source", "place_id", "place_name"]},
            )
        insp = self.trips.add_inspiration(
            delegation.trip_id,
            delegation.member_id,
            InspirationCreate(
                title=payload.title,
                note=payload.note,
                source_url=payload.source_url,
                place_name=payload.place_name,
                place_id=payload.place_id,
                source=payload.source or f"agent:{delegation.agent_name}",
            ),
        )
        self.record(
            delegation,
            "inspiration.submit",
            "applied",
            target_ref=insp.id,
            detail={"title": insp.title, "source_url": insp.source_url, "source": insp.source},
        )
        return insp

    # ---- 提案（G 阶段） ----
    def compute_revision(self, trip_id: str) -> str:
        """行程状态指纹，用于 `expected_revision` 并发控制。

        覆盖 days/items/transports/places；需求与灵感变化不会制造无谓冲突。
        """
        days: list = []
        items: list = []
        transports: list = []
        if self.itinerary is not None:
            days = [d.model_dump(mode="json") for d in self.itinerary.list_days(trip_id)]
            items = [i.model_dump(mode="json") for i in self.itinerary.list_items(trip_id)]
            transports = [t.model_dump(mode="json") for t in self.itinerary.list_transports(trip_id)]
        places = [p.model_dump(mode="json") for p in self.trips.list_places(trip_id)]
        raw = json.dumps(
            {"days": days, "items": items, "transports": transports, "places": places},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def submit_proposal(self, delegation: Delegation, payload: AgentProposalCreate) -> dict:
        self.require_scope(delegation, "proposal:submit")
        if self.proposals is None:
            raise AgentError("proposals_unavailable", "当前实例未提供提案服务", 503)

        envelopes = self._parse_operations(delegation, payload.operations)
        current_revision = self.compute_revision(delegation.trip_id)
        if payload.expected_revision != current_revision:
            self.record(
                delegation,
                "proposal.submit",
                "denied",
                detail={
                    "reason": ERR_REVISION_CONFLICT,
                    "expected": payload.expected_revision,
                    "current": current_revision,
                },
            )
            raise AgentError(
                ERR_REVISION_CONFLICT,
                "行程已变化，请重新读取上下文后再提交提案",
                409,
                {"expected": payload.expected_revision, "current": current_revision},
            )

        proposal = self.proposals.build(
            delegation.trip_id,
            delegation.member_id,
            payload.summary,
            envelopes,
            origin=f"agent:{delegation.agent_name}",
            delegation_id=delegation.id,
        )
        self.record(
            delegation,
            "proposal.submit",
            "applied",
            target_ref=proposal.id,
            detail={
                "operations": len(envelopes),
                "revision": current_revision,
                "requires_confirmation": proposal.requires_confirmation,
                "blocked": sum(1 for i in proposal.impacts if i.blocked_reason is not None),
            },
        )

        decision = self._auto_confirm_decision(delegation, proposal, payload.expected_revision)
        if not decision["allowed"]:
            self.record(
                delegation,
                "proposal.auto_confirm_skipped",
                "denied",
                target_ref=proposal.id,
                detail=decision,
            )
            return {
                "proposal": proposal.model_dump(mode="json"),
                "revision": current_revision,
                "auto_confirmed": False,
                "auto_confirm_skipped_reason": decision["reason"],
            }

        try:
            result = self.proposals.confirm(proposal.id, delegation.member_id)
        except Exception as exc:  # noqa: BLE001 - 校验/执行失败都退回人工
            self.record(
                delegation,
                "proposal.auto_confirm_skipped",
                "denied",
                target_ref=proposal.id,
                detail={"reason": "confirm_failed", "error": str(exc), "policy": decision["policy"]},
            )
            return {
                "proposal": self.proposals.get(proposal.id).model_dump(mode="json"),
                "revision": self.compute_revision(delegation.trip_id),
                "auto_confirmed": False,
                "auto_confirm_skipped_reason": "confirm_failed",
                "error": str(exc),
            }

        if self.history is not None:
            self.history.record(
                delegation.trip_id,
                ChangeType.PROPOSAL_CONFIRM,
                f"自动确认 agent 提案：{proposal.summary}",
                member_id=delegation.member_id,
                target_ref=proposal.id,
            )
        self.record(
            delegation,
            "proposal.auto_confirmed",
            "applied",
            target_ref=proposal.id,
            detail={
                "executed": len(result.get("executed", [])),
                "skipped": len(result.get("skipped", [])),
                "policy": decision["policy"],
                "cost_delta": decision.get("cost_delta", 0),
            },
        )
        return {
            "proposal": self.proposals.get(proposal.id).model_dump(mode="json"),
            "revision": self.compute_revision(delegation.trip_id),
            "auto_confirmed": True,
            "executed": result.get("executed", []),
            "skipped": result.get("skipped", []),
        }

    def list_proposals(self, delegation: Delegation, status: str | None = None) -> list[dict]:
        self.require_scope(delegation, "proposal:read")
        if self.proposals is None:
            raise AgentError("proposals_unavailable", "当前实例未提供提案服务", 503)
        items = self.proposals.list(delegation.trip_id)
        if status:
            items = [p for p in items if p.status == status]
        return [p.model_dump(mode="json") for p in items]

    def get_proposal(self, delegation: Delegation, proposal_id: str):
        from fairmeet.ai.proposal import ProposalError

        self.require_scope(delegation, "proposal:read")
        if self.proposals is None:
            raise AgentError("proposals_unavailable", "当前实例未提供提案服务", 503)
        try:
            proposal = self.proposals.get(proposal_id)
        except ProposalError as exc:
            raise NotFound(str(exc)) from exc
        if proposal.trip_id != delegation.trip_id:
            raise NotFound("提案不属于该旅程")
        return proposal

    def _parse_operations(
        self, delegation: Delegation, raw_operations: list[dict]
    ) -> list[ProposedOperationEnvelope]:
        if not raw_operations:
            raise AgentError(ERR_EMPTY_PROPOSAL, "提案至少需要一个操作", 422)
        envelopes: list[ProposedOperationEnvelope] = []
        for index, raw in enumerate(raw_operations):
            if not isinstance(raw, dict):
                raise AgentError(
                    ERR_INVALID_OPERATION, f"第 {index + 1} 个操作必须是对象", 422, {"index": index}
                )
            try:
                envelope = ProposedOperationEnvelope.model_validate({"operation": raw})
            except ValidationError as exc:
                first = exc.errors()[0] if exc.errors() else {}
                raise AgentError(
                    ERR_INVALID_OPERATION,
                    f"第 {index + 1} 个操作结构非法",
                    422,
                    {
                        "index": index,
                        "field": ".".join(str(part) for part in first.get("loc", ())),
                        "reason": first.get("msg"),
                    },
                ) from exc
            op = envelope.operation
            if isinstance(op, OpAddNeed) and op.kind is NeedKind.HARD:
                self.record(
                    delegation,
                    "proposal.submit",
                    "denied",
                    detail={"reason": ERR_HARD_CONSTRAINT, "operation_index": index},
                )
                raise AgentError(
                    ERR_HARD_CONSTRAINT,
                    f"第 {index + 1} 个操作声明了硬约束；硬约束必须由成员本人确认",
                    403,
                    {"operation_index": index, "suggested_kind": NeedKind.PREFERENCE.value},
                )
            if isinstance(op, OpAddInspiration) and not (
                op.source_url or op.source or op.place_name
            ):
                self.record(
                    delegation,
                    "proposal.submit",
                    "denied",
                    detail={"reason": ERR_SOURCE_REQUIRED, "operation_index": index},
                )
                raise AgentError(
                    ERR_SOURCE_REQUIRED,
                    f"第 {index + 1} 个灵感操作缺少来源",
                    422,
                    {"operation_index": index, "accepted": ["source_url", "source", "place_name"]},
                )
            envelopes.append(envelope)
        return envelopes

    def _auto_confirm_decision(
        self, delegation: Delegation, proposal, expected_revision: str
    ) -> dict:
        """判断提案是否落在成员预先授权的自动确认范围内。

        保守条件（任一不满足即退回人工）：
        - 委托开启 auto_confirm 且设置了成本上限；
        - 所有操作可执行，且只做追加（add_day/item/need/inspiration/place），不修改已有条目；
        - 追加成本不超过上限，且不突破旅程预算；
        - 旅程当前不存在任何硬约束（因此不可能引入硬约束冲突）；
        - 行程在提交与确认之间没有再次变化。
        """
        policy = {
            "enabled": delegation.auto_confirm,
            "max_cost": delegation.auto_confirm_max_cost,
        }

        def skip(reason: str, **extra) -> dict:
            return {"allowed": False, "reason": reason, "policy": policy, **extra}

        if not delegation.auto_confirm:
            return skip("auto_confirm_disabled")
        if delegation.auto_confirm_max_cost is None:
            return skip("max_cost_not_set")
        if any(impact.blocked_reason is not None for impact in proposal.impacts):
            return skip("blocked_operation")
        append_types = (OpAddDay, OpAddItem, OpAddNeed, OpAddInspiration, OpAddPlace)
        if any(not isinstance(impact.op.operation, append_types) for impact in proposal.impacts):
            return skip("operation_not_append_only")

        cost_delta = sum(
            (impact.op.operation.cost or 0.0)
            for impact in proposal.impacts
            if isinstance(impact.op.operation, OpAddItem)
        )
        if cost_delta > delegation.auto_confirm_max_cost:
            return skip("cost_over_limit", cost_delta=cost_delta)
        if any(n.kind is NeedKind.HARD for n in self.trips.list_needs(delegation.trip_id)):
            return skip("hard_constraint_present")
        if self.checks is not None:
            trip = self.trips.get_trip(delegation.trip_id)
            budget = self.checks.budget(delegation.trip_id)
            if trip.budget is not None and budget.estimated_total + cost_delta > trip.budget:
                return skip(
                    "budget_over",
                    cost_delta=cost_delta,
                    estimated_total=budget.estimated_total,
                    trip_budget=trip.budget,
                )
        if self.compute_revision(delegation.trip_id) != expected_revision:
            return skip("revision_changed")
        return {"allowed": True, "reason": "within_policy", "policy": policy, "cost_delta": cost_delta}

    # ---- 审计 ----
    def record(
        self,
        delegation: Delegation,
        action: str,
        outcome: str,
        target_ref: str | None = None,
        detail: dict | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            id=_new_id(),
            trip_id=delegation.trip_id,
            delegation_id=delegation.id,
            member_id=delegation.member_id,
            agent_name=delegation.agent_name,
            action=action,
            outcome=outcome,
            target_ref=target_ref,
            detail=detail or {},
        )
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_audit("
                "id,trip_id,delegation_id,member_id,agent_name,action,outcome,target_ref,detail,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.id,
                    entry.trip_id,
                    entry.delegation_id,
                    entry.member_id,
                    entry.agent_name,
                    entry.action,
                    entry.outcome,
                    entry.target_ref,
                    json.dumps(entry.detail, ensure_ascii=False),
                    entry.created_at,
                ),
            )
        return entry

    def list_audit(self, trip_id: str, member_id: str, limit: int = 200) -> list[AuditEntry]:
        self.trips.require_member(trip_id, member_id)
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT * FROM agent_audit WHERE trip_id=? ORDER BY created_at DESC LIMIT ?",
                (trip_id, limit),
            )
            return [
                AuditEntry(
                    id=r["id"],
                    trip_id=r["trip_id"],
                    delegation_id=r["delegation_id"],
                    member_id=r["member_id"],
                    agent_name=r["agent_name"],
                    action=r["action"],
                    outcome=r["outcome"],
                    target_ref=r["target_ref"],
                    detail=json.loads(r["detail"] or "{}"),
                    created_at=r["created_at"],
                )
                for r in cur.fetchall()
            ]

    # ---- 只读上下文 ----
    def build_context(self, delegation: Delegation) -> dict:
        trip = self.trips.get_trip(delegation.trip_id)
        members = self.trips.list_members(delegation.trip_id)
        needs = self.trips.list_needs(delegation.trip_id)
        me = next((m for m in members if m.id == delegation.member_id), None)
        name_by_id = {m.id: m.display_name for m in members}

        own_needs = [n for n in needs if n.member_id == delegation.member_id]
        other_needs = [n for n in needs if n.member_id != delegation.member_id]
        other_hard_count = sum(1 for n in other_needs if n.kind is NeedKind.HARD)

        def _need(n: Need, with_owner: bool) -> dict:
            data = {
                "id": n.id,
                "kind": n.kind.value,
                "category": n.category,
                "value": n.value,
                "source": n.source,
                "created_at": n.created_at,
            }
            if with_owner:
                data["member"] = name_by_id.get(n.member_id)
            return data

        context = {
            "trip": _trip_view(trip),
            "revision": self.compute_revision(trip.id),
            "member": (
                {"id": me.id, "display_name": me.display_name, "is_owner": me.is_owner} if me else None
            ),
            "agent": {
                "name": delegation.agent_name,
                "scopes": delegation.scopes,
                "auto_confirm": delegation.auto_confirm,
                "auto_confirm_max_cost": delegation.auto_confirm_max_cost,
            },
            "members": [
                {"display_name": m.display_name, "is_owner": m.is_owner} for m in members
            ],
            "needs": {
                "mine": [_need(n, False) for n in own_needs],
                # 他人的偏好/可协商项可见；硬约束只暴露数量，不暴露具体值。
                "others": [_need(n, True) for n in other_needs if n.kind is not NeedKind.HARD],
                "other_hard_constraint_count": other_hard_count,
            },
            "inspirations": [i.model_dump(mode="json") for i in self.trips.list_inspirations(trip.id)],
            "places": [p.model_dump(mode="json") for p in self.trips.list_places(trip.id)],
        }
        if self.itinerary is not None:
            context["itinerary"] = {
                "days": [d.model_dump(mode="json") for d in self.itinerary.list_days(trip.id)],
                "items": [i.model_dump(mode="json") for i in self.itinerary.list_items(trip.id)],
                "transports": [
                    t.model_dump(mode="json") for t in self.itinerary.list_transports(trip.id)
                ],
            }
        if self.checks is not None:
            context["checks"] = self._check_view(delegation)
            context["budget"] = self.checks.budget(trip.id).model_dump(mode="json")
        if self.proposals is not None:
            context["proposals"] = [
                {
                    "id": p.id,
                    "summary": p.summary,
                    "status": p.status.value,
                    "requires_confirmation": p.requires_confirmation,
                    "created_at": p.created_at,
                }
                for p in self.proposals.list(trip.id)
            ]
        return context

    def checks_view(self, delegation: Delegation) -> dict:
        self.require_scope(delegation, "checks:read")
        if self.checks is None:
            raise AgentError("checks_unavailable", "当前实例未提供校验服务", 503)
        return {
            "check": self._check_view(delegation),
            "budget": self.checks.budget(delegation.trip_id).model_dump(mode="json"),
        }

    def _check_view(self, delegation: Delegation) -> dict:
        """校验结果对 agent 的视图：他人硬约束只保留「存在冲突」，不泄露具体值。"""
        needs = {n.id: n for n in self.trips.list_needs(delegation.trip_id)}
        data = self.checks.check(delegation.trip_id).model_dump(mode="json")
        for conflict in data.get("conflicts", []):
            need = needs.get(conflict.get("ref"))
            if need is None or need.member_id == delegation.member_id:
                continue
            if need.kind is NeedKind.HARD:
                conflict["message"] = (
                    "其他成员的硬约束与当前安排存在冲突；具体内容需由该成员本人提供。"
                )
                conflict["ref"] = None
        return data


def _trip_view(trip: Trip) -> dict:
    """对 agent 暴露的旅程字段：刻意不包含 invite_token。"""
    return {
        "id": trip.id,
        "title": trip.title,
        "destination": trip.destination,
        "start_date": trip.start_date,
        "end_date": trip.end_date,
        "budget": trip.budget,
        "status": trip.status.value,
    }
