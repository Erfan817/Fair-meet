#计算多个（经纬度）点的球面公平中心
def geographic_center(points: list[tuple[float, float]]) -> tuple[float, float]:
    """
    计算多个（经纬度）点的球面公平中心。

    参数:
        points (list[tuple[float, float]]): 包含多个点的经纬度列表，每个点为 (经度, 纬度)。

    返回:
        tuple[float, float]: 球面公平中心的 (经度, 纬度)。
    """
    import math

    if not points:
        raise ValueError("点列表不能为空")

    x = 0.0
    y = 0.0
    z = 0.0

    for lng, lat in points:
        lat_rad = math.radians(lat)
        lng_rad = math.radians(lng)

        x += math.cos(lat_rad) * math.cos(lng_rad)
        y += math.cos(lat_rad) * math.sin(lng_rad)
        z += math.sin(lat_rad)

    total_points = len(points)
    x /= total_points
    y /= total_points
    z /= total_points

    central_lng = math.atan2(y, x)
    hyp = math.sqrt(x * x + y * y)
    central_lat = math.atan2(z, hyp)

    return (math.degrees(central_lng), math.degrees(central_lat))