"""轨迹播放引擎。

- 按速度剖面积分推进弧长（真实物理感受：转弯自动减速）。
- 独立的 worker 线程：即使设备下发阻塞也不会卡住 HTTP/WS，且保证同一时刻只有一个
  set_location 在飞行中（顺序严格不乱）。
- 倍速只影响推进速率，不影响下发频率（send_hz）。
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable

from . import geoutils, noise, protocol, route_engine

STATUS_INTERVAL = 0.5
TICK = 0.05
MAX_CATCHUP = 5.0

# 哨兵：用于区分"参数未提供"与"显式传 False/0"
_UNSET: Any = object()


class PlaybackEngine:
    def __init__(
        self,
        runner: Any,
        config: Any,
        on_location: Callable[[dict[str, Any]], None],
        on_status: Callable[[dict[str, Any]], None],
        on_log: Callable[[str, str], None] | None = None,
    ) -> None:
        self.runner = runner
        self.config = config
        self.on_location = on_location
        self.on_status = on_status
        self.on_log = on_log

        self._lock = threading.RLock()
        self._route: route_engine.Route | None = None
        self._provider: Any = None
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop_flag = threading.Event()

        self.playing = False
        self.paused = False
        self.loop = False
        self.speed_scale = 1.0
        # 播放代次：每次 start 自增，旧线程发现自己的代次过期就立即退出。
        # 这样即使 stop 时旧线程正卡在一次慢速下发里，也不会与新线程同时驱动设备。
        self._generation = 0
        self.distance = 0.0        # 已推进的弧长（米）
        self.tick_time = 0.0       # 播放用的虚拟时间（秒，已按倍速折算）
        self.started_at = 0.0
        self.last_status_at = 0.0
        self.last_sent_at = 0.0
        # 轨迹上的当前点（供 status 使用；上报点用 last_reported，两者不能混用）
        self.last_point: tuple[float, float] | None = None
        self.source = "play"
        self.sent_count = 0
        self.error: str | None = None
        # 状态序号：便于测试与前端判断"新旧"，避免异步推送乱序带来的误判
        self.status_seq = 0
        # 是否已经 clear 过（避免 stop + shutdown 重复 clear）
        self._cleared = True
        # 最近一次在途的设备下发 future：clear 之前要等它结束，保证 set 与 clear 的顺序
        self._pending_dispatch: Any = None

        # ---- 真实感模拟：GPS 漂移 + 速度浮动（都能开关，开关与幅度可随时改） ----
        self.drift_enabled = bool(config.get("drift_enabled", True))
        self.drift_m = float(config.get("drift_m") or noise.DEFAULT_DRIFT_M)
        self.speed_jitter_enabled = bool(config.get("speed_jitter_enabled", True))
        self.speed_jitter = float(config.get("speed_jitter") or noise.DEFAULT_SPEED_JITTER)
        self._drift = noise.default_generator()
        self.last_drift_m: float = 0.0
        # 发送节奏抖动（真实 GPS 不是精确等间隔）
        self.timing_jitter_enabled = bool(config.get("timing_jitter_enabled", True))
        # 上报速度由实际位移/时间推算（与 iOS 的算法一致）
        self.derive_speed = bool(config.get("derive_speed", True))
        # 粗差注入（默认关闭，用于测试自己 App 的过滤逻辑）
        self.gross_error_enabled = bool(config.get("gross_error_enabled", False))
        self.gross_error_probability = float(config.get("gross_error_probability") or 0.0)
        self.gross_error_m = float(config.get("gross_error_m") or 30.0)
        self.send_index = 0
        # 注意：self.last_point 是"轨迹上的点"（供 status 用），
        # 上报位置必须用独立变量，否则两者会被互相覆盖（曾因此导致播放线程崩溃）。
        self.last_reported: tuple[float, float] | None = None
        # 上一次"上报"的单调时刻：速度推算只依赖它，避免与其它时间戳变量互相干扰
        self.last_report_mono = 0.0
        # 最近一次上报的位移/时间间隔（供诊断与测试使用）
        self.last_speed_jump = 0.0
        self.last_speed_dt = 0.0

    # ---------------- 属性 ----------------
    @property
    def route(self) -> route_engine.Route | None:
        with self._lock:
            return self._route

    def set_provider(self, provider: Any) -> None:
        with self._lock:
            self._provider = provider

    @property
    def active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ---------------- 真实感调参（漂移 / 速度浮动） ----------------
    def tuning(self) -> dict[str, Any]:
        return {
            "drift_enabled": self.drift_enabled,
            "drift_m": round(self.drift_m, 2),
            "speed_jitter_enabled": self.speed_jitter_enabled,
            "speed_jitter": round(self.speed_jitter, 4),
            "speed_jitter_percent": round(self.speed_jitter * 100, 1),
            "timing_jitter_enabled": self.timing_jitter_enabled,
            "derive_speed": self.derive_speed,
            "gross_error_enabled": self.gross_error_enabled,
            "gross_error_probability": round(self.gross_error_probability, 3),
            "gross_error_m": round(self.gross_error_m, 1),
        }

    def update_tuning(self, drift_enabled: Any = _UNSET, drift_m: Any = _UNSET,
                      speed_jitter_enabled: Any = _UNSET, speed_jitter: Any = _UNSET,
                      timing_jitter_enabled: Any = _UNSET, derive_speed: Any = _UNSET,
                      gross_error_enabled: Any = _UNSET, gross_error_probability: Any = _UNSET,
                      gross_error_m: Any = _UNSET) -> dict[str, Any]:
        """运行中实时调整（不影响正在播放的轨迹推进，只影响后续下发/速度）。

        参数默认值是哨兵 `_UNSET` 而不是 None —— 否则无法把开关显式设成 False
        （None 与 False 都会被当成"未提供"，开关就永远关不掉）。
        """
        with self._lock:
            if drift_enabled is not _UNSET:
                self.drift_enabled = bool(drift_enabled)
            if timing_jitter_enabled is not _UNSET:
                self.timing_jitter_enabled = bool(timing_jitter_enabled)
            if derive_speed is not _UNSET:
                self.derive_speed = bool(derive_speed)
            if gross_error_enabled is not _UNSET:
                self.gross_error_enabled = bool(gross_error_enabled)
            if gross_error_probability is not _UNSET:
                value = protocol.finite(gross_error_probability, "粗差概率")
                if not (0.0 <= value <= 0.5):
                    raise protocol.AppError("粗差概率需在 0 ~ 0.5 之间", "bad_tuning")
                self.gross_error_probability = value
            if gross_error_m is not _UNSET:
                value = protocol.finite(gross_error_m, "粗差幅度")
                if not (1.0 <= value <= 500.0):
                    raise protocol.AppError("粗差幅度需在 1 ~ 500 米之间", "bad_tuning")
                self.gross_error_m = value
            if drift_m is not _UNSET:
                value = protocol.finite(drift_m, "漂移半径")
                if not (0.0 <= value <= noise.MAX_DRIFT_M):
                    raise protocol.AppError(
                        f"漂移半径需在 0 ~ {noise.MAX_DRIFT_M:g} 米之间", "bad_tuning"
                    )
                self.drift_m = value
            if speed_jitter_enabled is not _UNSET:
                self.speed_jitter_enabled = bool(speed_jitter_enabled)
            if speed_jitter is not _UNSET:
                value = protocol.finite(speed_jitter, "速度浮动幅度")
                # 允许前端传百分数（如 8 表示 8%），但不能把"500%"悄悄当成"5%"
                if 1.0 < value <= 100.0:
                    value = value / 100.0
                if not (0.0 <= value <= noise.MAX_SPEED_JITTER):
                    raise protocol.AppError(
                        f"速度浮动幅度需在 0 ~ {noise.MAX_SPEED_JITTER * 100:g}% 之间，"
                        "或 0 ~ 1 的小数",
                        "bad_tuning",
                    )
                self.speed_jitter = value
            result = self.tuning()
        # 落盘，下次启动沿用（用户按校园跑场景调好的参数不该丢）
        try:
            self.config.update({
                "drift_enabled": result["drift_enabled"],
                "drift_m": result["drift_m"],
                "speed_jitter_enabled": result["speed_jitter_enabled"],
                "speed_jitter": result["speed_jitter"],
                "timing_jitter_enabled": result["timing_jitter_enabled"],
                "derive_speed": result["derive_speed"],
                "gross_error_enabled": result["gross_error_enabled"],
                "gross_error_probability": result["gross_error_probability"],
                "gross_error_m": result["gross_error_m"],
            })
        except Exception:
            pass
        self._emit_status(force=True)
        return result

    def _speed_factor(self) -> float:
        """当前速度乘性系数（1.0 = 不浮动）。"""
        if not self.speed_jitter_enabled or self.speed_jitter <= 0:
            return 1.0
        return self._drift.speed_factor(
            time.time() - self.started_at, self.speed_jitter, noise.DEFAULT_DRIFT_PERIOD_S
        )

    def _reported_point(self, route: route_engine.Route,
                        point: tuple[float, float]) -> tuple[float, float]:
        """把"轨迹上的真实位置"加上多尺度漂移（可选粗差），得到实际下发给手机的位置。"""
        lat, lng = point
        if not self.drift_enabled or self.drift_m <= 0:
            self.last_drift_m = 0.0
            dlat = dlng = 0.0
        else:
            heading = route.heading_at_distance(self.distance)
            dlat, dlng = self._drift.offset_latlng(lat, lng, self.distance, heading, self.drift_m)
        # 粗差（默认关闭）：偶发坏点，用来测自己 App 的精度过滤/跳点剔除
        if self.gross_error_enabled and self.gross_error_probability > 0:
            east_m, north_m = self._drift.gross_error_m(
                self.send_index, self.gross_error_probability, self.gross_error_m
            )
            if east_m or north_m:
                cos_lat = math.cos(math.radians(max(-85.0, min(85.0, lat))))
                dlat += north_m / 111320.0
                dlng += east_m / (111320.0 * max(0.05, cos_lat))
        if dlat == 0.0 and dlng == 0.0:
            self.last_drift_m = 0.0
            return point
        self.last_drift_m = geoutils.haversine(lat, lng, lat + dlat, lng + dlng)
        return (lat + dlat, lng + dlng)

    # ---------------- 事件日志（仅用户主动操作时调用，不在循环里） ----------------
    def log_tuning(self) -> None:
        state = self.tuning()
        drift = f"{state['drift_m']:g} 米" if state["drift_enabled"] else "关闭"
        jitter = (
            f"±{state['speed_jitter_percent']:g}%" if state["speed_jitter_enabled"] else "关闭"
        )
        self._log("info", f"真实感设置：定位漂移 {drift}，速度浮动 {jitter}")

    # ---------------- 控制 ----------------
    def start(self, route: route_engine.Route, speed_scale: float | None = None,
              loop: bool | None = None, start_index: int = 0, provider: Any = None) -> None:
        with self._lock:
            if provider is not None:
                self._provider = provider
            old_thread = self._thread
            self.stop_locked(notify=False)
            generation = self._generation + 1
            self._generation = generation
            self._route = route
            self.speed_scale = protocol.validate_speed_scale(
                speed_scale if speed_scale is not None else route.speed_scale or 1.0
            )
            self.loop = bool(route.loop if loop is None else loop)
            self.playing = True
            self.paused = False
            self.error = None
            self.sent_count = 0
            self.source = "play"
            acc = route.dense_acc
            total = acc[-1] if acc else 0.0
            index = max(0, min(int(start_index), max(0, len(route.path) - 1)))
            self.distance = acc[index] if acc else 0.0
            self.tick_time = self._time_at_distance(self.distance)
            self.started_at = time.time()
            self.last_status_at = 0.0
            # 用当前时刻初始化，使首个间隔就是正常节奏（否则会在 50ms 后立刻补发第二个点）
            self.last_sent_at = time.time()
            self.last_report_mono = 0.0
            self.last_reported = None
            self.send_index = 0
            self.last_point = route.point_at_distance(self.distance) if route.path else None
            self._stop_flag.clear()
            self._wake.clear()
            self._cleared = False
            self._pending_dispatch = None
            self._thread = threading.Thread(
                target=self._loop, args=(generation,), name=f"playback-{generation}", daemon=True
            )
            self._thread.start()
        # 旧线程若还卡在慢速下发里，等它退出（最多 6 秒）；它每轮都会检查代次并自行结束。
        if old_thread is not None and old_thread.is_alive() and old_thread is not threading.current_thread():
            old_thread.join(timeout=6.0)
            if old_thread.is_alive():
                self._log("warn", "上一次播放的下发仍在收尾，已让新播放接替（旧线程会自行退出）")
        if route.distance <= 0 and len(route.path) <= 1:
            self._log("warn", "轨迹只有一个点：已把设备定位到该点")
        self._emit_status(force=True)

    def pause(self) -> None:
        with self._lock:
            if not self.playing or self.paused:
                return
            self.paused = True
        self._log("info", "播放已暂停")
        self._emit_status(force=True)

    def resume(self) -> None:
        with self._lock:
            if not self.playing or not self.paused:
                return
            self.paused = False
            self.last_sent_at = 0.0
        self._wake.set()
        self._log("info", "继续播放")
        self._emit_status(force=True)

    def stop(self, notify: bool = True, clear_provider: bool = True) -> None:
        with self._lock:
            provider = self._provider if clear_provider else None
            already_cleared = self._cleared
            route = self._route
            old_thread = self.stop_locked(notify=notify)
            self._cleared = True
        if provider is not None and not already_cleared:
            # 恢复真实定位放到后台线程：不要让「停止」的应答等设备往返（实测会拖慢 2~3 秒，
            # 界面要过几秒才变回"未播放"）。清理失败只记日志，不影响停止语义。
            threading.Thread(
                target=self._clear_provider_async, args=(provider, old_thread),
                name="playback-clear", daemon=True,
            ).start()
        if notify and route is not None:
            self._emit_status(force=True)
            self.on_location({
                "lng": None, "lat": None, "source": "cleared", "progress": 0.0,
                "index": 0, "speed": 0.0, "sent_at": protocol.now_ts(),
            })

    def _clear_provider_async(self, provider: Any, old_thread: threading.Thread | None = None) -> None:
        try:
            # 关键顺序保证：必须等在途的 set_location 真正结束之后再 clear，
            # 否则（对"帧结束时才生效"的实现）可能变成 clear 之后又来一次 set，
            # 设备就停在模拟点上而界面显示"已恢复真实定位"。
            if old_thread is not None and old_thread.is_alive():
                old_thread.join(timeout=6.0)
            pending = self._pending_dispatch
            if pending is not None:
                try:
                    pending.result(timeout=6.0)
                except Exception:
                    pass
            self.runner.run(provider.clear_location(), timeout=8.0, wait=True)
            self._log("success", "已停止模拟，iPhone 恢复真实定位")
        except Exception as exc:
            self._log("warn", f"恢复真实定位失败：{_short(exc)}")

    def stop_locked(self, notify: bool = True, join_timeout: float = 0.6) -> threading.Thread | None:
        self._stop_flag.set()
        self._wake.set()
        # 代次自增：即便旧线程还卡在慢速下发里，它恢复后也会因为代次过期而立即退出
        self._generation += 1
        thread = self._thread
        self._thread = None
        self.playing = False
        self.paused = False
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            # 不长时间等待：代次机制已经保证旧线程不会再下发/推送，界面无需为它等待。
            # 锁外 join，避免持锁等待阻塞其它命令（如 play.start / 状态查询）。
            self._lock.release()
            try:
                thread.join(timeout=join_timeout)
            finally:
                self._lock.acquire()
        return thread

    def seek(self, progress: float) -> dict[str, Any]:
        route = self.route
        if route is None or not route.path:
            raise protocol.AppError("当前没有正在播放的轨迹", "no_route")
        # 播放已结束/已停止时不允许 seek：否则会在"未播放"状态下重新把设备定位过去
        with self._lock:
            if not (self.playing or self.paused):
                raise protocol.AppError("当前没有正在播放的轨迹（请先点播放）", "not_playing")
        value = protocol.finite(progress, "进度")
        value = protocol.clamp(value, 0.0, 1.0)
        with self._lock:
            acc = route.dense_acc
            total = acc[-1] if acc else 0.0
            self.distance = total * value
            self.tick_time = self._time_at_distance(self.distance)
            self.last_sent_at = 0.0
        self._wake.set()
        point = route.point_at_distance(self.distance)
        self._send_point(point, force=True)
        self._emit_status(force=True)
        return self.status()

    def set_speed(self, speed_scale: float) -> dict[str, Any]:
        value = protocol.validate_speed_scale(speed_scale)
        with self._lock:
            self.speed_scale = value
        self._emit_status(force=True)
        return self.status()

    def set_loop(self, loop: bool) -> dict[str, Any]:
        with self._lock:
            self.loop = bool(loop)
        self._emit_status(force=True)
        return self.status()

    # ---------------- 单点定位 ----------------
    def set_point(self, lat: float, lng: float, provider: Any = None) -> None:
        with self._lock:
            if provider is not None:
                self._provider = provider
            self.source = "manual"
            self.last_point = (lat, lng)
            self.last_sent_at = time.time()
        self._dispatch(lat, lng, source="manual", progress=0.0, index=0, speed=0.0)

    def clear_point(self, provider: Any = None) -> None:
        with self._lock:
            if provider is not None:
                self._provider = provider
            self.source = "manual"
        self.stop(notify=True, clear_provider=True)

    # ---------------- worker ----------------
    def _loop(self, generation: int) -> None:
        def stale() -> bool:
            """自己是不是已被更新的播放/停止取代。"""
            with self._lock:
                return generation != self._generation or self._stop_flag.is_set()

        route = self.route
        if route is None:
            return
        send_hz = float(self.config.get("send_hz") or protocol.DEFAULT_SEND_HZ)
        send_interval = 1.0 / protocol.clamp(send_hz, protocol.MIN_SEND_HZ, protocol.MAX_SEND_HZ)
        last = time.monotonic()
        stall_logged = False
        try:
            # 先立刻把起点发出去，用户点击播放后马上生效
            self._send_point(route.point_at_distance(self.distance), force=True, generation=generation)
            while not stale():
                self._wake.wait(TICK)
                self._wake.clear()
                now = time.monotonic()
                delta = min(MAX_CATCHUP, max(0.0, now - last))
                last = now
                if stale():
                    break
                paused = self.paused
                finished = False
                with self._lock:
                    if not self.playing:
                        break
                    if not paused and route.path:
                        acc = route.dense_acc
                        total = acc[-1] if acc else 0.0
                        if total > 0:
                            speed = route.speed_at_distance(self.distance)
                            # 速度浮动：让瞬时速度在设定值上下平滑浮动（校园跑场景的关键真实感）。
                            # 同一个 tick 内只算一次系数，推进与上报必须用同一个值，否则上报速度会系统性偏高。
                            self._speed_factor_now = self._speed_factor()
                            step = max(0.05, speed) * self.speed_scale * self._speed_factor_now * delta
                            self.distance += step
                            self.tick_time += delta * self.speed_scale
                            if self.distance >= total:
                                if self.loop:
                                    self.distance = self.distance % total if total > 0 else 0.0
                                else:
                                    self.distance = total
                                    finished = True
                        else:
                            finished = not self.loop
                    if not paused:
                        point = route.point_at_distance(self.distance)
                        self.last_point = point
                if not paused:
                    self._maybe_send(route, send_interval, generation=generation)
                    if stale():
                        break
                if finished:
                    self._log("success", f"轨迹播放完成：{_fmt_distance(route.distance)}，共下发 {self.sent_count} 次定位")
                    with self._lock:
                        if generation == self._generation:
                            self.playing = False
                            self.paused = False
                    self._emit_status(force=True)
                    break
                self._emit_status()
        except Exception as exc:  # 不让线程静默死掉
            self.error = _short(exc)
            self._log("error", f"播放线程异常：{self.error}")
            with self._lock:
                self.playing = False
                self.paused = False
            self._emit_status(force=True)
            if not stall_logged:
                stall_logged = True

    def _maybe_send(self, route: route_engine.Route, send_interval: float, generation: int | None = None) -> None:
        now = time.time()
        # 真实 GPS 的回调不是精确等间隔：加节奏抖动（含偶发丢采样），
        # 间隔完全均匀（精确 1.000s）是最容易被识别的特征之一。
        jitter_factor = self._drift.timing_factor(self.send_index) if self.timing_jitter_enabled else 1.0
        wait = send_interval * jitter_factor
        if self.last_sent_at and (now - self.last_sent_at) < wait:
            return
        with self._lock:
            point = route.point_at_distance(self.distance)
            self.last_sent_at = now
        self._send_point(point, generation=generation)

    def _send_point(self, point: tuple[float, float], force: bool = False, generation: int | None = None) -> None:
        route = self.route
        if point is None:
            return
        total = route.distance if route is not None else 0.0
        progress = 0.0
        speed = 0.0
        index = 0
        if route is not None and route.path:
            acc = route.dense_acc
            total = acc[-1] if acc else 0.0
            progress = protocol.clamp(self.distance / total, 0.0, 1.0) if total > 0 else 1.0
            index = _index_of(acc, self.distance)
        reported = self._reported_point(route, point) if route is not None else point
        # ---- 上报速度：优先由"实际下发点之间的位移 ÷ 实际时间"推算 ----
        # 这与 iOS 从坐标序列推算 CLLocation.speed 的方式一致，因此速度、位移、
        # 时间戳三者天然自洽；不会出现"上报速度与轨迹推进矛盾"这种自曝式数据。
        now = time.time()
        mono = time.monotonic()
        derive = self.derive_speed and self.last_reported is not None and self.last_report_mono > 0
        if derive:
            prev_lat, prev_lng = self.last_reported
            dt = mono - self.last_report_mono
            jump = geoutils.haversine(prev_lat, prev_lng, reported[0], reported[1])
            if dt >= 0.05:
                if jump > 400.0:
                    # 粗差/跳点：真实接收机此时 speed 无效（-1），而不是给出一个巨大速度
                    speed = -1.0
                else:
                    speed = jump / dt
            else:
                speed = 0.0
            self.last_speed_jump = jump
            self.last_speed_dt = dt
        else:
            factor = getattr(self, "_speed_factor_now", None)
            if factor is None:
                factor = self._speed_factor()
            speed = (route.speed_at_distance(self.distance) * factor) if route is not None else 0.0
        with self._lock:
            self.last_reported = (reported[0], reported[1])
            self.last_report_mono = mono
        self.send_index += 1
        self._dispatch(
            reported[0], reported[1], source="play", progress=progress, index=index,
            speed=speed, generation=generation,
        )

    def _dispatch(self, lat: float, lng: float, source: str, progress: float, index: int, speed: float,
                  generation: int | None = None) -> None:
        provider = self._provider
        if provider is None:
            self._log("error", "尚未连接设备：无法下发定位")
            with self._lock:
                self.playing = False
            return
        try:
            future = self.runner.run(provider.set_location(lat, lng), timeout=6.0, wait=True)
            self._pending_dispatch = future
        except Exception as exc:
            message = _short(exc)
            self._log("error", f"下发定位失败：{message}")
            with self._lock:
                self.error = message
                if generation is None or generation == self._generation:
                    self.playing = False
                    self.paused = False
            self._emit_status(force=True)
            self._stop_flag.set()
            return
        # 下发期间可能已经被停止/被新的播放取代：这次结果作废，且不再推送位置事件
        with self._lock:
            if generation is not None and generation != self._generation:
                return
        self.sent_count += 1
        # 逐次下发的日志按 5 秒聚合，避免 5Hz 刷屏（前端日志面板与 WS 带宽都受不了）
        now = time.time()
        if now - getattr(self, "_last_send_log", 0.0) >= 5.0:
            self._last_send_log = now
            self._log("info", f"正在按轨迹下发定位（已下发 {self.sent_count} 次）")
        self.on_location({
            "lng": protocol.round6(lng),
            "lat": protocol.round6(lat),
            "source": source,
            "progress": round(progress, 5),
            "index": index,
            "speed": round(speed, 3),
            "drift_m": round(self.last_drift_m, 2),
            "sent_at": protocol.now_ts(),
        })

    # ---------------- 状态 ----------------
    def _time_at_distance(self, distance: float) -> float:
        route = self.route
        if route is None or not route.path or not route.speeds:
            return 0.0
        acc = route.dense_acc
        total = 0.0
        for i in range(1, len(route.path)):
            seg = acc[i] - acc[i - 1]
            if acc[i - 1] >= distance:
                break
            if acc[i] <= distance:
                total += seg / max(0.05, (route.speeds[i] + route.speeds[i - 1]) / 2.0)
            else:
                part = max(0.0, distance - acc[i - 1])
                total += part / max(0.05, route.speeds[i])
                break
        return total

    def status(self, force: bool = False) -> dict[str, Any]:
        route = self.route
        with self._lock:
            total_distance = route.distance if route is not None else 0.0
            progress = protocol.clamp(self.distance / total_distance, 0.0, 1.0) if total_distance > 0 else (1.0 if route else 0.0)
            elapsed = self.tick_time
            duration = route.duration if route is not None else 0.0
            remaining = max(0.0, duration - elapsed)
            point = self.last_point
            return {
                "playing": bool(self.playing),
                "paused": bool(self.paused),
                "progress": round(progress, 5),
                "index": 0 if route is None else _index_of(route.dense_acc, self.distance),
                "total": 0 if route is None else len(route.path),
                "elapsed": round(elapsed, 2),
                "remaining": round(remaining, 2),
                "duration": round(duration, 2),
                "distance_done": round(self.distance, 2),
                "distance": round(total_distance, 2),
                "speed_scale": self.speed_scale,
                "loop": self.loop,
                "lng": protocol.round6(point[1]) if point else None,
                "lat": protocol.round6(point[0]) if point else None,
                "name": route.name if route is not None else None,
                "error": self.error,
                "sent_count": self.sent_count,
                "tuning": self.tuning(),
                "drift_m": round(self.last_drift_m, 2),
            }

    def _emit_status(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self.last_status_at) < STATUS_INTERVAL:
            return
        self.last_status_at = now
        self.status_seq += 1
        try:
            self.on_status({**self.status(force=force), "seq": self.status_seq})
        except Exception:
            pass

    def _log(self, level: str, message: str) -> None:
        if self.on_log:
            try:
                self.on_log(level, message)
            except Exception:
                pass


def _index_of(acc: list[float], distance: float) -> int:
    if not acc:
        return 0
    lo, hi = 0, len(acc) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if acc[mid] <= distance:
            lo = mid + 1
        else:
            hi = mid
    return max(0, min(lo - 1, len(acc) - 1))


def _short(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:200]


def _fmt_distance(meters: float) -> str:
    if meters >= 1000:
        return f"{meters / 1000:.2f} 公里"
    return f"{meters:.0f} 米"


__all__ = ["PlaybackEngine", "geoutils"]
