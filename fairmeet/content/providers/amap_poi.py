"""Real Amap POI discovery, not reviews or travel-guide search.

Reuses the explicitly configured real geographic adapter. No credentials are
stored in hits, logs or the research database; no automatic source fallback.
"""

from __future__ import annotations

import os
from urllib.parse import quote

from fairmeet.adapters import GeoUnavailable
from fairmeet.content.models import ContentHit, ContentProviderError
from fairmeet.content.safety import safe_text
from fairmeet.domain import ProvenanceKind


class AmapContentProvider:
    source = "amap_poi"
    is_simulated = False

    def __init__(self, geo=None):
        self.geo = geo

    @property
    def daily_limit(self):
        try:
            return max(0, int(os.environ.get("FAIRMEET_AMAP_SEARCH_DAILY_LIMIT", "20")))
        except ValueError:
            return 0

    def available(self):
        return bool(self.daily_limit > 0 and self.geo is not None and hasattr(self.geo, "search_city"))

    def search(self, destination: str, scope: str = "", *, topic: str = "general") -> list[ContentHit]:
        if not self.available():
            raise ContentProviderError("source_disabled")
        keyword = safe_text(scope, 120) or {
            "culture": "博物馆",
            "outdoors": "公园",
            "dining": "餐厅",
            "general": "公园",
        }.get(topic, "公园")
        try:
            pois = self.geo.search_city(destination, keyword)
        except GeoUnavailable:
            raise ContentProviderError("provider_failure") from None
        hits = []
        seen = set()
        for poi in pois:
            if poi.provenance != ProvenanceKind.EXTERNAL or poi.id in seen:
                continue
            if poi.city.rstrip("市") != destination.rstrip("市"):
                continue
            hits.append(
                ContentHit(
                    title=poi.name,
                    url="https://www.amap.com/place/" + quote(poi.id, safe=""),
                    excerpt=f"名称：{poi.name}；城市：{poi.city}；地址：{poi.address}；分类：{poi.category or '未知'}。"
                    "这是地图POI结构化记录，不含用户点评、实时营业、票价或预约核实。",
                    platform="高德地点检索（非点评/攻略）",
                    content_scope="structured",
                    area=poi.city,
                    category="meal" if topic == "dining" else "activity",
                )
            )
            seen.add(poi.id)
            if len(hits) >= 5:
                break
        return hits

    def read(self, url: str) -> None:
        raise ContentProviderError("unsupported_read")
