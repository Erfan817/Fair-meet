# ⚠️ 学习参考材料 —— 不是项目代码！
#
# 这是 AI 生成的进阶版 geocode 实现，保留在这里作为学习素材。
# 约定：当你能用自己的话解释下面 5 个概念时（见 docs/study-plan.md 的进阶清单），
# 才可以把对应写法吸收进 fairmeet/geocode.py，并在 devlog 里记录"我为什么这样升级"。
#
#   1. `*` 强制关键字参数
#   2. TypedDict / total=False
#   3. raise ... from exc 异常链
#   4. data.get("geocodes") or [] 的两种情况
#   5. cast() 与 pyright 类型检查
#
# ---- 以下为原文件内容 ----

from typing import TypedDict, cast

import httpx


class GeocodeError(Exception):
    """高德地理编码失败：网络异常、HTTP 错误状态码、或高德返回了错误码。"""


class GeocodeEntry(TypedDict):
    """高德返回的一条候选地点，只声明我们实际用得到的字段。"""

    location: str


class GeocodeResponse(TypedDict, total=False):
    """地理编码接口的响应体。

    total=False 表示"字段不保证存在"，因此取值时要用 .get()，
    这样缺字段只会得到 None，而不是运行时的 KeyError。
    """

    status: str
    info: str
    infocode: str
    geocodes: list[GeocodeEntry]


AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"


def geocode(address: str, *, key: str) -> tuple[float, float] | None:
    """把地址解析成 (经度, 纬度)。

    返回 None 表示这个地名高德认不出来（正常业务分支，不是错误）。
    """
    address = address.strip()
    if not address:
        return None

    try:
        response = httpx.get(AMAP_GEOCODE_URL, params={"address": address, "key": key})
    except httpx.HTTPError as exc:
        # 断网 / 超时 / 连接被拒：统一包成 GeocodeError，上层只认一种异常
        raise GeocodeError(f"地图API请求失败: {exc}") from exc

    if response.status_code != 200:
        raise GeocodeError(f"地图API请求失败: {response.status_code} {response.text}")

    # httpx 的 json() 签名返回 Any：在库边界处一次性收敛成 TypedDict，
    # 此后所有字段访问都是具体类型，Unknown / Any 不会再往下扩散
    data = cast(GeocodeResponse, response.json())

    if data.get("status") != "1":
        raise GeocodeError(f"地图API请求失败: {data.get('info')}")

    # 显式标注：空列表 [] 没有类型上下文，不标注会退化成 list[Unknown]
    geocodes: list[GeocodeEntry] = data.get("geocodes") or []
    if not geocodes:
        # 地名认不出来：按 FR-2 交给上层收集进 dropped_locations，而不是崩溃
        return None

    lng_text, lat_text = geocodes[0]["location"].split(",")
    return float(lng_text), float(lat_text)
