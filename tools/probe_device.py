#!/usr/bin/env python3
"""列出 iOS 设备并打印 iOS 版本 / DVT 虚拟定位可用性。

用法（项目根目录下执行）：

    ./.venv/bin/python tools/probe_device.py
    ./.venv/bin/python tools/probe_device.py --rsd 10.7.0.1 51234
    ./.venv/bin/python tools/probe_device.py --json

说明：
* 没有连接设备时打印中文提示并以退出码 2 结束，不会抛栈。
* ``--rsd HOST PORT`` 时会对 iOS 17+ 设备真正建立一次 RSD+DVT 会话做连通性验证
  （不设置任何位置，验证后立即断开）。
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
    pymobiledevice3_version,
    pymobiledevice3_available,
)

#: iOS 17 起 DVT 服务只能通过 RSD 隧道访问
RSD_REQUIRED_IOS_MAJOR = 17


def _print(text: str = "") -> None:
    print(text, flush=True)


async def _enumerate_devices(udid: str | None) -> list[dict]:
    """优先用 lead 的 core/device.py，不可用时退回直接查 usbmux。"""
    devices: list[dict] = []
    try:  # lead 的 DeviceManager（可能尚未实现或签名不同，失败则退化）
        from core.device import DeviceManager  # type: ignore

        for factory in (lambda: DeviceManager(), lambda: DeviceManager(mock=False)):
            try:
                manager = factory()
                raw = manager.list_devices()
                if asyncio.iscoroutine(raw):
                    raw = await raw
                for item in raw or []:
                    devices.append(dict(item))
                if devices:
                    return devices
            except Exception:
                continue
    except Exception:
        pass

    # 退化路径：直接用 pymobiledevice3 的 usbmux
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux

    for mux_device in await usbmux.list_devices():
        if udid and not mux_device.matches_udid(udid):
            continue
        entry: dict = {
            "udid": mux_device.serial,
            "connection": (mux_device.connection_type or "").lower(),
            "name": None,
            "product_type": None,
            "ios_version": None,
            "ios_major": 0,
            "developer_mode": None,
            "state": "connected",
            "error": None,
        }
        try:
            lockdown = await create_using_usbmux(serial=mux_device.serial, autopair=False)
        except Exception as exc:
            entry["state"] = "unpaired"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            devices.append(entry)
            continue
        try:
            entry["name"] = lockdown.all_values.get("DeviceName")
            entry["product_type"] = lockdown.all_values.get("ProductType")
            version = str(lockdown.product_version or "")
            entry["ios_version"] = version
            try:
                entry["ios_major"] = int(version.split(".", 1)[0])
            except (ValueError, IndexError):
                entry["ios_major"] = 0
            try:
                entry["developer_mode"] = await lockdown.get_developer_mode_status()
            except Exception:
                entry["developer_mode"] = None
        finally:
            try:
                await lockdown.close()
            except Exception:
                pass
        devices.append(entry)
    return devices


def _judge(device: dict, rsd: tuple | None) -> tuple[bool, str]:
    """返回 (DVT 是否可能可用, 中文说明)。"""
    major = int(device.get("ios_major") or 0)
    state = device.get("state")
    if state == "unpaired":
        return False, "未配对：请在 iPhone 上点击「信任此电脑」后重试"
    if device.get("developer_mode") is False:
        return False, "未开启开发者模式：设置 → 隐私与安全性 → 开发者模式（iOS16+ 必须开启）"
    if major >= RSD_REQUIRED_IOS_MAJOR:
        if rsd:
            return True, f"iOS {major} ≥ 17：需要 RSD 隧道，已提供 {rsd[0]}:{rsd[1]}（待实测）"
        return False, (
            f"iOS {major} ≥ 17：必须提供 RSD 隧道（--rsd HOST PORT）。"
            "免 root：pymobiledevice3 remote start-tunnel --native --script-mode；"
            "需 root：sudo pymobiledevice3 remote start-tunnel --script-mode"
        )
    if major == 0:
        return False, "无法识别 iOS 版本：设备可能未解锁或未配对"
    return True, f"iOS {major} < 17：可经 usbmux/lockdown 直连 DVT（或老版 simulatelocation 服务）"


async def _probe_dvt(device: dict, rsd: tuple | None) -> dict:
    """真正建立一次会话验证 DVT 是否可用（不设置位置）。"""
    provider = RealLocationProvider(
        rsd_host=rsd[0] if rsd else None,
        rsd_port=rsd[1] if rsd else None,
        udid=device.get("udid"),
    )
    try:
        info = await provider.connect(device)
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
    try:
        await provider.close()
    except Exception:
        pass
    return {"ok": True, "detail": info.get("detail", "连接成功"), "strategy": info.get("strategy")}


async def _main_async(args: argparse.Namespace) -> int:
    rsd = (args.rsd[0], int(args.rsd[1])) if args.rsd else None

    if not pymobiledevice3_available():
        _print(f"[错误] {PMD3_MISSING_HINT}")
        return 1
    _print(f"pymobiledevice3 版本：{pymobiledevice3_version()}")
    _print(f"Python：{sys.version.split()[0]}")
    if rsd:
        _print(f"RSD 隧道：{rsd[0]}:{rsd[1]}")
    _print("")

    try:
        devices = await _enumerate_devices(args.udid)
    except Exception as exc:
        _print(f"[错误] 枚举设备失败：{type(exc).__name__}: {exc}")
        return 1

    if not devices:
        _print("未发现 iOS 设备。请：")
        _print("  1) 用 USB 数据线连接 iPhone（不是仅充电线）；")
        _print("  2) 解锁手机并在弹窗中点击「信任此电脑」；")
        _print("  3) 若使用 Wi-Fi 连接，请确认与电脑在同一网络。")
        return 2

    results = []
    for device in devices:
        available, reason = _judge(device, rsd)
        entry = {"device": device, "available": available, "reason": reason, "probe": None}
        if args.probe and available:
            entry["probe"] = await _probe_dvt(device, rsd)
            entry["available"] = bool(entry["probe"].get("ok"))
        results.append(entry)

    if args.json:
        _print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    for index, entry in enumerate(results, 1):
        device = entry["device"]
        _print(f"=== 设备 {index} ===")
        _print(f"  UDID        : {device.get('udid')}")
        _print(f"  名称        : {device.get('name') or '(未知)'}")
        _print(f"  型号        : {device.get('product_type') or '(未知)'}")
        _print(f"  iOS 版本    : {device.get('ios_version') or '(未知)'} (major={device.get('ios_major')})")
        _print(f"  连接方式    : {device.get('connection') or '(未知)'}")
        _print(f"  配对状态    : {device.get('state')}")
        developer_mode = device.get("developer_mode")
        _print(f"  开发者模式  : {'已开启' if developer_mode else ('未开启' if developer_mode is False else '未知')}")
        if device.get("error"):
            _print(f"  错误        : {device['error']}")
        _print(f"  DVT 可用性  : {'可能可用' if entry['available'] else '不可用'} —— {entry['reason']}")
        if entry["probe"]:
            probe = entry["probe"]
            _print(f"  实测连接    : {'成功' if probe.get('ok') else '失败'} —— {probe.get('detail')}")
        _print("")

    _print("提示：iOS 17+ 必须先建立 RSD 隧道；不带 --rsd 时只做静态判断。")
    _print("      加 --probe --rsd HOST PORT 可实测建立一次 DVT 会话。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="列出 iOS 设备并检查 DVT 虚拟定位可用性")
    parser.add_argument("--rsd", nargs=2, metavar=("HOST", "PORT"), help="RSD 隧道地址（iOS 17+ 必需）")
    parser.add_argument("--udid", help="只检查指定 UDID")
    parser.add_argument("--probe", action="store_true", help="真正建立一次 DVT 会话做连通性验证")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
