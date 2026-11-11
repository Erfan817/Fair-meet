"""阶段 D：数据源可用性台账测试（内存库 + FakeGeoAdapter，不联网）。

覆盖：
- SourceRegistry.record/list 往返（available 真假）。
- probe_geo/probe_route：FakeGeoAdapter 可用 -> True 且入台账；
  FakeGeoAdapter(unavailable=True) -> False 且 detail 记录、不抛。
"""

from __future__ import annotations

import pytest

from fairmeet.adapters.fake import FakeGeoAdapter
from fairmeet.ai.sources import SourceRegistry
from fairmeet.db import Database


@pytest.fixture()
def db() -> Database:
    return Database(":memory:")


@pytest.fixture()
def registry(db: Database) -> SourceRegistry:
    return SourceRegistry(db)


# ---------------------------------------------------------------------------
# record / list 往返
# ---------------------------------------------------------------------------
def test_record_and_list_roundtrip(registry: SourceRegistry):
    registry.record("geo", True, "ok")
    registry.record("route", False, "timeout")
    entries = {e["source"]: e for e in registry.list()}
    assert entries["geo"]["available"] is True
    assert entries["route"]["available"] is False
    assert entries["route"]["detail"] == "timeout"


def test_record_upsert_overwrites(registry: SourceRegistry):
    registry.record("geo", True, "first")
    registry.record("geo", False, "second")
    entries = {e["source"]: e for e in registry.list()}
    assert len(entries) == 1  # 同一 source 覆盖
    assert entries["geo"]["available"] is False
    assert entries["geo"]["detail"] == "second"


# ---------------------------------------------------------------------------
# probe_geo / probe_route：可用
# ---------------------------------------------------------------------------
def test_probe_geo_available(registry: SourceRegistry):
    result = registry.probe_geo(FakeGeoAdapter())
    assert result["available"] is True
    entries = {e["source"]: e for e in registry.list()}
    assert entries["geo"]["available"] is True


def test_probe_route_available(registry: SourceRegistry):
    result = registry.probe_route(FakeGeoAdapter())
    assert result["available"] is True
    entries = {e["source"]: e for e in registry.list()}
    assert entries["route"]["available"] is True


# ---------------------------------------------------------------------------
# probe_geo / probe_route：不可用（不抛、记 detail）
# ---------------------------------------------------------------------------
def test_probe_geo_unavailable(registry: SourceRegistry):
    result = registry.probe_geo(FakeGeoAdapter(unavailable=True))
    assert result["available"] is False
    assert result.get("detail")
    entries = {e["source"]: e for e in registry.list()}
    assert entries["geo"]["available"] is False
    assert entries["geo"]["detail"]


def test_probe_route_unavailable(registry: SourceRegistry):
    result = registry.probe_route(FakeGeoAdapter(unavailable=True))
    assert result["available"] is False
    assert result.get("detail")
    entries = {e["source"]: e for e in registry.list()}
    assert entries["route"]["available"] is False
    assert entries["route"]["detail"]
