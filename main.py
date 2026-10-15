#!/usr/bin/env python3
"""iOS 虚拟定位轨迹工具 — 程序入口（macOS / Windows 通用）。

用法（Windows）：
    .\\scripts\\run.ps1                    # 正常启动（自动装依赖、自动检查/安装 Apple 驱动）
    .venv\\Scripts\\python.exe main.py --mock          # 无设备模拟模式
    .venv\\Scripts\\python.exe main.py --check-driver  # 只检查 Apple Mobile Device Support
    .venv\\Scripts\\python.exe main.py --install-driver # 强制检查并安装

用法（macOS）：
    ./scripts/run.sh
    ./.venv/bin/python main.py --mock
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from core import platform as plat  # noqa: E402

ROOT, DATA_ROOT = plat.resolve_roots()
sys.path.insert(0, str(ROOT))

from core import protocol, server  # noqa: E402
from core import device as device_mod  # noqa: E402

BANNER = r"""
  ___  ___    ___                     _      _   _
 |_ _|/ __|  | _ \_____ __ _____ _ _ (_)__ _| |_(_)___ _ _
  | | \__ \  |  _/ _ \ V  V / -_) ' \| / _` |  _| / _ \ ' \
 |___||___/  |_| \___/\_/\_/\___|_||_|_\__,_|\__|_\___/_||_|
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ios-location",
        description=f"{protocol.APP_NAME} v{protocol.APP_VERSION}",
    )
    parser.add_argument("--host", default=protocol.DEFAULT_HOST,
                        help="监听地址（默认 127.0.0.1，仅本机可访问）")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT,
                        help=f"监听端口（默认 {protocol.DEFAULT_PORT}）")
    parser.add_argument("--mock", action="store_true",
                        help="无设备模拟模式：不连真机，用于界面演示与验收")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开界面")
    parser.add_argument("--window", choices=["app", "browser", "none"], default="app",
                        help="界面打开方式：app=Chrome/Edge 应用窗口（默认），browser=默认浏览器，none=不打开")
    # Apple 驱动（仅 Windows 有意义）
    parser.add_argument("--check-driver", action="store_true",
                        help="只检查 Apple Mobile Device Support 状态后退出")
    parser.add_argument("--install-driver", action="store_true",
                        help="检查 Apple Mobile Device Support，缺失时自动安装")
    parser.add_argument("--no-driver-check", action="store_true",
                        help="跳过启动时的 Apple 驱动检查")
    parser.add_argument("--no-setup", action="store_true",
                        help="跳过启动时的「环境准备」窗口（依赖已就绪时可用）")
    # 内部助手模式：`<可执行文件> tunnel-helper --udid XXX` 建立 RSD 隧道并打印 HOST PORT
    parser.add_argument("command", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--udid", default=None,
                        help=argparse.SUPPRESS)  # 供 tunnel-helper 指定设备
    parser.add_argument("--version", action="version",
                        version=f"{protocol.APP_NAME} {protocol.APP_VERSION}")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# Apple Mobile Device Support（Windows 上的 usbmuxd）
# --------------------------------------------------------------------------
def driver_status_line(status: dict) -> str:
    if not status.get("applicable", True):
        return "不适用（非 Windows）"
    if status.get("ready"):
        probe = status.get("probe") or {}
        devices = (status.get("devices") or {}).get("count", 0)
        return f"已就绪（usbmuxd 可通信，发现 {devices} 台设备）" if devices else "已就绪（usbmuxd 可通信）"
    return status.get("summary") or "不可用"


def ensure_apple_driver(log, allow_install: bool) -> bool:
    """启动时检查 Apple Mobile Device Support；缺失则自动安装。

    返回 True 表示 usbmuxd 可用。任何异常都不应阻止程序启动（界面仍可用模拟模式）。
    """
    from core import apple_driver

    def emit(level: str, message: str) -> None:
        level = level if level in ("info", "warn", "error", "success") else "info"
        log(level, message)
        # 打包成窗口程序时没有控制台，这行只在源码/命令行运行时可见
        print(f"[apple-driver] {message}", flush=True)

    try:
        status = apple_driver.status(deep=True)
    except Exception as exc:
        log("warn", f"Apple 驱动检测失败（忽略）：{exc}")
        return False

    if status.get("ready"):
        log("info", f"Apple Mobile Device Support：{driver_status_line(status)}")
        return True

    log("warn", f"Apple Mobile Device Support：{status.get('summary')}")
    if not allow_install:
        log("warn", "已跳过自动安装。可运行 `main.py --install-driver` 手动修复。")
        return False

    if not plat.is_admin():
        log("error", "安装 Apple Mobile Device Support 需要管理员权限。"
                     "请右键程序 →「以管理员身份运行」，或运行 `main.py --install-driver`。")
        return False

    try:
        result = apple_driver.ensure(log=emit, allow_install=True)
    except Exception as exc:
        log("error", f"自动安装 Apple 驱动时出错：{exc}")
        return False

    if result.get("ok"):
        action = result.get("action")
        if action == "none":
            log("info", "Apple Mobile Device Support 已就绪")
        elif action == "start_service":
            log("info", "Apple Mobile Device Service 已启动")
        else:
            log("info", "Apple Mobile Device Support 安装完成")
        if result.get("reboot_required"):
            log("warn", "安装完成但**需要重启 Windows** 后 usbmuxd 才会生效，请重启后重新打开本程序。")
        return True

    log("error", result.get("error") or "Apple Mobile Device Support 不可用")
    log("warn", "设备发现将不可用。可手动安装 iTunes：https://www.apple.com/itunes/download/win64")
    return False


def run_check_driver(install: bool) -> int:
    """`--check-driver` / `--install-driver` 的实现。"""
    from core import apple_driver

    def emit(level: str, message: str) -> None:
        prefix = {"error": "[错误]", "warn": "[警告]", "info": "[信息]"}.get(level, "[信息]")
        print(f"{prefix} {message}", flush=True)

    if not plat.IS_WINDOWS:
        print("Apple Mobile Device Support 只在 Windows 上需要（macOS 自带 usbmuxd）。")
        return 0

    status = apple_driver.status(deep=True)
    print(f"平台：{plat.platform_label()}（管理员：{'是' if plat.is_admin() else '否'}）")
    print(f"服务：{status['service']['state']}"
          + ("（Apple Mobile Device Service）" if status["service"]["exists"] else ""))
    product = status.get("product") or {}
    if product:
        print(f"版本：{product.get('display_name')} {product.get('displayversion', '?')}")
        if product.get("upgrade_code"):
            print(f"UpgradeCode：{product['upgrade_code']}")
    probe = status.get("probe") or {}
    print(f"usbmuxd(127.0.0.1:27015)：{'可通信' if probe.get('ok') else '不可用 —— ' + str(probe.get('error'))}")
    devices = status.get("devices") or {}
    if devices.get("ok"):
        print(f"设备：{devices.get('count', 0)} 台")
        for item in devices.get("devices", []):
            print(f"  · {item['serial']}（{item['connection']}）")
    print(f"结论：{status['summary']}")

    if status.get("ready"):
        return 0
    if not install:
        print("\n如需自动安装，请运行：main.py --install-driver（需要管理员权限）")
        return 1

    print("\n开始自动安装…", flush=True)
    result = apple_driver.ensure(log=emit, allow_install=True)
    if result.get("ok"):
        print("\n[成功] Apple Mobile Device Support 已可用。")
        return 0
    print(f"\n[失败] {result.get('error')}")
    return 1


def run_tunnel_helper(udid: str | None) -> int:
    """建立 iOS 17+ 的 RSD 隧道并保持进程存活，向 stdout 打印「HOST PORT」。

    - macOS：借 Apple ``remoted`` 的 native 隧道（内核可路由，父进程能直接连）。
    - Windows：userspace 隧道（进程内 PyTCP）。**它的地址父进程连不上**，
      因此本助手模式在 Windows 上只用于诊断/兼容，正式路径是
      ``core/tunnel.py`` 在应用自己的事件循环里直接建隧道。
    """
    import asyncio

    def _emit(line: str) -> None:
        # 打包成窗口程序时 sys.stdout 可能是 None，stdout/stderr 各写一份
        for stream in (sys.stdout, sys.stderr):
            if stream is None:
                continue
            try:
                stream.write(line + "\n")
                stream.flush()
            except Exception:
                pass

    async def main_async() -> int:
        if plat.IS_WINDOWS:
            from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

            tunnel = UserspaceRsdTunnel(serial=udid, autopair=True,
                                        remotepairing_fallback=True)
            try:
                rsd = await tunnel.aopen()
                address, port = rsd.service.address
                _emit(f"{address} {port}")
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return 0
            except Exception as exc:
                _emit(f"隧道建立失败：{exc}")
                return 1
            finally:
                try:
                    await tunnel.aclose()
                except Exception:
                    pass
            return 0

        try:
            from pymobiledevice3.remote.native_tunnel import establish_native_rsd
        except Exception as exc:
            _emit(f"无法加载隧道模块：{exc}")
            return 2
        try:
            rsd = await establish_native_rsd(serial=udid)
        except Exception as exc:
            _emit(f"建立隧道失败：{exc}")
            return 1
        address, port = rsd.service.address
        _emit(f"{address} {port}")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return 0
        return 0

    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def wait_health(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1.5) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def open_ui(url: str, mode: str) -> None:
    if mode == "none":
        return
    if mode == "app":
        binary = plat.find_app_browser()
        if binary is not None:
            try:
                import subprocess

                subprocess.Popen(
                    [str(binary), f"--app={url}", "--window-size=1440,900",
                     "--user-data-dir=" + str(plat.app_window_profile_dir())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def find_free_port(host: str, preferred: int) -> int:
    for candidate in range(preferred, preferred + 20):
        if not port_in_use(host, candidate):
            return candidate
    return preferred


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "tunnel-helper":
        return run_tunnel_helper(args.udid)
    if args.check_driver or args.install_driver:
        return run_check_driver(install=args.install_driver)

    mock_flag = bool(args.mock or os.environ.get("IOSLOC_MOCK") == "1")

    # ---------------- 启动时的「环境准备」窗口 ----------------
    # 每次打开都显示：逐项列出依赖，缺的可以直接点右侧按钮安装，
    # 必需项全部就绪后「进入程序」才可点击。
    if not args.no_setup and not mock_flag:
        from core import setup_window

        setup_result = setup_window.run_setup_window(title="环境准备")
        if setup_result is False:
            print("已取消（环境未就绪）。")
            return 0
        if setup_result is None:
            print("  [提示] 无法显示「环境准备」窗口（tkinter 不可用），"
                  "改为在后台自动检查并安装依赖。")
            args.no_driver_check = False

    mock = mock_flag
    host = args.host
    url_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    if port_in_use(host, args.port):
        # 已有实例在跑：直接复用，避免双开互相抢 RSD 连接
        url = f"http://{url_host}:{args.port}"
        if wait_health(url, timeout=3.0):
            print(f"检测到已有一个实例在运行：{url}")
            open_ui(url, args.window)
            return 0
        args.port = find_free_port(host, args.port + 1)

    print(BANNER)
    print(f"  {protocol.APP_NAME} v{protocol.APP_VERSION}")
    print(f"  平台 {plat.platform_label()} | Python {sys.version.split()[0]} | 数据目录 {DATA_ROOT}")
    if mock:
        print("  模式：无设备模拟（--mock），不会连接真机")
    else:
        print(f"  pymobiledevice3：{'已就绪' if device_mod.pymobiledevice3_importable() else '未安装'}"
              f" | RSD 隧道：{'userspace（进程内）' if plat.IS_WINDOWS else 'native'}")
        if plat.IS_WINDOWS:
            print(f"  管理员权限：{'是' if plat.is_admin() else '否（userspace 隧道不需要）'}")

    app = server.App(root=DATA_ROOT, mock=mock, port=args.port, resources=ROOT)
    app.log("info", f"{protocol.APP_NAME} 启动，端口 {args.port}")

    # 关键依赖自检：打包后漏打模块会立刻暴露，而不是等到"设备发现莫名退化成
    # libimobiledevice"才发现。Windows 需要额外检查 userspace 隧道链路。
    missing = []
    modules = [
        "pymobiledevice3",
        "pymobiledevice3.lockdown",
        "pymobiledevice3.usbmux",
        "pymobiledevice3.remote.remote_service_discovery",
        "pymobiledevice3.services.dvt.instruments.dvt_provider",
        "pymobiledevice3.services.dvt.instruments.location_simulation",
    ]
    modules.append(
        "pymobiledevice3.remote.userspace_tunnel" if plat.IS_WINDOWS
        else "pymobiledevice3.remote.native_tunnel"
    )
    if plat.IS_WINDOWS:
        modules.append("pytun_pmd3")
    for module in modules:
        try:
            __import__(module)
        except Exception as exc:
            missing.append(f"{module}({exc})")
    if missing:
        app.log("error", "关键模块缺失，真机功能将不可用：" + "；".join(missing))
        print("  [错误] 关键模块缺失：" + "；".join(missing))
    elif not mock:
        app.log("info", "关键模块自检通过（pymobiledevice3 / RSD 隧道 / DVT 定位）")

    # Windows：Apple Mobile Device Support（usbmuxd）——没有它一台设备都发现不了。
    # 这是本工具在 Windows 上唯一的系统级依赖，缺失时自动安装。
    #
    # 关键：放到**后台线程**里做。首次运行可能要下载 40~200MB，如果同步跑，
    # 界面会迟迟不出现（打包成窗口程序后用户看不到任何进度）。后台跑的话，
    # 界面已经打开，安装日志会实时通过 WebSocket 推到界面上。
    driver_state: dict[str, Any] = {"ok": None, "done": threading.Event()}

    def _driver_worker() -> None:
        ok = ensure_apple_driver(app.log, allow_install=True)
        driver_state["ok"] = ok
        try:
            app.devices.refresh()
            app._on_devices_change()
        except Exception:
            pass
        driver_state["done"].set()

    if plat.IS_WINDOWS and not mock and not args.no_driver_check:
        threading.Thread(target=_driver_worker, name="apple-driver", daemon=True).start()

    if getattr(app.config, "_repaired", False):
        app.log("warn", "config.json 中有非法项（类型/范围不合法），已自动回退为默认值")

    # 兜底清理：无论怎么退出（SIGINT/SIGTERM/异常），都不能把隧道留在后台，
    # 否则 iOS 17+ 的单条 RSD 连接会被残留占住，下一次定位会失败。
    import atexit
    import signal

    atexit.register(app.shutdown)
    for sig in plat.termination_signals():
        try:
            signal.signal(sig, lambda *_a: app.shutdown_event.set())
        except (ValueError, OSError, AttributeError):
            pass
    if mock:
        app.log("warn", "当前为模拟模式：定位不会真正下发到设备")

    app.devices.refresh()
    app.devices.start_monitor()
    last_device = app.config.get("last_device")
    if last_device and app.devices.get(last_device):
        try:
            app.devices.select(last_device)
        except Exception:
            pass
    devices = app.devices.list_devices()
    if devices:
        for device in devices:
            app.log("info", f"发现设备：{device.name}（iOS {device.ios_version or '未知'}，{device.udid}）")
    elif not mock:
        if plat.IS_WINDOWS and not args.no_driver_check:
            app.log("info", "正在检查/安装 Apple Mobile Device Support（usbmuxd），完成后会自动刷新设备列表…")
        else:
            app.log("warn", "暂未检测到 iOS 设备：请用数据线连接、解锁并在手机上点「信任」")

    httpd = server.serve(app, host=host, port=args.port)
    url = f"http://{url_host}:{args.port}"
    print(f"  界面地址：{url}")
    print("  按 Ctrl+C 退出\n")

    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()

    if not args.no_browser and args.window != "none":
        def _open() -> None:
            if wait_health(url, timeout=15.0):
                open_ui(url, args.window)

        threading.Thread(target=_open, name="open-ui", daemon=True).start()

    try:
        while not app.shutdown_event.is_set():
            app.shutdown_event.wait(0.5)
    except KeyboardInterrupt:
        print("\n收到退出信号，正在关闭…")
    finally:
        app.log("info", "正在关闭：停止模拟并释放隧道")
        app.shutdown()
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        print("已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
