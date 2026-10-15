"""无设备模拟定位提供者（``--mock`` 模式）。

契约（冻结，见 docs/PROTOCOL.md 第 4 节与 docs/DEVICE_NOTES.md）：

    class MockLocationProvider:
        async def connect(self, device) -> dict
        async def set_location(self, lat, lon) -> None
        async def clear_location(self) -> None
        async def close(self) -> None
        @property
        def connected(self) -> bool

行为：只记录日志、绝不报错，每次下发延时 5~10ms 模拟真机往返。
本模块**不依赖 pymobiledevice3**，任何平台都能 import。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Callable, Optional

__all__ = ["MockLocationProvider", "MOCK_UDID", "MOCK_IOS_VERSION", "MOCK_IOS_MAJOR"]

logger = logging.getLogger("iosloc.location.mock")

#: 与 docs/PROTOCOL.md 第 4 节一致的假设备标识
MOCK_UDID = "MOCK-0001"
MOCK_IOS_VERSION = "26.0"
MOCK_IOS_MAJOR = 26

#: 模拟单次下发往返耗时（秒），遵循任务书 5~10ms 的要求
MOCK_LATENCY_MIN = 0.005
MOCK_LATENCY_MAX = 0.010


def _detect_mock_device_fields(device: Any) -> dict:
    """从 lead 的 DeviceManager 返回的 Device dict 中提取字段（容错）。

    允许 ``device`` 为 ``None`` / dict / 具有同名属性的对象。
    """
    fields = {
        "udid": MOCK_UDID,
        "name": "模拟设备 (iPhone 17 Pro)",
        "ios_version": MOCK_IOS_VERSION,
        "ios_major": MOCK_IOS_MAJOR,
        "connection": "mock",
    }
    if device is None:
        return fields

    def _get(key: str) -> Any:
        if isinstance(device, dict):
            return device.get(key)
        return getattr(device, key, None)

    udid = _get("udid")
    if udid:
        fields["udid"] = str(udid)
    name = _get("name")
    if name:
        fields["name"] = str(name)
    ios_version = _get("ios_version")
    if ios_version:
        fields["ios_version"] = str(ios_version)
    ios_major = _get("ios_major")
    if isinstance(ios_major, int) and ios_major > 0:
        fields["ios_major"] = ios_major
    return fields


class MockLocationProvider:
    """假定位提供者：接口与 :class:`core.location_provider.RealLocationProvider` 完全一致。"""

    def __init__(
        self,
        device: Any = None,
        on_log: Optional[Callable[[str, str], None]] = None,
        latency: Optional[tuple] = None,
        log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """
        :param device: 可选，``DeviceManager`` 返回的 Device dict（仅用于取 udid/版本信息）。
        :param on_log: 可选日志回调 ``(level, msg)``，供服务端把日志推给前端。
        :param latency: 可选 ``(min, max)`` 秒；默认 5~10ms。
        :param log: ``on_log`` 的别名（``core/server.py`` 用 ``MockLocationProvider(log=self.log)``）。
        """
        self._device = device
        self._on_log = log or on_log
        self._latency = latency or (MOCK_LATENCY_MIN, MOCK_LATENCY_MAX)
        self._connected = False
        self._info: dict = {}
        self._last_location: Optional[tuple] = None
        self._send_count = 0
        self._last_log_at = 0.0
        self._started_at = time.time()

    # ------------------------------------------------------------------ 日志
    def _log(self, level: str, msg: str) -> None:
        text = f"[模拟模式] {msg}"
        getattr(logger, level if level in ("debug", "info", "warning", "error") else "info", logger.info)(text)
        if self._on_log is not None:
            try:
                self._on_log(level, text)
            except Exception:  # 日志失败绝不能影响定位流程
                logger.exception("on_log 回调异常（已忽略）")

    async def _sleep_roundtrip(self) -> None:
        """模拟真机 USB/DVT 往返耗时 5~10ms。"""
        low, high = self._latency
        delay = random.uniform(float(low), float(high))
        await asyncio.sleep(delay)

    # -------------------------------------------------------------- 契约接口
    @property
    def connected(self) -> bool:
        """是否已「连接」（mock 下 connect 成功后恒为 True）。"""
        return self._connected

    @property
    def last_location(self) -> Optional[tuple]:
        """最近一次下发的 ``(lat, lon)``，未下发过为 ``None``。"""
        return self._last_location

    @property
    def send_count(self) -> int:
        """累计下发次数（便于自测断言）。"""
        return self._send_count

    async def connect(self, device: Any = None) -> dict:
        """「连接」假设备，永远成功，不抛异常。"""
        if device is not None:
            self._device = device
        self._info = _detect_mock_device_fields(self._device)
        await self._sleep_roundtrip()
        self._connected = True
        self._log("success", f"已连接{self._info['name']}（udid={self._info['udid']}，iOS {self._info['ios_version']}）")
        self._log("info", "模拟模式：定位下发仅记录日志，不会真正修改设备位置")
        return {
            "ok": True,
            "mock": True,
            "udid": self._info["udid"],
            "name": self._info["name"],
            "ios_version": self._info["ios_version"],
            "ios_major": self._info["ios_major"],
            "connection": "mock",
            "tunnel": "connected",
            "detail": f"模拟设备已连接（iOS {self._info['ios_version']}，无需隧道）",
        }

    async def set_location(self, lat: float, lon: float) -> None:
        """记录一次单点定位；永远成功。"""
        lat = float(lat)
        lon = float(lon)
        await self._sleep_roundtrip()
        if not self._connected:
            # mock 模式刻意不报错：自动补一次 connect，保证端到端流程永远走得通
            self._log("warn", "尚未 connect 就收到 set_location，已自动补建模拟会话")
            await self.connect(self._device)
        self._last_location = (lat, lon)
        self._send_count += 1
        # 播放时按 5Hz 下发，逐次刷日志会淹没界面（服务端播放引擎已有"每 5 秒聚合"日志），
        # 这里同样做节流：每 25 次或每 5 秒才记一条。
        _now = time.time()
        if (
            self._send_count <= 3
            or self._send_count % 25 == 0
            or (_now - self._last_log_at) >= 5.0
        ):
            self._last_log_at = _now
            self._log("info", f"下发定位 lat={lat:.6f}, lon={lon:.6f}（第 {self._send_count} 次，模拟）")

    async def clear_location(self) -> None:
        """「恢复真实定位」；永远成功。"""
        await self._sleep_roundtrip()
        if self._last_location is not None:
            lat, lon = self._last_location
            self._log("info", f"清除模拟定位，设备恢复真实定位（上次 lat={lat:.6f}, lon={lon:.6f}，模拟）")
        else:
            self._log("info", "清除模拟定位，设备恢复真实定位（此前未下发过，模拟）")
        self._last_location = None

    async def close(self) -> None:
        """关闭会话；永远成功、幂等。"""
        if not self._connected:
            return
        await self._sleep_roundtrip()
        self._connected = False
        self._log("info", "模拟定位会话已关闭")

    def connect_sync(self, runner: Any, device: Any = None) -> dict:
        """同步建立会话（供同步代码提交给 ``AsyncRunner``，绝不新建事件循环）。

        ``core/server.py`` 的 mock 分支就是 ``provider.connect_sync(self.runner)``。
        """
        if self._connected:
            return dict(self._info)
        run = getattr(runner, "run", None)
        if not callable(run):
            raise RuntimeError("传入的 runner 不支持 run(coro)，无法同步建立模拟会话")
        return run(self.connect(device))

    async def probe(self) -> dict:
        """「测试连接」按钮用：mock 下恒为可用。"""
        major = int(self._info.get("ios_major") or MOCK_IOS_MAJOR)
        return {
            "ok": True,
            "detail": f"模拟模式：DVT 虚拟定位可用（iOS {major}，无需隧道）",
            "ios_major": major,
        }

    # ------------------------------------------------------------------ 辅助
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<MockLocationProvider connected={self._connected} "
            f"udid={self._info.get('udid')!r} sends={self._send_count}>"
        )

    def stats(self) -> dict:
        """运行统计（供 /api/health 或调试使用）。"""
        return {
            "mock": True,
            "connected": self._connected,
            "send_count": self._send_count,
            "last_location": self._last_location,
            "started_at": getattr(self, "_started_at", None) or time.time(),
        }
