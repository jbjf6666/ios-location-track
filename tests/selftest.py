#!/usr/bin/env python3
"""端到端自检：单元 + 集成（无设备，走 --mock）。

运行：./.venv/bin/python tests/selftest.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config_store, geoutils, protocol, route_engine, tilecache, ws  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  \033[31m✗\033[0m {name} {detail}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# ---------------------------------------------------------------- 单元
def test_protocol() -> None:
    section("1. 协议与校验")
    check("合法经纬度通过", protocol.validate_latlng(39.9, 116.4) == (39.9, 116.4))
    for bad in [(91, 0), (0, 181), ("a", 1), (float("nan"), 0)]:
        try:
            protocol.validate_latlng(*bad)
            check(f"非法经纬度被拒 {bad}", False)
        except protocol.AppError:
            check(f"非法经纬度被拒 {bad}", True)
    check("倍速上下限", protocol.validate_speed_scale(1.5) == 1.5)
    try:
        protocol.validate_speed_scale(999)
        check("倍速超限被拒", False)
    except protocol.AppError:
        check("倍速超限被拒", True)
    check("模式解析", protocol.validate_mode("cycling") == "cycling")
    check("JSON 中文不被转义", "定位" in protocol.dumps({"msg": "定位"}))


def test_geo() -> None:
    section("2. 地理计算")
    d = geoutils.haversine(39.9, 116.4, 39.9, 116.41)
    check("haversine 精度", 830 < d < 870, f"got {d:.1f}m")
    b = geoutils.bearing(39.9, 116.4, 40.0, 116.4)
    check("正北方位角≈0", b < 1 or b > 359, f"got {b:.2f}")
    path = [(39.9, 116.4), (39.91, 116.41)]
    dense = geoutils.densify(path, 20.0)
    check("路径加密点变多", len(dense) > 50, f"{len(dense)} 点")
    acc = geoutils.cumulative_lengths(dense)
    total = acc[-1]
    check("累计弧长单调递增", all(acc[i] <= acc[i + 1] for i in range(len(acc) - 1)))
    mid, _ = geoutils.interpolate(dense, acc, total / 2)
    check("弧长中点落在起终点之间", 39.9 <= mid[0] <= 39.91, f"{mid}")
    simplified = geoutils.simplify(dense, 2.0)
    check("简化后点数下降且首尾保留", len(simplified) < len(dense) and simplified[0] == dense[0])


def test_route_engine() -> None:
    section("3. 路线引擎")
    config = config_store.ConfigStore(ROOT / "config.json")
    engine = route_engine.RouteEngine(config)
    route = engine.build([[116.40, 39.90], [116.41, 39.905], [116.42, 39.90]], mode="driving", use_osrm=False)
    check("直线兜底引擎标记", route.engine == "straight")
    check("路径非空且已加密", len(route.path) > 100, f"{len(route.path)} 点")
    check("距离 > 1.5km", route.distance > 1500, f"{route.distance:.0f}m")
    check("时长合理", 60 < route.duration < 1200, f"{route.duration:.0f}s")
    check("速度剖面长度一致", len(route.speeds) == len(route.path))
    check("速度均为正", all(v > 0 for v in route.speeds))
    mid = route.point_at_distance(route.distance / 2)
    check("中点取点有效", 39.89 <= mid[0] <= 39.91, f"{mid}")
    payload = route.to_json()
    check("JSON 坐标是 [lng,lat]", abs(payload["path"][0][0] - 116.40) < 0.01, str(payload["path"][0]))
    restored = route_engine.sanitize_route_payload(payload)
    check("route 往返一致", abs(restored.distance - route.distance) < 1.0, f"{restored.distance:.1f} vs {route.distance:.1f}")
    check("高亮子路径不超过全长", len(route.highlight(route.distance / 2)) <= len(route.path))

    # GPX 往返
    gpx = route_engine.to_gpx(route, creator="selftest")
    check("GPX 含 trkpt", "<trkpt" in gpx and gpx.startswith("<?xml"))
    coords, name, _ = route_engine.from_gpx(gpx)
    check("GPX 导入保形（不抽稀丢点）", len(coords) == len(route.path), f"{len(coords)} vs {len(route.path)}")
    total = geoutils.cumulative_lengths(coords)[-1]
    check("GPX 往返距离误差 < 2m", abs(total - route.distance) < 2.0, f"误差 {abs(total - route.distance):.3f}m")

    # 单点与异常
    single = engine.build([[116.40, 39.90]], mode="walking", use_osrm=False)
    check("单点轨迹可用", len(single.path) == 1 and single.distance == 0)
    for bad, label in [([], "空途经点"), ([[200, 39]], "非法经度")]:
        try:
            engine.build(bad, use_osrm=False)
            check(f"{label}被拒", False)
        except protocol.AppError:
            check(f"{label}被拒", True)

    # OSRM URL 生成（不依赖外网）
    check("flight 不走 OSRM", engine._osrm([(39.9, 116.4), (39.91, 116.41)], "flight") is None)


def test_tiles() -> None:
    section("4. 瓦片与缓存")
    source = config_store.DEFAULT_TILE_SOURCES["amap"]
    url1 = tilecache.build_url(source, 12, 3367, 1580)
    check("高德 URL 生成", url1.startswith("https://webrd0") and "{x}" not in url1, url1)
    url2 = tilecache.build_url(source, 12, 3368, 1580)
    check("子域轮换生效", url1.split("/")[2] != url2.split("/")[2], f"{url1.split('/')[2]} vs {url2.split('/')[2]}")
    arc = config_store.DEFAULT_TILE_SOURCES["arcgis_sat"]
    check("Esri URL 含 z/y/x", "/tile/12/1580/3367" in tilecache.build_url(arc, 12, 3367, 1580))
    check("瓦片坐标校验", tilecache.validate_tile_request(12, 3367, 1580) == (12, 3367, 1580))
    try:
        tilecache.validate_tile_request(12, 99999, 1)
        check("越界瓦片被拒", False)
    except protocol.AppError:
        check("越界瓦片被拒", True)

    tmp = ROOT / ".cache-test"
    cache = tilecache.TileCache(tmp, max_mb=1)
    result = cache.get_tile("amap", source, 12, 0, 0)
    check("瓦片请求返回图片", result.body[:4] in (b"\x89PNG", b"\xff\xd8") and result.content_type.startswith("image/"),
          f"{result.status} {result.source} {len(result.body)}B")
    check("瓦片已落盘缓存", any(tmp.rglob("*.png")) or any(tmp.rglob("*.jpg")))
    result2 = cache.get_tile("amap", source, 12, 0, 0)
    check("第二次命中缓存", result2.source == "cache", result2.source)
    removed = cache.clear()
    check("清理缓存", removed >= 1, f"removed={removed}")
    try:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass


def test_config() -> None:
    section("5. 配置")
    path = ROOT / ".config-test.json"
    path.unlink(missing_ok=True)
    store = config_store.ConfigStore(path)
    check("默认瓦片源为高德", store.get("tile_source") == "amap")
    try:
        store.update({"send_hz": 999})
        check("非法 send_hz 被拒（不再静默夹取）", False)
    except protocol.AppError as exc:
        check("非法 send_hz 被拒（不再静默夹取）", exc.code == "bad_config", exc.code)
    store.update({"send_hz": 8})
    check("合法 send_hz 生效", store.get("send_hz") == 8.0, str(store.get("send_hz")))
    store.update({"tile_source": "amap_sat"})
    store2 = config_store.ConfigStore(path)
    check("配置持久化", store2.get("tile_source") == "amap_sat")
    try:
        store.update({"不存在的项": 1})
        check("未知配置被拒", False)
    except protocol.AppError:
        check("未知配置被拒", True)
    sources = store.tile_sources()
    check("瓦片源齐全", all(k in sources for k in ("amap", "amap_sat", "arcgis_sat", "osm")))
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------- WS 客户端
class WSClient:
    def __init__(self, host: str, port: int, timeout: float = 30.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /ws HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("握手失败")
            header += chunk
        if b"101" not in header.split(b"\r\n")[0]:
            raise RuntimeError(f"握手未升级：{header[:200]!r}")
        expected = base64.b64encode(hashlib.sha1((key + ws.GUID).encode()).digest()).decode()
        if expected.encode() not in header:
            raise RuntimeError("Sec-WebSocket-Accept 校验失败")
        self.buf = header.split(b"\r\n\r\n", 1)[1]
        self.messages: list[dict] = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _recv_exact(self, n: int) -> bytes | None:
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                return None
            if not chunk:
                return None
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            head = self._recv_exact(2)
            if head is None:
                return
            opcode = head[0] & 0x0F
            length = head[1] & 0x7F
            if length == 126:
                ext = self._recv_exact(2)
                if ext is None:
                    return
                length = struct.unpack("!H", ext)[0]
            elif length == 127:
                ext = self._recv_exact(8)
                if ext is None:
                    return
                length = struct.unpack("!Q", ext)[0]
            payload = self._recv_exact(length) if length else b""
            if payload is None:
                return
            if opcode == 0x8:
                return
            if opcode in (0x9, 0xA):
                continue
            try:
                with self.lock:
                    self.messages.append(json.loads(payload.decode("utf-8")))
            except Exception:
                continue

    def send(self, obj: dict) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def wait_for(self, predicate, timeout: float = 15.0) -> dict | None:
        deadline = time.time() + timeout
        seen = 0
        while time.time() < deadline:
            with self.lock:
                batch = self.messages[seen:]
                seen = len(self.messages)
            for message in batch:
                if predicate(message):
                    return message
            time.sleep(0.05)
        return None

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def test_integration() -> None:
    section("6. 端到端（--mock 模式，真 HTTP + 真 WebSocket）")
    from core import server as server_mod

    port = 8899
    app = server_mod.App(root=ROOT, mock=True, port=port)
    app.devices.refresh()
    httpd = server_mod.serve(app, host="127.0.0.1", port=port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    time.sleep(0.4)
    try:
        # --- REST ---
        with urllib.request.urlopen(base + "/api/health", timeout=5) as response:
            health = json.loads(response.read())
        check("GET /api/health", health.get("ok") and health.get("version") == protocol.APP_VERSION, str(health)[:120])
        check("mock 模式标记", health.get("mock") is True)

        with urllib.request.urlopen(base + "/api/devices", timeout=5) as response:
            devices = json.loads(response.read())
        check("GET /api/devices 返回模拟设备", len(devices["devices"]) == 1 and devices["devices"][0]["ios_major"] == 26,
              str(devices)[:150])

        with urllib.request.urlopen(base + "/api/settings/tiles", timeout=5) as response:
            tiles = json.loads(response.read())
        keys = {item["key"] for item in tiles["sources"]}
        check("瓦片源接口含 key/datum", "amap" in keys and tiles["sources"][0]["datum"] in ("gcj02", "wgs84"), str(tiles)[:150])
        check("瓦片源三种字段名都在", tiles["current"] == tiles["tile_source"] == tiles["selected"])

        with urllib.request.urlopen(base + "/", timeout=5) as response:
            html = response.read().decode("utf-8", "replace")
        check("GET / 返回前端页面", "<html" in html.lower(), html[:80])

        # 路线规划（REST）
        body = json.dumps({"waypoints": [[116.40, 39.90], [116.41, 39.905], [116.42, 39.90]], "mode": "driving"}).encode()
        request = urllib.request.Request(base + "/api/route/build", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            route_result = json.loads(response.read())
        check("POST /api/route/build", route_result.get("ok") and route_result["route"]["distance"] > 500,
              str(route_result)[:120])

        # GPX 往返（REST）
        export_body = json.dumps({"route": route_result["route"], "name": "自检轨迹"}).encode()
        request = urllib.request.Request(base + "/api/gpx/export", data=export_body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            gpx_text = response.read().decode()
        check("POST /api/gpx/export", "<trkpt" in gpx_text and "自检轨迹" in gpx_text)
        request = urllib.request.Request(base + "/api/gpx/import", data=gpx_text.encode(), headers={"Content-Type": "application/gpx+xml"})
        with urllib.request.urlopen(request, timeout=15) as response:
            imported = json.loads(response.read())
        check("POST /api/gpx/import", imported["ok"] and imported["count"] >= 2, str(imported)[:120])
        check("GPX 导入不丢几何（返回全量点）", len(imported["waypoints"]) == imported["count"] >= 100,
              f"count={imported['count']}")
        check("GPX 导入同时提供 path/points 全量数组",
              imported.get("path") == imported["waypoints"] and imported.get("points") == imported["waypoints"])
        imported_coords = [(p[1], p[0]) for p in imported["waypoints"]]
        imported_total = geoutils.cumulative_lengths(imported_coords)[-1]
        check("GPX 导入距离保真（误差 < 2m）", abs(imported_total - route_result["route"]["distance"]) < 2.0,
              f"误差 {abs(imported_total - route_result['route']['distance']):.3f}m")

        # 瓦片代理
        with urllib.request.urlopen(base + "/api/tiles/amap/12/3367/1580.png", timeout=25) as response:
            tile_bytes = response.read()
        check("GET /api/tiles 返回图片", tile_bytes[:4] in (b"\x89PNG", b"\xff\xd8"), f"{len(tile_bytes)}B")

        # --- WebSocket ---
        client = WSClient("127.0.0.1", port)
        client.send({"type": "hello", "id": "h1", "client": "selftest"})
        ack = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "h1")
        check("WS hello ack 携带 welcome 数据", ack is not None and ack.get("mock") is True and "config" in ack,
              str(ack)[:140])
        check("welcome 里带设备与隧道快照", ack is not None and "tunnel" in ack and ack.get("version") == protocol.APP_VERSION,
              str(ack)[:140])

        client.send({"type": "ping", "id": "p1"})
        pong = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "p1")
        check("WS ping→ack(ts)", pong is not None and "ts" in pong, str(pong)[:120])

        client.send({"type": "device.connect", "id": "c1"})
        connected = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "c1", timeout=20)
        check("WS device.connect", connected is not None and connected.get("ok", True), str(connected)[:160])

        client.send({"type": "location.set", "id": "l1", "lat": 39.9088, "lng": 116.3975})
        ack = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "l1", timeout=15)
        check("WS location.set", ack is not None and ack.get("ok", True), str(ack)[:160])
        location = client.wait_for(lambda m: m.get("type") == "location" and m.get("source") == "manual", timeout=10)
        check("收到 location 事件（单点）", location is not None and abs(location["lng"] - 116.3975) < 1e-6, str(location)[:160])

        # 多设备/非法命令
        client.send({"type": "play.start", "id": "e1", "route": {"path": [], "waypoints": []}})
        bad = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "e1", timeout=10)
        check("空轨迹被拒并回中文错误", bad is not None and bad.get("ok") is False and "轨迹" in bad.get("error", ""), str(bad)[:160])

        # 播放流程
        route_payload = route_result["route"]
        client.send({"type": "play.start", "id": "s1", "route": route_payload, "speed_scale": 8.0, "loop": False})
        started = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "s1", timeout=20)
        check("WS play.start", started is not None and started.get("ok", True), str(started)[:160])
        first = client.wait_for(lambda m: m.get("type") == "location" and m.get("source") == "play", timeout=10)
        check("播放下发首个定位", first is not None and first.get("progress", 1) < 0.2, str(first)[:160])
        progressed = client.wait_for(lambda m: m.get("type") == "location" and m.get("progress", 0) > 0.05, timeout=20)
        check("播放推进（进度增长）", progressed is not None, str(progressed)[:160])
        status = client.wait_for(lambda m: m.get("type") == "status" and m.get("playing") is True, timeout=10)
        check("status 事件含剩余时间", status is not None and "remaining" in status, str(status)[:160])

        client.send({"type": "play.pause", "id": "p2"})
        paused = client.wait_for(lambda m: m.get("type") == "status" and m.get("paused") is True, timeout=10)
        check("暂停生效", paused is not None, str(paused)[:120])
        time.sleep(0.6)
        with client.lock:
            after_pause = len([m for m in client.messages if m.get("type") == "location" and m.get("source") == "play"])
        time.sleep(0.8)
        with client.lock:
            still = len([m for m in client.messages if m.get("type") == "location" and m.get("source") == "play"])
        check("暂停后不再下发", still - after_pause <= 1, f"{after_pause} → {still}")

        client.send({"type": "play.resume", "id": "r1"})
        resumed = client.wait_for(lambda m: m.get("type") == "status" and m.get("paused") is False and m.get("playing") is True, timeout=10)
        check("继续生效", resumed is not None, str(resumed)[:120])

        client.send({"type": "play.speed", "id": "sp1", "speed_scale": 20.0})
        fast = client.wait_for(lambda m: m.get("type") == "status" and m.get("speed_scale") == 20.0, timeout=10)
        check("倍速调整生效", fast is not None, str(fast)[:120])

        client.send({"type": "play.seek", "id": "sk1", "progress": 0.5})
        seeked = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "sk1", timeout=10)
        check("seek 生效", seeked is not None and seeked.get("ok", True), str(seeked)[:120])
        at_half = client.wait_for(lambda m: m.get("type") == "location" and m.get("progress", 0) > 0.45, timeout=10)
        check("seek 后位置过半", at_half is not None, str(at_half)[:140])

        # 播放到结束（循环 + 高倍速）
        client.send({"type": "play.speed", "id": "sp2", "speed_scale": 50.0})
        client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "sp2", timeout=10)
        done = client.wait_for(
            lambda m: m.get("type") == "status" and m.get("playing") is False and m.get("progress", 0) >= 0.999,
            timeout=90,
        )
        check("轨迹播放到终点", done is not None, str(done)[:160])

        # 循环模式
        client.send({"type": "play.start", "id": "s2", "route": route_payload, "speed_scale": 50.0, "loop": True})
        client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "s2", timeout=15)
        wrapped = client.wait_for(
            lambda m: m.get("type") == "status" and m.get("loop") is True and m.get("progress", 1) < 0.5 and m.get("playing"),
            timeout=90,
        )
        check("loop 模式回到起点继续", wrapped is not None, str(wrapped)[:160])

        client.send({"type": "play.stop", "id": "st1"})
        stopped = client.wait_for(lambda m: m.get("type") == "status" and m.get("playing") is False, timeout=15)
        check("停止播放", stopped is not None, str(stopped)[:140])
        cleared = client.wait_for(lambda m: m.get("type") == "location" and m.get("source") == "cleared", timeout=10)
        check("停止后通知恢复真实定位", cleared is not None, str(cleared)[:120])

        client.send({"type": "log.list", "id": "lg1", "limit": 50})
        logs = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "lg1", timeout=10)
        check("日志回填", logs is not None, str(logs)[:120])

        client.send({"type": "不存在的命令", "id": "x1"})
        unknown = client.wait_for(lambda m: m.get("type") == "ack" and m.get("id") == "x1", timeout=10)
        check("未知命令回中文错误", unknown is not None and unknown.get("ok") is False, str(unknown)[:140])

        client.close()
        time.sleep(0.3)
    finally:
        app.shutdown()
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass


def main() -> int:
    print(f"\033[1m{protocol.APP_NAME} 自检\033[0m  (Python {sys.version.split()[0]})")
    started = time.time()
    test_protocol()
    test_geo()
    test_route_engine()
    test_tiles()
    test_config()
    try:
        test_integration()
    except Exception as exc:
        global FAIL
        FAIL += 1
        FAILURES.append(f"集成测试异常：{exc}")
        print(f"  \033[31m✗\033[0m 集成测试异常：{exc.__class__.__name__}: {exc}")
        import traceback

        traceback.print_exc()
    print(f"\n{'=' * 56}")
    print(f"通过 \033[32m{PASS}\033[0m 项，失败 \033[31m{FAIL}\033[0m 项，用时 {time.time() - started:.1f}s")
    if FAILURES:
        print("\n失败明细：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("\033[32m全部通过\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
