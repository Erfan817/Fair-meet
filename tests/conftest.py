"""共享测试夹具（阶段 E 起使用）。"""

from __future__ import annotations

import pytest

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.checks import CheckService
from fairmeet.db import Database
from fairmeet.history import HistoryService
from fairmeet.itinerary import ItineraryService
from fairmeet.replan import ReplanService
from fairmeet.services import TripService


@pytest.fixture()
def db() -> Database:
    return Database(":memory:")


@pytest.fixture()
def trips(db: Database) -> TripService:
    return TripService(db)


@pytest.fixture()
def itin(db: Database, trips: TripService) -> ItineraryService:
    return ItineraryService(db, trips, FakeGeoAdapter())


@pytest.fixture()
def checks(trips: TripService, itin: ItineraryService) -> CheckService:
    return CheckService(trips, itin)


@pytest.fixture()
def hist(db: Database) -> HistoryService:
    return HistoryService(db)


@pytest.fixture()
def replan(trips: TripService, itin: ItineraryService, hist: HistoryService) -> ReplanService:
    return ReplanService(trips, itin, hist)
