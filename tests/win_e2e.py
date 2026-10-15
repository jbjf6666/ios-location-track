"""Windows 真机端到端验收：userspace 隧道 → 开发者镜像 → DVT → 下发定位 → 恢复。

这是 docs/WINDOWS_PORT.md 第 7 节「真机验收」的自动化版本，也是 Windows 版
**唯一的可行性判定脚本**（第 3 节要求先验证隧道再写界面）。

用法（项目根目录）：
    .venv\\Scripts\\python.exe tests\\win_e2e.py --confirm
    .venv\\Scripts\\python.exe tests\\win_e2e.py --confirm --lat 39.908 --lon 116.397 --hold 8
    .venv\\Scripts\\python.exe tests\\win_e2e.py --confirm --rsd 10.7.0.1 51234   # 用已有隧道

不加 --confirm 只做只读检查（设备发现 + 隧道建立），不下发定位。
退出前一定会调用 clear_location()，让设备恢复真实定位。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


async def list_devices() -> list:
    from pymobiledevice3.usbmux import list_devices

    return await list_devices()


async def main_async(args: argparse.Namespace) -> int:
    print("=" * 68)
    print("Windows 真机端到端验收")
    print("=" * 68)

    # ---------- 1. 依赖 ----------
    try:
        import pymobiledevice3

        version = getattr(pymobiledevice3, "__version__", "?")
        check("pymobiledevice3 可导入", True, f"v{version}")
    except Exception as exc:
        check("pymobiledevice3 可导入", False, str(exc))
        return 1

    # ---------- 2. 设备发现 ----------
    try:
        devices = await list_devices()
    except Exception as exc:
        check("usbmux 设备发现", False, repr(exc))
        return 1
    check("usbmux 设备发现", len(devices) > 0,
          f"{len(devices)} 台" + (f"：{devices[0].serial}" if devices else "（请插线并信任）"))
    if not devices:
        return 1
    udid = args.udid or devices[0].serial

    # ---------- 3. 隧道 ----------
    #
    # 关键（Windows 与 macOS 的架构差异）：
    # Windows 的 userspace 隧道把设备地址放在**本进程的 PyTCP 用户态协议栈**里，
    # 内核路由不到它（直接 TCP connect 会得到 WinError 1231 网络位置不可达）。
    # pymobiledevice3 的做法是把一个 relay dialer 注入到它返回的 RemoteServiceDiscoveryService
    # 对象上（`open_connection=dial_plane.dial`）。因此**必须复用这个 rsd 对象**，
    # 不能拿 (host, port) 另建一个 RSD —— 后者用的是 stdlib dialer，必然失败。
    # RealLocationProvider 已经预留了 `rsd=` 注入口（见 core/location_provider.py:242/395）。
    tunnel = None
    rsd = None
    rsd_host = rsd_port = None
    if args.rsd:
        rsd_host, rsd_port = args.rsd[0], int(args.rsd[1])
        check("隧道（手动指定）", True, f"{rsd_host}:{rsd_port}")
    else:
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

        tunnel = UserspaceRsdTunnel(serial=udid, autopair=True,
                                    remotepairing_fallback=True)
        started = time.monotonic()
        try:
            rsd = await asyncio.wait_for(tunnel.aopen(), timeout=args.tunnel_timeout)
            rsd_host, rsd_port = rsd.service.address
            inproc = getattr(rsd, "is_in_process_tunnel", False)
            check("userspace 隧道建立", True,
                  f"{rsd_host} {rsd_port}（{time.monotonic()-started:.1f}s，"
                  f"进程内={inproc}）")
        except Exception as exc:
            check("userspace 隧道建立", False, repr(exc))
            traceback.print_exc()
            return 1

    # ---------- 4. 开发者镜像 ----------
    from core.location_provider import RealLocationProvider

    logs: list[str] = []

    def on_log(level: str, msg: str) -> None:
        logs.append(f"[{level}] {msg}")
        print(f"    · {msg}", flush=True)

    provider = RealLocationProvider(
        rsd=rsd,
        rsd_host=rsd_host, rsd_port=rsd_port, udid=udid,
        connect_timeout=args.connect_timeout, on_log=on_log,
    )

    exit_code = 0
    connected = False
    try:
        print("  --- 挂载镜像并建立 DVT 会话 ---", flush=True)
        info = await provider.connect(None)
        connected = True
        check("开发者镜像 + DVT 会话", True,
              f"策略={info.get('strategy')} iOS={info.get('ios_version') or info.get('ios_major')}")

        if not args.confirm:
            print("\n  （未加 --confirm：只验证到会话就绪，不下发定位）")
            return 0

        # ---------- 5. 下发定位 ----------
        before = args.lat, args.lon
        await provider.set_location(before[0], before[1])
        check("下发虚拟定位", True, f"lat={before[0]:.6f} lon={before[1]:.6f}")

        print(f"  --- 保持 {args.hold:.0f} 秒（请观察手机定位是否移动）---", flush=True)
        await asyncio.sleep(args.hold)

        # ---------- 6. 再下发一个点，验证连接是长连接 ----------
        if args.hold2 is not None:
            await provider.set_location(before[0] + 0.001, before[1] + 0.001)
            check("同一会话连续下发", True, "第二点成功（证明 DVT 长连接有效）")
            await asyncio.sleep(1.0)

    except Exception as exc:
        check("真机定位流程", False, repr(exc))
        traceback.print_exc()
        exit_code = 1
    finally:
        if connected:
            try:
                await provider.clear_location()
                check("恢复真实定位", True, "已调用 clear_location()")
            except Exception as exc:
                check("恢复真实定位", False, repr(exc))
                exit_code = 1
            try:
                await provider.close()
            except Exception:
                pass
        if tunnel is not None:
            try:
                await tunnel.aclose()
            except Exception:
                pass

    print("\n" + "=" * 68)
    failed = [c for c in CHECKS if not c[1]]
    print(f"结果：{len(CHECKS)-len(failed)}/{len(CHECKS)} 项通过")
    for name, _ok, detail in failed:
        print(f"  FAIL {name}: {detail}")
    print("=" * 68)
    return exit_code or (1 if failed else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows 真机端到端验收")
    parser.add_argument("--confirm", action="store_true",
                        help="确认对真机下发虚拟定位（不加则只读到会话就绪）")
    parser.add_argument("--lat", type=float, default=39.9087, help="纬度（默认 39.9087 天安门）")
    parser.add_argument("--lon", type=float, default=116.3975, help="经度")
    parser.add_argument("--hold", type=float, default=8.0, help="首个点保持秒数（默认 8）")
    parser.add_argument("--hold2", action="store_true", help="额外下发第二个点验证长连接")
    parser.add_argument("--udid", default=None)
    parser.add_argument("--rsd", nargs=2, metavar=("HOST", "PORT"), default=None)
    parser.add_argument("--tunnel-timeout", type=float, default=90.0)
    parser.add_argument("--connect-timeout", type=float, default=30.0)
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
