"""Opt-in real search/model/map joint acceptance; never collected by pytest.

No fixtures, source fallback, retries, saved credentials, or normal-trip writes.
Public web remains skipped until the user explicitly confirms official verification.
Each eligible source: one search, one model call, <=5 POI lookups, <=4 routes.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

from fairmeet.adapters.amap import AmapGeoAdapter
from fairmeet.ai.deepseek import DeepSeekError, DeepSeekLLM
from fairmeet.app import create_app
from fairmeet.config import get_amap_web_key, has_amap_web_key
from fairmeet.content import ResearchRequest
from fairmeet.db import Database
from fairmeet.demo import DemoSettings
from fairmeet.domain import DayCreate, TripCreate
from fairmeet.planning import PlanningRequest
from fairmeet.services import FairMeetError


class BoundedModel:
    def __init__(self, key):
        self.client = DeepSeekLLM(key, max_tokens=1500)
        self.calls = 0
        self.error_code = None

    def complete(self, system, user):
        if self.calls >= 1:
            raise FairMeetError("single_model_call_limit")
        self.calls += 1
        try:
            return self.client.complete(system + " 本次验收最多抽取2个具体地点。", user)
        except DeepSeekError as exc:
            # Only retain status class, not URLs, upstream bodies or chained errors.
            text = str(exc)
            self.error_code = next(
                (f"http_{code}" for code in (400, 401, 402, 403, 429, 500, 503) if f"HTTP {code}" in text),
                "model_transport_or_response_error",
            )
            raise


class BoundedGeo(AmapGeoAdapter):
    def __init__(self, key):
        super().__init__(key)
        self.city_calls = 0
        self.route_calls = 0

    def search_city(self, city, keyword):
        if self.city_calls >= 6:
            raise FairMeetError("poi_request_limit")
        self.city_calls += 1
        return super().search_city(city, keyword)

    def route(self, origin, destination, mode):
        if self.route_calls >= 4:
            raise FairMeetError("route_request_limit")
        self.route_calls += 1
        return super().route(origin, destination, mode)


def run_path(source: str, model_key: str, scope: str):
    db = Database(":memory:")
    geo, model = BoundedGeo(get_amap_web_key()), BoundedModel(model_key)
    app = create_app(db, geo=geo, llm=model, demo_settings=DemoSettings())
    research = app.state.research
    outcome = {
        "source": source,
        "status": "failed",
        "stage": "setup",
        "search": {},
        "model": {},
        "map": {},
        "execution": {},
        "error_code": None,
    }
    try:
        trip, member = app.state.service.create_trip(TripCreate(title="真实联合验收·虚构旅程", destination="成都"))
        day = app.state.itinerary.add_day(trip.id, member.id, DayCreate(day_index=1))
        research.set_consent(trip.id, member.id, True)
        outcome["stage"] = "search"
        run = research.create(
            trip.id,
            member.id,
            ResearchRequest(
                message="城市地点与文化活动",
                scope=scope,
                enabled_sources=[source],
                network_consent=True,
                idempotency_key="one-search",
            ),
        )
        deadline = time.monotonic() + 45
        while run["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.1)
            run = research.get(trip.id, member.id, run["id"])
        outcome["search"] = {
            "status": run["status"],
            "is_simulated": run["is_simulated"],
            "usage": run["usage"],
            "documents": [
                {k: d.get(k) for k in ("id", "title", "url", "platform", "content_scope", "fetched_at")}
                for d in run["documents"]
            ],
            "errors": [{"source": e["source"], "code": e["code"]} for e in run["errors"]],
        }
        if run["status"] not in {"succeeded", "partial"} or not run["candidates"]:
            outcome["error_code"] = "search_unavailable_or_empty"
            return outcome
        assert not run["is_simulated"] and all(not d["is_simulated"] for d in run["documents"])
        outcome["stage"] = "model_and_map"
        result = app.state.evidence_planner.build(
            trip.id,
            member.id,
            run["id"],
            PlanningRequest(
                candidate_ids=[c["id"] for c in run["candidates"][:2]],
                day_ids=[day.id],
                idempotency_key="one-plan",
                model_consent=True,
                geo_consent=True,
            ),
        )
        planning = result["planning"]
        outcome["model"] = {
            "mode": planning["model_mode"],
            "calls": model.calls,
            "places": [p["name"] for p in planning["places"]],
        }
        outcome["map"] = {
            "city_requests": geo.city_calls,
            "route_requests": geo.route_calls,
            "places": [
                {
                    "name": p["name"],
                    "match_status": p["match_status"],
                    "poi": p.get("poi"),
                    "evidence_ids": p["evidence_ids"],
                }
                for p in planning["places"]
            ],
            "routes": planning["routes"],
            "warnings": planning["warnings"],
        }
        outcome["stage"] = "confirm"
        before = len(app.state.itinerary.list_items(trip.id))
        assert before == 0 and not app.state.service.list_places(trip.id)
        confirmed = app.state.proposals.confirm(result["proposal"]["id"], member.id)
        items = app.state.itinerary.list_items(trip.id)
        segments = app.state.itinerary.list_transports(trip.id)
        with db.cursor() as cur:
            links = cur.execute("SELECT COUNT(*) FROM research_evidence_links WHERE target_type='item'").fetchone()[0]
        outcome["execution"] = {
            "before_confirm": before,
            "after_confirm": len(items),
            "skipped": len(confirmed["skipped"]),
            "linked_places": sum(item.place_id is not None for item in items),
            "evidence_links": links,
            "transport_segments": len(segments),
            "costs_remain_unknown": all(item.cost is None for item in items),
        }
        all_matched = all(p["match_status"] == "map_matched" for p in planning["places"])
        complete = (
            planning["model_mode"] == "deepseek"
            and len(items) >= 2
            and all_matched
            and len(segments) >= 1
            and not confirmed["skipped"]
            and links == len(items)
        )
        outcome["status"] = "passed" if complete else "partial"
        outcome["stage"] = "finished"
        if not complete:
            outcome["error_code"] = "not_all_places_or_routes_verified"
        return outcome
    except Exception as exc:
        safe_reason = None
        if isinstance(exc, FairMeetError):
            text = str(exc)
            if text.startswith("模型抽取未通过校验"):
                safe_reason = "model_call_or_schema_validation_failed"
            elif text.startswith("资料中没有可逐字核对"):
                safe_reason = "no_literal_evidence_places"
            elif "条件" in text or "旧旅程" in text:
                safe_reason = "context_changed"
        outcome["error_code"] = getattr(exc, "code", None) or model.error_code or safe_reason or type(exc).__name__
        outcome["model"]["calls"] = model.calls
        outcome["map"]["city_requests"] = geo.city_calls
        outcome["map"]["route_requests"] = geo.route_calls
        return outcome
    finally:
        research.close()
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources", nargs="+", choices=["amap_poi", "brave", "public_web"], default=["amap_poi", "brave", "public_web"]
    )
    parser.add_argument(
        "--public-verification-cleared",
        action="store_true",
        help="Use only after the user resolved official verification; no automatic retry.",
    )
    args = parser.parse_args()
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "destination": "成都",
        "uses_fixtures": False,
        "paths": [],
    }
    eligible = []
    for source in dict.fromkeys(args.sources):
        code = None
        if source == "public_web" and not args.public_verification_cleared:
            code = "prior_verification_not_confirmed_cleared"
        elif source == "brave" and not (
            os.getenv("BRAVE_SEARCH_API_KEY")
            and os.getenv("FAIRMEET_RESEARCH_BRAVE_VERIFIED") == "1"
            and os.getenv("FAIRMEET_RESEARCH_DAILY_LIMIT", "0").isdigit()
            and int(os.getenv("FAIRMEET_RESEARCH_DAILY_LIMIT", "0")) > 0
        ):
            code = "missing_brave_configuration"
        elif not has_amap_web_key():
            code = "missing_amap_configuration"
        if code:
            report["paths"].append({"source": source, "status": "blocked", "error_code": code})
            print(f"{source}: blocked ({code}); no request sent.", flush=True)
        else:
            eligible.append(source)
    if eligible:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            key = getpass("DeepSeek API key (hidden; memory only, no saving): ").strip()
        if not key:
            report["paths"].extend(
                {"source": s, "status": "blocked", "error_code": "missing_model_key"} for s in eligible
            )
        else:
            for source in eligible:
                print(f"{source}: starting one real search + one model call + bounded map calls.", flush=True)
                result = run_path(source, key, "公园" if source == "amap_poi" else "")
                report["paths"].append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
    path = Path(__file__).resolve().parents[1] / "docs" / "plan" / "live-joint-acceptance.json"
    # Fresh artifact per execution; never overwrite an earlier acceptance result.
    if path.exists():
        path = path.with_name("live-joint-acceptance-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Sanitized report:", path, flush=True)


if __name__ == "__main__":
    main()
