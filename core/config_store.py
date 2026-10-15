"""配置存储：config.json 的读写与默认值（见 docs/PROTOCOL.md 2.3）。"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from . import protocol

DEFAULT_TILE_SOURCES: dict[str, dict[str, Any]] = {
    "amap": {
        "name": "高德矢量（带标注）",
        "url": "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}",
        "subdomains": ["1", "2", "3", "4"],
        "max_zoom": 18,
        "attribution": "© 高德地图",
        "coord": "gcj02",
    },
    "amap_sat": {
        "name": "高德卫星（含路网标注）",
        "url": "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
        "subdomains": ["1", "2", "3", "4"],
        "max_zoom": 18,
        "attribution": "© 高德地图",
        "coord": "gcj02",
    },
    "arcgis_sat": {
        "name": "全球卫星(Esri)",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "subdomains": [],
        "max_zoom": 19,
        "attribution": "Esri, Maxar, Earthstar Geographics",
        "coord": "wgs84",
    },
    "osm": {
        "name": "OpenStreetMap（国内不可达）",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "subdomains": [],
        "max_zoom": 19,
        "attribution": "© OpenStreetMap contributors",
        "coord": "wgs84",
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "tile_source": "amap",
    "tile_sources": DEFAULT_TILE_SOURCES,
    "tile_cache_dir": "~/.ios-location-tiles",
    "tile_cache_max_mb": 1024,
    "send_hz": protocol.DEFAULT_SEND_HZ,
    "default_speed_scale": 1.0,
    "mode_defaults": dict(protocol.MODE_DEFAULTS),
    "osrm_url": "https://router.project-osrm.org/route/v1",
    "last_device": None,
    "window": {"width": 1360, "height": 860},
    # 真实感模拟：GPS 漂移与速度浮动（校园跑等 App 测试用）
    "drift_enabled": True,
    "drift_m": 4.0,
    "speed_jitter_enabled": True,
    "speed_jitter": 0.08,
    # 真实感进阶：发送节奏抖动 / 速度由位移推算 / 粗差注入
    "timing_jitter_enabled": True,
    "derive_speed": True,
    "gross_error_enabled": False,
    "gross_error_probability": 0.0,
    "gross_error_m": 30.0,
}

_IMMUTABLE_KEYS = {"tile_sources"}


class ConfigStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self._repaired = False
        self.load()

    # ------------ 持久化 ------------
    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._merge(raw)
                except Exception:
                    backup = self.path.with_suffix(".bak.json")
                    try:
                        self.path.replace(backup)
                    except OSError:
                        pass
            # 归一化必须放在补齐瓦片源之前：文件里可能是被写坏的字符串等非法值
            self._normalize()
            sources = self._data.get("tile_sources")
            if not isinstance(sources, dict):
                sources = copy.deepcopy(DEFAULT_TILE_SOURCES)
            for key, value in DEFAULT_TILE_SOURCES.items():
                sources.setdefault(key, copy.deepcopy(value))
            self._data["tile_sources"] = sources
            if self._data.get("tile_source") not in sources:
                self._data["tile_source"] = "amap"
                self._repaired = True
            return copy.deepcopy(self._data)

    def _normalize(self) -> None:
        """把每一项都过一遍校验，非法项回退到默认值。"""
        for key, default in DEFAULT_CONFIG.items():
            if key not in self._data:
                self._data[key] = copy.deepcopy(default)
                continue
            try:
                self._data[key] = self._validate(key, self._data[key])
            except protocol.AppError:
                self._data[key] = copy.deepcopy(default)
                self._repaired = True
        try:
            self._data["tile_sources"] = self._validate("tile_sources", self._data.get("tile_sources"))
        except protocol.AppError:
            self._data["tile_sources"] = copy.deepcopy(DEFAULT_TILE_SOURCES)
            self._repaired = True
        if self._data.get("tile_source") not in (self._data.get("tile_sources") or {}):
            self._data["tile_source"] = "amap"
            self._repaired = True

    def _merge(self, incoming: dict[str, Any]) -> None:
        for key, value in incoming.items():
            if key in _IMMUTABLE_KEYS and isinstance(value, dict):
                merged = copy.deepcopy(DEFAULT_TILE_SOURCES)
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, dict):
                        merged[sub_key] = {**merged.get(sub_key, {}), **sub_value}
                self._data[key] = merged
            else:
                self._data[key] = value

    def save(self) -> None:
        with self._lock:
            payload = copy.deepcopy(self._data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ------------ 访问 ------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def update(self, patch: dict[str, Any], persist: bool = True) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise protocol.AppError("配置必须是 JSON 对象", "bad_config")
        with self._lock:
            allowed = set(DEFAULT_CONFIG) | {"tile_sources"}
            unknown = [k for k in patch if k not in allowed]
            if unknown:
                raise protocol.AppError(
                    "不支持的配置项：" + "、".join(unknown), "bad_config"
                )
            validated = {}
            incoming = patch.get("tile_sources")
            self._incoming_sources = incoming if isinstance(incoming, dict) else None
            try:
                for key, value in patch.items():
                    validated[key] = self._validate(key, value)
            finally:
                self._incoming_sources = None
            self._merge(validated)
        if persist:
            self.save()
        return self.snapshot()

    def _validate(self, key: str, value: Any) -> Any:
        """逐项做类型与范围校验：非法值必须被拒绝，绝不能写进运行态。

        历史上 `tile_sources` 被写成字符串会导致所有瓦片请求 500（地图永久空白），
        `send_hz` 被写成 NaN 会让 welcome 变成非法 JSON，因此这里逐项严格校验。
        """
        if key == "tile_sources":
            if not isinstance(value, dict) or not value:
                raise protocol.AppError("tile_sources 必须是「源名称 → 配置」的对象", "bad_config")
            cleaned: dict[str, Any] = {}
            for name, item in value.items():
                if not isinstance(name, str) or not name.strip():
                    raise protocol.AppError("瓦片源名称必须是非空字符串", "bad_config")
                if not isinstance(item, dict):
                    raise protocol.AppError(f"瓦片源 {name} 的配置必须是对象", "bad_config")
                url = item.get("url")
                if url is not None and (not isinstance(url, str) or "{z}" not in url):
                    raise protocol.AppError(
                        f"瓦片源 {name} 的 url 必须包含 {{z}}/{{x}}/{{y}} 占位符", "bad_config"
                    )
                subs = item.get("subdomains") or []
                if not isinstance(subs, list) or any(not isinstance(s, (str, int)) for s in subs):
                    raise protocol.AppError(f"瓦片源 {name} 的 subdomains 必须是字符串数组", "bad_config")
                if "max_zoom" in item:
                    zoom = protocol.finite(item["max_zoom"], f"{name}.max_zoom")
                    if not (0 <= zoom <= 22):
                        raise protocol.AppError(f"瓦片源 {name} 的 max_zoom 需在 0~22 之间", "bad_config")
                    item = {**item, "max_zoom": int(zoom)}
                cleaned[name] = {**DEFAULT_TILE_SOURCES.get(name, {}), **item}
            return cleaned
        if key == "tile_source":
            if not isinstance(value, str) or not value.strip():
                raise protocol.AppError("tile_source 必须是非空字符串", "bad_config")
            # 同一请求里若同时提供了 tile_sources，就以新集合为准（否则自定义源会被误拒）
            incoming = getattr(self, "_incoming_sources", None)
            known = incoming if isinstance(incoming, dict) and incoming else (
                self._data.get("tile_sources") or DEFAULT_TILE_SOURCES
            )
            if value not in known:
                raise protocol.AppError(f"未知的瓦片源：{value}", "bad_config")
            return value
        if key == "tile_cache_dir":
            if not isinstance(value, str) or not value.strip():
                raise protocol.AppError("tile_cache_dir 必须是非空路径字符串", "bad_config")
            return value
        if key == "tile_cache_max_mb":
            number = protocol.finite(value, "tile_cache_max_mb")
            if not (64 <= number <= 102400):
                raise protocol.AppError("瓦片缓存上限需在 64 ~ 102400 MB 之间", "bad_config")
            return int(number)
        if key == "send_hz":
            number = protocol.finite(value, "send_hz")
            if not (protocol.MIN_SEND_HZ <= number <= protocol.MAX_SEND_HZ):
                raise protocol.AppError(
                    f"下发频率需在 {protocol.MIN_SEND_HZ} ~ {protocol.MAX_SEND_HZ} Hz 之间", "bad_config"
                )
            return number
        if key == "default_speed_scale":
            return protocol.validate_speed_scale(value)
        if key in ("drift_enabled", "speed_jitter_enabled", "timing_jitter_enabled",
                   "derive_speed", "gross_error_enabled"):
            if not isinstance(value, bool):
                raise protocol.AppError(f"{key} 必须是 true/false", "bad_config")
            return value
        if key == "gross_error_probability":
            number = protocol.finite(value, "粗差概率")
            if not (0.0 <= number <= 0.5):
                raise protocol.AppError("粗差概率需在 0 ~ 0.5 之间", "bad_config")
            return number
        if key == "gross_error_m":
            number = protocol.finite(value, "粗差幅度")
            if not (1.0 <= number <= 500.0):
                raise protocol.AppError("粗差幅度需在 1 ~ 500 米之间", "bad_config")
            return number
        if key == "drift_m":
            number = protocol.finite(value, "漂移半径")
            if not (0.0 <= number <= 50.0):
                raise protocol.AppError("漂移半径需在 0 ~ 50 米之间", "bad_config")
            return number
        if key == "speed_jitter":
            number = protocol.finite(value, "速度浮动幅度")
            if 1.0 < number <= 100.0:
                number = number / 100.0   # 允许传百分数，但 500% 这种非法值不能变成 5%
            if not (0.0 <= number <= 0.6):
                raise protocol.AppError("速度浮动幅度需在 0 ~ 60%（或 0 ~ 0.6）之间", "bad_config")
            return number
        if key == "mode_defaults":
            if not isinstance(value, dict):
                raise protocol.AppError("mode_defaults 必须是对象", "bad_config")
            out: dict[str, float] = {}
            for mode, speed in value.items():
                if mode not in protocol.MODE_DEFAULTS:
                    raise protocol.AppError(f"未知出行方式：{mode}", "bad_config")
                number = protocol.finite(speed, f"{mode} 基准速度")
                if not (0.2 <= number <= 400):
                    raise protocol.AppError(f"{mode} 基准速度需在 0.2 ~ 400 m/s 之间", "bad_config")
                out[mode] = number
            return {**protocol.MODE_DEFAULTS, **out}
        if key == "osrm_url":
            if not isinstance(value, str):
                raise protocol.AppError("osrm_url 必须是字符串", "bad_config")
            return value.strip()
        if key == "last_device":
            if value is not None and not isinstance(value, str):
                raise protocol.AppError("last_device 必须是字符串或 null", "bad_config")
            return value
        if key == "window":
            if not isinstance(value, dict):
                raise protocol.AppError("window 必须是对象", "bad_config")
            out_window: dict[str, int] = {}
            for name in ("width", "height"):
                if name in value:
                    number = protocol.finite(value[name], f"window.{name}")
                    if not (200 <= number <= 10000):
                        raise protocol.AppError(f"window.{name} 需在 200 ~ 10000 之间", "bad_config")
                    out_window[name] = int(number)
            return {**(self._data.get("window") or {}), **out_window}
        return value

    def tile_sources(self) -> dict[str, dict[str, Any]]:
        return self.get("tile_sources") or {}

    def tile_source(self, key: str | None = None) -> tuple[str, dict[str, Any]]:
        sources = self.tile_sources()
        wanted = key or self.get("tile_source") or "amap"
        if wanted not in sources:
            wanted = "amap"
        return wanted, sources[wanted]

    def cache_dir(self) -> Path:
        raw = str(self.get("tile_cache_dir") or "~/.ios-location-tiles")
        return Path(raw).expanduser()
