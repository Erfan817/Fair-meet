"""离线虚构资料；只展示成都样例，绝不借 Geo 的假坐标冒充匹配。"""

from fairmeet.content.models import ContentHit, ContentProviderError


class FakeContentProvider:
    source = "fake"
    is_simulated = True
    daily_limit = 0

    def available(self) -> bool:
        return True

    def search(self, destination: str, scope: str = "", *, topic: str = "general") -> list[ContentHit]:
        if destination.strip() not in {"成都", "成都市", "四川成都", "四川省成都市"}:
            return []
        hits = [
            ContentHit(
                title="虚构示例·成都公园漫步", area="成都示例区域 A", platform="虚构演示资料",
                url="https://example.com/fairmeet/fictional-chengdu-walk",
                excerpt="虚构资料：演示慢节奏活动候选；不代表真实景点、开放情况或无障碍条件。",
            ),
            ContentHit(
                title="虚构示例·成都川菜聚餐", area="成都示例区域 B", category="meal", platform="虚构演示资料",
                url="https://example.com/fairmeet/fictional-chengdu-meal",
                excerpt="虚构资料：演示多人聚餐候选；不是实际商家，不含真实价格、排队或过敏安全信息。",
            ),
            ContentHit(
                title="虚构示例·成都街区文化体验", area="成都示例区域 C", platform="虚构演示资料",
                url="https://example.com/fairmeet/fictional-chengdu-culture",
                excerpt="虚构资料：演示文化体验候选；没有真实地址、预约和日期依据，不可当作可执行行程。",
            ),
        ]
        index = {"outdoors": 0, "dining": 1, "culture": 2}.get(topic)
        return hits if index is None else [hits[index]]

    def read(self, url: str) -> None:
        raise ContentProviderError("unsupported_read")
