"""Explicit opt-in real-model smoke, never collected by pytest.

Read DEEPSEEK_API_KEY from the process environment. Exactly one model request;
no search request and no map request. Public search may be separately blocked.
Only synthetic travel context and a labelled extraction fixture are transmitted.
"""

from __future__ import annotations

import json
import os
import sys

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.ai.deepseek import DeepSeekLLM
from fairmeet.app import create_app
from fairmeet.content import ContentHit, ResearchRequest
from fairmeet.db import Database
from fairmeet.demo import DemoSettings
from fairmeet.domain import DayCreate, TripCreate
from fairmeet.planning import PlanningRequest


class ExtractionFixture:
    source = "public_web"
    is_simulated = False
    daily_limit = 1

    def available(self):
        return True

    def search(self, destination, scope="", *, topic="general"):
        return [
            ContentHit(
                title="地点抽取验收夹具：成都公园与场馆",
                excerpt="本条为人工编写的验收夹具，不是实时搜索结果。人民公园、成都博物馆是待核对地点名称；"
                "不提供营业、票价、预约或安全结论。",
                url="https://example.org/labelled-extraction-fixture",
                platform="人工编写抽取夹具（非搜索验收）",
            )
        ]


def main():
    import time

    if not os.environ.get("DEEPSEEK_API_KEY"):
        from getpass import getpass

        key = getpass("DeepSeek API key (hidden; memory only): ").strip()
        if not key:
            raise SystemExit("No credential entered; no model request sent.")
        os.environ["DEEPSEEK_API_KEY"] = key
    db = Database(":memory:")
    model = DeepSeekLLM(os.environ["DEEPSEEK_API_KEY"], max_tokens=1500)
    app = create_app(
        db, geo=FakeGeoAdapter(), llm=model, demo_settings=DemoSettings(), content_provider=ExtractionFixture()
    )
    research = app.state.research
    try:
        trip, member = app.state.service.create_trip(TripCreate(title="虚构验收旅程", destination="成都"))
        day = app.state.itinerary.add_day(trip.id, member.id, DayCreate(day_index=1))
        research.set_consent(trip.id, member.id, True)
        run = research.create(
            trip.id,
            member.id,
            ResearchRequest(
                message="地点抽取验收",
                enabled_sources=["public_web"],
                network_consent=True,
                idempotency_key="single-model-smoke",
            ),
        )
        for _ in range(100):
            run = research.get(trip.id, member.id, run["id"])
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        result = app.state.evidence_planner.build(
            trip.id,
            member.id,
            run["id"],
            PlanningRequest(
                candidate_ids=[run["candidates"][0]["id"]],
                day_ids=[day.id],
                idempotency_key="single-plan",
                model_consent=True,
                geo_consent=False,
            ),
        )
        before = len(app.state.itinerary.list_items(trip.id))
        confirmed = app.state.proposals.confirm(result["proposal"]["id"], member.id)
        after = len(app.state.itinerary.list_items(trip.id))
        print(
            json.dumps(
                {
                    "model_mode": result["planning"]["model_mode"],
                    "places": [p["name"] for p in result["planning"]["places"]],
                    "before_confirm": before,
                    "after_confirm": after,
                    "skipped": len(confirmed["skipped"]),
                    "search_verified": False,
                    "input": "labelled synthetic extraction fixture",
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        # Never print repr, upstream body, tracebacks or credentials.
        print("Model smoke failed:", type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        research.close()
        db.close()


if __name__ == "__main__":
    main()
