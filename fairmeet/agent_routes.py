"""阶段 F：agent 委托与受控写入的 HTTP 路由。

- 成员端接口用 member_id 标识调用人（与现有原型一致），用于创建/查看/撤销委托。
- agent 端接口用 `Authorization: Bearer <token>` 鉴权，错误统一返回
  `{"error": {"code", "message", "details"}}`，便于外部 agent 按错误码纠正。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from fairmeet.agent import (
    ERR_CONFIRMATION_REQUIRES_MEMBER,
    AgentError,
    AgentService,
    DelegationCreate,
)
from fairmeet.domain import InspirationCreate, NeedCreate
from fairmeet.services import FairMeetError, Forbidden, NotFound


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, AgentError):
        code, message, status, details = exc.code, exc.message, exc.status_code, exc.details
    elif isinstance(exc, NotFound):
        code, message, status, details = "not_found", str(exc), 404, {}
    elif isinstance(exc, Forbidden):
        code, message, status, details = "forbidden", str(exc), 403, {}
    elif isinstance(exc, FairMeetError):
        code, message, status, details = "bad_request", str(exc), 400, {}
    else:  # pragma: no cover - 防御性兜底
        code, message, status, details = "internal_error", str(exc), 500, {}
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details}},
    )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AgentError("missing_token", "缺少 Authorization: Bearer 令牌", 401)
    return token.strip()


def install_agent_routes(app: FastAPI, agents: AgentService) -> None:
    # ---- 成员管理委托 ----
    @app.post("/api/trips/{trip_id}/agent-delegations")
    def create_delegation(trip_id: str, payload: DelegationCreate, member_id: str):
        try:
            delegation, token = agents.create_delegation(trip_id, member_id, payload)
        except Exception as exc:  # noqa: BLE001 - 统一转结构化错误
            return _error_response(exc)
        return {
            "delegation": delegation.model_dump(mode="json"),
            # 明文令牌只在这里出现一次；服务端只存哈希。
            "token": token,
            "note": "请立即保存令牌。服务端不保存明文，丢失后只能撤销并重新创建。",
        }

    @app.get("/api/trips/{trip_id}/agent-delegations")
    def list_delegations(trip_id: str, member_id: str):
        try:
            items = agents.list_delegations(trip_id, member_id)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return [d.model_dump(mode="json") for d in items]

    @app.delete("/api/trips/{trip_id}/agent-delegations/{delegation_id}")
    def revoke_delegation(trip_id: str, delegation_id: str, member_id: str):
        try:
            delegation = agents.revoke_delegation(trip_id, member_id, delegation_id)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return delegation.model_dump(mode="json")

    @app.get("/api/trips/{trip_id}/agent-audit")
    def list_agent_audit(trip_id: str, member_id: str, limit: int = 200):
        try:
            entries = agents.list_audit(trip_id, member_id, limit)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return [e.model_dump(mode="json") for e in entries]

    # ---- agent 只读 ----
    @app.get("/api/agent/delegation")
    def agent_whoami(request: Request):
        try:
            delegation = agents.resolve(_bearer_token(request))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        return delegation.model_dump(mode="json")

    @app.get("/api/agent/trip")
    def agent_trip_context(request: Request):
        try:
            delegation = agents.resolve(_bearer_token(request))
            agents.require_scope(delegation, "trip:read")
            return agents.build_context(delegation)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.get("/api/agent/checks")
    def agent_checks(request: Request):
        try:
            delegation = agents.resolve(_bearer_token(request))
            return agents.checks_view(delegation)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    # ---- agent 受控写入 ----
    @app.post("/api/agent/needs")
    def agent_submit_need(payload: NeedCreate, request: Request):
        try:
            delegation = agents.resolve(_bearer_token(request))
            return agents.submit_need(delegation, payload).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @app.post("/api/agent/inspirations")
    def agent_submit_inspiration(payload: InspirationCreate, request: Request):
        try:
            delegation = agents.resolve(_bearer_token(request))
            return agents.submit_inspiration(delegation, payload).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    # ---- 明确拒绝：确认权属于成员本人 ----
    @app.post("/api/agent/proposals/{proposal_id}/confirm")
    def agent_confirm_proposal(proposal_id: str, request: Request):
        try:
            delegation = agents.resolve(_bearer_token(request))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        agents.record(
            delegation,
            "proposal.confirm_denied",
            "denied",
            target_ref=proposal_id,
            detail={"reason": ERR_CONFIRMATION_REQUIRES_MEMBER},
        )
        return _error_response(
            AgentError(
                ERR_CONFIRMATION_REQUIRES_MEMBER,
                "确认动作必须由成员本人完成；agent 只能提交提案并等待成员确认",
                403,
            )
        )
