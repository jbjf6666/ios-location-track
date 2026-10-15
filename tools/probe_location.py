#!/usr/bin/env python3
"""命令行测试真机虚拟定位（单点设置 / 清除）。

用法（项目根目录下执行）：

    # iOS<17：USB 直连，设置单点位置并保持（Ctrl-C 结束，结束时自动恢复真实定位）
    ./.venv/bin/python tools/probe_location.py --lat 39.908 --lon 116.397

    # iOS 17+：先建隧道再指定 RSD 地址
    ./.venv/bin/python tools/probe_location.py --rsd 10.7.0.1 51234 --lat 39.908 --lon 116.397

    # 只恢复真实定位
    ./.venv/bin/python tools/probe_location.py --rsd 10.7.0.1 51234 --clear

    # 自动化：只保持 10 秒后自动清除
    ./.venv/bin/python tools/probe_location.py --lat 39.908 --lon 116.397 --duration 10

注意：DVT 虚拟定位依赖**保持连接**（连接断开即失效），所以本工具在 --duration 期间
一直持有同一个 DVT 会话；退出前会调用 clear_location() 让手机恢复真实定位。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.location_provider import (  # noqa: E402
    PMD3_MISSING_HINT,
    RealLocationProvider,
    pymobiledevice3_available,
    pymobiledevice3_version,
)


def _print(text: str = "") -> None:
    print(text, flush=True)


def _log(level: str, msg: str) -> None:
    prefix = {"error": "[错误]", "warning": "[警告]", "success": "[成功]"}.get(level, "[信息]")
    _print(f"{prefix} {msg}")


async def _main_async(args: argparse.Namespace) -> int:
    if not pymobiledevice3_available():
        _print(f"[错误] {PMD3_MISSING_HINT}")
        return 1

    if not args.clear and (args.lat is None or args.lon is None):
        _print("[错误] 需要同时提供 --lat 与 --lon（或使用 --clear 仅恢复真实定位）")
        return 1

    provider = RealLocationProvider(
        rsd_host=args.rsd[0] if args.rsd else None,
        rsd_port=int(args.rsd[1]) if args.rsd else None,
        udid=args.udid,
        connect_timeout=args.timeout,
        on_log=_log,
    )

    _print(f"pymobiledevice3 版本：{pymobiledevice3_version()}")
    if args.rsd:
        _print(f"RSD 隧道：{args.rsd[0]}:{args.rsd[1]}")
    else:
        _print("未指定 --rsd：将尝试 usbmux/lockdown 直连（仅 iOS<17 可用）")
    _print("")

    try:
        info = await provider.connect(None)
    except Exception as exc:
        _print(f"[错误] 连接失败：{exc}")
        return 1

    _print(f"[信息] 会话已建立：{info.get('detail')}")
    _print(f"[信息] 策略={info.get('strategy')} iOS={info.get('ios_version') or info.get('ios_major')}")

    exit_code = 0
    try:
        if args.clear:
            await provider.clear_location()
            _print("[成功] 已恢复真实定位")
        else:
            await provider.set_location(float(args.lat), float(args.lon))
            _print(f"[成功] 已设置虚拟定位 lat={float(args.lat):.6f}, lon={float(args.lon):.6f}")
            if args.json:
                _print(json.dumps({"ok": True, "lat": args.lat, "lon": args.lon}, ensure_ascii=False))

            if args.duration and args.duration > 0:
                _print(f"[信息] 保持 {args.duration:.0f} 秒（期间不要断开连接）…")
                await asyncio.sleep(args.duration)
            else:
                _print("[信息] 定位保持中：按 Ctrl-C 结束并恢复真实定位…")
                try:
                    while True:
                        await asyncio.sleep(1.0)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    _print("")
                    _print("[信息] 收到中断，准备恢复真实定位…")
    except KeyboardInterrupt:
        _print("")
        _print("[信息] 收到中断，准备恢复真实定位…")
    except Exception as exc:
        _print(f"[错误] {exc}")
        exit_code = 1
    finally:
        try:
            await provider.clear_location()
            _print("[信息] 已调用 clear_location()，设备恢复真实定位")
        except Exception as exc:
            _print(f"[警告] 恢复真实定位失败：{exc}")
            exit_code = exit_code or 1
        try:
            await provider.close()
        except Exception as exc:
            _print(f"[警告] 关闭会话时出错：{exc}")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="命令行测试真机虚拟定位（DVT）")
    parser.add_argument("--lat", type=float, help="纬度（-90~90，WGS84）")
    parser.add_argument("--lon", type=float, help="经度（-180~180，WGS84）")
    parser.add_argument("--clear", action="store_true", help="只清除虚拟定位，恢复真实定位")
    parser.add_argument("--rsd", nargs=2, metavar=("HOST", "PORT"), help="RSD 隧道地址（iOS 17+ 必需）")
    parser.add_argument("--udid", help="目标设备 UDID（默认第一台 USB 设备）")
    parser.add_argument("--duration", type=float, default=0.0, help="保持秒数；0 表示等待 Ctrl-C（默认 0）")
    parser.add_argument("--timeout", type=float, default=15.0, help="建立会话超时秒数（默认 15）")
    parser.add_argument("--json", action="store_true", help="成功后额外输出一行 JSON")
    args = parser.parse_args()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
