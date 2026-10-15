"""设备发现与管理。

优先使用 pymobiledevice3 的 Python API（import 失败时自动降级到 libimobiledevice 的
idevice_id / ideviceinfo 命令行），保证在依赖没装好的机器上也能列出设备。

不丢异常：任何一步失败都写进 Device.last_error，前端只看到中文提示。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import protocol

USBMUX_TIMEOUT = 6.0


@dataclass
class Device:
    udid: str
    name: str = "iPhone"
    product_type: str = ""
    ios_version: str = ""
    ios_major: int = 0
    connection: str = "usb"
    state: str = "connected"
    tunnel: str = "idle"
    simulating: bool = False
    developer_mode: bool | None = None
    tunnel_hint: str | None = None
    last_error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_json(self) -> dict[str, Any]:
        return {
            "udid": self.udid,
            "name": self.name,
            "product_type": self.product_type,
            "ios_version": self.ios_version,
            "ios_major": self.ios_major,
            "connection": self.connection,
            "state": self.state,
            "tunnel": self.tunnel,
            "simulating": self.simulating,
            "developer_mode": self.developer_mode,
            "tunnel_hint": self.tunnel_hint,
            "last_error": self.last_error,
        }


PRODUCT_NAMES = {
    "iPhone10,3": "iPhone X", "iPhone10,6": "iPhone X", "iPhone11,2": "iPhone XS",
    "iPhone11,8": "iPhone XR", "iPhone12,1": "iPhone 11", "iPhone12,3": "iPhone 11 Pro",
    "iPhone13,1": "iPhone 12 mini", "iPhone13,2": "iPhone 12", "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,5": "iPhone 13", "iPhone14,7": "iPhone 14", "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,4": "iPhone 15", "iPhone16,1": "iPhone 15 Pro", "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,3": "iPhone 16", "iPhone18,1": "iPhone 17 Pro", "iPhone18,3": "iPhone 17",
}


def _ios_major(version: str) -> int:
    match = re.match(r"(\d+)", str(version or ""))
    return int(match.group(1)) if match else 0


def _friendly_name(product_type: str, fallback: str) -> str:
    return PRODUCT_NAMES.get(product_type or "", "") or fallback or "iPhone"


class DeviceManager:
    """设备列表 + 选中设备。线程安全，可被 HTTP/WS 线程并发调用。"""

    def __init__(self, mock: bool = False, on_change: Callable[[], None] | None = None) -> None:
        self.mock = mock
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._selected: str | None = None
        self._on_change = on_change
        self._last_signature = ""
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()
        self.idevice_id = shutil.which("idevice_id")
        self.ideviceinfo = shutil.which("ideviceinfo")
        self._backend = "unavailable"
        if mock:
            device = Device(
                udid="MOCK-0001",
                name="模拟设备 (iPhone 17 Pro)",
                product_type="iPhone18,1",
                ios_version="26.0",
                ios_major=26,
                connection="mock",
                state="connected",
                tunnel="connected",
                developer_mode=True,
            )
            self._devices[device.udid] = device
            self._selected = device.udid
            self._backend = "mock"

    # ---------------- 生命周期 ----------------
    def start_monitor(self, interval: float = 3.0) -> None:
        if self.mock or self._monitor is not None:
            return
        self._stop.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, args=(interval,), name="device-monitor", daemon=True)
        self._monitor.start()

    def stop_monitor(self) -> None:
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=2.0)
            self._monitor = None

    def _monitor_loop(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                pass
            self._stop.wait(interval)

    # ---------------- 查询 ----------------
    def _pick_default(self, devices: dict[str, "Device"]) -> str | None:
        """从多台设备里挑一台最可能"真的能用来做虚拟定位"的。

        排序依据（依次比较）：
        1. 是否是"可用"状态（未配对/出错的排后面）；
        2. USB 优先于网络连接（Wi-Fi 连接的设备常常没挂开发者镜像，且隧道不稳）；
        3. iOS 版本更新的优先（新系统对 DVT 支持更完整）。
        之前只取列表第一台，导致同时插着两台设备时随机选中不可用的那台。
        """
        if not devices:
            return None

        def score(device: Device) -> tuple:
            usable = 0 if device.state == "connected" else 1
            usb = 0 if device.connection == "usb" else 1
            return (usable, usb, -(device.ios_major or 0))

        return sorted(devices.values(), key=score)[0].udid

    def refresh(self) -> list[Device]:
        if self.mock:
            return self.list_devices()
        raw_devices = self._discover()
        with self._lock:
            previous = self._devices
            devices: dict[str, Device] = {}
            for item in raw_devices:
                udid = item.get("udid")
                if not udid:
                    continue
                old = previous.get(udid)
                product_type = item.get("product_type") or (old.product_type if old else "")
                device = Device(
                    udid=udid,
                    name=_friendly_name(product_type, item.get("name") or ""),
                    product_type=product_type,
                    ios_version=item.get("ios_version") or (old.ios_version if old else ""),
                    ios_major=_ios_major(item.get("ios_version") or (old.ios_version if old else "")),
                    connection=item.get("connection") or "usb",
                    state=item.get("state") or "connected",
                    tunnel=old.tunnel if old else "idle",
                    simulating=old.simulating if old else False,
                    developer_mode=item.get("developer_mode", old.developer_mode if old else None),
                    tunnel_hint=old.tunnel_hint if old else None,
                    last_error=item.get("last_error") or (old.last_error if old else None),
                    raw=item.get("raw") or {},
                )
                devices[udid] = device
            if self._selected not in devices:
                self._selected = self._pick_default(devices)
            self._devices = devices
            signature = json.dumps(
                [{k: v for k, v in d.to_json().items() if k not in ("simulating",)} for d in devices.values()],
                sort_keys=True,
                ensure_ascii=False,
            ) + f"|{self._selected}"
        self._backend = self._backend if self._backend == "mock" else ("pymobiledevice3" if not raw_devices else self._backend)
        if signature != self._last_signature:
            self._last_signature = signature
            if self._on_change:
                try:
                    self._on_change()
                except Exception:
                    pass
        return self.list_devices()

    def list_devices(self) -> list[Device]:
        with self._lock:
            return list(self._devices.values())

    def devices_json(self) -> dict[str, Any]:
        with self._lock:
            return {
                "devices": [d.to_json() for d in self._devices.values()],
                "selected": self._selected,
            }

    def get(self, udid: str | None = None) -> Device | None:
        with self._lock:
            if udid:
                return self._devices.get(udid)
            if self._selected:
                return self._devices.get(self._selected)
            return next(iter(self._devices.values()), None)

    def get_selected(self) -> Device | None:
        return self.get(None)

    def select(self, udid: str) -> Device:
        with self._lock:
            device = self._devices.get(udid)
            if device is None:
                raise protocol.AppError("设备不存在或已断开，请刷新设备列表", "no_device", status=404)
            self._selected = udid
        self.refresh()
        return self.get(udid) or device

    def selected_udid(self) -> str | None:
        with self._lock:
            return self._selected

    def mark(self, udid: str, **fields: Any) -> None:
        with self._lock:
            device = self._devices.get(udid)
            if device is None:
                return
            for key, value in fields.items():
                if hasattr(device, key):
                    setattr(device, key, value)

    @property
    def backend(self) -> str:
        return self._backend

    # ---------------- 发现实现 ----------------
    def _discover(self) -> list[dict[str, Any]]:
        devices = self._discover_pymobiledevice3()
        if devices is not None:
            return devices
        return self._discover_cli()

    def _discover_pymobiledevice3(self) -> list[dict[str, Any]] | None:
        try:
            from pymobiledevice3.lockdown import create_using_usbmux  # type: ignore
            from pymobiledevice3.usbmux import list_devices as usbmux_list  # type: ignore
        except Exception:
            return None
        try:
            import asyncio

            async def _collect() -> list[dict[str, Any]]:
                found = await usbmux_list()
                out: list[dict[str, Any]] = []
                for item in found:
                    udid = getattr(item, "serial", None) or getattr(item, "udid", None)
                    if not udid:
                        continue
                    # 注意：pymobiledevice3 的 connection_type 区分大小写，必须是 "USB" / "Network"
                    raw_type = str(getattr(item, "connection_type", "USB") or "USB")
                    usb = raw_type.strip().lower() == "usb"
                    entry: dict[str, Any] = {
                        "udid": udid,
                        "connection": "usb" if usb else "network",
                        "state": "connected",
                        "raw": {},
                    }
                    try:
                        client = await create_using_usbmux(
                            serial=udid,
                            autopair=True,
                            connection_type="USB" if usb else "Network",
                            # 配对对话框最长等 12 秒太久了：设备已配对时不会走这里，
                            # 未配对时短超时能避免 /api/devices 与 welcome 长时间阻塞。
                            pair_timeout=3.0,
                        )
                    except Exception as exc:
                        entry["state"] = "unpaired"
                        entry["last_error"] = _friendly_pair_error(exc)
                        out.append(entry)
                        continue
                    try:
                        info: dict[str, Any] = {}
                        for getter in ("short_info", "all_values"):
                            try:
                                candidate = getattr(client, getter, None)
                                if callable(candidate):
                                    candidate = candidate()
                                if isinstance(candidate, dict) and candidate:
                                    info = candidate
                                    break
                            except Exception:
                                continue
                        entry["product_type"] = str(info.get("ProductType") or "")
                        entry["ios_version"] = str(
                            info.get("ProductVersion") or getattr(client, "product_version", "") or ""
                        )
                        entry["name"] = str(info.get("DeviceName") or "")
                        entry["developer_mode"] = await _read_developer_mode(client, info)
                        entry["raw"] = {
                            "build": info.get("BuildVersion"),
                            "model": info.get("HardwareModel"),
                            "connection": raw_type,
                        }
                    except Exception as exc:
                        entry["state"] = "unpaired"
                        entry["last_error"] = _friendly_pair_error(exc)
                    finally:
                        try:
                            closed = client.close()
                            if inspect.isawaitable(closed):
                                await closed
                        except Exception:
                            pass
                    out.append(entry)
                return out

            result = asyncio.run(_collect())
            self._backend = "pymobiledevice3"
            return result
        except Exception:
            return None

    def _discover_cli(self) -> list[dict[str, Any]]:
        if not self.idevice_id:
            self._backend = "unavailable"
            return []
        self._backend = "libimobiledevice"
        try:
            output = subprocess.run(
                [self.idevice_id, "-l"], capture_output=True, text=True, timeout=USBMUX_TIMEOUT
            ).stdout
        except Exception:
            return []
        devices: list[dict[str, Any]] = []
        for line in output.splitlines():
            udid = line.strip()
            if not udid or udid.startswith("Error"):
                continue
            entry: dict[str, Any] = {"udid": udid, "connection": "usb", "state": "connected", "raw": {}}
            if self.ideviceinfo:
                try:
                    info = subprocess.run(
                        [self.ideviceinfo, "-u", udid, "-k", "ProductVersion"],
                        capture_output=True, text=True, timeout=USBMUX_TIMEOUT,
                    )
                    entry["ios_version"] = info.stdout.strip()
                except Exception:
                    pass
                try:
                    info = subprocess.run(
                        [self.ideviceinfo, "-u", udid, "-k", "ProductType"],
                        capture_output=True, text=True, timeout=USBMUX_TIMEOUT,
                    )
                    entry["product_type"] = info.stdout.strip()
                except Exception:
                    pass
                try:
                    info = subprocess.run(
                        [self.ideviceinfo, "-u", udid, "-k", "DeviceName"],
                        capture_output=True, text=True, timeout=USBMUX_TIMEOUT,
                    )
                    entry["name"] = info.stdout.strip()
                except Exception:
                    pass
            entry["name"] = _friendly_name(entry.get("product_type", ""), entry.get("name", ""))
            devices.append(entry)
        return devices


async def _read_developer_mode(client: Any, info: dict[str, Any]) -> bool | None:
    """读取开发者模式状态（该接口在 11.x 是协程）。"""
    for key in ("DeveloperModeStatus", "developer_mode_status"):
        if key in info:
            return bool(info[key])
    getter = getattr(client, "get_developer_mode_status", None)
    if not callable(getter):
        return None
    try:
        result = getter()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    except Exception:
        return None


def _friendly_pair_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "pair" in lowered or "trust" in lowered:
        return "设备未信任此电脑：请在 iPhone 上点「信任」并输入锁屏密码"
    if "password" in lowered or "protected" in lowered:
        return "设备已锁定：请解锁 iPhone 后重试"
    return f"读取设备信息失败：{text[:120]}"


def haversine_is_available() -> bool:
    return shutil.which("pymobiledevice3") is not None


def pymobiledevice3_importable() -> bool:
    try:
        import pymobiledevice3  # noqa: F401

        return True
    except Exception:
        return False


def wait_for_pymobiledevice3(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pymobiledevice3_importable():
            return True
        time.sleep(1.0)
    return pymobiledevice3_importable()
