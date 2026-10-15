"""HTTP + WebSocket 服务端：实现 docs/PROTOCOL.md 的全部接口。

架构：
- 一个后台线程跑 asyncio 事件循环（AsyncRunner），设备/DVT 的所有协程都在那里执行；
- ThreadingHTTPServer 处理 HTTP 与 WebSocket 升级，每条连接一个线程；
- 播放引擎线程通过 AsyncRunner 串行下发定位。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import config_store, device as device_mod, protocol, route_engine, tilecache, tunnel as tunnel_mod, ws

def _web_root() -> Path:
    """前端资源目录。

    - 源码运行：项目根目录下的 web/
    - 打包成 .app 独立运行：PyInstaller 解包目录（sys._MEIPASS）/web
    """
    import sys

    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / "web"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent / "web"


WEB_ROOT = _web_root()


class AsyncRunner:
    """把一个 asyncio 事件循环放到后台线程，供同步代码提交协程。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="async-runner", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def run(self, coro, timeout: float | None = 30.0, wait: bool = True):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        if not wait:
            return future
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise protocol.AppError("设备响应超时，请检查连接后重试", "device_timeout", status=504)

    def shutdown(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=3.0)
        except Exception:
            pass


class App:
    """应用装配：配置、设备、隧道、定位会话、路由、播放、瓦片、日志。"""

    def __init__(self, root: Path, mock: bool = False, port: int = protocol.DEFAULT_PORT) -> None:
        self.root = Path(root)
        self.mock = mock
        self.port = port
        self.host = protocol.DEFAULT_HOST
        self.config = config_store.ConfigStore(self.root / "config.json")
        # 首次运行就把配置落盘：否则界面上改的设置（漂移/下发频率等）重启后会丢，
        # 打包成 .app 时数据目录是新建的，这一点尤其明显。
        if not self.config.path.exists():
            try:
                self.config.save()
            except Exception:
                pass
        self.runner = AsyncRunner()
        self.hub = ws.Hub()
        self.tunnel = tunnel_mod.TunnelManager(log=self.log, on_change=self._on_tunnel_change)
        self.devices = device_mod.DeviceManager(mock=mock, on_change=self._on_devices_change)
        self.routes = route_engine.RouteEngine(self.config)
        self.tiles = tilecache.TileCache(
            self.config.cache_dir(), int(self.config.get("tile_cache_max_mb") or 1024)
        )
        self.logs: deque[dict[str, Any]] = deque(maxlen=500)
        self._lock = threading.RLock()
        self.provider: Any = None
        self.provider_udid: str | None = None
        self.playback = None  # 延后创建（依赖 provider 注入）
        from .playback import PlaybackEngine

        self.playback = PlaybackEngine(
            runner=self.runner,
            config=self.config,
            on_location=self._broadcast_location,
            on_status=self._broadcast_status,
            on_log=self.log,
        )
        self.started_at = time.time()
        self.shutdown_event = threading.Event()
        # 开发者镜像挂载是否正在进行（避免重复触发）
        self._mount_running = False

    # ---------------- 日志 ----------------
    def log(self, level: str, message: str) -> None:
        entry = {"ts": protocol.now_ts(), "level": level, "msg": str(message)}
        self.logs.append(entry)
        self.hub.broadcast({"type": "log", **entry})

    def recent_logs(self, limit: int = 200) -> list[dict[str, Any]]:
        try:
            count = int(limit)
        except (TypeError, ValueError) as exc:
            raise protocol.AppError("limit 必须是整数", "bad_request") from exc
        items = list(self.logs)[-max(1, min(count, 500)):]
        return items

    # ---------------- 广播 ----------------
    def _broadcast_location(self, payload: dict[str, Any]) -> None:
        self.hub.broadcast({"type": "location", **payload})

    def _broadcast_status(self, payload: dict[str, Any]) -> None:
        self.hub.broadcast({"type": "status", **payload})

    def _on_devices_change(self) -> None:
        self.hub.broadcast({"type": "devices", **self.devices.devices_json()})

    def _on_tunnel_change(self) -> None:
        self.hub.broadcast({"type": "tunnel", **self.tunnel.snapshot()})

    def welcome_payload(self) -> dict[str, Any]:
        return {
            "type": "welcome",
            "version": protocol.APP_VERSION,
            "name": protocol.APP_NAME,
            "mock": self.mock,
            "config": self.config.snapshot(),
            "devices": self.devices.list_devices() and [d.to_json() for d in self.devices.list_devices()],
            "selected": self.devices.selected_udid(),
            # 与 tunnel.ensure 用同一套语义：mock 模式/IOS<17 不该显示"空闲/建立中"
            "tunnel": self._tunnel_semantics(),
            "backend": self.devices.backend,
            "pymobiledevice3": device_mod.pymobiledevice3_importable(),
        }

    def _tunnel_semantics(self) -> dict[str, Any]:
        """统一的隧道语义（welcome / REST / WS 三处共用，避免行为不一致）。"""
        device = self.devices.get_selected()
        if self.mock:
            return {
                "state": "connected",
                "message": "模拟模式：无需建立 RSD 隧道",
                "rsd": "mock",
                "mode": "mock",
                "udid": device.udid if device else None,
                "ios_major": device.ios_major if device else None,
                "need_tunnel": False,
            }
        if device is not None and (device.ios_major or 0) < 17:
            return {
                "state": "unsupported",
                "message": f"iOS {device.ios_version} 使用 usbmux + 开发者镜像的经典通道，不需要 RSD 隧道",
                "rsd": None,
                "mode": None,
                "udid": device.udid,
                "ios_major": device.ios_major,
                "need_tunnel": False,
            }
        snapshot = self.tunnel.snapshot()
        snapshot["ios_major"] = device.ios_major if device else None
        snapshot["need_tunnel"] = True
        return snapshot

    # ---------------- 会话管理 ----------------
    def provider_for(self, device: device_mod.Device, connect: bool = True) -> Any:
        """按需创建并连接定位 provider（mock 或真机）。"""
        with self._lock:
            if self.provider is not None and self.provider_udid == device.udid:
                if not connect or getattr(self.provider, "connected", False):
                    return self.provider
            if self.mock:
                from .location_mock import MockLocationProvider

                provider = MockLocationProvider(log=self.log)
                provider.connect_sync(self.runner)
                self.provider = provider
                self.provider_udid = device.udid
                self.playback.set_provider(provider)
                return provider

            from .location_provider import RealLocationProvider

            # 连之前先体检：iOS<17 必须有开发者镜像，否则连了也是白连，
            # 而且如果同时插着另一台可用的设备，要直接告诉用户换哪台。
            self._preflight(device)

            rsd = None
            if (device.ios_major or 0) >= 17:
                if not self.tunnel.ready:
                    snapshot = self.tunnel.ensure(device.udid, allow_sudo=False)
                    if snapshot["state"] != "connected":
                        raise protocol.AppError(
                            snapshot.get("message") or tunnel_mod._HINT_NATIVE_FAILED,
                            "tunnel_required",
                            status=409,
                        )
                rsd = self.tunnel.address()
            provider = RealLocationProvider(
                rsd_host=rsd[0] if rsd else None,
                rsd_port=rsd[1] if rsd else None,
                udid=device.udid,
                log=self.log,
            )
            self.runner.run(provider.connect(device), timeout=40.0)
            self.provider = provider
            self.provider_udid = device.udid
            self.playback.set_provider(provider)
            device_mod.DeviceManager.mark(self.devices, device.udid, simulating=True)
            return provider

    def _preflight(self, device: device_mod.Device) -> None:
        """连接前的可用性体检，尽量在动手之前给出可操作的中文提示。

        典型场景：同时插着两台设备，用户选中的那台（例如 Wi-Fi 连接的旧设备）没挂开发者镜像，
        而另一台 USB 设备是可用的——这时必须明确告诉用户该换哪台，而不是抛底层英文错误。
        """
        if (device.ios_major or 0) >= 17:
            return  # iOS 17+ 走 RSD 隧道，不需要开发者镜像

        mounted = self._developer_image_mounted(device.udid)
        if mounted is None or mounted:
            return
        others = [
            d for d in self.devices.list_devices()
            if d.udid != device.udid and (d.ios_major or 0) >= 17 and d.state == "connected"
        ]
        hint = ""
        if others:
            best = others[0]
            hint = f"；建议改用另一台设备：{best.name}（iOS {best.ios_version}，{best.connection.upper()}）"
        raise protocol.AppError(
            f"{device.name}（iOS {device.ios_version}）尚未挂载开发者镜像，无法做虚拟定位。"
            f"请执行：./.venv/bin/python -m pymobiledevice3 mounter auto-mount --udid {device.udid}{hint}",
            "developer_image_missing",
            status=409,
        )

    def _developer_session_ready(self, udid: str, major: int) -> bool:
        """判断"现在能不能真的做虚拟定位"。

        - **iOS 17 及以上**：DVT 走 RSD 隧道，lockdown 里根本没有那个服务名，
          因此不能拿服务名去试。这里以"镜像是否已挂载"为准 —— 后续连接设备时
          会自动建隧道并打开 DVT，真连不上会在那一步给出明确报错。
        - **iOS 16 及更早**：镜像挂载 ≠ DVT 可用（实测 16.5 上个性化镜像挂上了
          但服务不存在），必须真的开一次服务才算就绪。
        """
        import asyncio

        if not major or major >= 17:
            return self._developer_image_mounted(udid) is True

        async def probe() -> bool:
            from pymobiledevice3.lockdown import create_using_usbmux

            try:
                client = await create_using_usbmux(
                    serial=udid, autopair=False, connection_type="USB", pair_timeout=3.0
                )
            except Exception:
                return False
            try:
                conn = await client.start_lockdown_developer_service(
                    "com.apple.instruments.remoteserver.DVTSecureSocketProxy"
                )
                try:
                    conn.close()
                except Exception:
                    pass
                return True
            except Exception:
                return False
            finally:
                try:
                    await client.close()
                except Exception:
                    pass

        try:
            return bool(self.runner.run(probe(), timeout=25.0))
        except Exception:
            return False

    def _developer_image_mounted(self, udid: str) -> bool | None:
        """查询设备是否已挂载开发者镜像（查询失败返回 None）。

        用 Python API 查询（不再依赖 pymobiledevice3 的 CLI），
        因此打包成 .app 后同样可用。
        """
        async def probe() -> bool:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.mobile_image_mounter import (
                MobileImageMounterService,
            )

            client = await create_using_usbmux(
                serial=udid, autopair=False, connection_type="USB", pair_timeout=3.0
            )
            try:
                async with MobileImageMounterService(lockdown=client) as mounter:
                    for image_type in ("Personalized", "Developer"):
                        try:
                            if await mounter.is_image_mounted(image_type):
                                return True
                        except Exception:
                            continue
                    return False
            finally:
                try:
                    await client.close()
                except Exception:
                    pass

        try:
            return bool(self.runner.run(probe(), timeout=25.0))
        except Exception:
            return None

    def close_session(self, stop_playback: bool = True) -> None:
        with self._lock:
            provider = self.provider
            self.provider = None
            self.provider_udid = None
        if stop_playback and self.playback is not None:
            self.playback.stop_locked(notify=False)
        if provider is not None:
            try:
                self.runner.run(provider.close(), timeout=12.0, wait=True)
            except Exception as exc:
                self.log("warn", f"释放设备会话时出错：{exc}")
        self.playback.set_provider(None)
        for device in self.devices.list_devices():
            device_mod.DeviceManager.mark(self.devices, device.udid, simulating=False)

    # ---------------- 命令分发 ----------------
    def dispatch(self, cmd: str, message: dict[str, Any]) -> dict[str, Any]:
        """同步执行一条命令，异常统一转成 AppError（供 REST 复用）。"""
        try:
            handler = self._COMMANDS.get(cmd)
            if handler is None:
                raise protocol.AppError(f"未知命令：{cmd}", "unknown_command", status=404)
            return handler(self, message) or {}
        except protocol.AppError:
            raise
        except Exception as exc:
            detail = f"{exc.__class__.__name__}: {exc}"
            self.log("error", f"{cmd} 内部错误：{detail}")
            raise protocol.AppError(f"内部错误：{detail[:160]}", "internal", status=500) from exc

    def handle_command(self, cmd: str, message: dict[str, Any]) -> dict[str, Any]:
        return self.dispatch(cmd, message)

    def handle_ws(self, conn: ws.WSConnection, message: dict[str, Any]) -> None:
        cmd = str(message.get("type") or "").strip()
        request_id = message.get("id")
        try:
            handler = self._COMMANDS.get(cmd)
            if handler is None:
                raise protocol.AppError(f"未知命令：{cmd or '(空)'}", "unknown_command")
            result = handler(self, message)
            if request_id is not None:
                # 注意：payload 里可能自带 type（如 welcome），必须先剔除，否则会把 ack 的类型覆盖掉
                payload = {k: v for k, v in (result or {}).items() if k not in ("type", "id")}
                conn.send_json({"type": "ack", "id": request_id, "cmd": cmd, **payload})
        except protocol.AppError as exc:
            self.log("error", f"{cmd}: {exc.message}")
            if request_id is not None:
                conn.send_json({"type": "ack", "id": request_id, "cmd": cmd, "ok": False,
                                "error": exc.message, "code": exc.code})
            else:
                conn.send_json({"type": "error", "error": exc.message, "code": exc.code})
        except Exception as exc:  # 兜底，绝不让 WS 线程崩掉
            detail = f"{exc.__class__.__name__}: {exc}"
            self.log("error", f"{cmd} 内部错误：{detail}")
            if request_id is not None:
                conn.send_json({"type": "ack", "id": request_id, "cmd": cmd, "ok": False,
                                "error": f"内部错误：{detail[:160]}", "code": "internal"})
            else:
                conn.send_json({"type": "error", "error": f"内部错误：{detail[:160]}", "code": "internal"})

    def _cmd_hello(self, message: dict[str, Any]) -> dict[str, Any]:
        self.devices.refresh()
        return self.welcome_payload()

    def _cmd_ping(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"ts": protocol.now_ts()}

    def _cmd_devices_refresh(self, message: dict[str, Any]) -> dict[str, Any]:
        self.devices.refresh()
        return self.devices.devices_json()

    def _cmd_device_select(self, message: dict[str, Any]) -> dict[str, Any]:
        udid = str(message.get("udid") or "").strip()
        if not udid:
            raise protocol.AppError("缺少 udid", "bad_request")
        self.close_session()
        device = self.devices.select(udid)
        self.config.update({"last_device": udid})
        return {"device": device.to_json()}

    def _cmd_device_connect(self, message: dict[str, Any]) -> dict[str, Any]:
        device = self.devices.get_selected()
        if device is None:
            self.devices.refresh()
            device = self.devices.get_selected()
        if device is None:
            raise protocol.AppError(
                "未检测到 iOS 设备：请用数据线连接、解锁设备并在弹窗中点「信任」", "no_device", status=404
            )
        self.log("info", f"正在连接 {device.name}（iOS {device.ios_version or '未知'}）…")
        provider = self.provider_for(device)
        detail = {}
        probe = getattr(provider, "probe", None)
        if callable(probe):
            try:
                detail = self.runner.run(probe(), timeout=25.0) or {}
            except Exception as exc:
                self.log("warn", f"连接自检未通过：{exc}")
        self.log("success", f"设备已连接：{device.name}")
        return {
            "device": (self.devices.get(device.udid) or device).to_json(),
            "tunnel": self.tunnel.snapshot(),
            "probe": detail,
        }

    def _cmd_device_disconnect(self, message: dict[str, Any]) -> dict[str, Any]:
        self.close_session()
        self.tunnel.stop()
        self.log("info", "已断开设备并释放隧道")
        return {"device": (self.devices.get_selected().to_json() if self.devices.get_selected() else None),
                "tunnel": self.tunnel.snapshot()}

    def _cmd_location_set(self, message: dict[str, Any]) -> dict[str, Any]:
        lat, lng = protocol.validate_latlng(message.get("lat"), message.get("lng"))
        device = self._require_device()
        provider = self.provider_for(device)
        if self.playback.active:
            self.playback.stop(notify=False, clear_provider=False)
        self.playback.set_provider(provider)
        self.playback.set_point(lat, lng, provider=provider)
        self.log("success", f"已把设备定位到 {lat:.6f}, {lng:.6f}")
        return {}

    def _cmd_location_clear(self, message: dict[str, Any]) -> dict[str, Any]:
        device = self.devices.get_selected()
        if device is not None and self.provider is not None:
            self.provider_for(device)
        self.playback.clear_point(provider=self.provider)
        return {}

    def _cmd_play_start(self, message: dict[str, Any]) -> dict[str, Any]:
        device = self._require_device()
        route = route_engine.sanitize_route_payload(message.get("route"))
        speed_scale = message.get("speed_scale")
        loop = message.get("loop")
        try:
            start_index = int(message.get("start_index") or 0)
        except (TypeError, ValueError):
            start_index = 0
        # 前端可以把"真实感"参数一并带上（漂移 / 速度浮动）
        tuning = message.get("tuning")
        if isinstance(tuning, dict):
            patch = {k: tuning[k] for k in self._TUNING_KEYS if k in tuning}
            if patch:
                self.playback.update_tuning(**patch)
        provider = self.provider_for(device)
        self.playback.start(route=route, speed_scale=speed_scale, loop=loop, start_index=start_index, provider=provider)
        self.log(
            "success",
            f"开始沿轨迹播放：{route.name}，{route.distance / 1000:.2f} 公里 / "
            f"{_fmt_duration(route.duration)}，倍速 {self.playback.speed_scale}x，下发 {self.config.get('send_hz')}Hz",
        )
        self.playback.log_tuning()
        return {"status": self.playback.status(force=True), "route": route.to_json(),
                "tuning": self.playback.tuning()}

    def _cmd_play_pause(self, message: dict[str, Any]) -> dict[str, Any]:
        self.playback.pause()
        return {"status": self.playback.status(force=True)}

    def _cmd_play_resume(self, message: dict[str, Any]) -> dict[str, Any]:
        self.playback.resume()
        return {"status": self.playback.status(force=True)}

    def _cmd_play_stop(self, message: dict[str, Any]) -> dict[str, Any]:
        self.playback.stop(notify=True, clear_provider=True)
        return {"status": self.playback.status(force=True)}

    def _cmd_play_seek(self, message: dict[str, Any]) -> dict[str, Any]:
        self.playback.seek(message.get("progress"))
        return {"status": self.playback.status(force=True)}

    def _cmd_play_speed(self, message: dict[str, Any]) -> dict[str, Any]:
        self.playback.set_speed(message.get("speed_scale"))
        return {"status": self.playback.status(force=True)}

    _TUNING_KEYS = (
        "drift_enabled", "drift_m", "speed_jitter_enabled", "speed_jitter",
        "timing_jitter_enabled", "derive_speed",
        "gross_error_enabled", "gross_error_probability", "gross_error_m",
    )

    def _cmd_play_tuning(self, message: dict[str, Any]) -> dict[str, Any]:
        """实时调整"真实感"参数（只改参数，不打断播放）。

        只传消息里**实际出现**的字段：缺失的字段保持原值，
        显式传 false/0 也能生效（服务端 update_tuning 用哨兵区分"未提供"）。
        """
        patch = {k: message[k] for k in self._TUNING_KEYS if k in message}
        tuning = self.playback.update_tuning(**patch)
        if "log" in message and message.get("log"):
            self.playback.log_tuning()
        return {"tuning": tuning, "status": self.playback.status(force=True)}

    def _cmd_play_loop(self, message: dict[str, Any]) -> dict[str, Any]:
        self.playback.set_loop(message.get("loop"))
        return {"status": self.playback.status(force=True)}

    # ---------------- 开发者镜像挂载（iOS 17 以下必需） ----------------
    def _cmd_mount_developer_image(self, message: dict[str, Any]) -> dict[str, Any]:
        """挂载开发者镜像。iOS<17 的 DVT 定位必须先挂镜像，iOS 17+ 一般由 Xcode 自动挂。

        在后台线程执行（下载 + 挂载可能几十秒），过程日志实时推给界面。
        """
        device = self.devices.get_selected()
        if device is None:
            self.devices.refresh()
            device = self.devices.get_selected()
        if device is None:
            raise protocol.AppError("未检测到 iOS 设备", "no_device", status=404)
        if self.mock:
            self.log("info", "模拟模式：跳过镜像挂载")
            return {"mounted": True, "message": "模拟模式，无需挂载"}
        if self._mount_running:
            raise protocol.AppError("正在挂载中，请稍候…", "busy", status=409)

        self._mount_running = True
        threading.Thread(
            target=self._mount_worker, args=(device.udid, device.name, device.ios_version, device.ios_major),
            name="mounter", daemon=True,
        ).start()
        return {"started": True}

    def _mount_worker(self, udid: str, name: str, version: str, major: int) -> None:
        try:
            self.log("info", f"开始为 {name}（iOS {version}）挂载开发者镜像…")
            # 关键：不能只看"镜像是否挂载"就判定可用 —— 实测 iOS 16.5 上个性化镜像
            # 显示 IsMounted=true，但 DVT 服务并不会启动（挂载成功 ≠ 能虚拟定位）。
            # 因此这里以"开发者服务是否真的能打开"为准。
            if self._developer_session_ready(udid, major):
                self.log("success", "开发者服务已就绪，无需重复挂载")
                self.hub.broadcast({"type": "mounted", "mounted": True, "message": "开发者服务已就绪"})
                return

            ok, detail = self._mount_with_fallback(udid, version, major)

            # 用"服务是否可用"做最终判定，而不是仅看挂载标志
            ready = self._developer_session_ready(udid, major)
            if ok and ready:
                self.log("success", f"开发者镜像挂载完成且服务已就绪（{detail}）")
                self.hub.broadcast({"type": "mounted", "mounted": True, "message": detail})
            elif ok and not ready:
                note = (
                    "镜像已挂到设备上，但开发者服务仍未启动，虚拟定位还不可用。"
                    "请用 Xcode（Window → Devices and Simulators）连接一次设备完成 "
                    "Preparing device for development，再回来重试。"
                )
                self.log("error", note)
                self.hub.broadcast({"type": "mounted", "mounted": False, "message": note})
            else:
                self.log("error", f"挂载失败：{detail}")
                self.hub.broadcast({"type": "mounted", "mounted": False, "message": detail})
        except Exception as exc:
            self.log("error", f"挂载异常：{exc.__class__.__name__}: {exc}")
            self.hub.broadcast({"type": "mounted", "mounted": False, "message": str(exc)[:150]})
        finally:
            self._mount_running = False

    def _mount_with_fallback(self, udid: str, version: str, major: int) -> tuple[bool, str]:
        """挂载开发者镜像（面向 iOS 17 及以上）。

        本工具支持的虚拟定位路径是 **iOS 17+ 的 RSD + DVT**：
        应用内置一套个性化镜像（全系共用），挂载时由 Apple TSS 按设备在线签发，
        因此不需要逐版本素材，iOS 17 到 27.x 都走同一条路。

        iOS 16 及更早不在支持范围：那套经典镜像由 Apple 按系统构建号签发，
        社区镜像在多数新构建上会被设备拒签（实测 16.5/20F66 失败，libimobiledevice 同样失败）。
        这里直接给出明确提示，不做徒劳重试。
        """
        if major and major < 17:
            return False, (
                "本工具只支持 iOS 17 及以上。你的设备是 iOS " + version + "，"
                "它的开发者镜像必须由 Apple 按系统构建号签发，第三方镜像会被设备拒签，"
                "所以无法通过本工具挂载。若确实要调试该设备，请先用 Xcode 连接一次"
                "（Window → Devices and Simulators）让它自动完成镜像准备。"
            )

        if not self._stage_personalized_ddi():
            return False, (
                "应用内没有个性化镜像素材，且自动下载失败。"
                "请在网络可用时重试，或用 Xcode 连接设备自动挂载一次。"
            )

        self.log("info", f"用内置个性化镜像挂载（iOS {version}）…")
        ok, detail = self._mount_personalized(udid, version)
        if ok:
            return True, detail
        return False, self._clean_mounter_error(detail)

    @staticmethod
    def _clean_mounter_error(raw: str) -> str:
        """把底层异常整理成一句人话（原始字符串里塞满了嵌套的 Error Domain）。"""
        text = str(raw)
        low = text.lower()
        if "alreadymounted" in low.replace(" ", "") or "already mounted" in low:
            return "镜像已经在设备上（无需重复挂载）"
        if "failed to verify the disk image signature" in low:
            return "设备拒绝了这个镜像的签名（该系统的镜像需由 Apple 按构建号签发）"
        if "reset by peer" in low or "connection aborted" in low:
            return "与 Apple 服务器通信被中断（TSS 签名需要联网，请检查网络后重试）"
        if "access denied" in low or "requires root" in low:
            return "需要管理员权限"
        if "no device" in low:
            return "未找到设备"
        # 去掉嵌套的 UserInfo 噪声，只留第一句
        first = text.replace("\n", " ").split("UserInfo=")[0].strip()
        return first[:120] if first else "未知错误"

    def _mount_personalized(self, udid: str, version: str) -> tuple[bool, str]:
        """用内置个性化镜像挂载（需联网向 Apple 申请 TSS 签名）。

        优先走 Python API（打包版可用），失败再退回 CLI（源码版）。
        """
        ok, detail = self._run_auto_mount(udid, version)
        if ok:
            return True, detail
        if self._mounter_cli() is not None:
            self.log("info", "Python API 挂载失败，改用命令行重试…")
            return self._run_mounter(["auto-mount", "--udid", udid])
        return False, detail
    def _run_auto_mount(self, udid: str, version: str) -> tuple[bool, str]:
        """用 Python API 直接挂载（不依赖 pymobiledevice3 的 CLI）。

        `auto_mount` 会按 iOS 版本自动分流：
        - iOS < 17 → auto_mount_developer（经典镜像，需要能写入 Xcode 的 DeviceSupport 目录）
        - iOS >= 17 → auto_mount_personalized（用内置个性化镜像 + Apple TSS 在线签名）
        打包成 .app 后没有 CLI 可用，因此这条路径是打包版唯一可行的挂载方式。
        """
        async def worker() -> None:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.mobile_image_mounter import (
                MobileImageMounterService,
                auto_mount,
            )

            client = await create_using_usbmux(
                serial=udid, autopair=True, connection_type="USB", pair_timeout=5.0
            )
            try:
                # 先查设备上是否已有镜像：已有就直接返回，避免 AlreadyMountedError。
                # 两种类型都查（经典 Developer / 个性化 Personalized）。
                try:
                    async with MobileImageMounterService(lockdown=client) as mounter:
                        for image_type in ("Personalized", "Developer"):
                            try:
                                if await mounter.is_image_mounted(image_type):
                                    return
                            except Exception:
                                continue
                except Exception:
                    pass
                await auto_mount(client, version=version)
            finally:
                try:
                    await client.close()
                except Exception:
                    pass

        try:
            self.runner.run(worker(), timeout=420.0)
            return True, "挂载完成"
        except Exception as exc:
            name = exc.__class__.__name__
            text = str(exc)
            # 设备上已经有镜像时 pymobiledevice3 会抛 AlreadyMountedError ——
            # 这不是失败（我们要的就是"已挂载"状态），直接当成功处理。
            if "AlreadyMounted" in name or "already mounted" in text.lower():
                return True, "镜像已在设备上（无需重复挂载）"
            return False, f"{name}: {text}"

    def _run_mounter(self, args: list[str]) -> tuple[bool, str]:
        """执行 pymobiledevice3 mounter 子命令（源码版兜底路径），输出实时写入日志。"""
        import subprocess as _sp

        cli = self._mounter_cli()
        if cli is None:
            return False, "当前环境无法调用 pymobiledevice3 命令行"
        command = [*cli, "mounter", *args]
        self.log("info", "执行：" + " ".join(str(part) for part in command[1:]))
        try:
            process = _sp.Popen(
                command, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except Exception as exc:
            return False, f"无法启动挂载进程：{exc}"
        tail: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            text = line.strip()
            if not text:
                continue
            tail.append(text)
            if len(tail) > 40:
                tail.pop(0)
            self.log("info", "[挂载] " + text[:200])
        code = process.wait(timeout=300)
        if code == 0:
            return True, "挂载命令执行成功"
        summary = tail[-1] if tail else f"退出码 {code}"
        if "AccessDenied" in summary or "root" in summary.lower():
            summary += "（可能需要管理员权限）"
        return False, summary

    def _mounter_cli(self) -> list[str] | None:
        """返回可用的 pymobiledevice3 CLI 前缀（打包版不支持，返回 None）。"""
        import sys

        if getattr(sys, "frozen", False):
            return None
        return self.tunnel._cli_command()

    def _stage_personalized_ddi(self) -> bool:
        """把应用内置的个性化镜像装到 pymobiledevice3 期望的缓存目录。

        期望位置：~/.pymobiledevice3/Xcode_iOS_DDI_Personalized/
        文件名必须是 Image.dmg / BuildManifest.plist / **Image.trustcache**
        （注意源文件名是 Image.dmg.trustcache）。

        这样 iOS 17+ 全系（17 ~ 27.x）都能离线挂载；只有 TSS 签名需要联网，
        而那是 Apple 服务器按设备实时签发的，无法预置。
        """
        import sys

        sources: list[Path] = []
        base = getattr(sys, "_MEIPASS", None)
        if base:
            sources.append(Path(base) / "resources" / "ddi" / "personalized")
        sources.append(self.root / "resources" / "ddi" / "personalized")
        sources.append(Path.home() / ".ios-location-ddi" / "personalized")

        src = next((d for d in sources if (d / "Image.dmg").exists()), None)
        if src is None:
            return False

        dest = Path.home() / ".pymobiledevice3" / "Xcode_iOS_DDI_Personalized"
        try:
            dest.mkdir(parents=True, exist_ok=True)
            mapping = {
                "Image.dmg": "Image.dmg",
                "BuildManifest.plist": "BuildManifest.plist",
                "Image.dmg.trustcache": "Image.trustcache",
            }
            copied = []
            for source_name, dest_name in mapping.items():
                source_file = src / source_name
                if not source_file.exists():
                    continue
                target = dest / dest_name
                # 体积一致就跳过，避免每次挂载都拷 15MB
                if target.exists() and target.stat().st_size == source_file.stat().st_size:
                    continue
                shutil.copy2(source_file, target)
                copied.append(dest_name)
            if copied:
                self.log("info", "已从应用内置素材安装个性化镜像：" + "、".join(copied))
            return True
        except Exception as exc:
            self.log("warn", f"安装内置个性化镜像失败：{exc}")
            return False
        device = self.devices.get_selected()
        if device is None:
            return {"mounted": None, "running": self._mount_running}
        return {
            "mounted": self._developer_image_mounted(device.udid),
            "running": self._mount_running,
            "ios_major": device.ios_major,
        }

    def _cmd_mount_status(self, message: dict[str, Any]) -> dict[str, Any]:
        """查询当前设备的开发者镜像/会话状态。"""
        device = self.devices.get_selected()
        if device is None:
            return {"mounted": None, "ready": False, "running": self._mount_running}
        major = device.ios_major or 0
        return {
            "mounted": self._developer_image_mounted(device.udid),
            "ready": self._developer_session_ready(device.udid, major),
            "running": self._mount_running,
            "ios_major": major,
            "supported": major >= 17,
        }

    def _cmd_log_list(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"lines": self.recent_logs(_as_int(message.get("limit"), 200, "limit"))}

    def _cmd_route_build(self, message: dict[str, Any]) -> dict[str, Any]:
        route = self.routes.build(
            message.get("waypoints") or [],
            mode=message.get("mode") or "driving",
            name=message.get("name"),
            use_osrm=bool(message.get("use_osrm", True)),
            loop=bool(message.get("loop")),
            speed_scale=message.get("speed_scale") or 1.0,
        )
        engine = route.engine
        self.log(
            "info",
            f"路线规划完成（{'OSRM 真实路网' if engine == 'osrm' else '直线'}）："
            f"{route.distance / 1000:.2f} 公里，预计 {_fmt_duration(route.duration)}",
        )
        return {"route": route.to_json(), "engine": engine}

    def _cmd_tunnel_manual(self, message: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.tunnel.use_manual(
            str(message.get("host") or ""), message.get("port"), self.devices.selected_udid()
        )
        self.log("info", f"已切换为手动 RSD：{snapshot.get('rsd')}")
        return {"tunnel": snapshot}

    def _cmd_tunnel_ensure(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.mock or (self.devices.get_selected() is not None
                         and (self.devices.get_selected().ios_major or 0) < 17):
            semantics = self._tunnel_semantics()
            return {"tunnel": semantics, "need_tunnel": False}
        device = self.devices.get_selected()
        snapshot = self.tunnel.ensure(device.udid if device else None, allow_sudo=bool(message.get("sudo")))
        snapshot["ios_major"] = device.ios_major if device else None
        snapshot["need_tunnel"] = True
        return {"tunnel": snapshot, "need_tunnel": True}

    def _cmd_tiles_clear(self, message: dict[str, Any]) -> dict[str, Any]:
        removed = self.tiles.clear()
        self.log("info", f"已清理瓦片缓存：{removed} 个文件")
        return {"removed": removed}

    def _cmd_config_update(self, message: dict[str, Any]) -> dict[str, Any]:
        patch = message.get("config") or {}
        updated = self.config.update(patch)
        self.tiles.max_bytes = max(64, int(updated.get("tile_cache_max_mb") or 1024)) * 1024 * 1024
        return {"config": updated}

    def _require_device(self) -> device_mod.Device:
        device = self.devices.get_selected()
        if device is None:
            self.devices.refresh()
            device = self.devices.get_selected()
        if device is None:
            raise protocol.AppError(
                "未检测到 iOS 设备：请用数据线连接、解锁设备并在弹窗中点「信任」", "no_device", status=404
            )
        return device

    _COMMANDS: dict[str, Callable[["App", dict[str, Any]], dict[str, Any]]] = {
        "hello": _cmd_hello,
        "ping": _cmd_ping,
        "devices.refresh": _cmd_devices_refresh,
        "device.select": _cmd_device_select,
        "device.connect": _cmd_device_connect,
        "device.disconnect": _cmd_device_disconnect,
        "location.set": _cmd_location_set,
        "location.clear": _cmd_location_clear,
        "play.start": _cmd_play_start,
        "play.pause": _cmd_play_pause,
        "play.resume": _cmd_play_resume,
        "play.stop": _cmd_play_stop,
        "play.seek": _cmd_play_seek,
        "play.speed": _cmd_play_speed,
        "play.tuning": _cmd_play_tuning,
        "play.loop": _cmd_play_loop,
        "log.list": _cmd_log_list,
        "route.build": _cmd_route_build,
        "tunnel.manual": _cmd_tunnel_manual,
        "tunnel.ensure": _cmd_tunnel_ensure,
        "tiles.clear": _cmd_tiles_clear,
        "device.mount_image": _cmd_mount_developer_image,
        "device.mount_status": _cmd_mount_status,
        "config.update": _cmd_config_update,
    }

    # ---------------- 关停 ----------------
    def shutdown(self) -> None:
        try:
            self.playback.stop(notify=False, clear_provider=True)
        except Exception:
            pass
        try:
            self.close_session(stop_playback=False)
        except Exception:
            pass
        try:
            self.tunnel.stop()
        except Exception:
            pass
        try:
            self.devices.stop_monitor()
        except Exception:
            pass
        try:
            self.hub.close_all()
        except Exception:
            pass
        try:
            self.runner.shutdown()
        except Exception:
            pass


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"iOSLocationTool/{protocol.APP_VERSION}"
    protocol_version = "HTTP/1.1"
    app: App  # 由 serve() 注入

    # 静音默认访问日志，改用 app.log
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def log_error(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def handle_one_request(self) -> None:
        """浏览器关闭 keep-alive 连接时不要往终端刷一堆 ConnectionResetError 回溯。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True

    # ---------- 安全校验 ----------
    # 本工具监听在本机端口上，默认只允许「本机页面 / 无 Origin 的本机客户端」访问。
    # 否则任意一个网页都能通过跨站简单请求（text/plain 不需要预检）改配置、
    # 下发虚拟定位、甚至调用 /api/shutdown 关掉程序。
    _LOCAL_HOST_RE = re.compile(r"^(?:localhost|127\.0\.0\.1|\[::1\]|::1)(?::\d+)?$", re.I)

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return  # 非浏览器客户端（curl / 本工具自身）不带 Origin
        if origin == "null":
            raise protocol.AppError("已拒绝来自沙箱页面（Origin: null）的请求", "forbidden", status=403)
        netloc = urllib.parse.urlparse(origin).netloc.strip()
        request_host = (self.headers.get("Host") or "").strip()
        # 同源请求永远放行（这样 --host 0.0.0.0 时用局域网地址访问也正常）
        if netloc and request_host and netloc.lower() == request_host.lower():
            return
        if self._LOCAL_HOST_RE.match(netloc):
            return
        raise protocol.AppError(
            f"已拒绝来自 {origin} 的跨站请求（本工具仅允许本机/同源页面访问）", "forbidden", status=403
        )

    def _check_host(self) -> None:
        """防 DNS rebinding：攻击者把域名解析到 127.0.0.1 时 Host 会是其域名。"""
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return
        if self._LOCAL_HOST_RE.match(host):
            return
        # 允许用户显式用 --host 0.0.0.0 指定其它地址访问的场景
        if self.app.host not in ("127.0.0.1", "localhost"):
            return
        raise protocol.AppError(
            f"已拒绝 Host: {host} 的请求（防 DNS rebinding；请用 127.0.0.1 或 localhost 访问）",
            "forbidden",
            status=403,
        )

    # ---------- 工具 ----------
    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = protocol.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > 64 * 1024 * 1024:
            raise protocol.AppError("请求体过大", "too_large", status=413)
        return self.rfile.read(length)

    def _json_body(self) -> dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            data = protocol.loads(raw)
        except Exception as exc:
            raise protocol.AppError("请求体不是合法 JSON", "bad_json") from exc
        if not isinstance(data, dict):
            raise protocol.AppError("请求体必须是 JSON 对象", "bad_json")
        return data

    def _query(self) -> dict[str, list[str]]:
        parsed = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(parsed.query)

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            self._check_origin()
            self._check_host()
            if path == "/ws":
                self._handle_ws()
                return
            if path.startswith("/api/"):
                self._handle_api_get(path)
                return
            self._serve_static(path)
        except protocol.AppError as exc:
            self._send_json(exc.to_dict(), exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.app.log("error", f"GET {path} 失败：{exc.__class__.__name__}: {exc}")
            self._send_json({"ok": False, "error": f"服务端错误：{exc}"[:200], "code": "internal"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            self._check_origin()
            self._check_host()
            if path.startswith("/api/"):
                self._handle_api_post(path)
                return
            self._send_json({"ok": False, "error": "接口不存在", "code": "not_found"}, 404)
        except protocol.AppError as exc:
            self._send_json(exc.to_dict(), exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.app.log("error", f"POST {path} 失败：{exc.__class__.__name__}: {exc}")
            self._send_json({"ok": False, "error": f"服务端错误：{exc}"[:200], "code": "internal"}, 500)

    # ---------- WebSocket ----------
    def _handle_ws(self) -> None:
        if not ws.is_websocket_upgrade(self.headers):
            self._send_json({"ok": False, "error": "该端点仅支持 WebSocket", "code": "bad_upgrade"}, 400)
            return
        connection = ws.handshake(self.connection, self.headers)
        if connection is None:
            self.close_connection = True
            return
        self.close_connection = True  # 连接已被劫持，HTTP 循环不再读取
        self.app.hub.add(connection)
        self.app.log("info", f"界面已连接（{connection.addr[0]}）")

        def on_message(conn: ws.WSConnection, message: dict[str, Any]) -> None:
            self.app.handle_ws(conn, message)

        def on_close(conn: ws.WSConnection) -> None:
            self.app.hub.remove(conn)

        connection.run(on_message, on_close)

    # ---------- API ----------
    def _handle_api_get(self, path: str) -> None:
        app = self.app
        if path == "/api/health":
            self._send_json({
                "ok": True,
                "version": protocol.APP_VERSION,
                "name": protocol.APP_NAME,
                "mock": app.mock,
                "device_count": len(app.devices.list_devices()),
                "backend": app.devices.backend,
                "pymobiledevice3": device_mod.pymobiledevice3_importable(),
                "uptime": round(time.time() - app.started_at, 2),
                "ws_clients": app.hub.count,
            })
            return
        if path == "/api/devices":
            self._send_json({"ok": True, **app.devices.devices_json()})
            return
        if path == "/api/config":
            self._send_json({"ok": True, "config": app.config.snapshot()})
            return
        if path == "/api/settings/tiles":
            sources = app.config.tile_sources()
            current = app.config.get("tile_source") or "amap"
            items = [
                {
                    "key": key,
                    "id": key,
                    "name": value.get("name") or key,
                    "url": value.get("url") or "",
                    "max_zoom": int(value.get("max_zoom") or 18),
                    "attribution": value.get("attribution") or "",
                    "datum": str(value.get("coord") or ("gcj02" if key.startswith(("amap", "tencent")) else "wgs84")),
                    "subdomains": value.get("subdomains") or [],
                }
                for key, value in sources.items()
            ]
            self._send_json({
                "ok": True,
                "sources": items,
                "tile_sources": sources,
                "current": current,
                "tile_source": current,
                "selected": current,
                "cache": {
                    "dir": str(app.config.cache_dir()),
                    "size_mb": round(app.tiles.size_bytes() / 1048576, 2),
                    "max_mb": int(app.config.get("tile_cache_max_mb") or 1024),
                    "stats": dict(app.tiles.stats),
                },
            })
            return
        if path == "/api/tunnel":
            self._send_json({"ok": True, "tunnel": app._tunnel_semantics()})
            return
        if path == "/api/logs":
            raw_limit = (self._query().get("limit") or ["200"])[0]
            self._send_json({"ok": True, "lines": app.recent_logs(_as_int(raw_limit, 200, "limit"))})
            return
        if path.startswith("/api/tiles/"):
            self._serve_tile(path)
            return
        raise protocol.AppError("接口不存在", "not_found", status=404)

    def _handle_api_post(self, path: str) -> None:
        app = self.app
        if path == "/api/devices/select":
            body = self._json_body()
            udid = str(body.get("udid") or "").strip()
            if not udid:
                raise protocol.AppError("缺少 udid", "bad_request")
            app.close_session()
            device = app.devices.select(udid)
            app.config.update({"last_device": udid})
            self._send_json({"ok": True, "device": device.to_json()})
            return
        if path == "/api/devices/connect":
            result = app.handle_command("device.connect", {})
            tunnel = app.handle_command("tunnel.ensure", {}).get("tunnel") or app.tunnel.snapshot()
            self._send_json({"ok": True, **result, "tunnel": tunnel})
            return
        if path == "/api/devices/disconnect":
            app.close_session()
            app.tunnel.stop()
            device = app.devices.get_selected()
            self._send_json({
                "ok": True,
                "device": device.to_json() if device else None,
                "tunnel": app.tunnel.snapshot(),
            })
            return
        if path == "/api/config":
            patch = self._json_body()
            updated = app.config.update(patch.get("config") if "config" in patch else patch)
            self._send_json({"ok": True, "config": updated})
            return
        if path == "/api/route/build":
            body = self._json_body()
            result = app.handle_command("route.build", body)
            self._send_json({"ok": True, **result})
            return
        if path == "/api/gpx/export":
            body = self._json_body()
            route = route_engine.sanitize_route_payload(body.get("route") or body)
            if body.get("name"):
                route.name = str(body["name"])[:80]
            text = route_engine.to_gpx(route)
            body_bytes = text.encode("utf-8")
            filename = urllib.parse.quote(f"{route.name or 'track'}.gpx")
            self.send_response(200)
            self.send_header("Content-Type", "application/gpx+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
            self.end_headers()
            try:
                self.wfile.write(body_bytes)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path == "/api/gpx/import":
            raw = self._read_body()
            text = raw.decode("utf-8", "replace")
            coords, name, _kind = route_engine.from_gpx(text)
            payload = [[protocol.round6(lng), protocol.round6(lat)] for lat, lng in coords]
            self._send_json({
                "ok": True,
                # waypoints 与 path 都给全量点：前端可用 path 保形重放，也可只取端点做途经点
                "waypoints": payload,
                "path": payload,
                "points": payload,
                "name": name,
                "count": len(payload),
            })
            return
        if path == "/api/location":
            body = self._json_body()
            result = app.handle_command("location.set", body)
            self._send_json({"ok": True, **result})
            return
        if path == "/api/tunnel":
            body = self._json_body()
            # 统一走命令分发：mock 模式 / iOS<17 的"不需要隧道"语义只在一处实现，
            # 避免 REST 与 WS 两条路径行为不一致（曾导致 mock 下显示"隧道空闲/建立中"）。
            cmd = "tunnel.manual" if body.get("host") else "tunnel.ensure"
            result = app.handle_command(cmd, body)
            self._send_json({"ok": True, **result})
            return
        if path == "/api/tunnel/manual":
            body = self._json_body()
            result = app.handle_command("tunnel.manual", body)
            self._send_json({"ok": True, **result})
            return
        if path == "/api/shutdown":
            self._send_json({"ok": True})
            app.log("info", "收到退出请求，正在关闭…")
            app.shutdown_event.set()
            return
        raise protocol.AppError("接口不存在", "not_found", status=404)

    def _serve_tile(self, path: str) -> None:
        parts = path[len("/api/tiles/"):].split("/")
        if len(parts) < 4:
            raise protocol.AppError("瓦片地址格式应为 /api/tiles/{source}/{z}/{x}/{y}.png", "bad_tile", status=404)
        source_key = urllib.parse.unquote(parts[0])
        z, x = parts[1], parts[2]
        y = re.sub(r"\.(png|jpg|jpeg|webp)$", "", parts[3], flags=re.I)
        iz, ix, iy = tilecache.validate_tile_request(z, x, y)
        key, source = self.app.config.tile_source(source_key)
        result = self.app.tiles.get_tile(key, source, iz, ix, iy)
        if result.status != 200:
            # 契约要求失败回 404（前端才会走重试/告警逻辑），而不是用 200 + 占位图掩盖
            self._send_bytes(result.body, "image/png", 404, cache="no-store")
            return
        self._send_bytes(result.body, result.content_type, 200, cache="public, max-age=604800")

    # ---------- 静态文件 ----------
    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        if rel.startswith(".."):
            raise protocol.AppError("非法路径", "bad_path", status=400)
        target = (WEB_ROOT / rel).resolve()
        if not str(target).startswith(str(WEB_ROOT.resolve())):
            raise protocol.AppError("非法路径", "bad_path", status=400)
        if target.is_dir():
            target = target / "index.html"
        if not target.exists() or not target.is_file():
            # 前端是单页应用：未知路径回落到 index.html
            fallback = WEB_ROOT / "index.html"
            if fallback.exists() and "." not in rel.rsplit("/", 1)[-1]:
                target = fallback
            else:
                self._send_json({"ok": False, "error": f"文件不存在：{rel}", "code": "not_found"}, 404)
                return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        body = target.read_bytes()
        self._send_bytes(body, content_type, 200, cache="no-cache")


def _as_int(value: Any, default: int, field: str) -> int:
    """把外部输入安全地转成整数；非法值回中文 400，而不是 500 内部错误。"""
    if value is None:
        return default
    if isinstance(value, bool):
        raise protocol.AppError(f"{field} 必须是整数", "bad_request")
    number = protocol.finite(value, field)
    if number != int(number):
        raise protocol.AppError(f"{field} 必须是整数", "bad_request")
    return int(number)


def _fmt_duration(seconds: float) -> str:
    total = int(max(0, round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def serve(app: App, host: str = protocol.DEFAULT_HOST, port: int = protocol.DEFAULT_PORT) -> ThreadingHTTPServer:
    app.host = host
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
