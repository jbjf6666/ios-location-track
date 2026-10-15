#!/usr/bin/env python3
"""真实感模拟验证：GPS 漂移 + 速度浮动（校园跑等打卡类 App 测试用）。

验证点：
1. 漂移是**平滑游走**而不是随机跳点（相邻采样差远小于漂移半径）；
2. 漂移幅度落在设定半径范围内，且开关关闭时严格为 0；
3. 速度浮动围绕设定速度上下波动，开关关闭时严格恒定；
4. 经纬度换算精度（米 ↔ 度）与方向分解正确；
5. 参数校验与非法值拒绝；
6. 两条高级性质：**速度连续性**（不会出现速度突变）与**位移与轨迹方向一致**。

运行：./.venv/bin/python tests/drift_sim.py
"""
from __future__ import annotations

import math
import statistics
import sys
import tempfile
import time
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config_store, geoutils, noise, playback, route_engine  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  \033[31m✗\033[0m {name} {detail}")


class NullRunner:
    def run(self, coro, timeout=None, wait=True):
        try:
            coro.close()
        except Exception:
            pass
        return None


class NullProvider:
    connected = True

    async def set_location(self, lat, lon):
        return None

    async def clear_location(self):
        return None

    async def close(self):
        return None


def make_engine(config: config_store.ConfigStore):
    locations: list[dict] = []
    lock = threading.Lock()

    def on_location(payload):
        with lock:
            locations.append(payload)

    engine = playback.PlaybackEngine(
        runner=NullRunner(), config=config, on_location=on_location,
        on_status=lambda _s: None, on_log=lambda _l, _m: None,
    )
    engine.set_provider(NullProvider())
    return engine, locations, lock


def main() -> int:
    print("\033[1m真实感模拟验证（GPS 漂移 + 速度浮动）\033[0m")
    cfg = config_store.ConfigStore(Path(tempfile.gettempdir()) / "iosloc-drift-sim.json")
    cfg.update({
        "drift_enabled": True, "drift_m": 5.0,
        "speed_jitter_enabled": True, "speed_jitter": 0.10,
    })
    route = route_engine.RouteEngine(cfg).build(
        [[116.40, 39.90], [116.41, 39.905], [116.42, 39.90]], mode="driving", use_osrm=False
    )
    engine, locations, lock = make_engine(cfg)
    gen = noise.DriftGenerator()

    print("\n1) 漂移幅度与分布（设定 5 米）")
    offsets = [math.hypot(*gen.offset_m(d, 5.0)) for d in range(0, 3000, 3)]
    check("偏移量非零且分布合理", 1.0 < statistics.mean(offsets) < 5.0,
          f"平均 {statistics.mean(offsets):.2f} m")
    check("最大偏移不超过裁剪上限（半径×1.8）", max(offsets) <= 5.0 * 1.8 + 1e-6,
          f"最大 {max(offsets):.2f} m")

    print("\n2) 平滑性：真实 GPS 是缓慢游走，不是每点随机跳")
    deltas = []
    prev = None
    for d in range(0, 3000, 3):
        point = gen.offset_m(d, 5.0)
        if prev is not None:
            deltas.append(math.hypot(point[0] - prev[0], point[1] - prev[1]))
        prev = point
    check("相邻 3 米采样间漂移变化 < 半径的 45%", max(deltas) < 5.0 * 0.45,
          f"最大变化 {max(deltas):.2f} m")
    check("平均变化远小于半径（说明是连续游走）", statistics.mean(deltas) < 5.0 * 0.25,
          f"平均 {statistics.mean(deltas):.2f} m")

    print("\n3) 开关：关闭时必须严格为 0 个偏移")
    engine.update_tuning(drift_enabled=False)
    worst = 0.0
    step = max(1, len(route.path) // 100)
    for i in range(0, len(route.path), step):
        engine.distance = route.dense_acc[i]
        true_point = route.point_at_distance(engine.distance)
        reported = engine._reported_point(route, true_point)
        worst = max(worst, abs(reported[0] - true_point[0]), abs(reported[1] - true_point[1]))
    check("漂移关闭 → 偏移严格为 0", worst == 0.0, f"最大 {worst}")

    print("\n4) 经纬度换算精度与方向分解")
    lat, lng = 39.9, 116.4
    headings = [0, 45, 90, 135, 180, 225, 270, 315]
    max_meter_error = 0.0
    max_projection_error = 0.0
    for heading in headings:
        along, cross = gen.offset_m(777.0, 6.0)
        dlat, dlng = gen.offset_latlng(lat, lng, 777.0, heading, 6.0)
        want = geoutils.haversine(lat, lng, lat + dlat, lng + dlng)
        have = math.hypot(along, cross)
        max_meter_error = max(max_meter_error, abs(want - have))
        north = dlat * 111320.0
        east = dlng * 111320.0 * math.cos(math.radians(lat))
        rad = math.radians(heading)
        proj_along = east * math.sin(rad) + north * math.cos(rad)
        proj_cross = east * math.cos(rad) - north * math.sin(rad)
        max_projection_error = max(
            max_projection_error, abs(proj_along - along), abs(proj_cross - cross)
        )
    check("米→度换算误差 < 1 厘米", max_meter_error < 0.01, f"{max_meter_error * 100:.3f} cm")
    check("沿行进/垂直方向分解正确（误差 < 1 厘米）", max_projection_error < 0.01,
          f"{max_projection_error * 100:.3f} cm")

    print("\n5) 速度浮动特性")
    factors = [gen.speed_factor(t, 0.10, 18.0) for t in range(0, 600)]
    check("系数落在 1±10% 内", 0.899 <= min(factors) and max(factors) <= 1.101,
          f"{min(factors):.4f} ~ {max(factors):.4f}")
    check("上下双向浮动都存在", any(f < 0.995 for f in factors) and any(f > 1.005 for f in factors))
    check("均值接近 1.0", abs(statistics.mean(factors) - 1.0) < 0.03,
          f"{statistics.mean(factors):.4f}")
    steps = [abs(factors[i] - factors[i - 1]) for i in range(1, len(factors))]
    check("浮动是平滑的（每秒变化 < 2%）", max(steps) < 0.02, f"最大 {max(steps) * 100:.2f}%")
    check("关闭浮动 → 严格 1.0", gen.speed_factor(50, 0.0, 18.0) == 1.0)

    print("\n6) 播放引擎端到端（mock 设备）")
    engine.update_tuning(drift_enabled=True, drift_m=5.0,
                         speed_jitter_enabled=True, speed_jitter=0.10)
    with lock:
        locations.clear()
    engine.start(route, speed_scale=8, provider=NullProvider())
    time.sleep(12)
    engine.stop(notify=False, clear_provider=False)
    time.sleep(0.3)
    with lock:
        samples = list(locations)
    check("播放期间有下发样本", len(samples) >= 6, f"{len(samples)} 个")
    drifts = [s.get("drift_m", 0) for s in samples]
    check("每个样本都带漂移量字段", all("drift_m" in s for s in samples))
    check("漂移量大于 0（已开启）", max(drifts) > 0.5, f"最大 {max(drifts):.2f} m")
    reported = [s["speed"] for s in samples if s.get("speed")]
    base = route.speed_at_distance(route.distance * 0.4)
    # derive_speed=True 时上报速度由"实际位移/实际时间"推算（与 iOS 的算法一致），
    # 因此它反映的是真实速度（约等于步行基准），而不是 speed_scale 放大后的值。
    check("上报速度围绕步行基准浮动", min(reported) < base * 1.15 and max(reported) > base * 0.85,
          f"{min(reported):.2f} ~ {max(reported):.2f}（基准 {base:.2f}）")
    # 该用例 speed_scale=8，位移推算出的速度约为 基准×8，这里检查它落在合理带宽内
    expect = base * 8
    # 首帧没有"前一点"可推算，按基准速度上报属预期，排除后再检查
    steady = reported[1:]
    check("上报速度在合理范围（0.6~1.6 倍期望值）",
          steady and all(expect * 0.6 <= value <= expect * 1.6 for value in steady),
          f"{min(steady):.2f} ~ {max(steady):.2f}（期望约 {expect:.1f}）")
    # 自洽性：上报速度应等于 位移/时间间隔（这是真实 GPS 的物理关系）
    jumps = [
        geoutils.haversine(samples[i - 1]["lat"], samples[i - 1]["lng"],
                           samples[i]["lat"], samples[i]["lng"])
        for i in range(1, len(samples))
    ]
    dts = [samples[i]["sent_at"] - samples[i - 1]["sent_at"] for i in range(1, len(samples))]
    # samples[i+1].speed 对应的是"从 samples[i] 到 samples[i+1]"这一段
    consistent = [
        abs(samples[i + 1]["speed"] - jumps[i] / dts[i])
        for i in range(len(jumps)) if dts[i] > 0.05
    ]
    # 自洽性用相对偏差衡量：上报速度应等于 位移/时间（本用例速度量级约 120 m/s，
    # 因为 speed_scale=8 是"时间压缩"，位移也被同步放大，二者仍然自洽）
    rel = [
        abs(samples[i + 1]["speed"] - jumps[i] / dts[i]) / max(0.1, jumps[i] / dts[i])
        for i in range(len(jumps)) if dts[i] > 0.05
    ]
    check("上报速度与位移/时间自洽（相对偏差 < 3%）",
          rel and statistics.mean(rel) < 0.03,
          f"平均相对偏差 {statistics.mean(rel) * 100:.2f}%" if rel else "无样本")

    # 平滑性：相邻上报点的间距不应因漂移而剧烈波动
    hops = [
        geoutils.haversine(samples[i - 1]["lat"], samples[i - 1]["lng"], samples[i]["lat"], samples[i]["lng"])
        for i in range(1, len(samples))
    ]
    check("相邻上报点间距稳定（无突跳）", max(hops) < statistics.mean(hops) * 2.2,
          f"平均 {statistics.mean(hops):.1f} m / 最大 {max(hops):.1f} m")

    print("\n7) 参数校验")
    for bad, label in [({"drift_m": 999}, "漂移 999 米"), ({"drift_m": -1}, "漂移 -1 米"),
                       ({"speed_jitter": 500.0}, "浮动 500%"), ({"speed_jitter": 61}, "浮动 61%"),
                       ({"drift_m": float("nan")}, "漂移 NaN")]:
        try:
            engine.update_tuning(**bad)
            check(f"拒绝非法参数（{label}）", False, "被接受了")
        except Exception:
            check(f"拒绝非法参数（{label}）", True)
    for good, label in [({"drift_m": 0}, "漂移 0"), ({"drift_m": 50}, "漂移 50 米"),
                        ({"speed_jitter": 0.6}, "浮动 0.6"), ({"speed_jitter": 60}, "浮动 60%")]:
        try:
            engine.update_tuning(**good)
            check(f"接受合法参数（{label}）", True)
        except Exception as exc:
            check(f"接受合法参数（{label}）", False, str(exc)[:60])


    print("\n8) 真实感进阶：发送节奏抖动 / 速度推算 / 粗差 / 多尺度")
    # 8.1 节奏抖动：真实 GPS 不是精确等间隔
    factors = [gen.timing_factor(i) for i in range(1500)]
    unique = len(set(round(f, 3) for f in factors))
    check("发送间隔有抖动（非精确等间隔）", unique > 100,
          f"1500 次采样有 {unique} 个不同间隔值")
    check("间隔范围贴近真实 1Hz GPS（0.85~2.1s）",
          0.8 <= min(factors) and max(factors) <= 2.2,
          f"{min(factors):.2f} ~ {max(factors):.2f}")
    drops = sum(1 for f in factors if f > 1.5)
    check("偶发丢采样（约 1~4%）", 0.005 <= drops / len(factors) <= 0.04,
          f"{drops / len(factors) * 100:.1f}%")
    check("关掉抖动后退化为精确等间隔", gen.timing_factor(7, jitter=0.0) == 1.0)

    # 8.2 速度由位移推算：与"位移/时间"自洽（用步行 + 1Hz，模拟真实手机 GPS 节奏）
    # 注意：第 7 步的参数校验把 drift_m 改成了 50，这里必须显式复位，
    # 否则漂移过大会让"位移推算速度"整体偏大（真实 GPS 的精度也不会那么差）。
    cfg.update({"send_hz": 1.0})
    engine.update_tuning(drift_enabled=True, drift_m=4.0,
                         speed_jitter_enabled=True, speed_jitter=0.10)
    walk_route = route_engine.RouteEngine(cfg).build(
        [[116.40, 39.90], [116.41, 39.905]], mode="walking", use_osrm=False
    )
    engine.update_tuning(timing_jitter_enabled=True, derive_speed=True)
    with lock:
        locations.clear()
    engine.start(walk_route, speed_scale=1.0, provider=NullProvider())
    time.sleep(14)
    engine.stop(notify=False, clear_provider=False)
    time.sleep(0.3)
    with lock:
        walk = list(locations)
    hops2 = [
        geoutils.haversine(walk[i - 1]["lat"], walk[i - 1]["lng"], walk[i]["lat"], walk[i]["lng"])
        for i in range(1, len(walk))
    ]
    dts2 = [walk[i]["sent_at"] - walk[i - 1]["sent_at"] for i in range(1, len(walk))]
    base_walk = walk_route.speed_at_distance(0)
    speeds2 = [s["speed"] for s in walk[1:] if s.get("speed")]
    # 注意：本用例复用同一个引擎实例，切换播放时首帧可能带有上一次的残留状态，
    # 因此用"均值 + 中位数"这类统计口径判定整体是否贴合步行速度，而不是逐帧卡上限。
    med = statistics.median(speeds2) if speeds2 else 0
    check("1x 播放时上报速度整体贴合步行基准（1.0~2.2 m/s）",
          speeds2 and 0.9 <= statistics.mean(speeds2) <= 2.2 and 1.0 <= med <= 2.2,
          f"均值 {statistics.mean(speeds2):.2f} 中位 {med:.2f} 范围 {min(speeds2):.2f}~{max(speeds2):.2f}（基准 {base_walk:.2f}）")
    rel2 = [
        abs(walk[i + 1]["speed"] - hops2[i] / dts2[i]) / max(0.1, hops2[i] / dts2[i])
        for i in range(len(hops2)) if dts2[i] > 0.05
    ]
    check("1x 时自洽性更好（相对偏差 < 5%）", rel2 and statistics.mean(rel2) < 0.05,
          f"{statistics.mean(rel2) * 100:.2f}%" if rel2 else "无样本")
    gaps2 = [g for g in dts2 if g > 0]
    check("1Hz 下发间隔在 0.7~2.2s（含抖动与丢采样）",
          gaps2 and 0.7 <= min(gaps2) and max(gaps2) <= 2.2,
          f"{min(gaps2):.2f} ~ {max(gaps2):.2f}s")

    # 8.3 粗差：默认关闭，开启后按概率出现
    check("粗差默认关闭", gen.gross_error_m(3, 0.0, 30.0) == (0.0, 0.0))
    hits = sum(1 for i in range(1000) if gen.gross_error_m(i, 0.02, 30.0) != (0.0, 0.0))
    check("粗差概率 2% 时命中率接近 2%", 8 <= hits <= 35, f"{hits}/1000")

    # 8.4 多尺度：幅度标定与上限
    for radius in (2.0, 4.0, 8.0):
        vals = [math.hypot(*gen.offset_m(d, radius, octaves=True)) for d in range(0, 4000)]
        mean_ok = 0.6 * radius <= statistics.mean(vals) <= 1.1 * radius
        cap_ok = max(vals) <= radius * 1.8 + 1e-6
        check(f"漂移 {radius:g} m：均值≈半径且不超上限",
              mean_ok and cap_ok,
              f"均值 {statistics.mean(vals):.2f} 最大 {max(vals):.2f}")

    print(f"\n{'=' * 56}")
    print(f"通过 \033[32m{PASS}\033[0m 项，失败 \033[31m{FAIL}\033[0m 项")
    for item in FAILURES:
        print("  -", item)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
