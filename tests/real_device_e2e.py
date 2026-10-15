#!/usr/bin/env python3
"""真机端到端验证：连接 iOS 设备 → 建立隧道 → 沿轨迹播放虚拟定位 → 停止恢复真实定位。

这是**会真的改变设备定位**的测试（结束后会 clear_location 恢复真实定位）。
只有在你明确同意时才运行：

    ./.venv/bin/python tests/real_device_e2e.py --confirm
    ./.venv/bin/python tests/real_device_e2e.py --confirm --udid 00008030-...
    ./.venv/bin/python tests/real_device_e2e.py --confirm --duration 6 --speed 20
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core import protocol, server as server_mod  # noqa: E402
from selftest import WSClient  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        print(f"  \033[31m✗\033[0m {name} {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true", help="确认会真的改变设备定位")
    parser.add_argument("--udid", default=None, help="指定设备 UDID（默认用发现的唯一 USB 设备）")
    parser.add_argument("--port", type=int, default=8811)
    parser.add_argument("--duration", type=float, default=6.0, help="播放时长（秒）")
    parser.add_argument("--speed", type=float, default=20.0, help="播放倍速")
    parser.add_argument("--keep", action="store_true", help="结束后不恢复真实定位（默认会恢复）")
    args = parser.parse_args()

    if not args.confirm:
        print("这是真机测试，会临时改变 iPhone 的定位。请加 --confirm 运行。")
        return 2

    print("\033[1miOS 虚拟轨迹 — 真机端到端验证\033[0m")
    app = server_mod.App(root=ROOT, mock=False, port=args.port)
    devices = app.devices.refresh()
    if not devices:
        print("  \033[31m✗\033[0m 未发现设备")
        return 1
    print("  发现的设备：")
    for d in devices:
        print(f"    - {d.name} | iOS {d.ios_version} | {d.connection} | 开发者模式={d.developer_mode} | {d.udid}")

    target = None
    for d in devices:
        if args.udid and d.udid != args.udid:
            continue
        if not args.udid and d.connection != "usb":
            continue
        target = d
        break
    target = target or devices[0]
    print(f"  目标设备：{target.name}（iOS {target.ios_version}）\n")
    app.devices.select(target.udid)

    httpd = server_mod.serve(app, host="127.0.0.1", port=args.port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)

    client = WSClient("127.0.0.1", args.port, timeout=120)
    exit_code = 0
    try:
        client.send({"type": "hello", "id": "h1", "client": "e2e"})
        ack = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "h1", timeout=20)
        check("连接服务端", ack is not None)

        print("\n  → 建立设备会话（iOS 17+ 会自动建 RSD 隧道，最长约 75 秒）…")
        started = time.time()
        client.send({"type": "device.connect", "id": "c1"})
        connected = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "c1", timeout=180)
        took = time.time() - started
        ok = bool(connected and connected.get("ok", True))
        check(f"设备连接成功（{took:.1f}s）", ok, str(connected)[:400])
        if not ok:
            print(f"    错误：{connected}")
            return 1
        probe = connected.get("probe") or {}
        check("DVT 自检通过", bool(probe.get("ok")), str(probe)[:300])
        print(f"    自检详情：{probe.get('detail')}")

        print("\n  → 单点定位测试…")
        client.send({"type": "location.set", "id": "l1", "lat": 39.9088, "lng": 116.3975})
        ack = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "l1", timeout=30)
        check("单点定位下发", bool(ack and ack.get("ok", True)), str(ack)[:300])
        location = client.wait_for(lambda m: m.get("type") == "location" and m.get("source") == "manual", timeout=15)
        check("收到单点 location 事件", location is not None, str(location)[:200])

        print(f"\n  → 轨迹播放测试（{args.duration:.0f} 秒，倍速 {args.speed}x）…")
        route = app.routes.build(
            [[116.3975, 39.9088], [116.4050, 39.9120], [116.4120, 39.9080]],
            mode="driving", use_osrm=False,
        )
        print(f"    轨迹：{route.distance / 1000:.2f} 公里 / {len(route.path)} 点 / 预计 {route.duration:.0f} 秒")
        client.send({"type": "play.start", "id": "s1", "route": route.to_json(),
                     "speed_scale": args.speed, "loop": False})
        ack = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "s1", timeout=30)
        check("播放启动", bool(ack and ack.get("ok", True)), str(ack)[:300])

        first = client.wait_for(lambda m: m.get("type") == "location" and m.get("source") == "play", timeout=20)
        check("播放下发首个定位", first is not None, str(first)[:200])
        progressed = client.wait_for(lambda m: m.get("type") == "location" and m.get("progress", 0) > 0.1, timeout=30)
        check("定位沿轨迹推进", progressed is not None, str(progressed)[:200])
        if progressed:
            print(f"    当前进度 {progressed.get('progress'):.1%} → {progressed.get('lat')}, {progressed.get('lng')}")

        sent_before = None
        with client.lock:
            sent_before = len([m for m in client.messages if m.get("type") == "location" and m.get("source") == "play"])
        time.sleep(args.duration)
        with client.lock:
            sent_after = len([m for m in client.messages if m.get("type") == "location" and m.get("source") == "play"])
        expect = args.duration * float(app.config.get("send_hz") or 5) * 0.5
        check(f"持续下发定位（{sent_after - sent_before} 次 / {args.duration:.0f}s）",
              (sent_after - sent_before) >= expect, f"期望 ≥{expect:.0f} 次")

        client.send({"type": "play.pause", "id": "p1"})
        paused = client.wait_for(lambda m: m.get("type") == "status" and m.get("paused") is True, timeout=15)
        check("暂停", paused is not None, str(paused)[:200])
        client.send({"type": "play.resume", "id": "r1"})
        resumed = client.wait_for(lambda m: m.get("type") == "status" and m.get("paused") is False and m.get("playing") is True, timeout=15)
        check("继续", resumed is not None, str(resumed)[:200])

        if not args.keep:
            print("\n  → 停止并恢复真实定位…")
            client.send({"type": "play.stop", "id": "st1"})
            stopped = client.wait_for(lambda m: m.get("type") == "status" and m.get("playing") is False, timeout=25)
            check("停止播放", stopped is not None, str(stopped)[:200])
            cleared = client.wait_for(lambda m: m.get("type") == "location" and m.get("source") == "cleared", timeout=20)
            check("已恢复设备真实定位", cleared is not None, str(cleared)[:200])
    except Exception as exc:
        print(f"  \033[31m✗\033[0m 异常：{exc.__class__.__name__}: {exc}")
        import traceback

        traceback.print_exc()
        FAIL += 1
    finally:
        try:
            if not args.keep:
                app.playback.stop(notify=False, clear_provider=True)
        except Exception:
            pass
        client.close()
        app.shutdown()
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass

    print(f"\n{'=' * 50}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
