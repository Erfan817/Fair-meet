"""Explicit browser-only fixture: no actual search/model/map requests.

Used to exercise the new planning UI while upstream public search is blocked.
Every document/summary visibly identifies this fixture as synthetic.
"""

import os
import tempfile

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.ai.orchestrator import FakeLLM
from fairmeet.app import create_app
from fairmeet.content import ContentHit
from fairmeet.db import Database
from fairmeet.demo import DemoSettings


class LabelledFixture:
    source = "public_web"
    is_simulated = False
    daily_limit = 20

    def available(self):
        return True

    def search(self, destination, scope="", *, topic="general"):
        return [
            ContentHit(
                title="【人工夹具，非真实搜索】人民公园与成都博物馆",
                excerpt="本条仅用于界面验收：人民公园。成都博物馆。没有执行网络搜索；没有开放时间或票价。",
                url="https://example.org/ui-test-fixture",
                platform="人工夹具（非真实搜索验收）",
            )
        ]


os.environ["FAIRMEET_PUBLIC_SEARCH_ENABLED"] = "0"
app = create_app(
    Database(os.path.join(tempfile.mkdtemp(prefix="fairmeet-plan-fixture-"), "fixture.db")),
    geo=FakeGeoAdapter(),
    llm=FakeLLM(),
    demo_settings=DemoSettings(),
    content_provider=LabelledFixture(),
)
