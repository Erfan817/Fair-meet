"""FairMeet FastAPI 应用入口（阶段 B）。

组装领域服务与 HTTP 路由；同时提供移动端优先的 Jinja2 页面（邀请链接可直接打开）。

- `create_app(db)` 工厂便于测试注入内存数据库。
- 模块级 `app` 供 uvicorn 使用：`uvicorn fairmeet.app:app --reload`。
- 成员身份通过不可猜邀请链接进入后，由服务端下发的 member_id 携带（阶段 B 本机演示，
  邀请链接不作为生产级认证，公网部署前另定正式账户体系，见 07-confirmed-decisions.md）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fairmeet.adapters import GeoAdapter
from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.ai.explain import Explainer
from fairmeet.ai.orchestrator import LLMClient, Orchestrator
from fairmeet.ai.proposal import ProposalError, ProposalService
from fairmeet.ai.sources import SourceRegistry
from fairmeet.ai.tools import Toolbox
from fairmeet.checks import CheckService
from fairmeet.db import Database
from fairmeet.domain import (
    ChangeType,
    DayCreate,
    FieldDiff,
    InspirationCreate,
    InspirationStatus,
    ItemCreate,
    ItemStatus,
    ItemType,
    ItemUpdate,
    MemberJoin,
    NeedCreate,
    NeedKind,
    PlaceCreate,
    ProvenanceKind,
    TransportCreate,
    TransportMode,
    TripCreate,
)
from fairmeet.history import HistoryService
from fairmeet.itinerary import ItineraryService
from fairmeet.replan import ReplanService
from fairmeet.services import FairMeetError, Forbidden, NotFound, TripService

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def _build_geo_adapter(use_amap: bool | None = None) -> GeoAdapter:
    """选择地理适配器。

    - 显式 geo 参数优先（测试注入 Fake）。
    - FAIRMEET_GEO=amap 或（auto 且存在 AMAP_WEB_KEY）→ 真实高德；
    - 否则 Fake（离线可跑）。
    """
    from fairmeet.config import get_amap_web_key, get_geo_mode, has_amap_web_key

    mode = get_geo_mode()
    if use_amap is None:
        use_real = mode == "amap" or (mode == "auto" and has_amap_web_key())
    else:
        use_real = use_amap and has_amap_web_key()
    if mode == "fake":
        use_real = False
    if use_real:
        from fairmeet.adapters.amap import AmapGeoAdapter

        try:
            return AmapGeoAdapter(get_amap_web_key())
        except ValueError:
            return FakeGeoAdapter()
    return FakeGeoAdapter()


def create_app(
    db: Database | None = None,
    geo: GeoAdapter | None = None,
    llm: LLMClient | None = None,
) -> FastAPI:
    app = FastAPI(title="FairMeet", version="0.1.0")
    database = db or Database()
    service = TripService(database)
    geo_adapter = geo or _build_geo_adapter()
    itinerary = ItineraryService(database, service, geo_adapter)
    checks = CheckService(service, itinerary)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # 阶段 D：AI 协调组件。LLM 选择：显式注入优先；否则有 DEEPSEEK_API_KEY 用真实 DeepSeek，否则 FakeLLM(离线)。
    if llm is None:
        from fairmeet.ai.deepseek import build_llm_from_env

        llm_client = build_llm_from_env()
    else:
        llm_client = llm
    explainer = Explainer(service, checks)
    sources = SourceRegistry(database)

    def toolbox_factory(trip_id: str, member_id: str) -> Toolbox:
        return Toolbox(service, itinerary, checks, trip_id, member_id)

    proposals = ProposalService(database, toolbox_factory)
    orchestrator = Orchestrator(service, proposals, explainer, sources, llm_client, itinerary=itinerary)
    history = HistoryService(database)
    replan = ReplanService(service, itinerary, history)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.db = database
    app.state.service = service
    app.state.itinerary = itinerary
    app.state.checks = checks
    app.state.geo = geo_adapter
    app.state.proposals = proposals
    app.state.orchestrator = orchestrator
    app.state.sources = sources
    app.state.history = history
    app.state.replan = replan

    def svc() -> TripService:
        return app.state.service

    def itin() -> ItineraryService:
        return app.state.itinerary

    def chk() -> CheckService:
        return app.state.checks

    def prop() -> ProposalService:
        return app.state.proposals

    def orch() -> Orchestrator:
        return app.state.orchestrator

    def hist() -> HistoryService:
        return app.state.history

    def rp() -> ReplanService:
        return app.state.replan

    def _err(exc: FairMeetError) -> HTTPException:
        if isinstance(exc, NotFound):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, Forbidden):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    # ======================================================================
    # JSON API
    # ======================================================================
    @app.post("/api/trips")
    def api_create_trip(payload: TripCreate, service: TripService = Depends(svc)):
        trip, owner = service.create_trip(payload)
        return {
            "trip": trip.model_dump(),
            "owner": owner.model_dump(),
            "invite_url": f"/join/{trip.invite_token}",
        }

    @app.get("/api/trips/{trip_id}")
    def api_get_trip(trip_id: str, service: TripService = Depends(svc)):
        try:
            return service.get_trip(trip_id).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.post("/api/trips/{trip_id}/members")
    def api_join_trip(trip_id: str, payload: MemberJoin, service: TripService = Depends(svc)):
        try:
            return service.add_member(trip_id, payload).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.get("/api/trips/{trip_id}/members")
    def api_list_members(trip_id: str, service: TripService = Depends(svc)):
        return [m.model_dump() for m in service.list_members(trip_id)]

    @app.post("/api/trips/{trip_id}/needs")
    def api_add_need(
        trip_id: str, payload: NeedCreate, member_id: str, service: TripService = Depends(svc)
    ):
        try:
            return service.add_need(trip_id, member_id, payload).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.get("/api/trips/{trip_id}/needs")
    def api_list_needs(trip_id: str, service: TripService = Depends(svc)):
        return [n.model_dump() for n in service.list_needs(trip_id)]

    @app.post("/api/trips/{trip_id}/places")
    def api_add_place(
        trip_id: str, payload: PlaceCreate, member_id: str, service: TripService = Depends(svc)
    ):
        try:
            return service.add_place(trip_id, member_id, payload).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.get("/api/trips/{trip_id}/places")
    def api_list_places(trip_id: str, service: TripService = Depends(svc)):
        return [p.model_dump() for p in service.list_places(trip_id)]

    @app.post("/api/trips/{trip_id}/inspirations")
    def api_add_inspiration(
        trip_id: str, payload: InspirationCreate, member_id: str, service: TripService = Depends(svc)
    ):
        try:
            return service.add_inspiration(trip_id, member_id, payload).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.get("/api/trips/{trip_id}/inspirations")
    def api_list_inspirations(trip_id: str, service: TripService = Depends(svc)):
        return [i.model_dump() for i in service.list_inspirations(trip_id)]

    # ======================================================================
    # 移动端页面（Jinja2，服务端渲染，邀请链接可直接打开）
    # ======================================================================
    @app.get("/", response_class=HTMLResponse)
    def page_home(request: Request):
        return templates.TemplateResponse(request, "index.html", {"trips": []})

    @app.post("/ui/trips")
    def ui_create_trip(
        title: str = Form(...),
        owner_name: str = Form("发起人"),
        destination: str = Form("成都"),
        start_date: str = Form(""),
        end_date: str = Form(""),
        budget: str = Form(""),
        service: TripService = Depends(svc),
    ):
        payload = TripCreate(
            title=title,
            owner_name=owner_name,
            destination=destination or "成都",
            start_date=start_date or None,
            end_date=end_date or None,
            budget=float(budget) if budget.strip() else None,
        )
        trip, owner = service.create_trip(payload)
        return RedirectResponse(url=f"/trip/{trip.id}?member={owner.id}", status_code=303)

    @app.get("/join/{token}", response_class=HTMLResponse)
    def page_join(request: Request, token: str, service: TripService = Depends(svc)):
        try:
            trip = service.get_trip_by_token(token)
        except NotFound:
            return templates.TemplateResponse(request, "join_invalid.html", {}, status_code=404)
        return templates.TemplateResponse(request, "join.html", {"trip": trip})

    @app.post("/join/{token}")
    def ui_join(token: str, display_name: str = Form(...), service: TripService = Depends(svc)):
        try:
            trip = service.get_trip_by_token(token)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="邀请链接无效") from exc
        member = service.add_member(trip.id, MemberJoin(display_name=display_name))
        return RedirectResponse(url=f"/trip/{trip.id}?member={member.id}", status_code=303)

    @app.get("/trip/{trip_id}", response_class=HTMLResponse)
    def page_trip(request: Request, trip_id: str, member: str = "", service: TripService = Depends(svc)):
        try:
            trip = service.get_trip(trip_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="旅程不存在") from exc
        members = service.list_members(trip_id)
        needs = service.list_needs(trip_id)
        inspirations = service.list_inspirations(trip_id)
        places = service.list_places(trip_id)
        me = None
        if member:
            try:
                me = service.require_member(trip_id, member)
            except Forbidden:
                me = None
        return templates.TemplateResponse(
            request,
            "trip.html",
            {
                "trip": trip,
                "members": members,
                "needs": needs,
                "inspirations": inspirations,
                "places": places,
                "me": me,
                "need_kinds": list(NeedKind),
                "insp_statuses": list(InspirationStatus),
            },
        )

    @app.post("/ui/trips/{trip_id}/needs")
    def ui_add_need(
        trip_id: str,
        member: str = Form(...),
        kind: str = Form(...),
        category: str = Form(...),
        value: str = Form(...),
        service: TripService = Depends(svc),
    ):
        try:
            service.add_need(trip_id, member, NeedCreate(kind=NeedKind(kind), category=category, value=value))
        except FairMeetError as exc:
            raise _err(exc) from exc
        return RedirectResponse(url=f"/trip/{trip_id}?member={member}", status_code=303)

    @app.post("/ui/trips/{trip_id}/inspirations")
    def ui_add_inspiration(
        trip_id: str,
        member: str = Form(...),
        title: str = Form(...),
        note: str = Form(""),
        source_url: str = Form(""),
        place_name: str = Form(""),
        service: TripService = Depends(svc),
    ):
        payload = InspirationCreate(
            title=title,
            note=note or None,
            source_url=source_url or None,
            place_name=place_name or None,
        )
        try:
            service.add_inspiration(trip_id, member, payload)
        except FairMeetError as exc:
            raise _err(exc) from exc
        return RedirectResponse(url=f"/trip/{trip_id}?member={member}", status_code=303)

    @app.post("/ui/trips/{trip_id}/inspirations/{insp_id}/status")
    def ui_set_insp_status(
        trip_id: str,
        insp_id: str,
        member: str = Form(...),
        status: str = Form(...),
        service: TripService = Depends(svc),
    ):
        try:
            service.set_inspiration_status(trip_id, member, insp_id, InspirationStatus(status))
        except FairMeetError as exc:
            raise _err(exc) from exc
        return RedirectResponse(url=f"/trip/{trip_id}?member={member}", status_code=303)

    # ======================================================================
    # 阶段 C：逐日行程 / 交通段 / 检查 / 预算（JSON API）
    # ======================================================================
    @app.post("/api/trips/{trip_id}/days")
    def api_add_day(trip_id: str, payload: DayCreate, member_id: str, itin: ItineraryService = Depends(itin)):
        try:
            return itin.add_day(trip_id, member_id, payload).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.get("/api/trips/{trip_id}/days")
    def api_list_days(trip_id: str, itin: ItineraryService = Depends(itin)):
        return [d.model_dump() for d in itin.list_days(trip_id)]

    @app.post("/api/trips/{trip_id}/days/{day_id}/items")
    def api_add_item(
        trip_id: str, day_id: str, payload: ItemCreate, member_id: str, itin: ItineraryService = Depends(itin)
    ):
        try:
            return itin.add_item(trip_id, member_id, day_id, payload).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.get("/api/trips/{trip_id}/items")
    def api_list_items(trip_id: str, day_id: str | None = None, itin: ItineraryService = Depends(itin)):
        return [i.model_dump() for i in itin.list_items(trip_id, day_id)]

    @app.patch("/api/trips/{trip_id}/items/{item_id}")
    def api_update_item(
        trip_id: str, item_id: str, payload: ItemUpdate, member_id: str, itin: ItineraryService = Depends(itin)
    ):
        try:
            return itin.update_item(trip_id, member_id, item_id, payload).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.post("/api/trips/{trip_id}/items/{item_id}/status")
    def api_set_item_status(
        trip_id: str, item_id: str, member_id: str, status: ItemStatus, itin: ItineraryService = Depends(itin)
    ):
        try:
            return itin.set_item_status(trip_id, member_id, item_id, status).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.post("/api/trips/{trip_id}/days/{day_id}/transports")
    def api_add_transport(
        trip_id: str,
        day_id: str,
        payload: TransportCreate,
        member_id: str,
        compute: bool = True,
        itin: ItineraryService = Depends(itin),
    ):
        try:
            return itin.add_transport(trip_id, member_id, day_id, payload, compute=compute).model_dump()
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.get("/api/trips/{trip_id}/transports")
    def api_list_transports(trip_id: str, day_id: str | None = None, itin: ItineraryService = Depends(itin)):
        return [s.model_dump() for s in itin.list_transports(trip_id, day_id)]

    @app.get("/api/trips/{trip_id}/checks")
    def api_checks(trip_id: str, chk: CheckService = Depends(chk)):
        return chk.check(trip_id).model_dump()

    @app.get("/api/trips/{trip_id}/budget")
    def api_budget(trip_id: str, chk: CheckService = Depends(chk)):
        return chk.budget(trip_id).model_dump()

    @app.get("/api/trips/{trip_id}/map-data")
    def api_map_data(trip_id: str, itin: ItineraryService = Depends(itin), service: TripService = Depends(svc)):
        """地图数据：地点坐标 + 每日条目序列。坐标缺失的点标记 unknown。"""
        places = {p.id: p for p in service.list_places(trip_id)}
        days = itin.list_days(trip_id)
        items = itin.list_items(trip_id)
        out_days = []
        for d in days:
            day_items = []
            for it in [i for i in items if i.day_id == d.id]:
                p = places.get(it.place_id) if it.place_id else None
                day_items.append(
                    {
                        "id": it.id,
                        "title": it.title,
                        "item_type": it.item_type.value,
                        "status": it.status.value,
                        "place": (
                            {"name": p.name, "lng": p.lng, "lat": p.lat} if p and p.lng is not None else None
                        ),
                    }
                )
            out_days.append({"day_index": d.day_index, "date": d.date, "title": d.title, "items": day_items})
        return {"days": out_days, "places_with_coords": sum(1 for p in places.values() if p.lng is not None)}

    # ======================================================================
    # 阶段 C：行程编辑页 + 时间线/地图页（移动端）
    # ======================================================================
    @app.get("/trip/{trip_id}/plan", response_class=HTMLResponse)
    def page_plan(
        request: Request,
        trip_id: str,
        member: str = "",
        itin: ItineraryService = Depends(itin),
        chk: CheckService = Depends(chk),
        service: TripService = Depends(svc),
    ):
        try:
            trip = service.get_trip(trip_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="旅程不存在") from exc
        me = None
        if member:
            try:
                me = service.require_member(trip_id, member)
            except Forbidden:
                me = None
        days = itin.list_days(trip_id)
        items = itin.list_items(trip_id)
        transports = itin.list_transports(trip_id)
        places = service.list_places(trip_id)
        result = chk.check(trip_id)
        budget = chk.budget(trip_id)
        return templates.TemplateResponse(
            request,
            "plan.html",
            {
                "trip": trip,
                "me": me,
                "days": days,
                "items": items,
                "transports": transports,
                "places": places,
                "check": result,
                "budget": budget,
                "item_types": list(ItemType),
                "item_statuses": list(ItemStatus),
                "transport_modes": list(TransportMode),
            },
        )

    @app.get("/trip/{trip_id}/map", response_class=HTMLResponse)
    def page_map(request: Request, trip_id: str, service: TripService = Depends(svc)):
        try:
            trip = service.get_trip(trip_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="旅程不存在") from exc
        return templates.TemplateResponse(request, "map.html", {"trip": trip})

    @app.post("/ui/trips/{trip_id}/days")
    def ui_add_day(
        trip_id: str,
        member: str = Form(...),
        day_index: int = Form(...),
        date: str = Form(""),
        title: str = Form(""),
        itin: ItineraryService = Depends(itin),
    ):
        try:
            itin.add_day(trip_id, member, DayCreate(day_index=day_index, date=date or None, title=title or None))
        except FairMeetError as exc:
            raise _err(exc) from exc
        return RedirectResponse(url=f"/trip/{trip_id}/plan?member={member}", status_code=303)

    @app.post("/ui/trips/{trip_id}/days/{day_id}/items")
    def ui_add_item(
        trip_id: str,
        day_id: str,
        member: str = Form(...),
        item_type: str = Form("activity"),
        title: str = Form(...),
        place_id: str = Form(""),
        start_time: str = Form(""),
        end_time: str = Form(""),
        stay_minutes: str = Form(""),
        cost: str = Form(""),
        cost_provenance_kind: str = Form("estimated"),
        note: str = Form(""),
        itin: ItineraryService = Depends(itin),
        hist: HistoryService = Depends(hist),
    ):
        payload = ItemCreate(
            item_type=ItemType(item_type),
            title=title,
            place_id=place_id or None,
            start_time=start_time or None,
            end_time=end_time or None,
            stay_minutes=int(stay_minutes) if stay_minutes.strip() else None,
            cost=float(cost) if cost.strip() else None,
            cost_provenance_kind=ProvenanceKind(cost_provenance_kind),
            note=note or None,
        )
        try:
            item = itin.add_item(trip_id, member, day_id, payload)
            hist.record(
                trip_id,
                ChangeType.ITEM_CREATE,
                f"新增行程条目：{item.title}",
                member_id=member,
                target_ref=item.id,
                diffs=[FieldDiff(field="title", before=None, after=item.title)],
            )
        except FairMeetError as exc:
            raise _err(exc) from exc
        return RedirectResponse(url=f"/trip/{trip_id}/plan?member={member}", status_code=303)

    @app.post("/ui/trips/{trip_id}/items/{item_id}/status")
    def ui_set_item_status(
        trip_id: str,
        item_id: str,
        member: str = Form(...),
        status: str = Form(...),
        itin: ItineraryService = Depends(itin),
        hist: HistoryService = Depends(hist),
    ):
        try:
            before = itin.get_item(trip_id, item_id)
            updated = itin.set_item_status(trip_id, member, item_id, ItemStatus(status))
            hist.record(
                trip_id,
                ChangeType.ITEM_STATUS,
                f"条目「{updated.title}」状态变更为 {updated.status.value}",
                member_id=member,
                target_ref=item_id,
                diffs=[FieldDiff(field="status", before=before.status.value, after=updated.status.value)],
            )
        except FairMeetError as exc:
            raise _err(exc) from exc
        return RedirectResponse(url=f"/trip/{trip_id}/plan?member={member}", status_code=303)

    # ======================================================================
    # 阶段 D：AI 协调（对话 / 提案 / 确认 / 解释 / 来源台账）
    # ======================================================================
    @app.post("/api/trips/{trip_id}/ai/chat")
    def api_ai_chat(trip_id: str, payload: dict, member_id: str, orch: Orchestrator = Depends(orch)):
        message = (payload or {}).get("message", "")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(status_code=400, detail="message 不能为空")
        try:
            return orch.handle(trip_id, member_id, message)
        except FairMeetError as exc:
            raise _err(exc) from exc

    @app.get("/api/trips/{trip_id}/ai/explain")
    def api_ai_explain(trip_id: str):
        # 解释冲突只需旅程存在；直接复用 orchestrator 的确定性路径
        return app.state.orchestrator.explainer.explain_conflicts(trip_id)

    @app.get("/api/trips/{trip_id}/proposals")
    def api_list_proposals(trip_id: str, prop: ProposalService = Depends(prop)):
        return [p.model_dump(mode="json") for p in prop.list(trip_id)]

    @app.get("/api/trips/{trip_id}/proposals/{proposal_id}")
    def api_get_proposal(trip_id: str, proposal_id: str, prop: ProposalService = Depends(prop)):
        try:
            return prop.get(proposal_id).model_dump(mode="json")
        except ProposalError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/trips/{trip_id}/proposals/{proposal_id}/confirm")
    def api_confirm_proposal(
        trip_id: str, proposal_id: str, member_id: str,
        prop: ProposalService = Depends(prop), hist: HistoryService = Depends(hist),
    ):
        try:
            result = prop.confirm(proposal_id, member_id)
        except ProposalError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        hist.record(
            trip_id,
            ChangeType.PROPOSAL_CONFIRM,
            f"确认提案并执行 {len(result.get('executed', []))} 项修改",
            member_id=member_id,
            target_ref=proposal_id,
        )
        return result

    @app.post("/api/trips/{trip_id}/proposals/{proposal_id}/reject")
    def api_reject_proposal(trip_id: str, proposal_id: str, member_id: str, prop: ProposalService = Depends(prop)):
        try:
            return prop.reject(proposal_id, member_id).model_dump(mode="json")
        except ProposalError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sources")
    def api_sources():
        return app.state.sources.list()

    @app.post("/api/sources/probe")
    def api_probe_sources(member_id: str | None = None):
        geo = app.state.geo
        return {
            "geo": app.state.sources.probe_geo(geo),
            "route": app.state.sources.probe_route(geo),
            "registry": app.state.sources.list(),
        }

    @app.get("/trip/{trip_id}/assistant", response_class=HTMLResponse)
    def page_assistant(request: Request, trip_id: str, member: str = "", service: TripService = Depends(svc)):
        try:
            trip = service.get_trip(trip_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="旅程不存在") from exc
        me = None
        if member:
            try:
                me = service.require_member(trip_id, member)
            except Forbidden:
                me = None
        proposals_list = app.state.proposals.list(trip_id)
        return templates.TemplateResponse(
            request,
            "assistant.html",
            {"trip": trip, "me": me, "proposals": proposals_list},
        )

    # ======================================================================
    # 阶段 E：变更历史 / 局部重排 / 通知 / 摘要 / 途中查看
    # ======================================================================
    @app.get("/api/trips/{trip_id}/history")
    def api_history(trip_id: str, limit: int = 200, hist: HistoryService = Depends(hist)):
        return [e.model_dump(mode="json") for e in hist.list_entries(trip_id, limit)]

    @app.get("/api/trips/{trip_id}/updates")
    def api_updates(trip_id: str, since: str | None = None, hist: HistoryService = Depends(hist)):
        entries = hist.updates_since(trip_id, since)
        return {"since": since, "count": len(entries), "entries": [e.model_dump(mode="json") for e in entries]}

    @app.get("/api/trips/{trip_id}/revisions")
    def api_revisions(trip_id: str, hist: HistoryService = Depends(hist)):
        return [r.model_dump(mode="json") for r in hist.list_revisions(trip_id)]

    @app.post("/api/trips/{trip_id}/revisions")
    def api_snapshot(
        trip_id: str,
        member_id: str,
        label: str = "手动快照",
        hist: HistoryService = Depends(hist),
        service: TripService = Depends(svc),
        itin: ItineraryService = Depends(itin),
    ):
        try:
            service.require_member(trip_id, member_id)
        except FairMeetError as exc:
            raise _err(exc) from exc
        snap = _trip_snapshot(service, itin, trip_id)
        return hist.snapshot(trip_id, label, snap, member_id=member_id).model_dump(mode="json")

    @app.post("/api/trips/{trip_id}/replan")
    def api_replan(
        trip_id: str,
        member_id: str,
        payload: dict,
        rp_service: ReplanService = Depends(rp),
        hist: HistoryService = Depends(hist),
        service: TripService = Depends(svc),
        itin: ItineraryService = Depends(itin),
    ):
        from fairmeet.domain import ReplanRequest

        try:
            req = ReplanRequest.model_validate(payload or {})
            result = rp_service.replan_day(trip_id, member_id, req)
        except FairMeetError as exc:
            raise _err(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 重排后保存一个版本快照，便于回看
        snap = _trip_snapshot(service, itin, trip_id)
        hist.snapshot(trip_id, "局部重排后", snap, member_id=member_id, change_log_id=result.change_log_id)
        return result.model_dump(mode="json")

    @app.get("/api/trips/{trip_id}/summary")
    def api_summary(trip_id: str, chk: CheckService = Depends(chk), itin: ItineraryService = Depends(itin),
                    service: TripService = Depends(svc)):
        return _build_summary(service, itin, chk, trip_id)

    @app.get("/trip/{trip_id}/summary", response_class=HTMLResponse)
    def page_summary(
        request: Request, trip_id: str, service: TripService = Depends(svc),
        itin: ItineraryService = Depends(itin), chk: CheckService = Depends(chk),
    ):
        try:
            trip = service.get_trip(trip_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="旅程不存在") from exc
        summary = _build_summary(service, itin, chk, trip_id)
        return templates.TemplateResponse(request, "summary.html", {"trip": trip, "summary": summary})

    @app.get("/trip/{trip_id}/ontrip", response_class=HTMLResponse)
    def page_ontrip(
        request: Request, trip_id: str, member: str = "", service: TripService = Depends(svc),
        itin: ItineraryService = Depends(itin), chk: CheckService = Depends(chk),
    ):
        try:
            trip = service.get_trip(trip_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="旅程不存在") from exc
        me = None
        if member:
            try:
                me = service.require_member(trip_id, member)
            except Forbidden:
                me = None
        days = itin.list_days(trip_id)
        items = itin.list_items(trip_id)
        transports = itin.list_transports(trip_id)
        check = chk.check(trip_id)
        return templates.TemplateResponse(
            request, "ontrip.html",
            {"trip": trip, "me": me, "days": days, "items": items, "transports": transports, "check": check},
        )

    @app.get("/trip/{trip_id}/history", response_class=HTMLResponse)
    def page_history(request: Request, trip_id: str, service: TripService = Depends(svc),
                     hist: HistoryService = Depends(hist)):
        try:
            trip = service.get_trip(trip_id)
        except NotFound as exc:
            raise HTTPException(status_code=404, detail="旅程不存在") from exc
        entries = hist.list_entries(trip_id)
        revisions = hist.list_revisions(trip_id)
        return templates.TemplateResponse(
            request, "history.html", {"trip": trip, "entries": entries, "revisions": revisions}
        )

    @app.get("/api/health")
    def health():
        return JSONResponse({"status": "ok"})

    return app


def _trip_snapshot(service: TripService, itin: ItineraryService, trip_id: str) -> dict:
    """生成旅程行程快照（用于版本）。"""
    days = itin.list_days(trip_id)
    items = itin.list_items(trip_id)
    transports = itin.list_transports(trip_id)
    return {
        "days": [d.model_dump(mode="json") for d in days],
        "items": [i.model_dump(mode="json") for i in items],
        "transports": [t.model_dump(mode="json") for t in transports],
    }


def _build_summary(service: TripService, itin: ItineraryService, checks: CheckService, trip_id: str) -> dict:
    """出发前摘要：基本信息 + 每日条目 + 预算 + 检查结果（含来源/估算标注）。"""
    trip = service.get_trip(trip_id)
    days = itin.list_days(trip_id)
    items = itin.list_items(trip_id)
    budget = checks.budget(trip_id)
    check = checks.check(trip_id)
    day_summaries = []
    for d in days:
        day_items = sorted([i for i in items if i.day_id == d.id], key=lambda x: x.sort_order)
        day_summaries.append(
            {
                "day_index": d.day_index,
                "date": d.date,
                "title": d.title,
                "items": [
                    {
                        "title": i.title,
                        "item_type": i.item_type.value,
                        "status": i.status.value,
                        "start_time": i.start_time,
                        "end_time": i.end_time,
                        "cost": i.cost,
                        "cost_provenance": i.cost_provenance.kind.value,
                    }
                    for i in day_items
                ],
            }
        )
    return {
        "trip": {
            "title": trip.title,
            "destination": trip.destination,
            "start_date": trip.start_date,
            "end_date": trip.end_date,
            "status": trip.status.value,
        },
        "members": [m.display_name for m in service.list_members(trip_id)],
        "days": day_summaries,
        "budget": budget.model_dump(),
        "check": check.model_dump(),
    }


app = create_app()
