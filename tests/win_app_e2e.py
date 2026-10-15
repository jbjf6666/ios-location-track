"""Windows 应用层真机验收：走 App 的完整代码路径（隧道→注入 rsd→DVT→下发→恢复）。

与 tests/win_e2e.py 的区别：
   win_e2e.py   直接调 pymobiledevice3 + RealLocationProvider，验证平台可行性
   本脚本        走 core/server.py:App 的真实装配路径（TunnelManager + rsd 对象注入），
                 也就是界面点「连接设备」时实际执行的代码。

用法（项目根目录）：
    .venv\\Scripts\\python.exe tests\\win_app_e2e.py --confirm
    .venv\\Scripts\\python.exe tests\\win_app_e2e.py --confirm --hold 8
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import platform as plat  # noqa: E402
from core import server as server_mod  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true", help="确认对真机下发定位")
    parser.add_argument("--lat", type=float, default=39.9087)
    parser.add_argument("--lon", type=float, default=116.3975)
    parser.add_argument("--hold", type=float, default=6.0)
    parser.add_argument("--udid", default=None)
    args = parser.parse_args()

    print("=" * 68)
    print("Windows 应用层真机验收（走 App 装配路径）")
    print("=" * 68)
    print(f"  平台 {plat.platform_label()} | 管理员 {'是' if plat.is_admin() else '否'}")

    data_root = plat.data_dir() / "_e2e"
    data_root.mkdir(parents=True, exist_ok=True)
    app = server_mod.App(root=data_root, mock=False, port=0, resources=ROOT)

    exit_code = 0
    try:
        # ---------- 1. 设备发现 ----------
        app.devices.refresh()
        devices = app.devices.list_devices()
        check("设备发现", len(devices) > 0, f"{len(devices)} 台")
        if not devices:
            return 1
        device = next((d for d in devices if d.udid == args.udid), devices[0])
        check("选中设备", True, f"{device.name} / iOS {device.ios_version} / {device.udid}")

        # ---------- 2. 走 App 的连接路径（含隧道建立 + rsd 注入）----------
        started = time.monotonic()
        try:
            provider = app.provider_for(device, connect=True)
        except Exception as exc:
            check("App 连接设备", False, repr(exc))
            traceback.print_exc()
            return 1
        elapsed = time.monotonic() - started
        snapshot = app.tunnel.snapshot()
        check("App 连接设备（隧道+DVT）", True,
              f"{elapsed:.1f}s | 隧道 mode={snapshot.get('mode')} "
              f"in_process={snapshot.get('in_process')} | 策略={provider._strategy}")
        check("隧道为进程内 userspace",
              snapshot.get("mode") == "userspace" and snapshot.get("in_process") is True,
              f"mode={snapshot.get('mode')} in_process={snapshot.get('in_process')}")
        check("provider 复用了隧道返回的 rsd 对象",
              provider._injected_rsd is not None
              and provider._rsd_obj is provider._injected_rsd,
              "同一个对象，未另行 TCP 连接")

        if not args.confirm:
            print("\n  （未加 --confirm：只验证到会话就绪）")
            return 0

        # ---------- 3. 下发定位（走 playback 用的同一条路径）----------
        app.runner.run(provider.set_location(args.lat, args.lon), timeout=20.0)
        check("下发虚拟定位", True, f"lat={args.lat} lon={args.lon}")
        print(f"  --- 保持 {args.hold:.0f} 秒 ---", flush=True)
        time.sleep(args.hold)

        # ---------- 4. 同会话第二点 ----------
        app.runner.run(provider.set_location(args.lat + 0.0009, args.lon + 0.0009),
                       timeout=20.0)
        check("同一会话连续下发", True, "DVT 长连接有效")

    except Exception as exc:
        check("应用层真机流程", False, repr(exc))
        traceback.print_exc()
        exit_code = 1
    finally:
        try:
            if app.provider is not None:
                app.runner.run(app.provider.clear_location(), timeout=20.0)
                check("恢复真实定位", True, "已调用 clear_location()")
        except Exception as exc:
            check("恢复真实定位", False, repr(exc))
            exit_code = 1
        try:
            app.shutdown()
            check("App 关闭（隧道已释放）", not app.tunnel.ready,
                  f"隧道 state={app.tunnel.state}")
        except Exception as exc:
            check("App 关闭", False, repr(exc))
            exit_code = 1

    print("\n" + "=" * 68)
    failed = [c for c in CHECKS if not c[1]]
    print(f"结果：{len(CHECKS)-len(failed)}/{len(CHECKS)} 项通过")
    for name, _ok, detail in failed:
        print(f"  FAIL {name}: {detail}")
    print("=" * 68)
    return exit_code or (1 if failed else 0)


if __name__ == "__main__":
    raise SystemExit(main())
