"""高德地理编码的早期兼容入口。"""

import httpx


class GeocodeError(Exception):
    """地理编码失败。"""


AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"


def geocode(address: str, key: str) -> tuple[float, float]:
    """将地址解析为经纬度，无法识别时抛出 GeocodeError。"""
    try:
        response = httpx.get(AMAP_GEOCODE_URL, params={"address": address, "key": key}, timeout=10.0)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GeocodeError("地图 API 请求失败") from exc

    if data.get("status") != "1":
        raise GeocodeError(f"地图 API 请求失败: {data.get('info', '未知错误')}")

    geocodes = data.get("geocodes")
    if not geocodes:
        raise GeocodeError(f"地图 API 请求失败: 未找到 {address} 的坐标")

    try:
        longitude, latitude = geocodes[0]["location"].split(",")
        return float(longitude), float(latitude)
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodeError("地图 API 响应结构异常") from exc
