"""地理计算：距离、方位角、弧长插值、路径加密。

约定：坐标一律 (lat, lng) 的浮点元组，单位为度，WGS84。
对外 JSON 里的坐标是 [lng, lat]（GeoJSON 习惯），转换在 route_engine 里做。
"""
from __future__ import annotations

import math

from .protocol import EARTH_RADIUS_M, clamp, round6


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点大圆距离（米）。"""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """起点到终点的方位角（度，0=正北，顺时针）。"""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_point(p1: tuple[float, float], p2: tuple[float, float], t: float) -> tuple[float, float]:
    """线性插值（小范围下与测地线插值差异可忽略）。"""
    return (lerp(p1[0], p2[0], t), lerp(p1[1], p2[1], t))


def catmull_rom(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    """Catmull-Rom 样条插值，用于把折线平滑成车道路径。"""
    t2 = t * t
    t3 = t2 * t
    lat = 0.5 * (
        (2 * p1[0])
        + (-p0[0] + p2[0]) * t
        + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
        + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
    )
    lng = 0.5 * (
        (2 * p1[1])
        + (-p0[1] + p2[1]) * t
        + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
        + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
    )
    return (lat, lng)


def cumulative_lengths(path: list[tuple[float, float]]) -> list[float]:
    """返回与 path 等长的累计距离数组（米），首元素为 0。"""
    acc = [0.0]
    total = 0.0
    for i in range(1, len(path)):
        total += haversine(path[i - 1][0], path[i - 1][1], path[i][0], path[i][1])
        acc.append(total)
    return acc


def _binary_search(acc: list[float], value: float) -> int:
    """返回 i 使得 acc[i] <= value < acc[i+1]（value 超界时返回最后一个区间）。"""
    lo, hi = 0, len(acc) - 1
    if value <= acc[0]:
        return 0
    if value >= acc[-1]:
        return max(0, hi - 1)
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if acc[mid] <= value:
            lo = mid
        else:
            hi = mid
    return lo


def interpolate(
    path: list[tuple[float, float]], acc: list[float], distance: float
) -> tuple[tuple[float, float], float]:
    """按弧长在路径上取点，返回 ((lat,lng), 该段内的比例 t)。"""
    if not path:
        raise ValueError("路径为空")
    if len(path) == 1:
        return path[0], 0.0
    i = _binary_search(acc, distance)
    seg = acc[i + 1] - acc[i]
    t = 0.0 if seg <= 1e-9 else clamp((distance - acc[i]) / seg, 0.0, 1.0)
    return lerp_point(path[i], path[i + 1], t), t


def slice_path(
    path: list[tuple[float, float]], acc: list[float], distance: float
) -> list[tuple[float, float]]:
    """返回从起点到指定弧长的子路径（用于前端高亮已播放段）。"""
    if len(path) < 2:
        return list(path)
    i = _binary_search(acc, distance)
    out = path[: i + 1]
    point, _ = interpolate(path, acc, distance)
    out.append(point)
    return out


def densify_segment(
    p1: tuple[float, float], p2: tuple[float, float], step_m: float
) -> list[tuple[float, float]]:
    """在两点之间按 step_m 加密（不含起点，含终点）。"""
    dist = haversine(p1[0], p1[1], p2[0], p2[1])
    if dist <= 1e-6:
        return [p2]
    n = max(1, int(math.ceil(dist / max(1.0, step_m))))
    return [lerp_point(p1, p2, k / n) for k in range(1, n + 1)]


def densify(
    points: list[tuple[float, float]], step_m: float = 2.0, smooth: bool = False
) -> list[tuple[float, float]]:
    """把稀疏折线加密到约 step_m 的采样间隔；smooth=True 时用样条平滑。"""
    pts = _dedupe(points)
    if len(pts) < 2:
        return pts
    if not smooth or len(pts) < 3:
        out = [pts[0]]
        for i in range(1, len(pts)):
            out.extend(densify_segment(pts[i - 1], pts[i], step_m))
        return out

    padded = [pts[0]] + pts + [pts[-1]]
    out = [pts[0]]
    for i in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[i - 1], padded[i], padded[i + 1], padded[i + 2]
        seg_len = haversine(p1[0], p1[1], p2[0], p2[1])
        n = max(1, int(math.ceil(seg_len / max(1.0, step_m))))
        for k in range(1, n + 1):
            out.append(catmull_rom(p0, p1, p2, p3, k / n))
    return out


def _dedupe(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for point in points:
        if not out or haversine(out[-1][0], out[-1][1], point[0], point[1]) > 0.05:
            out.append(point)
    return out


def simplify(points: list[tuple[float, float]], tolerance_m: float = 1.5) -> list[tuple[float, float]]:
    """Douglas-Peucker 简化，用于压缩轨迹体积。"""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        max_dist = -1.0
        index = start
        for i in range(start + 1, end):
            d = _point_segment_distance(points[i], points[start], points[end])
            if d > max_dist:
                max_dist = d
                index = i
        if max_dist > tolerance_m:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
    return [p for p, k in zip(points, keep) if k]


def _point_segment_distance(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """点到线段的近似距离（米）：先把经纬度投影到局部平面。"""
    lat0 = math.radians((a[0] + b[0]) / 2)
    scale_x = EARTH_RADIUS_M * math.cos(lat0) * math.pi / 180.0
    scale_y = EARTH_RADIUS_M * math.pi / 180.0
    px, py = (p[1] - a[1]) * scale_x, (p[0] - a[0]) * scale_y
    bx, by = (b[1] - a[1]) * scale_x, (b[0] - a[0]) * scale_y
    denom = bx * bx + by * by
    if denom <= 1e-12:
        return math.hypot(px, py)
    t = clamp((px * bx + py * by) / denom, 0.0, 1.0)
    return math.hypot(px - t * bx, py - t * by)


def wgs84_to_gcj02_approx(lat: float, lng: float) -> tuple[float, float]:
    """后端不参与地图显示，这里仅提供与前端一致的近似实现（用于自检）。"""
    if lng < 72.004 or lng > 137.8347 or lat < 0.8293 or lat > 55.8271:
        return lat, lng
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return round6(lat + dlat), round6(lng + dlng)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret
