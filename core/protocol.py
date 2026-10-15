"""公共常量与工具：前后端接口契约见 docs/PROTOCOL.md。

本模块不依赖任何第三方库。
"""
from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

APP_NAME = "iOS 虚拟定位轨迹工具"
APP_VERSION = "1.0.0"
PROTOCOL_VERSION = "v1"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# 地理常量
EARTH_RADIUS_M = 6371008.8

# 播放引擎默认参数
DEFAULT_SEND_HZ = 5.0
MIN_SEND_HZ = 1.0
MAX_SEND_HZ = 20.0

MODE_DEFAULTS = {
    "driving": 13.9,   # m/s ≈ 50 km/h
    "cycling": 5.5,    # m/s ≈ 20 km/h
    "walking": 1.4,    # m/s ≈ 5 km/h
    "flight": 60.0,    # m/s ≈ 216 km/h
}

MODE_LABELS = {
    "driving": "驾车",
    "cycling": "骑行",
    "walking": "步行",
    "flight": "飞行",
}

MIN_SPEED_SCALE = 0.1
MAX_SPEED_SCALE = 50.0

# 单点定位与轨迹点的合法范围
MIN_LAT, MAX_LAT = -85.05112878, 85.05112878
MIN_LNG, MAX_LNG = -180.0, 180.0


class AppError(Exception):
    """带机器码与中文提示的业务异常。"""

    def __init__(self, message: str, code: str = "app_error", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.message, "code": self.code}


def now_ts() -> float:
    return time.time()


def round6(value: float) -> float:
    return round(float(value), 6)


def clamp(value: float, low: float, high: float) -> float:
    """夹取到 [low, high]。NaN 会直接抛错——NaN 参与比较恒为 False，会静默污染整条链路。"""
    if not math.isfinite(value):
        raise AppError("数值不是有限数（不允许 NaN / Infinity）", "bad_number")
    if value < low:
        return low
    if value > high:
        return high
    return value


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def dumps(obj: Any) -> str:
    """序列化为 JSON。

    allow_nan=False：任何 NaN/Infinity 都会抛错而不是产出 `NaN` 字面量——
    后者会让浏览器 JSON.parse 直接失败，把整帧数据丢掉。
    """
    try:
        return json.dumps(
            obj, ensure_ascii=False, separators=(",", ":"), default=_json_default, allow_nan=False
        )
    except ValueError as exc:
        raise AppError(f"内部数据包含非法数值（NaN/Infinity），已拒绝下发：{exc}", "bad_number") from exc


def finite(value: Any, field: str = "数值") -> float:
    """校验并返回有限浮点数。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AppError(f"{field}必须是数字", "bad_number") from exc
    if not math.isfinite(number):
        raise AppError(f"{field}不是有限数（不允许 NaN / Infinity）", "bad_number")
    return number


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def loads(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


def validate_latlng(lat: Any, lng: Any) -> tuple[float, float]:
    """校验并归一化经纬度，返回 (lat, lng)。"""
    try:
        flat = float(lat)
        flng = float(lng)
    except (TypeError, ValueError) as exc:
        raise AppError("经纬度必须是数字", "bad_coords") from exc
    if math.isnan(flat) or math.isnan(flng) or math.isinf(flat) or math.isinf(flng):
        raise AppError("经纬度不是有效数字", "bad_coords")
    if not (MIN_LAT <= flat <= MAX_LAT):
        raise AppError(f"纬度超出有效范围（{MIN_LAT:.4f} ~ {MAX_LAT:.4f}）", "bad_coords")
    if not (MIN_LNG <= flng <= MAX_LNG):
        raise AppError("经度超出有效范围（-180 ~ 180）", "bad_coords")
    return flat, flng


def validate_speed_scale(value: Any) -> float:
    scale = finite(value, "倍速")
    if scale <= 0:
        raise AppError("倍速必须大于 0", "bad_speed")
    if not (MIN_SPEED_SCALE <= scale <= MAX_SPEED_SCALE):
        raise AppError(
            f"倍速需在 {MIN_SPEED_SCALE} ~ {MAX_SPEED_SCALE} 之间", "bad_speed"
        )
    return scale


def validate_mode(mode: Any) -> str:
    if mode is None:
        return "driving"
    text = str(mode).strip().lower()
    if text not in MODE_DEFAULTS:
        raise AppError(
            "出行方式只支持 driving / cycling / walking / flight", "bad_mode"
        )
    return text
