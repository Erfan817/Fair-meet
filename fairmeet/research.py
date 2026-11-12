"""Research API and explicit, evidence-linked adoption. No automatic itinerary writes."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from fairmeet.ai.contracts import ProposedOperationEnvelope
from fairmeet.ai.proposal import ProposalError
from fairmeet.content import ResearchRequest
from fairmeet.domain import InspirationCreate, utcnow
from fairmeet.planning import PlanningRequest
from fairmeet.services import FairMeetError, Forbidden, NotFound


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=80)
    action: Literal["save", "dismiss", "build_proposal"]
    day_id: str | None = Field(default=None, max_length=80)
    place_id: str | None = Field(default=None, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=80)


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ResearchActions:
    """All selections are authorized and serialized with their side effects.

    The first provider returns discovery material, not verified merchant data.
    Adoption therefore creates only an explicitly unverified NOTE proposal.
    """

    def __init__(self, db, trips, itinerary, proposals, research):
        self.db, self.trips, self.itinerary = db, trips, itinerary
        self.proposals, self.research = proposals, research
        self.planner = None
        with db.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS research_selections (
                id TEXT PRIMARY KEY,
                trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                member_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
                request_key TEXT NOT NULL,
                request_body TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(trip_id, member_id, request_key),
                UNIQUE(run_id, member_id, selection_key))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS research_proposal_guards (
                proposal_id TEXT PRIMARY KEY REFERENCES change_proposals(id) ON DELETE CASCADE,
                trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                member_id TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_simulated INTEGER NOT NULL,
                evidence_snapshot TEXT NOT NULL)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS research_evidence_links (
                id TEXT PRIMARY KEY,
                trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(trip_id, target_type, target_id))""")

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _link(self, cur, trip_id, target_type, target_id, snapshot):
        cur.execute(
            "INSERT OR IGNORE INTO research_evidence_links VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, trip_id, target_type, target_id, self._json(snapshot), utcnow()),
        )

    def select(self, trip_id: str, member_id: str, run_id: str, payload: SelectionRequest) -> dict:
        with self.db.cursor() as cur:
            self.trips.require_member(trip_id, member_id)
            run = self.research.get(trip_id, member_id, run_id)
            if run["status"] not in {"succeeded", "partial"}:
                raise FairMeetError("研究尚未完成，或已取消，不能采纳")
            body = self._json({"run_id": run_id, **payload.model_dump(exclude={"idempotency_key"})})
            cur.execute(
                "SELECT request_body,result FROM research_selections WHERE trip_id=? AND member_id=? AND request_key=?",
                (trip_id, member_id, payload.idempotency_key),
            )
            previous = cur.fetchone()
            if previous:
                if previous["request_body"] != body:
                    raise FairMeetError("同一请求标识不能用于不同采纳操作")
                return json.loads(previous["result"])
            candidate = next((c for c in run["candidates"] if c["id"] == payload.candidate_id), None)
            if candidate is None:
                raise NotFound("候选不属于本次研究")
            selection_key = self._json([payload.candidate_id, payload.action, payload.day_id, payload.place_id])
            cur.execute(
                "SELECT result FROM research_selections WHERE run_id=? AND member_id=? AND selection_key=?",
                (run_id, member_id, selection_key),
            )
            previous = cur.fetchone()
            if previous:
                return json.loads(previous["result"])
            documents = {d["id"]: d for d in run["documents"]}
            if not candidate.get("evidence_ids") or any(i not in documents for i in candidate["evidence_ids"]):
                raise FairMeetError("候选缺少有效证据，不能采纳")
            evidence = [documents[i] for i in candidate["evidence_ids"]]
            # Only bounded provider records are snapshotted; never retain a whole page.
            snapshot = {"run_id": run_id, "candidate_id": candidate["id"],
                        "is_simulated": run["is_simulated"], "documents": evidence}
            label = "模拟资料 · 虚构候选" if run["is_simulated"] else "待核实资料 · 仅作发现线索"
            note = (label + "\n" + candidate.get("reason", "") + "\n注意："
                    + "；".join(candidate.get("risks", []))
                    + "\n价格、营业、分店及关键需求未经核实；不代表可执行安排。")[:2000]
            if payload.place_id:
                places = self.trips.list_places(trip_id)
                if not any(p.id == payload.place_id for p in places):
                    raise Forbidden("地点不属于该旅程")
            if payload.action == "save":
                source_url = next((d.get("url") for d in evidence if d.get("url")), None)
                inspiration = self.trips.add_inspiration(
                    trip_id, member_id,
                    InspirationCreate(title=candidate["name"][:200], note=note,
                                      source_url=source_url, place_id=payload.place_id),
                )
                self._link(cur, trip_id, "inspiration", inspiration.id, snapshot)
                result = {"saved": True, "inspiration": inspiration.model_dump(mode="json"),
                          "message": "已保存为待确认灵感，未修改行程、未新增未经核实的地点。"}
            elif payload.action == "dismiss":
                result = {"dismissed": True, "message": "本次不考虑该候选，原有灵感和行程未改变。"}
            else:
                if self.research.fingerprint(trip_id) != run["input_fingerprint"]:
                    raise FairMeetError("旅程条件或安排已变化，请重新检索后生成提案")
                if not run["is_simulated"] and not self.research.consent_enabled(trip_id, member_id):
                    raise Forbidden("联网授权已撤回，请重新授权并检索")
                if not payload.day_id:
                    raise FairMeetError("请先在行程计划中创建并选择一天")
                self.itinerary.get_day(trip_id, payload.day_id)
                operation = ProposedOperationEnvelope.model_validate({"operation": {
                    "op": "add_item", "day_id": payload.day_id, "item_type": "note",
                    "title": ("[模拟待核实] " if run["is_simulated"] else "[待核实候选] ") + candidate["name"][:160],
                    "cost_provenance_kind": "unknown", "note": note,
                    # Unresolved discovery material must not become an actual mapped activity.
                }})
                proposal = self.proposals.build(trip_id, member_id, "将候选保存为待核实备注（非正式安排）", [operation])
                cur.execute(
                    "INSERT INTO research_proposal_guards VALUES(?,?,?,?,?,?,?)",
                    (proposal.id, trip_id, member_id, self.research.fingerprint(trip_id),
                     run["expires_at"], int(run["is_simulated"]), self._json(snapshot)),
                )
                self._link(cur, trip_id, "proposal", proposal.id, snapshot)
                result = {"proposal": proposal.model_dump(mode="json"),
                          "message": "已生成待确认提案；确认后仅添加待核实备注，不承诺路线、费用或营业情况。"}
            cur.execute(
                "INSERT INTO research_selections VALUES(?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, trip_id, member_id, run_id, payload.idempotency_key, body,
                 selection_key, self._json(result), utcnow()),
            )
            return result

    def validate_proposal(self, proposal):
        """Called inside the confirmation transaction, including direct service calls."""
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM research_proposal_guards WHERE proposal_id=?", (proposal.id,))
            guard = cur.fetchone()
            if guard is None:
                return
            self.trips.require_member(proposal.trip_id, proposal.member_id)
            if datetime.fromisoformat(guard["expires_at"]) <= datetime.now(timezone.utc):
                raise ProposalError("检索依据已过期，请重新检索并生成提案")
            if self.research.fingerprint(proposal.trip_id) != guard["input_fingerprint"]:
                raise ProposalError("旅程日期、预算、成员需求或安排已变化，请重新生成提案")
            if not guard["is_simulated"] and not self.research.consent_enabled(proposal.trip_id, proposal.member_id):
                raise ProposalError("联网授权已撤回，旧研究提案不能执行")

    def link_execution(self, proposal, executed):
        with self.db.cursor() as cur:
            cur.execute("SELECT evidence_snapshot FROM research_proposal_guards WHERE proposal_id=?", (proposal.id,))
            guard = cur.fetchone()
            if guard:
                snapshot = json.loads(guard["evidence_snapshot"])
                for obj in executed:
                    if obj.get("kind") == "item":
                        self._link(cur, proposal.trip_id, "item", obj["id"], snapshot)
                if self.planner is not None:
                    self.planner.record_execution(proposal, executed, snapshot)


def install_research_routes(app, research, actions):
    def error(exc):
        status = 403 if isinstance(exc, Forbidden) else 404 if isinstance(exc, NotFound) else 400
        code = getattr(exc, "code", "")
        if code in {"busy", "trip_busy", "rate_limited", "queue_full", "daily_limit"}:
            status = 429
        elif code == "expired":
            status = 410
        return HTTPException(status_code=status, detail=str(exc))

    @app.get("/api/trips/{trip_id}/research-sources")
    def sources(trip_id: str, member_id: str):
        try:
            return {"sources": research.sources(trip_id, member_id),
                    "consent_enabled": research.consent_enabled(trip_id, member_id)}
        except FairMeetError as exc:
            raise error(exc) from exc

    @app.post("/api/trips/{trip_id}/research-consent")
    def consent(trip_id: str, member_id: str, payload: ConsentRequest):
        try:
            return research.set_consent(trip_id, member_id, payload.enabled)
        except FairMeetError as exc:
            raise error(exc) from exc

    @app.post("/api/trips/{trip_id}/research", status_code=202)
    def create(trip_id: str, member_id: str, payload: ResearchRequest):
        try:
            return JSONResponse(research.create(trip_id, member_id, payload), status_code=202)
        except FairMeetError as exc:
            raise error(exc) from exc

    @app.get("/api/trips/{trip_id}/research/{run_id}")
    def get(trip_id: str, member_id: str, run_id: str):
        try:
            result = research.get(trip_id, member_id, run_id)
            result["context_stale"] = result["input_fingerprint"] != research.fingerprint(trip_id)
            with actions.db.cursor() as cur:
                cur.execute(
                    "SELECT selection_key,result FROM research_selections "
                    "WHERE run_id=? AND member_id=? ORDER BY created_at",
                    (run_id, member_id),
                )
                choices = cur.fetchall()
            candidates = {c["id"]: c for c in result["candidates"]}
            for choice in choices:
                candidate_id, action, _, _ = json.loads(choice["selection_key"])
                candidate = candidates.get(candidate_id)
                if candidate is None:
                    continue
                if action == "dismiss":
                    candidate["status"] = "dismissed"
                elif action == "save":
                    candidate["saved"] = True
            return result
        except FairMeetError as exc:
            raise error(exc) from exc

    @app.post("/api/trips/{trip_id}/research/{run_id}/cancel")
    def cancel(trip_id: str, member_id: str, run_id: str):
        try:
            return research.cancel(trip_id, member_id, run_id)
        except FairMeetError as exc:
            raise error(exc) from exc

    @app.post("/api/trips/{trip_id}/research/{run_id}/plan")
    def plan(trip_id: str, member_id: str, run_id: str, payload: PlanningRequest):
        try:
            return app.state.evidence_planner.build(trip_id, member_id, run_id, payload)
        except FairMeetError as exc:
            raise error(exc) from exc
        except ProposalError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/trips/{trip_id}/research/{run_id}/selections")
    def select(trip_id: str, member_id: str, run_id: str, payload: SelectionRequest):
        try:
            return actions.select(trip_id, member_id, run_id, payload)
        except FairMeetError as exc:
            raise error(exc) from exc
        except ProposalError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
