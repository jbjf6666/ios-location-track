"""真机虚拟定位后端：pymobiledevice3 DVT / 老版 simulatelocation。

冻结契约（docs/PROTOCOL.md 5 节、task-2）：

    class RealLocationProvider:
        def __init__(self, rsd_host=None, rsd_port=None, udid=None)   # 可加可选关键字参数
        async def connect(self, device) -> dict
        async def set_location(self, lat, lon) -> None
        async def clear_location(self) -> None
        async def close(self) -> None
        @property
        def connected(self) -> bool
        async def probe(self) -> dict          # {"ok":bool,"detail":中文,"ios_major":int}

所有 API 调用均取自 pymobiledevice3 **真实源码**（版本与出处见 docs/DEVICE_NOTES.md）：

* ``pymobiledevice3/services/dvt/instruments/dvt_provider.py:8``  ``class DvtProvider``
* ``pymobiledevice3/services/dvt/instruments/location_simulation.py:17`` ``class LocationSimulation``
  （``set(latitude, longitude)`` / ``clear()``，async context manager）
* ``pymobiledevice3/dtx_service_provider.py:80`` ``DvtProvider.service_name_for()`` 自动在
  RSD（``com.apple.instruments.dtservicehub``，iOS17+）与 lockdown
  （``com.apple.instruments.remoteserver.DVTSecureSocketProxy``，iOS<17）之间选择。
* ``pymobiledevice3/remote/remote_service_discovery.py:103`` ``RemoteServiceDiscoveryService(address)``
* ``pymobiledevice3/services/simulate_location.py:8`` ``DtSimulateLocation``（iOS<17 老服务
  ``com.apple.dt.simulatelocation``，作为 DVT 失败时的兜底）

设计要点：
* **长连接**：``connect()`` 建立一次 DVT 会话，之后 ``set_location`` 复用同一个 DTX channel，
  绝不每次重连（DVT 定位在连接关闭后即失效，见 DEVICE_NOTES.md 的 CLI 取证）。
* **同一事件循环**：全部为原生 asyncio，不开线程、不新建事件循环。
* **友好降级**：pymobiledevice3 缺失 / 隧道未建立 / 无设备 / 未开开发者模式，都抛带中文说明的
  :class:`LocationProviderError`，不会暴露 AttributeError 之类的内部错误。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from . import platform as plat

__all__ = [
    "RealLocationProvider",
    "LocationProviderError",
    "pymobiledevice3_available",
    "PMD3_MISSING_HINT",
]

logger = logging.getLogger("iosloc.location.real")

#: 保活重发间隔（秒）。参考同类工具经验：<3s 没有额外收益，还容易在隧道里堆积消息。
KEEPALIVE_INTERVAL = 15.0

#: 当前平台的解释器路径 / 启动脚本（错误文案里直接给用户可粘贴的命令）
_PY = ".venv\\Scripts\\python.exe" if plat.IS_WINDOWS else "./.venv/bin/python"
_RUN = ".\\scripts\\run.ps1" if plat.IS_WINDOWS else "./run.sh"

#: 未安装 pymobiledevice3 时的中文修复建议（含本机实测可用的阿里云镜像）
PMD3_MISSING_HINT = (
    "未检测到 pymobiledevice3 库。请在项目目录执行："
    f"{_PY} -m pip install --index-url https://mirrors.aliyun.com/pypi/simple/ pymobiledevice3"
)

#: iOS 17 是「必须走 RSD 隧道」的分界线
RSD_REQUIRED_IOS_MAJOR = 17

#: 隧道提示（PROTOCOL.md 的 tunnel_hint 语义）。两个平台的建立方式不同，文案必须分开：
#: macOS 借 Apple remoted（--native，免 root）；Windows 用 userspace 隧道（免管理员）。
if plat.IS_WINDOWS:
    TUNNEL_HINT = (
        "iOS 17 及以上必须先用 RSD 隧道才能使用 DVT 虚拟定位。"
        "本程序会自动建立 userspace 隧道（不需要管理员权限）；若失败请重新运行 "
        f"{_RUN} 并查看日志。也请确认已关闭爱思助手等会抢占 RSD 连接的工具。"
    )
else:
    TUNNEL_HINT = (
        "iOS 17 及以上必须先用 RSD 隧道才能使用 DVT 虚拟定位："
        "请以管理员权限运行 run.sh（sudo ./run.sh）；"
        "macOS 上也可用免 root 的隧道："
        "./.venv/bin/python -m pymobiledevice3 remote start-tunnel --native --script-mode"
    )


class LocationProviderError(RuntimeError):
    """定位后端异常，``str(e)`` 一律是可直接展示给用户的中文信息。"""


# ---------------------------------------------------------------------------
# pymobiledevice3 懒加载：未安装时 import core.location_provider 仍然成功
# ---------------------------------------------------------------------------
_pmd3_cache: Optional[dict] = None
_pmd3_error: Optional[str] = None


def _import_pymobiledevice3() -> dict:
    """惰性导入 pymobiledevice3（成功结果缓存；失败抛 :class:`LocationProviderError`）。"""
    global _pmd3_cache, _pmd3_error
    if _pmd3_cache is not None:
        return _pmd3_cache
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.simulate_location import DtSimulateLocation
        from pymobiledevice3 import usbmux as usbmux_module
    except Exception as exc:  # ImportError / 依赖缺失 / 版本不兼容
        _pmd3_error = f"{type(exc).__name__}: {exc}"
        logger.error("导入 pymobiledevice3 失败：%s", _pmd3_error)
        raise LocationProviderError(f"{PMD3_MISSING_HINT}（详细错误：{_pmd3_error}）") from exc
    _pmd3_cache = {
        "create_using_usbmux": create_using_usbmux,
        "RemoteServiceDiscoveryService": RemoteServiceDiscoveryService,
        "DvtProvider": DvtProvider,
        "LocationSimulation": LocationSimulation,
        "DtSimulateLocation": DtSimulateLocation,
        "usbmux": usbmux_module,
    }
    return _pmd3_cache


def pymobiledevice3_available() -> bool:
    """pymobiledevice3 是否可用（不抛异常，供 /api/health、UI 提示使用）。"""
    try:
        _import_pymobiledevice3()
    except LocationProviderError:
        return False
    return True


def pymobiledevice3_version() -> Optional[str]:
    """返回已安装的 pymobiledevice3 版本号，取不到返回 ``None``。"""
    try:
        from importlib.metadata import version

        return version("pymobiledevice3")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _parse_ios_major(value: Any) -> int:
    """把 ``"26.0"`` / ``26`` / ``"17.4.1"`` 解析成主版本号；失败返回 0。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    head = text.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def _device_field(device: Any, key: str, default: Any = None) -> Any:
    if device is None:
        return default
    if isinstance(device, dict):
        value = device.get(key, default)
    else:
        value = getattr(device, key, default)
    return default if value is None else value


def _wrap(action: str, exc: BaseException) -> LocationProviderError:
    """把底层异常翻译成中文可读信息。"""
    if isinstance(exc, LocationProviderError):
        return exc
    name = type(exc).__name__
    text = str(exc) or name
    low = text.lower()

    # StartServiceError 的 __init__ 不把信息传给基类（exceptions.py:125-129），str() 为空
    if name == "StartServiceError":
        service_name = getattr(exc, "service_name", "?")
        message = getattr(exc, "message", "") or ""
        text = f"StartServiceError({service_name}): {message}"
        low = text.lower()

    if isinstance(exc, (ConnectionRefusedError,)) or "connection refused" in low or "errno 61" in low:
        detail = "无法连接 RSD 隧道（连接被拒绝），隧道可能已断开或尚未建立"
    elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timed out" in low:
        detail = "操作超时，设备可能未响应或隧道已断开"
    elif "developerdiskimage" in low or "developer disk image" in low or "mounter mount" in low:
        detail = (
            "设备未挂载开发者镜像（DeveloperDiskImage），DVT 服务不可用。"
            f"请执行：{_PY} -m pymobiledevice3 mounter auto-mount"
        )
    elif "developer mode" in low or "developermode" in low:
        detail = "设备未开启「开发者模式」：设置 → 隐私与安全性 → 开发者模式（需重启设备）"
    elif "invalidservice" in low or "no such service" in low or "startserviceerror" in low:
        detail = (
            "设备未提供所需的开发者服务。iOS<17 通常是尚未挂载开发者镜像，请执行："
            f"{_PY} -m pymobiledevice3 mounter auto-mount；"
            "iOS 17+ 则需要 RSD 隧道（隧道提示见下）"
        )
    elif "not paired" in low or "notpaired" in low or "pair" in low and "invalid" in low:
        detail = "设备未与此电脑配对：请在 iPhone 上点击「信任此电脑」，或先执行 pymobiledevice3 lockdown pair"
    elif "no device" in low or "nodeviceconnected" in low:
        detail = "未发现 iOS 设备：请用 USB 连接 iPhone，解锁并在手机上点击「信任」"
    else:
        detail = f"底层错误：{name}: {text}"
    return LocationProviderError(f"{action}失败：{detail}")


# ---------------------------------------------------------------------------
# 真机 Provider
# ---------------------------------------------------------------------------
class RealLocationProvider:
    """真机定位提供者。兼容 iOS<17（lockdown 直连 DVT）与 iOS≥17（RSD 隧道 + DVT）。

    策略（``self.strategy``）：

    ``dvt-rsd``
        iOS ≥ 17 且提供了 RSD 地址（或注入了已连接的 RSD 对象）：
        ``RemoteServiceDiscoveryService((host, port))`` → ``DvtProvider(rsd)`` →
        ``LocationSimulation``。
    ``dvt-lockdown``
        没有 RSD 地址时走 usbmux/lockdown：``create_using_usbmux(serial=udid)`` →
        ``DvtProvider(lockdown)`` → ``LocationSimulation``（适用于 iOS<17）。
    ``legacy``
        ``dvt-lockdown`` 失败时的兜底：``DtSimulateLocation``（老服务
        ``com.apple.dt.simulatelocation``，仅 iOS<17）。
    """

    def __init__(
        self,
        rsd_host: Optional[str] = None,
        rsd_port: Optional[int] = None,
        udid: Optional[str] = None,
        *,
        rsd: Any = None,
        lockdown: Any = None,
        connect_timeout: float = 15.0,
        on_log: Optional[Callable[[str, str], None]] = None,
        log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """
        :param rsd_host: RSD 隧道主机（由 lead 的 core/tunnel.py 提供），无隧道时为 ``None``。
        :param rsd_port: RSD 隧道端口。
        :param udid: 目标设备 UDID；``None`` 表示用第一台 USB 设备。
        :param rsd: **可选扩展**：已连接好的 ``RemoteServiceDiscoveryService`` 对象。用于把免 root 的
            进程内 userspace 隧道（``UserspaceRsdTunnel``）直接注入进来；给了之后就忽略 host/port。
        :param lockdown: **可选扩展**：已连接好的 ``LockdownClient``，避免重复建连。
        :param connect_timeout: 建立会话的超时秒数。
        :param on_log: 日志回调 ``(level, msg)``，供服务端推给前端。
        :param log: ``on_log`` 的别名（``core/server.py`` 用 ``RealLocationProvider(..., log=self.log)``）。
        """
        self._rsd_host = rsd_host
        self._rsd_port = int(rsd_port) if rsd_port else None
        self._udid = udid
        self._injected_rsd = rsd
        self._injected_lockdown = lockdown
        self._connect_timeout = float(connect_timeout)
        self._on_log = log or on_log

        self._connected = False
        self._strategy: Optional[str] = None
        self._session_info: dict = {}
        self._last_location: Optional[tuple] = None
        self._send_count = 0
        #: 保活：最后一次成功下发的单调时间 + 后台任务
        self._last_send_mono = 0.0
        self._keepalive_task: Optional[asyncio.Task] = None
        self._keepalive_count = 0
        self._connected_at: Optional[float] = None

        # pymobiledevice3 对象
        self._rsd_obj: Any = None
        self._lockdown_obj: Any = None
        self._dvt: Any = None
        self._location: Any = None
        self._legacy: Any = None

        # 连接过程中从设备侧探测到的真实信息
        self._detected_version: Optional[str] = None
        self._detected_udid: Optional[str] = None

    # ------------------------------------------------------------------ 日志
    def _log(self, level: str, msg: str) -> None:
        text = f"[真机] {msg}"
        log_fn = getattr(logger, level if level in ("debug", "info", "warning", "error") else "info", logger.info)
        log_fn(text)
        if self._on_log is not None:
            try:
                self._on_log(level, text)
            except Exception:
                logger.exception("on_log 回调异常（已忽略）")

    # -------------------------------------------------------------- 只读属性
    @property
    def connected(self) -> bool:
        """会话是否已建立（DVT channel 已就绪）。"""
        return self._connected

    @property
    def strategy(self) -> Optional[str]:
        """当前使用的策略：``dvt-rsd`` / ``dvt-lockdown`` / ``legacy``。"""
        return self._strategy

    @property
    def rsd_address(self) -> Optional[tuple]:
        """当前 RSD 地址 ``(host, port)``，没有则为 ``None``。"""
        if self._rsd_host and self._rsd_port:
            return (self._rsd_host, self._rsd_port)
        if self._rsd_obj is not None:
            address = getattr(getattr(self._rsd_obj, "service", None), "address", None)
            if address:
                return (address[0], int(address[1]))
        return None

    @property
    def last_location(self) -> Optional[tuple]:
        """最近一次成功下发的 ``(lat, lon)``。"""
        return self._last_location

    @property
    def send_count(self) -> int:
        """累计下发次数。"""
        return self._send_count

    # ------------------------------------------------------------------ 连接
    async def connect(self, device: Any = None) -> dict:
        """建立（长连接）DVT 会话，返回中文可读的会话信息 dict。

        :raises LocationProviderError: 任何失败都用中文说明原因与修复办法。
        """
        if self._connected:
            return dict(self._session_info)

        udid = _device_field(device, "udid", None) or self._udid
        if udid:
            self._udid = str(udid)
        ios_version = str(_device_field(device, "ios_version", "") or "")
        ios_major = _parse_ios_major(_device_field(device, "ios_major", None) or ios_version)
        developer_mode = _device_field(device, "developer_mode", None)
        state = _device_field(device, "state", None)
        self._connection = str(_device_field(device, "connection", "usb") or "usb")

        if state in ("unpaired",):
            raise LocationProviderError(
                f"设备未与此电脑配对：请在 iPhone 上点击「信任此电脑」后重试（或执行 "
                f"{_PY} -m pymobiledevice3 lockdown pair）"
            )
        if state in ("unavailable",):
            raise LocationProviderError("设备当前不可用：请重新插拔 USB 线并解锁 iPhone 后重试")
        if developer_mode is False:
            raise LocationProviderError(
                "设备未开启「开发者模式」：请在 iPhone 上打开 设置 → 隐私与安全性 → 开发者模式，"
                "重启设备并确认开启后重试（iOS 16 及以上必须开启）"
            )

        pmd3 = _import_pymobiledevice3()
        has_rsd = self._injected_rsd is not None or bool(self._rsd_host and self._rsd_port)

        if has_rsd:
            await self._connect_dvt_rsd(pmd3)
        else:
            await self._connect_lockdown_path(pmd3, ios_major, ios_version)

        # 隧道/设备侧查到的真实版本优先（连接前拿到的版本可能为空或过期）
        if self._detected_version:
            ios_version = self._detected_version
            ios_major = _parse_ios_major(self._detected_version) or ios_major
        if self._detected_udid and not self._udid:
            self._udid = self._detected_udid

        self._connected = True
        self._connected_at = time.time()
        # 会话建立后启动保活（见 _keepalive_loop 的说明）
        self._start_keepalive()
        self._session_info = {
            "ok": True,
            "mock": False,
            "udid": self._udid,
            "ios_version": ios_version,
            "ios_major": ios_major,
            "connection": self._connection,
            "tunnel": "connected" if self._strategy == "dvt-rsd" else "idle",
            "strategy": self._strategy,
            "rsd": f"{self.rsd_address[0]}:{self.rsd_address[1]}" if self.rsd_address else None,
            "pymobiledevice3": pymobiledevice3_version(),
            "detail": self._describe_ready(),
        }
        self._log("success", self._session_info["detail"])
        return dict(self._session_info)

    def connect_sync(self, runner: Any, device: Any = None) -> dict:
        """同步建立会话（把协程提交给调用方的 runner，绝不新建事件循环/线程）。

        与 :class:`MockLocationProvider.connect_sync` 行为一致，便于 lead 的同步装配代码统一调用。
        """
        if self._connected:
            return dict(self._session_info)
        run = getattr(runner, "run", None)
        if not callable(run):
            raise LocationProviderError("传入的 runner 不支持 run(coro)，无法同步建立设备会话")
        return run(self.connect(device))

    def _describe_ready(self) -> str:
        if self._strategy == "dvt-rsd":
            address = self.rsd_address
            where = f"RSD {address[0]}:{address[1]}" if address else "RSD 隧道"
            return f"DVT 虚拟定位会话已就绪（iOS 17+ / {where}，长连接保持中）"
        if self._strategy == "dvt-lockdown":
            return "DVT 虚拟定位会话已就绪（iOS<17 / usbmux+lockdown，长连接保持中）"
        return "虚拟定位会话已就绪（老版 com.apple.dt.simulatelocation 服务）"

    async def _connect_dvt_rsd(self, pmd3: dict) -> None:
        """iOS≥17：RSD 隧道 + DVT（长连接）。"""
        rsd = self._injected_rsd
        try:
            if rsd is None:
                rsd = pmd3["RemoteServiceDiscoveryService"]((self._rsd_host, self._rsd_port))
                self._log("info", f"正在连接 RSD 隧道 {self._rsd_host}:{self._rsd_port} …")
                await asyncio.wait_for(rsd.connect(), timeout=self._connect_timeout)
            self._rsd_obj = rsd
            # 隧道连上后可以校正设备信息（peer_info 里带 OSVersion / UniqueDeviceID）
            real_version = str(getattr(rsd, "product_version", "") or "")
            if real_version:
                self._detected_version = real_version
            real_udid = getattr(rsd, "udid", None)
            if real_udid:
                self._detected_udid = str(real_udid)
            self._log("info", "RSD 隧道已建立，正在打开 DVT（dtservicehub）通道 …")
            self._dvt = pmd3["DvtProvider"](rsd)
            await asyncio.wait_for(self._dvt.connect(), timeout=self._connect_timeout)
            self._location = pmd3["LocationSimulation"](self._dvt)
            await asyncio.wait_for(self._location.connect(), timeout=self._connect_timeout)
            self._strategy = "dvt-rsd"
        except LocationProviderError:
            await self._safe_close_rsd()
            raise
        except BaseException as exc:
            await self._safe_close_rsd()
            raise _wrap("建立 RSD/DVT 会话", exc) from exc

    async def _connect_lockdown_path(self, pmd3: dict, ios_major: int, ios_version: str) -> None:
        """无 RSD 地址：iOS<17 走 lockdown 直连 DVT，失败再退化到老服务。"""
        if ios_major >= RSD_REQUIRED_IOS_MAJOR:
            # iOS 17+ 没有隧道时 lockdown 上的 DVTSecureSocketProxy 已不存在，直接给中文修复建议
            raise LocationProviderError(
                f"设备为 iOS {ios_version or ios_major}，需要先建立 RSD 隧道才能虚拟定位。" + TUNNEL_HINT
            )

        lockdown = self._lockdown_obj or self._injected_lockdown
        if lockdown is None:
            try:
                self._log("info", f"正在通过 usbmux/lockdown 连接设备 {self._udid or '(第一台 USB 设备)'} …")
                lockdown = await asyncio.wait_for(
                    pmd3["create_using_usbmux"](serial=self._udid), timeout=self._connect_timeout
                )
            except BaseException as exc:
                raise _wrap("通过 usbmux 连接设备", exc) from exc
            self._lockdown_obj = lockdown
        elif self._injected_lockdown is not None:
            self._lockdown_obj = self._injected_lockdown

        # 用设备侧真实版本校正（连接前拿到的 ios_major 可能为空或过期）
        real_version = str(getattr(lockdown, "product_version", "") or "")
        if real_version:
            self._detected_version = real_version
        real_udid = getattr(lockdown, "udid", None)
        if real_udid:
            self._detected_udid = str(real_udid)
        real_major = _parse_ios_major(real_version) or ios_major
        if real_major >= RSD_REQUIRED_IOS_MAJOR:
            error = LocationProviderError(
                f"设备为 iOS {real_version or real_major}，需要先建立 RSD 隧道才能虚拟定位。" + TUNNEL_HINT
            )
            # 这个 lockdown 连接是我们自己建的，报错前先释放，避免泄漏
            if self._injected_lockdown is None and self._lockdown_obj is not None:
                try:
                    await self._lockdown_obj.close()
                except Exception:
                    pass
                self._lockdown_obj = None
            raise error

        # 首选取证过的 DVT 路径（长连接）
        try:
            self._dvt = pmd3["DvtProvider"](lockdown)
            await asyncio.wait_for(self._dvt.connect(), timeout=self._connect_timeout)
            self._location = pmd3["LocationSimulation"](self._dvt)
            await asyncio.wait_for(self._location.connect(), timeout=self._connect_timeout)
            self._strategy = "dvt-lockdown"
            return
        except BaseException as exc:
            dvt_error = _wrap("建立 DVT 会话（iOS<17 直连）", exc)
            self._log("warning", f"{dvt_error}；改用老版 simulatelocation 服务重试")

        # 兜底：com.apple.dt.simulatelocation（pymobiledevice3 CLI 对 iOS<17 的官方路径）
        self._dvt = None
        self._location = None
        try:
            self._legacy = pmd3["DtSimulateLocation"](lockdown)
            service_name = str(getattr(self._legacy, "SERVICE_NAME", "com.apple.dt.simulatelocation"))
            # 关键：DtSimulateLocation.__init__ 不做任何设备 I/O（services/simulate_location.py:19-22），
            # 它每次 set/clear 才新开服务。这里必须真开一次服务做校验，否则会出现
            # 「connect 报成功、第一次下发才失败」的假成功（真机实测踩到过）。
            self._log("info", f"正在验证老版虚拟定位服务 {service_name} …")
            service = await asyncio.wait_for(
                lockdown.start_lockdown_developer_service(service_name), timeout=self._connect_timeout
            )
            try:
                await service.close()
            except Exception:
                pass
            self._strategy = "legacy"
            self._log("info", f"已选用老版虚拟定位服务 {service_name}（服务可用性已实测）")
        except BaseException as exc:
            raise _wrap("建立老版虚拟定位会话（com.apple.dt.simulatelocation）", exc) from exc

    async def _safe_close_rsd(self) -> None:
        for obj in (self._location, self._dvt):
            if obj is None:
                continue
            try:
                await obj.close()
            except Exception:
                pass
        self._location = None
        self._dvt = None
        if self._rsd_obj is not None and self._injected_rsd is None:
            try:
                await self._rsd_obj.close()
            except Exception:
                pass
        self._rsd_obj = None

    # ------------------------------------------------------------------ 下发
    async def set_location(self, lat: float, lon: float) -> None:
        """下发一次单点定位（复用长连接会话）。"""
        lat = float(lat)
        lon = float(lon)
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise LocationProviderError(f"经纬度超出合法范围：lat={lat}, lon={lon}")
        if not self._connected:
            raise LocationProviderError("尚未连接设备，无法下发定位：请先调用 connect()（或点击界面上的「连接设备」）")

        try:
            if self._strategy in ("dvt-rsd", "dvt-lockdown"):
                await self._location.set(lat, lon)
            elif self._strategy == "legacy":
                await self._legacy.set(lat, lon)
            else:
                raise LocationProviderError("内部状态异常：没有可用的虚拟定位通道，请重新连接设备")
        except BaseException as exc:
            raise _wrap("下发定位", exc) from exc

        self._last_location = (lat, lon)
        self._send_count += 1
        self._last_send_mono = time.monotonic()
        if self._send_count == 1 or self._send_count % 25 == 0:
            self._log("debug", f"已下发 lat={lat:.6f}, lon={lon:.6f}（第 {self._send_count} 次）")

    # ------------------------------------------------------------------ 保活
    async def _keepalive_loop(self) -> None:
        """定期重发最后一个点，防止"隧道静默断开后设备偷偷恢复真实定位"。

        为什么需要：Windows 的 userspace 隧道是纯 Python 用户态协议栈，实测会**静默断开**
        —— 断开后设备自己恢复真实定位，而界面看起来一切正常（同类工具
        fake-location-cli 也独立踩到了这个坑，其 flc_set.py 里明确记录了这一点）。
        播放时每秒都在下发，不会触发；主要是「设了点但暂停/静止」时的保险。
        """
        try:
            while self._connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                target = self._last_location
                if target is None or not self._connected:
                    continue
                # 播放引擎刚发过就不重复发，避免和播放线程抢通道
                if time.monotonic() - self._last_send_mono < KEEPALIVE_INTERVAL * 0.8:
                    continue
                try:
                    if self._strategy in ("dvt-rsd", "dvt-lockdown"):
                        await self._location.set(target[0], target[1])
                    elif self._strategy == "legacy":
                        await self._legacy.set(target[0], target[1])
                    else:
                        continue
                    self._last_send_mono = time.monotonic()
                    self._keepalive_count += 1
                    self._log("debug",
                              f"保活重发 lat={target[0]:.6f}, lon={target[1]:.6f}"
                              f"（第 {self._keepalive_count} 次）")
                except Exception as exc:
                    # 不抛出去：保活失败不应打断播放，但要留下痕迹供排查
                    self._log("warning", f"保活重发失败（隧道可能已断开）：{exc}")
        except asyncio.CancelledError:
            return

    def _start_keepalive(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        try:
            self._keepalive_task = asyncio.get_running_loop().create_task(
                self._keepalive_loop(), name="location-keepalive"
            )
        except RuntimeError:
            self._keepalive_task = None

    def _stop_keepalive(self) -> None:
        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None and not task.done():
            task.cancel()

    async def clear_location(self) -> None:
        """让设备恢复真实定位（``play.stop`` / ``location.clear`` 语义）。"""
        if not self._connected:
            self._log("debug", "当前未连接设备，clear_location 忽略")
            return
        try:
            if self._strategy in ("dvt-rsd", "dvt-lockdown"):
                await self._location.clear()
            elif self._strategy == "legacy":
                await self._legacy.clear()
        except BaseException as exc:
            raise _wrap("清除虚拟定位", exc) from exc
        self._last_location = None
        self._last_send_mono = 0.0
        self._stop_keepalive()
        self._log("info", "已清除虚拟定位，设备恢复真实定位")

    async def close(self) -> None:
        """释放会话：先 clear（恢复真实定位）再关闭 DVT / RSD / lockdown。幂等。"""
        self._stop_keepalive()
        if not self._connected and self._dvt is None and self._rsd_obj is None and self._lockdown_obj is None:
            return
        try:
            if self._connected:
                await self.clear_location()
        except Exception as exc:
            self._log("warning", f"关闭前清除虚拟定位失败（继续关闭会话）：{exc}")

        for obj in (self._location, self._dvt):
            if obj is None:
                continue
            try:
                await obj.close()
            except Exception as exc:
                self._log("debug", f"关闭 {type(obj).__name__} 时出错（忽略）：{exc}")

        if self._rsd_obj is not None and self._injected_rsd is None:
            try:
                await self._rsd_obj.close()
            except Exception as exc:
                self._log("debug", f"关闭 RSD 时出错（忽略）：{exc}")
        if self._lockdown_obj is not None and self._injected_lockdown is None:
            try:
                await self._lockdown_obj.close()
            except Exception as exc:
                self._log("debug", f"关闭 lockdown 时出错（忽略）：{exc}")

        self._location = None
        self._dvt = None
        self._legacy = None
        self._rsd_obj = None
        self._lockdown_obj = None
        self._connected = False
        self._strategy = None
        self._connected_at = None
        self._log("info", "虚拟定位会话已关闭")

    # ------------------------------------------------------------------ 探测
    async def probe(self, device: Any = None) -> dict:
        """「测试连接」按钮：探测设备/DVT 是否可用。**不抛异常**。

        :param device: 可选，``DeviceManager`` 的 Device dict（与 ``connect()`` 同一个对象）。
            传入后会先做静态判断（未配对 / 未开开发者模式 / iOS 版本），能省掉一次设备往返。
        :returns: ``{"ok": bool, "detail": 中文说明, "ios_major": int}``（另附 strategy 等字段）
        """
        ios_major = _parse_ios_major(self._session_info.get("ios_major"))
        if device is not None:
            state = _device_field(device, "state", None)
            developer_mode = _device_field(device, "developer_mode", None)
            device_major = _parse_ios_major(
                _device_field(device, "ios_major", None) or _device_field(device, "ios_version", "")
            )
            if device_major:
                ios_major = device_major
            if state == "unpaired":
                return {
                    "ok": False,
                    "ios_major": ios_major,
                    "strategy": None,
                    "detail": "设备未与此电脑配对：请在 iPhone 上点击「信任此电脑」后重试",
                }
            if developer_mode is False:
                return {
                    "ok": False,
                    "ios_major": ios_major,
                    "strategy": None,
                    "detail": "设备未开启「开发者模式」：设置 → 隐私与安全性 → 开发者模式（iOS16+ 必须开启，需重启设备）",
                }

        try:
            pmd3 = _import_pymobiledevice3()
        except LocationProviderError as exc:
            return {"ok": False, "detail": str(exc), "ios_major": ios_major, "strategy": None}

        if self._connected:
            return {
                "ok": True,
                "detail": self._describe_ready(),
                "ios_major": ios_major,
                "strategy": self._strategy,
                "rsd": f"{self.rsd_address[0]}:{self.rsd_address[1]}" if self.rsd_address else None,
            }

        # 未连接：用一个临时会话探测，探测完立即释放（不会残留模拟定位）
        has_tunnel = self._injected_rsd is not None or bool(self._rsd_host and self._rsd_port)
        if not has_tunnel and (ios_major == 0 or ios_major >= RSD_REQUIRED_IOS_MAJOR):
            # 没有隧道、且是 iOS 17+（或版本未知）：只有隧道状态可报，不做无意义的重连
            detail = (
                f"尚未配置 RSD 隧道（ios_major={ios_major or '未知'}）。" + TUNNEL_HINT
                if ios_major >= RSD_REQUIRED_IOS_MAJOR or ios_major == 0
                else "尚未建立会话；iOS<17 可直接用 USB/lockdown 建立 DVT 会话，请先点击「连接设备」"
            )
            return {"ok": False, "detail": detail, "ios_major": ios_major, "strategy": None}

        # iOS<17 且无隧道时也做真实探测（点「测试连接」就应该真的去连一次）
        probe = RealLocationProvider(
            rsd_host=self._rsd_host,
            rsd_port=self._rsd_port,
            udid=self._udid,
            rsd=self._injected_rsd,
            lockdown=self._injected_lockdown,
            connect_timeout=self._connect_timeout,
        )
        try:
            info = await probe.connect(device)
        except LocationProviderError as exc:
            return {"ok": False, "detail": str(exc), "ios_major": ios_major, "strategy": None}
        except BaseException as exc:
            return {"ok": False, "detail": _wrap("探测设备", exc).args[0], "ios_major": ios_major, "strategy": None}
        try:
            await probe.close()
        except Exception:
            pass
        return {
            "ok": True,
            "detail": f"连接测试成功：{info.get('detail')}",
            "ios_major": _parse_ios_major(info.get("ios_major")) or ios_major,
            "strategy": info.get("strategy"),
            "rsd": info.get("rsd"),
        }

    # ------------------------------------------------------------------ 辅助
    def stats(self) -> dict:
        """运行统计（供日志/健康检查使用）。"""
        return {
            "mock": False,
            "connected": self._connected,
            "strategy": self._strategy,
            "udid": self._udid,
            "rsd": f"{self.rsd_address[0]}:{self.rsd_address[1]}" if self.rsd_address else None,
            "send_count": self._send_count,
            "last_location": self._last_location,
            "connected_at": self._connected_at,
        }

    def update_rsd(self, rsd_host: Optional[str], rsd_port: Optional[int]) -> None:
        """隧道重建后更新 RSD 地址（需要重新 connect 才生效）。"""
        self._rsd_host = rsd_host
        self._rsd_port = int(rsd_port) if rsd_port else None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<RealLocationProvider connected={self._connected} strategy={self._strategy} "
            f"udid={self._udid!r} rsd={self.rsd_address}>"
        )
