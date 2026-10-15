"""路线引擎：途经点 → 规划路线 → 加密路径 → 速度剖面 → GPX 导入导出。

输出结构（docs/PROTOCOL.md 2.2）：
    {"waypoints": [[lng,lat],...], "path": [[lng,lat],...], "speeds": [...],
     "distance": m, "duration": s, "engine": "osrm"|"straight"|"manual", ...}
播放引擎会用 Route.dense_acc 做弧长插值。
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from . import geoutils, protocol

MAX_PATH_POINTS = 60000
DEFAULT_STEP_M = 2.0
_OSRM_TIMEOUT = 8.0
_UA = "iOSLocationTool/1.0 (+local mac app)"


@dataclass
class Route:
    name: str = "未命名轨迹"
    mode: str = "driving"
    waypoints: list[tuple[float, float]] = field(default_factory=list)  # (lat,lng)
    path: list[tuple[float, float]] = field(default_factory=list)       # (lat,lng)
    speeds: list[float] = field(default_factory=list)                   # m/s
    engine: str = "straight"
    loop: bool = False
    speed_scale: float = 1.0
    _acc: list[float] = field(default_factory=list, repr=False)

    # ---------- 派生属性 ----------
    @property
    def dense_acc(self) -> list[float]:
        if not self._acc and self.path:
            self._acc = geoutils.cumulative_lengths(self.path)
        return self._acc

    @property
    def distance(self) -> float:
        acc = self.dense_acc
        return acc[-1] if acc else 0.0

    @property
    def duration(self) -> float:
        """按速度剖面积分得到的实际耗时（秒）。"""
        if len(self.path) < 2 or not self.speeds:
            return 0.0
        acc = self.dense_acc
        total = 0.0
        for i in range(1, len(self.path)):
            seg = acc[i] - acc[i - 1]
            v = (self.speeds[i] + self.speeds[i - 1]) / 2.0
            total += seg / v if v > 0.05 else seg / 0.05
        return total

    def point_at_distance(self, distance: float) -> tuple[float, float]:
        if not self.path:
            raise protocol.AppError("轨迹为空", "empty_route")
        acc = self.dense_acc
        point, _ = geoutils.interpolate(self.path, acc, distance)
        return point

    def speed_at_distance(self, distance: float) -> float:
        if not self.speeds or len(self.path) < 2:
            return 0.0
        acc = self.dense_acc
        idx = _index_of(acc, distance)
        return self.speeds[min(idx, len(self.speeds) - 1)]

    def heading_at_distance(self, distance: float) -> float:
        """行进方向（度，0=正北）。用于让漂移偏移相对于行进方向计算。"""
        if len(self.path) < 2:
            return 0.0
        acc = self.dense_acc
        idx = _index_of(acc, distance)
        idx = max(0, min(idx, len(self.path) - 2))
        a = self.path[idx]
        b = self.path[idx + 1]
        return geoutils.bearing(a[0], a[1], b[0], b[1])

    def highlight(self, distance: float) -> list[list[float]]:
        acc = self.dense_acc
        pts = geoutils.slice_path(self.path, acc, distance) if len(self.path) > 1 else list(self.path)
        return [[protocol.round6(lng), protocol.round6(lat)] for lat, lng in pts]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "waypoints": [[protocol.round6(lng), protocol.round6(lat)] for lat, lng in self.waypoints],
            "path": [[protocol.round6(lng), protocol.round6(lat)] for lat, lng in self.path],
            "speeds": [round(v, 3) for v in self.speeds],
            "distance": round(self.distance, 2),
            "duration": round(self.duration, 2),
            "engine": self.engine,
            "loop": self.loop,
            "speed_scale": self.speed_scale,
        }


def _index_of(acc: list[float], distance: float) -> int:
    lo, hi = 0, len(acc) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if acc[mid] <= distance:
            lo = mid + 1
        else:
            hi = mid
    return max(0, lo - 1)


class RouteEngine:
    def __init__(self, config: Any) -> None:
        self.config = config

    # ---------------- 构建 ----------------
    def build(
        self,
        waypoint_payloads: list[Any],
        mode: str = "driving",
        name: str | None = None,
        use_osrm: bool = True,
        loop: bool = False,
        speed_scale: float = 1.0,
    ) -> Route:
        mode = protocol.validate_mode(mode)
        speed_scale = protocol.validate_speed_scale(speed_scale)
        coords = parse_waypoints(waypoint_payloads)
        if len(coords) < 1:
            raise protocol.AppError("至少需要一个途经点", "no_waypoints")

        engine = "straight"
        if len(coords) >= 2 and use_osrm:
            planned = self._osrm(coords, mode)
            if planned is not None:
                coords = planned
                engine = "osrm"

        route = Route(
            name=name or "未命名轨迹",
            mode=mode,
            engine=engine,
            loop=bool(loop),
            speed_scale=speed_scale,
        )
        route.path = self._densify(coords)
        route.speeds = compute_speeds(route.path, mode, self._base_speed(mode))
        route._acc = geoutils.cumulative_lengths(route.path)
        route.waypoints = _pick_waypoints(route.path, parse_waypoints(waypoint_payloads))
        return route

    def from_coords(
        self,
        coords: list[tuple[float, float]],
        mode: str = "driving",
        name: str = "导入轨迹",
        engine: str = "manual",
        loop: bool = False,
        speed_scale: float = 1.0,
    ) -> Route:
        if len(coords) < 1:
            raise protocol.AppError("轨迹为空", "no_waypoints")
        route = Route(
            name=name,
            mode=protocol.validate_mode(mode),
            engine=engine,
            loop=bool(loop),
            speed_scale=protocol.validate_speed_scale(speed_scale),
        )
        route.path = self._densify(coords)
        route.speeds = compute_speeds(route.path, route.mode, self._base_speed(route.mode))
        route._acc = geoutils.cumulative_lengths(route.path)
        route.waypoints = _pick_waypoints(route.path, coords)
        return route

    def _densify(self, coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(coords) == 1:
            return list(coords)
        raw = geoutils.densify(coords, DEFAULT_STEP_M, smooth=False)
        if len(raw) <= MAX_PATH_POINTS:
            return raw
        # 超长时按比例降低采样密度，仍保证曲线形状
        rough = geoutils.cumulative_lengths(coords)[-1]
        step = max(DEFAULT_STEP_M, rough / MAX_PATH_POINTS * 1.05)
        out = geoutils.densify(coords, step, smooth=False)
        return out[:MAX_PATH_POINTS] if len(out) > MAX_PATH_POINTS else out

    def _base_speed(self, mode: str) -> float:
        defaults = self.config.get("mode_defaults") or {}
        try:
            value = float(defaults.get(mode, protocol.MODE_DEFAULTS.get(mode, 13.9)))
        except (TypeError, ValueError):
            value = protocol.MODE_DEFAULTS.get(mode, 13.9)
        return max(0.2, value)

    # ---------------- OSRM ----------------
    def _osrm(self, coords: list[tuple[float, float]], mode: str) -> list[tuple[float, float]] | None:
        profile = {"driving": "driving", "cycling": "bike", "walking": "foot", "flight": "driving"}[mode]
        if mode == "flight":
            return None
        base = str(self.config.get("osrm_url") or "").rstrip("/")
        if not base:
            return None
        coords_str = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in coords)
        url = f"{base}/{profile}/{coords_str}?overview=full&geometries=geojson&steps=false"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=_OSRM_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            return None
        try:
            if payload.get("code") != "Ok":
                return None
            geometry = payload["routes"][0]["geometry"]["coordinates"]
        except (KeyError, IndexError, TypeError):
            return None
        out: list[tuple[float, float]] = []
        for item in geometry:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    out.append((float(item[1]), float(item[0])))
                except (TypeError, ValueError):
                    continue
        return out if len(out) >= 2 else None

    def osrm_available(self) -> bool:
        base = str(self.config.get("osrm_url") or "").rstrip("/")
        if not base:
            return False
        try:
            request = urllib.request.Request(
                base.replace("/route/v1", "") + "/route/v1/driving/116.39,39.90;116.40,39.91?overview=false",
                headers={"User-Agent": _UA},
            )
            with urllib.request.urlopen(request, timeout=4.0) as response:
                return response.status == 200
        except Exception:
            return False


# ---------------- 速度剖面 ----------------
def compute_speeds(
    path: list[tuple[float, float]], mode: str, base_speed: float
) -> list[float]:
    """按转弯角度生成速度剖面，并做滑动平均让加减速自然。"""
    n = len(path)
    if n == 0:
        return []
    if n == 1:
        return [base_speed]
    if mode == "flight":
        return _smooth([base_speed] * n, window=1)

    acc = geoutils.cumulative_lengths(path)
    raw = [base_speed] * n
    for i in range(1, n - 1):
        # 用前后各 ~12 米的点计算航向变化，避免逐点抖动
        a = _point_near(path, acc, max(0.0, acc[i] - 12.0))
        b = path[i]
        c = _point_near(path, acc, acc[i] + 12.0)
        angle = abs(_angle_diff(
            geoutils.bearing(a[0], a[1], b[0], b[1]),
            geoutils.bearing(b[0], b[1], c[0], c[1]),
        ))
        factor = 1.0
        if angle >= 100:
            factor = 0.12
        elif angle >= 60:
            factor = 0.25
        elif angle >= 35:
            factor = 0.45
        elif angle >= 18:
            factor = 0.68
        elif angle >= 8:
            factor = 0.86
        raw[i] = base_speed * factor
    raw[0] = raw[1] if n > 1 else base_speed
    raw[-1] = raw[-2] if n > 1 else base_speed
    return _smooth(raw, window=max(3, min(41, n // 4 * 2 + 1)))


def _point_near(
    path: list[tuple[float, float]], acc: list[float], distance: float
) -> tuple[float, float]:
    point, _ = geoutils.interpolate(path, acc, distance)
    return point


def _angle_diff(a: float, b: float) -> float:
    diff = (b - a + 540.0) % 360.0 - 180.0
    return diff


def _smooth(values: list[float], window: int) -> list[float]:
    n = len(values)
    if n <= 2 or window <= 1:
        return [round(v, 3) for v in values]
    half = max(1, window // 2)
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        chunk = values[lo:hi]
        out.append(round(sum(chunk) / len(chunk), 3))
    return out


# ---------------- 途经点处理 ----------------
MAX_WAYPOINTS = 5000
MAX_REQUEST_POINTS = 200000


def parse_waypoints(payloads: Any, limit: int = MAX_WAYPOINTS) -> list[tuple[float, float]]:
    """把 [[lng,lat], ...] 或 [{"lng":..,"lat":..}, ...] 统一成 [(lat,lng)]。"""
    if payloads is None:
        return []
    if not isinstance(payloads, (list, tuple)):
        raise protocol.AppError("waypoints 必须是数组", "bad_waypoints")
    if len(payloads) > limit:
        raise protocol.AppError(
            f"坐标点过多（{len(payloads)} 个，上限 {limit} 个）：请减少途经点或改用 GPX 导入",
            "too_many_points",
            status=413,
        )
    out: list[tuple[float, float]] = []
    for item in payloads:
        if isinstance(item, dict):
            lat = item.get("lat")
            lng = item.get("lng", item.get("lon"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            lng, lat = item[0], item[1]
        else:
            raise protocol.AppError("途经点格式应为 [经度, 纬度]", "bad_waypoints")
        flat, flng = protocol.validate_latlng(lat, lng)
        out.append((flat, flng))
    return out


def _pick_waypoints(
    path: list[tuple[float, float]], wanted: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """用户可能只给了稀疏的途经点，这里保留原始途经点作为列表展示。"""
    return list(wanted) if wanted else list(path[:1] + path[-1:] if len(path) > 1 else path)


# ---------------- GPX ----------------
GPX_NS = "http://www.topografix.com/GPX/1/1"


def to_gpx(route: Route, creator: str = protocol.APP_NAME) -> str:
    root = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": creator,
            "xmlns": GPX_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{GPX_NS} http://www.topografix.com/GPX/1/1/gpx.xsd",
        },
    )
    metadata = ET.SubElement(root, "metadata")
    ET.SubElement(metadata, "name").text = route.name or "未命名轨迹"
    ET.SubElement(metadata, "desc").text = (
        f"{protocol.MODE_LABELS.get(route.mode, route.mode)}轨迹，"
        f"距离 {route.distance / 1000:.2f} km，预计耗时 {_fmt_duration(route.duration)}"
    )
    ET.SubElement(metadata, "time").text = _iso_now()

    for index, (lat, lng) in enumerate(route.waypoints, start=1):
        wpt = ET.SubElement(root, "wpt", {"lat": f"{lat:.7f}", "lon": f"{lng:.7f}"})
        ET.SubElement(wpt, "name").text = f"途经点 {index}"
        ET.SubElement(wpt, "sym").text = "Flag, Blue"

    trk = ET.SubElement(root, "trk")
    ET.SubElement(trk, "name").text = route.name or "未命名轨迹"
    ET.SubElement(trk, "type").text = protocol.MODE_LABELS.get(route.mode, route.mode)
    seg = ET.SubElement(trk, "trkseg")
    acc = route.dense_acc
    for i, (lat, lng) in enumerate(route.path):
        trkpt = ET.SubElement(seg, "trkpt", {"lat": f"{lat:.7f}", "lon": f"{lng:.7f}"})
        if i < len(route.speeds):
            ET.SubElement(trkpt, "speed").text = f"{route.speeds[i]:.3f}"
        if acc:
            ET.SubElement(trkpt, "time").text = _iso_offset(acc[i] / max(0.2, _avg_speed(route)))
    _indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def from_gpx(text: str) -> tuple[list[tuple[float, float]], str, str]:
    """解析 GPX，返回 (坐标[(lat,lng)], 名称, 类型)。优先 trkseg，其次 rte，最后 wpt。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise protocol.AppError("GPX 文件解析失败，请确认文件格式", "bad_gpx") from exc
    tag = _strip_ns(root.tag)
    if tag != "gpx":
        raise protocol.AppError("这不是一个 GPX 文件", "bad_gpx")

    name = ""
    for child in root:
        if _strip_ns(child.tag) == "metadata":
            for item in child:
                if _strip_ns(item.tag) == "name" and item.text:
                    name = item.text.strip()
                    break
        if name:
            break

    coords: list[tuple[float, float]] = []
    for child in root:
        ctag = _strip_ns(child.tag)
        if ctag == "trk":
            if not name:
                for item in child:
                    if _strip_ns(item.tag) == "name" and item.text:
                        name = item.text.strip()
                        break
            for seg in child:
                if _strip_ns(seg.tag) != "trkseg":
                    continue
                for point in seg:
                    parsed = _parse_point(point)
                    if parsed:
                        coords.append(parsed)
        elif ctag == "rte":
            for point in child:
                if _strip_ns(point.tag) == "rtept":
                    parsed = _parse_point(point)
                    if parsed:
                        coords.append(parsed)
    if not coords:
        for child in root:
            if _strip_ns(child.tag) == "wpt":
                parsed = _parse_point(child)
                if parsed:
                    coords.append(parsed)
    if not coords:
        raise protocol.AppError("GPX 里没有可用的轨迹点", "bad_gpx")
    # 不做抽稀：GPX 是需要保形重放的轨迹，丢点会导致导入后形状变化（仅在极端点数时等间隔抽稀）
    coords = _thin_if_huge(coords)
    return coords, (name or "导入的 GPX 轨迹"), "gpx"


def _thin_if_huge(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """仅在点数极多时按等间隔抽稀，保证不丢形状（正常大小的 GPX 原样返回）。"""
    if len(coords) <= MAX_PATH_POINTS:
        return coords
    step = len(coords) / float(MAX_PATH_POINTS)
    out: list[tuple[float, float]] = []
    index = 0.0
    while int(index) < len(coords):
        out.append(coords[int(index)])
        index += step
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def _parse_point(node: ET.Element) -> tuple[float, float] | None:
    lat = node.get("lat")
    lng = node.get("lon")
    if lat is None or lng is None:
        return None
    try:
        flat, flng = protocol.validate_latlng(float(lat), float(lng))
    except (protocol.AppError, TypeError, ValueError):
        return None
    return flat, flng


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _avg_speed(route: Route) -> float:
    if not route.speeds:
        return 1.0
    return max(0.2, sum(route.speeds) / len(route.speeds))


def _fmt_duration(seconds: float) -> str:
    total = int(max(0, round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _iso_now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_offset(seconds: float) -> str:
    import datetime

    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=float(seconds))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _indent(elem: ET.Element, level: int = 0) -> None:
    pad = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + "  "
        for child in elem:
            _indent(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = pad
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = pad


def haversine_total(path: list[tuple[float, float]]) -> float:
    if len(path) < 2:
        return 0.0
    return geoutils.cumulative_lengths(path)[-1]


def sanitize_route_payload(payload: Any) -> Route:
    """前端 play.start 直接传 route 时，从 payload 还原 Route 对象。"""
    if not isinstance(payload, dict):
        raise protocol.AppError("route 必须是对象", "bad_route")
    path_raw = payload.get("path") or []
    path = parse_waypoints(path_raw, limit=MAX_REQUEST_POINTS)
    if len(path) < 1:
        waypoints = parse_waypoints(payload.get("waypoints") or [])
        if len(waypoints) < 1:
            raise protocol.AppError("轨迹为空，请先添加途经点或绘制轨迹", "empty_route")
        path = waypoints
    speeds_raw = payload.get("speeds") or []
    mode = protocol.validate_mode(payload.get("mode"))
    route = Route(
        name=str(payload.get("name") or "未命名轨迹")[:80],
        mode=mode,
        waypoints=parse_waypoints(payload.get("waypoints") or []) or [path[0], path[-1]],
        path=path,
        engine=str(payload.get("engine") or "manual")[:16],
        loop=bool(payload.get("loop")),
        speed_scale=protocol.validate_speed_scale(payload.get("speed_scale") or 1.0),
    )
    if len(speeds_raw) == len(path):
        base = protocol.MODE_DEFAULTS.get(mode, 13.9)
        speeds: list[float] = []
        for value in speeds_raw:
            # 非有限数（NaN/Infinity）或越界值一律回退到基准速度，避免污染整条链路
            try:
                number = float(value)
            except (TypeError, ValueError):
                speeds.append(base)
                continue
            if not math.isfinite(number) or not (0.05 <= number <= 400.0):
                speeds.append(base)
            else:
                speeds.append(number)
        route.speeds = speeds
    else:
        route.speeds = compute_speeds(path, mode, protocol.MODE_DEFAULTS.get(mode, 13.9))
    route._acc = geoutils.cumulative_lengths(path)
    return route


def default_route_json(engine: RouteEngine, waypoints: list[Any], mode: str = "driving") -> dict[str, Any]:
    return engine.build(waypoints, mode=mode).to_json()
