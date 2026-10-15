#!/usr/bin/env python3
"""校园跑类 App 定位诊断：检查我们下发给手机的定位点是否"对 App 有效"。

运动世界校园这类 App 不显示里程，常见原因不在"定位有没有送到"，而在
"送到的点是否满足 App 的过滤条件"。本脚本按跑步 App 的典型过滤规则体检：

1. **速度是否合理**：配速过高（如用了驾车/飞行模式）会被判异常而不计里程；
2. **每帧位移是否够大**：位移太小会被"静止过滤"掉；
3. **下发频率**：运动类 App 通常按 1Hz 采样算里程，低于 1Hz 会丢点；
4. **坐标精度**：6 位小数（约 0.1 米）足够；
5. **轨迹是否在指定区域内**（校园跑要求在校内有效区域，这一点需要你提供学校坐标）。

用法：
    ./.venv/bin/python tools/diagnose_run.py                      # 只做参数体检
    ./.venv/bin/python tools/diagnose_run.py --live 20            # 连真机跑 20 秒并统计真实下发
    ./.venv/bin/python tools/diagnose_run.py --live 20 --lat 30.2741 --lng 120.1551
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core import geoutils, server as server_mod  # noqa: E402

# 跑步 App 的常见阈值（不同学校/版本略有差异，这里取保守值）
APP_MIN_SPEED = 0.4        # m/s，低于此速度通常判为"站着不动"
APP_MAX_SPEED_WALK = 4.0   # m/s，约 14.4 km/h；超过常被判"非跑步/作弊"
APP_MIN_PER_FRAME = 0.05   # 米，位移小于此值容易被静止过滤
APP_MAX_PER_FRAME = 300.0  # 米，位移过大（跳点）会被判异常
APP_MIN_HZ = 1.0           # App 侧通常按 ≥1Hz 采样


def banner(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


def check_parameters(hz: float, mode_speed: float, drift_m: float, jitter: float) -> int:
    problems = []
    banner("1) 参数体检（按下发频率与配速推算）")
    per_frame = mode_speed / hz if hz else 0
    print(f"  下发频率      : {hz:g} Hz → 每帧 {1000 / hz:.0f} ms" if hz else "  下发频率: 非法")
    print(f"  基准速度      : {mode_speed:.2f} m/s（{mode_speed * 3.6:.1f} km/h）")
    print(f"  每帧位移      : {per_frame:.3f} 米")
    print(f"  漂移半径      : {drift_m:g} 米 | 速度浮动: ±{jitter * 100:g}%")

    if hz < APP_MIN_HZ:
        problems.append(f"下发频率 {hz:g}Hz 低于 {APP_MIN_HZ:g}Hz，App 可能采样不到足够的点")
    if mode_speed < APP_MIN_SPEED:
        problems.append(f"速度 {mode_speed:.2f} m/s 过慢，可能被判为静止")
    if mode_speed > APP_MAX_SPEED_WALK:
        problems.append(
            f"速度 {mode_speed:.2f} m/s（{mode_speed * 3.6:.1f} km/h）对跑步来说过快，"
            f"运动类 App 常因此判为异常并**不记录里程**（建议 ≤{APP_MAX_SPEED_WALK * 3.6:.0f} km/h）"
        )
    if per_frame < APP_MIN_PER_FRAME:
        problems.append(f"每帧位移仅 {per_frame:.3f} 米，可能被静止过滤（建议提高下发频率或速度）")
    if per_frame > APP_MAX_PER_FRAME:
        problems.append(f"每帧位移 {per_frame:.0f} 米过大，会被判为跳点")
    if drift_m > 15:
        problems.append(f"漂移半径 {drift_m:g} 米偏大，可能触发精度过滤（建议 4~8 米）")

    if problems:
        print("\n  \033[33m⚠ 发现问题：\033[0m")
        for item in problems:
            print(f"    - {item}")
    else:
        print("\n  \033[32m✓ 参数组合合理\033[0m")
    return len(problems)


def check_area(lat: float | None, lng: float | None) -> None:
    banner("2) 区域检查（校园跑的关键！）")
    if lat is None or lng is None:
        print("  未提供学校坐标。运动世界校园只在**校内有效区域**内计里程，")
        print("  请用 --lat/--lng 指定你学校的位置，或直接在界面上用「快速定位到坐标」跳过去画轨迹。")
        return
    print(f"  目标坐标: {lat:.6f}, {lng:.6f}")
    print("  ⚠ 请确认这个坐标落在你学校的有效跑步区域内（该 App 会校验）。")
    print("    获取方式：手机上打开地图 App，长按学校操场位置，即可看到经纬度。")


def live_test(seconds: float, hz: float, mode: str, lat: float | None, lng: float | None) -> int:
    banner(f"3) 真机实测：连续下发 {seconds:.0f} 秒并统计")
    port = 8810
    app = server_mod.App(root=ROOT, mock=False, port=port)
    devices = app.devices.refresh()
    if not devices:
        print("  \033[31m✗ 未发现设备\033[0m")
        return 1
    device = app.devices.get_selected()
    print(f"  设备: {device.name} | iOS {device.ios_version} | {device.connection}")

    from selftest import WSClient  # 延迟导入，避免非真机场景的依赖

    httpd = server_mod.serve(app, host="127.0.0.1", port=port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    client = WSClient("127.0.0.1", port, timeout=180)
    try:
        client.send({"type": "hello", "id": "h1"})
        client.wait_for(lambda m: m.get("id") == "h1", timeout=20)
        client.send({"type": "device.connect", "id": "c1"})
        ack = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "c1", timeout=180)
        if not (ack and ack.get("ok", True)):
            print(f"  \033[31m✗ 连接设备失败：{ack}\033[0m")
            return 1
        print("  设备已连接（DVT 会话就绪）")

        if lat is None or lng is None:
            print("  未提供坐标，跳过实测（用 --lat/--lng 指定学校位置）")
            return 0
        # 以给定坐标为中心，造一条 1 公里左右的环形轨迹
        d = 0.0045  # 约 500 米
        waypoints = [
            [lng - d, lat - d], [lng + d, lat - d],
            [lng + d, lat + d], [lng - d, lat + d],
            [lng - d, lat - d],
        ]
        route = app.routes.build(waypoints, mode=mode, use_osrm=False)
        print(f"  轨迹: {route.distance:.0f} 米（{mode}，基准 {route.speed_at_distance(0):.2f} m/s）")
        client.send({"type": "play.start", "id": "s1", "route": route.to_json(),
                     "speed_scale": 1.0,
                     "tuning": {"drift_enabled": True, "drift_m": 4,
                                "speed_jitter_enabled": True, "speed_jitter": 0.08}})
        started = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "s1", timeout=30)
        if not (started and started.get("ok", True)):
            print(f"  \033[31m✗ 播放启动失败：{started}\033[0m")
            return 1
        print(f"  采集中…（{seconds:.0f} 秒）")
        time.sleep(seconds)
        with client.lock:
            samples = [m for m in client.messages if m.get("type") == "location" and m.get("source") == "play"]
        client.send({"type": "play.stop", "id": "st1"})
        client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "st1", timeout=20)

        if len(samples) < 3:
            print(f"  \033[31m✗ 只收到 {len(samples)} 个样本，定位可能没下发\033[0m")
            return 1
        gaps = [samples[i]["sent_at"] - samples[i - 1]["sent_at"] for i in range(1, len(samples))]
        hops = [
            geoutils.haversine(samples[i - 1]["lat"], samples[i - 1]["lng"],
                               samples[i]["lat"], samples[i]["lng"])
            for i in range(1, len(samples))
        ]
        speeds = [s["speed"] for s in samples if s.get("speed")]
        real_hz = 1.0 / statistics.mean(gaps) if gaps else 0
        total = sum(hops)
        print(f"\n  实际下发频率: {real_hz:.2f} Hz（{statistics.mean(gaps) * 1000:.0f} ms/点）")
        print(f"  每帧位移    : 平均 {statistics.mean(hops):.3f} 米（最小 {min(hops):.3f}）")
        print(f"  累计位移    : {total:.1f} 米 / {seconds:.0f} 秒")
        print(f"  上报速度    : 平均 {statistics.mean(speeds):.2f} m/s（{statistics.mean(speeds) * 3.6:.1f} km/h）")

        verdict = []
        if real_hz < APP_MIN_HZ:
            verdict.append(f"下发频率 {real_hz:.2f}Hz 偏低")
        if min(hops) < APP_MIN_PER_FRAME:
            verdict.append(f"存在 {min(hops):.3f} 米的极小位移（可能被静止过滤）")
        if max(hops) > APP_MAX_PER_FRAME:
            verdict.append(f"存在 {max(hops):.0f} 米的跳点")
        if statistics.mean(speeds) > APP_MAX_SPEED_WALK:
            verdict.append(f"速度 {statistics.mean(speeds) * 3.6:.1f} km/h 对跑步偏快")
        print()
        if verdict:
            print("  \033[33m⚠ 下发的点可能被 App 过滤：\033[0m")
            for item in verdict:
                print(f"    - {item}")
        else:
            print("  \033[32m✓ 下发的点在频率、位移、速度上都符合跑步 App 的接受范围\033[0m")
        print("\n  请同时在手机上确认：")
        print("   1. 该 App 的定位权限是「使用 App 期间」或「始终」，并且「精确位置」已打开；")
        print("   2. 手机上打开系统地图，蓝点应沿着你画的轨迹移动（验证定位确实生效）；")
        print("   3. 轨迹必须覆盖该 App 要求的必打点/必经点，否则里程不计入成绩。")
        return 1 if verdict else 0
    finally:
        client.close()
        app.shutdown()
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", type=float, default=0, help="连真机实测多少秒（0=不实测）")
    parser.add_argument("--mode", default="walking", choices=["walking", "cycling", "driving"])
    parser.add_argument("--lat", type=float, default=None, help="学校纬度（WGS84）")
    parser.add_argument("--lng", type=float, default=None, help="学校经度（WGS84）")
    args = parser.parse_args()

    print("\033[1m校园跑类 App 定位诊断\033[0m")
    from core.config_store import ConfigStore

    cfg = ConfigStore(ROOT / "config.json")
    hz = float(cfg.get("send_hz") or 5)
    mode_speed = float((cfg.get("mode_defaults") or {}).get(args.mode, 1.4))
    drift_m = float(cfg.get("drift_m") or 0)
    jitter = float(cfg.get("speed_jitter") or 0)

    problems = check_parameters(hz, mode_speed, drift_m, jitter)
    check_area(args.lat, args.lng)
    live_problems = 0
    if args.live > 0:
        live_problems = live_test(args.live, hz, args.mode, args.lat, args.lng)
    else:
        banner("3) 真机实测")
        print("  跳过（加 --live 20 可连真机实测 20 秒）")

    banner("结论")
    if problems or live_problems:
        print("  参数或下发质量存在问题，见上面 ⚠ 项。")
        return 1
    print("  定位下发本身没问题。如果 App 仍不显示里程，问题在 App 侧：")
    print("   · 是否在**校内有效区域**内跑（校园跑的核心规则）")
    print("   · 是否跑过该次要求的**必打点 / 必经点**")
    print("   · 有效里程是否达到最低门槛（很多学校要求单次 ≥1~2 公里）")
    print("   · App 是否要求前台运行 + 屏幕常亮 + 后台定位权限")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
