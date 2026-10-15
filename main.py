#!/usr/bin/env python3
"""iOS 虚拟定位轨迹工具 — 程序入口。

用法：
    ./run.sh                     # 正常启动（自动打开界面）
    ./.venv/bin/python main.py --mock    # 无设备模拟模式（验收/演示用）
    ./.venv/bin/python main.py --port 8899 --no-browser
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

def _resolve_roots() -> tuple[Path, Path]:
    """返回 (资源根, 数据根)。

    - 源码运行：两者都是项目目录
    - 打包成 .app：资源在 sys._MEIPASS（只读），数据写到
      ~/Library/Application Support/iOSLocationTrack
    """
    if getattr(sys, "frozen", False):
        resources = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        data = Path.home() / "Library" / "Application Support" / "iOSLocationTrack"
        data.mkdir(parents=True, exist_ok=True)
        return resources, data
    root = Path(__file__).resolve().parent
    return root, root


ROOT, DATA_ROOT = _resolve_roots()
sys.path.insert(0, str(ROOT))

from core import device as device_mod  # noqa: E402
from core import protocol, server  # noqa: E402

BANNER = r"""
  ___  ___    ___                     _      _   _
 |_ _|/ __|  | _ \_____ __ _____ _ _ (_)__ _| |_(_)___ _ _
  | | \__ \  |  _/ _ \ V  V / -_) ' \| / _` |  _| / _ \ ' \
 |___||___/  |_| \___/\_/\_/\___|_||_|_\__,_|\__|_\___/_||_|
"""

APP_MODE_BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ios-location",
        description=f"{protocol.APP_NAME} v{protocol.APP_VERSION}",
    )
    parser.add_argument("--host", default=protocol.DEFAULT_HOST, help="监听地址（默认 127.0.0.1，仅本机可访问）")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT, help=f"监听端口（默认 {protocol.DEFAULT_PORT}）")
    parser.add_argument("--mock", action="store_true", help="无设备模拟模式：不连真机，用于界面演示与验收")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开界面")
    parser.add_argument("--window", choices=["app", "browser", "none"], default="app",
                        help="界面打开方式：app=Chrome/Edge 应用窗口（默认，最像原生），browser=默认浏览器，none=不打开")
    # 内部助手模式：`<可执行文件> tunnel-helper --udid XXX` 建立 RSD 隧道并打印 HOST PORT
    parser.add_argument("command", nargs="?", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--udid", default=None,
                        help=argparse.SUPPRESS)  # 供 tunnel-helper 指定设备
    parser.add_argument("--version", action="version", version=f"{protocol.APP_NAME} {protocol.APP_VERSION}")
    return parser.parse_args(argv)


def run_tunnel_helper(udid: str | None) -> int:
    """建立 iOS 17+ 的 RSD 隧道并保持进程存活，向 stdout 打印「HOST PORT」。

    打包成 .app 后不能再调用 `python -m pymobiledevice3`，因此把助手模式做进本程序：
    主进程用 `sys.executable tunnel-helper` 递归启动自己（见 core/tunnel.py）。
    """
    import asyncio

    try:
        from pymobiledevice3.remote.native_tunnel import establish_native_rsd
    except Exception as exc:  # pragma: no cover
        print(f"无法加载隧道模块：{exc}", file=sys.stderr)
        return 2

    async def main_async() -> int:
        try:
            rsd = await establish_native_rsd(serial=udid)
        except Exception as exc:
            print(f"建立隧道失败：{exc}", file=sys.stderr)
            return 1
        address, port = rsd.service.address
        # 关键：这一行会被 core/tunnel.py 解析出 RSD 地址。
        # 打包成 .app 时若以"无控制台"方式启动，sys.stdout 可能是 None，
        # 此时打印会抛 AttributeError，导致隧道地址传不出去 → 真机连不上。
        # 因此 stdout 与 stderr 都写一份（父进程两者都会读取）。
        line = f"{address} {port}"
        for stream in (sys.stdout, sys.stderr):
            if stream is None:
                continue
            try:
                stream.write(line + "\n")
                stream.flush()
            except Exception:
                pass
        try:
            await asyncio.Event().wait()   # 持有隧道断言直到进程结束
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
        for binary in APP_MODE_BROWSERS:
            if os.path.exists(binary):
                try:
                    import subprocess

                    subprocess.Popen(
                        [binary, f"--app={url}", "--window-size=1440,900",
                         "--user-data-dir=" + str(Path.home() / ".ios-location-appwindow")],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return
                except Exception:
                    continue
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
        # 内部助手模式：由 TunnelManager 递归调用，只负责建隧道并打印 HOST PORT
        return run_tunnel_helper(args.udid)
    mock = bool(args.mock or os.environ.get("IOSLOC_MOCK") == "1")
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
    print(f"  Python {sys.version.split()[0]} | 数据目录 {DATA_ROOT}")
    if mock:
        print("  模式：无设备模拟（--mock），不会连接真机")
    else:
        print(f"  pymobiledevice3：{'已就绪' if device_mod.pymobiledevice3_importable() else '未安装（将使用 libimobiledevice 兜底，请运行 ./run.sh 自动安装）'}")

    app = server.App(root=DATA_ROOT, mock=mock, port=args.port)
    app.log("info", f"{protocol.APP_NAME} 启动，端口 {args.port}")
    # 关键依赖自检：打包成 .app 后若漏打模块，这里会立刻暴露，
    # 而不是等到"设备发现莫名退化成 libimobiledevice"才发现。
    missing = []
    for module in (
        "pymobiledevice3",
        "pymobiledevice3.lockdown",
        "pymobiledevice3.usbmux",
        "pymobiledevice3.remote.native_tunnel",
        "pymobiledevice3.remote.remote_service_discovery",
        "pymobiledevice3.services.dvt.instruments.dvt_provider",
        "pymobiledevice3.services.dvt.instruments.location_simulation",
    ):
        try:
            __import__(module)
        except Exception as exc:
            missing.append(f"{module}({exc})")
    if missing:
        app.log("error", "关键模块缺失，真机功能将不可用：" + "；".join(missing))
    elif not mock:
        app.log("info", "关键模块自检通过（pymobiledevice3 / RSD 隧道 / DVT 定位）")
    if getattr(app.config, "_repaired", False):
        app.log("warn", "config.json 中有非法项（类型/范围不合法），已自动回退为默认值")

    # 兜底清理：无论怎么退出（SIGINT/SIGTERM/异常），都不能把隧道子进程留在后台，
    # 否则 iOS 17+ 的单条 RSD 连接会被残留进程占住，下一次定位会失败。
    import atexit
    import signal

    atexit.register(app.shutdown)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
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
