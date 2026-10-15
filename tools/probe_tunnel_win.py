"""Windows userspace 隧道可行性验证（对应 docs/WINDOWS_PORT.md 第 3.3 节）。

用法： .venv\\Scripts\\python.exe tools\\probe_tunnel_win.py [--udid X]
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import platform
import sys
import traceback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid", default=None)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    print(f"platform={platform.system()} python={sys.version.split()[0]}")

    # 1) 列出设备
    try:
        from pymobiledevice3.usbmux import list_devices

        devices = asyncio.run(list_devices())
        print(f"usbmux devices: {len(devices)}")
        for d in devices:
            print(f"   {d.serial} {d.product_type if hasattr(d,'product_type') else ''} "
                  f"{getattr(d, 'product_version', '')}")
    except Exception:
        print("usbmux list FAILED")
        traceback.print_exc()

    # 2) 看看有哪些入口
    try:
        from pymobiledevice3.remote import userspace_tunnel as ut

        print("establish_userspace_rsd sig:",
              inspect.signature(ut.establish_userspace_rsd))
    except Exception:
        traceback.print_exc()

    # 3) 真正建隧道
    print("--- establishing userspace RSD tunnel ---", flush=True)
    try:
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

        tunnel = UserspaceRsdTunnel(serial=args.udid, autopair=True,
                                    remotepairing_fallback=True)

        async def run() -> str:
            rsd = await asyncio.wait_for(tunnel.aopen(), timeout=args.timeout)
            address, port = rsd.service.address
            return f"{address} {port}"

        line = asyncio.run(run())
        print(f"TUNNEL OK: {line}", flush=True)
        print("(隧道将在进程退出时释放)")
        return 0
    except Exception:
        print("TUNNEL FAILED:")
        traceback.print_exc()
        return 1
    finally:
        # 必须 await aclose()：UserspaceRsdTunnel.close() 是同步壳，内部调用 aclose()
        # 却不 await，直接调它会留下未 await 的协程且隧道不会真正释放。
        try:
            if tunnel is not None:
                asyncio.run(tunnel.aclose())
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
