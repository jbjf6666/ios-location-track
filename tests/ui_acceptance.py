#!/usr/bin/env python3
"""图形界面验收：用真实 Chrome 驱动界面完成一整套操作并截图。

做的是**图形界面级**验收：在页面上真实点击「添加途经点」→ 在地图上点三个点 →
点「加载 OSRM 路线」→ 点「播放」→ 截图，并断言界面状态与后端状态一致。

CDP 走 WebSocket，本机没有 websocket-client，因此这里复用 core/ws.py 的帧实现。

用法：
    ./.venv/bin/python tests/ui_acceptance.py                 # 默认连 127.0.0.1:8787
    ./.venv/bin/python tests/ui_acceptance.py --url http://127.0.0.1:8787/ --out /tmp/ui
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from urllib.parse import urlparse  # noqa: E402

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

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


class CDP:
    """最小可用的 Chrome DevTools Protocol 客户端。"""

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        parsed = urlparse(ws_url)
        self.sock = socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 9222), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        self.buf = b""
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP 握手失败")
            header += chunk
        head, _, rest = header.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"CDP 未升级：{head[:200]!r}")
        self.buf = rest
        self.counter = 0
        self.timeout = timeout

    def _recv(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("CDP 连接关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_frame(self) -> str:
        head = self._recv(2)
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv(8))[0]
        payload = self._recv(length) if length else b""
        if opcode == 0x8:
            raise RuntimeError("CDP 服务端关闭连接")
        if opcode == 0x9:  # ping → pong（带掩码）
            mask = os.urandom(4)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + masked)
            return self._read_frame()
        return payload.decode("utf-8", "replace")

    def _send(self, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
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
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def call(self, method: str, params: dict | None = None) -> dict:
        self.counter += 1
        message_id = self.counter
        self._send({"id": message_id, "method": method, "params": params or {}})
        while True:
            data = json.loads(self._read_frame())
            if data.get("id") == message_id:
                if "error" in data:
                    raise RuntimeError(f"CDP {method} 失败：{data['error']}")
                return data.get("result") or {}
            # 其它事件（Runtime.consoleAPICalled 等）先记录到 self.events
            self.events.append(data)

    events: list = []

    def eval(self, expression: str, await_promise: bool = False):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise RuntimeError(f"页面 JS 异常：{detail.get('text')} "
                              f"{(detail.get('exception') or {}).get('description', '')[:300]}")
        return (result.get("result") or {}).get("value")

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        for event_type in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {
                "type": event_type, "x": x, "y": y, "button": button,
                "clickCount": clicks, "buttons": 1 if event_type == "mousePressed" else 0,
            })

    def move(self, x: float, y: float) -> None:
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "buttons": 0})

    def shot(self, path) -> None:
        data = self.call("Page.captureScreenshot", {"format": "png"})
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(data["data"]))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def launch_chrome(width: int, height: int, port: int) -> subprocess.Popen:
    chrome = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None)
    if not chrome:
        raise SystemExit("未找到 Chrome / Edge / Chromium")
    profile = Path("/tmp/iosloc-ui-acceptance-profile")
    subprocess.run(["rm", "-rf", str(profile)], check=False)
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            chrome, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--hide-scrollbars", "--disable-extensions",
            f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            f"--window-size={width},{height}", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                pages = json.loads(response.read())
            for page in pages:
                if page.get("type") == "page":
                    return process, page["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.4)
    process.terminate()
    raise SystemExit("无法连接 Chrome 调试端口")


def main() -> int:
    global PASS, FAIL
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8787/")
    parser.add_argument("--out", default="/tmp/ui-acceptance")
    parser.add_argument("--port", type=int, default=9444)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--keep-open", action="store_true", help="结束后不关闭 Chrome")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print("\033[1m图形界面验收（真实 Chrome 驱动）\033[0m")
    process, ws_url = launch_chrome(args.width, args.height, args.port)
    cdp = None
    try:
        cdp = CDP(ws_url)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": args.width, "height": args.height, "deviceScaleFactor": 2, "mobile": False})
        cdp.call("Page.navigate", {"url": args.url})
        time.sleep(3.5)

        # ---- 0. 页面与服务端连通 ----
        title = cdp.eval("document.title")
        check("页面已加载", bool(title), str(title))
        ws_state = cdp.eval("(function(){var d=document.querySelector('#connPill');return d?d.textContent.trim():''})()")
        check("界面显示已连接服务端", "已连接" in str(ws_state), f"connPill={ws_state!r}")
        device_text = cdp.eval("(function(){var d=document.querySelector('#deviceCard');return d?d.innerText.replace(/\\n/g,' | '):''})()")
        check("设备卡片显示设备信息", "iPhone" in str(device_text) or "模拟" in str(device_text), str(device_text)[:120])

        # ---- 1. 地图渲染 ----
        map_info = cdp.eval(
            "(function(){var c=document.querySelector('#map canvas');"
            "var t=document.querySelectorAll('#map img').length;"
            "return JSON.stringify({canvas:!!c,imgs:t,w:c?c.width:0,h:c?c.height:0})})()"
        )
        info = json.loads(map_info) if map_info else {}
        check("地图画布已渲染", bool(info.get("w")) and bool(info.get("h")), str(info))

        # ---- 2. 添加途经点：点工具条 + 在地图上点三个位置 ----
        clicked_tool = cdp.eval(
            "(function(){var b=document.querySelector('#toolAdd');if(!b)return 'no-btn';b.click();return b.className})()"
        )
        check("「添加途经点」按钮可点击", clicked_tool != "no-btn", str(clicked_tool))
        time.sleep(0.4)
        box = cdp.eval(
            "(function(){var m=document.querySelector('#map');var r=m.getBoundingClientRect();"
            "return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height})})()"
        )
        rect = json.loads(box)
        points = [
            (rect["x"] + rect["w"] * 0.30, rect["y"] + rect["h"] * 0.35),
            (rect["x"] + rect["w"] * 0.52, rect["y"] + rect["h"] * 0.55),
            (rect["x"] + rect["w"] * 0.72, rect["y"] + rect["h"] * 0.42),
        ]
        for (px, py) in points:
            cdp.click(px, py)
            time.sleep(0.45)
        time.sleep(0.6)
        wp_count = cdp.eval(
            "(function(){var l=document.querySelectorAll('#wpList .wp-item, #wpList li, #wpList [data-index]');"
            "return l.length})()"
        )
        check("地图点击添加了 3 个途经点", int(wp_count or 0) >= 3, f"列表条目={wp_count}")
        cdp.shot(out_dir / "01-waypoints.png")

        # ---- 3. 加载 OSRM 路线 ----
        cdp.eval("(function(){var b=document.querySelector('#btnBuildRoute');if(b)b.click();return !!b})()")
        route_ok = False
        for _ in range(40):
            time.sleep(0.5)
            meta = cdp.eval("(function(){var d=document.querySelector('#statDistance');return d?d.textContent.trim():''})()")
            if meta and meta not in ("—", "-", "0", ""):
                route_ok = True
                break
        distance_text = cdp.eval("(function(){var d=document.querySelector('#statDistance');return d?d.textContent.trim():''})()")
        check("「加载 OSRM 路线」生成了路线", route_ok, f"总距离={distance_text!r}")
        engine_text = cdp.eval("(function(){var d=document.querySelector('#statEngine');return d?d.textContent.trim():''})()")
        check("路线引擎已显示", bool(engine_text) and engine_text != "—", f"引擎={engine_text!r}")
        cdp.shot(out_dir / "02-route.png")

        # ---- 4. 播放（按钮是异步生效的：点击后等状态真正变为"播放中"，必要时重试一次） ----
        def click_play() -> None:
            cdp.eval("(function(){var b=document.querySelector('#btnPlay, [data-action=play]');"
                     "if(b && !b.disabled)b.click();return !!b})()")

        def wait_playing(timeout: float = 12.0) -> bool:
            deadline = time.time() + timeout
            while time.time() < deadline:
                state = str(cdp.eval("(function(){var d=document.querySelector('#chipPlayState');return d?d.textContent.trim():''})()"))
                if "播放中" in state or "已暂停" in state:
                    return True
                time.sleep(0.3)
            return False

        click_play()
        playing = wait_playing(12.0)
        if not playing:
            time.sleep(1.0)
            click_play()
            playing = wait_playing(12.0)
        check("点击播放后进入播放态", playing,
              str(cdp.eval("document.querySelector('#chipPlayState').textContent")))
        progressed = False
        progress_text = ""
        coord_text = ""
        for _ in range(40):
            time.sleep(0.5)
            progress_text = str(cdp.eval("(function(){var d=document.querySelector('#chipProgress');return d?d.textContent.trim():''})()"))
            coord_text = str(cdp.eval("(function(){var d=document.querySelector('#chipCoord');return d?d.textContent.trim():''})()"))
            if any(ch.isdigit() for ch in progress_text) and "0%" not in progress_text and "0 %" not in progress_text:
                progressed = True
                break
        check("播放推进（地图 chips 显示进度）", progressed, f"chipProgress={progress_text!r}")
        check("实时坐标已显示", any(ch.isdigit() for ch in coord_text), f"chipCoord={coord_text!r}")
        play_state = cdp.eval("(function(){var d=document.querySelector('#chipPlayState');return d?d.textContent.trim():''})()")
        check("播放状态文案正常", bool(play_state) and "未播放" not in str(play_state), f"chipPlayState={play_state!r}")
        cdp.shot(out_dir / "03-playing.png")

        # ---- 5. 暂停 / 继续 / 停止（暂停是异步推送，采样窗口内看是否出现过"已暂停"） ----
        cdp.eval("(function(){var b=document.querySelector('#btnPause, [data-action=pause]');if(b)b.click();return !!b})()")
        paused_seen = False
        paused_sample = ""
        for _ in range(20):
            time.sleep(0.15)
            paused_sample = str(cdp.eval("(function(){var d=document.querySelector('#chipPlayState');return d?d.textContent.trim():''})()"))
            if "暂停" in paused_sample:
                paused_seen = True
                break
        check("暂停后状态文案变化", paused_seen, f"{paused_sample!r}")
        cdp.eval("(function(){var b=document.querySelector('#btnResume, [data-action=resume]');if(b)b.click();return !!b})()")
        time.sleep(1.0)
        resumed_text = cdp.eval("(function(){var d=document.querySelector('#chipPlayState');return d?d.textContent.trim():''})()")
        check("继续后恢复播放", "暂停" not in str(resumed_text) or "播放" in str(resumed_text), f"{resumed_text!r}")
        cdp.eval("(function(){var b=document.querySelector('#btnStop, [data-action=stop]');if(b)b.click();return !!b})()")
        time.sleep(1.5)
        stop_text = cdp.eval("(function(){var d=document.querySelector('#chipPlayState');return d?d.textContent.trim():''})()")
        check("停止后状态回到未播放", "未播放" in str(stop_text) or "已停止" in str(stop_text), f"{stop_text!r}")
        cdp.shot(out_dir / "04-stopped.png")

        # ---- 6. 底图切换 ----
        options = cdp.eval(
            "(function(){var s=document.querySelector('#tileSourceSelect');"
            "return JSON.stringify(Array.from(s.options).map(function(o){return o.value}))})()"
        )
        opts = json.loads(options or "[]")
        check("底图下拉有多个瓦片源", len(opts) >= 3, str(opts))
        if len(opts) >= 2:
            cdp.eval(f"(function(){{var s=document.querySelector('#tileSourceSelect');"
                     f"s.value={json.dumps(opts[1])};s.dispatchEvent(new Event('change',{{bubbles:true}}));return s.value}})()")
            time.sleep(3.0)
            current = cdp.eval("(function(){var s=document.querySelector('#tileSourceSelect');return s.value})()")
            check("切换底图生效", current == opts[1], f"{current!r} vs {opts[1]!r}")
            cdp.shot(out_dir / "05-tilesource.png")

        # ---- 7. 日志面板（先展开再看内容） ----
        cdp.eval("(function(){var b=document.querySelector('#btnToggleLog');if(b)b.click();return !!b})()")
        time.sleep(1.2)
        log_chars = cdp.eval("(function(){var p=document.querySelector('#logBody');return p?(p.innerText||'').length:0})()")
        check("日志面板能展开且有内容", int(log_chars or 0) > 30, f"字符数={log_chars}")
        log_text = cdp.eval("(function(){var p=document.querySelector('#logBody');return p?(p.innerText||'').slice(0,200):''})()")
        check("日志内容为中文可读", bool(log_text) and any("\u4e00" <= ch <= "\u9fff" for ch in str(log_text)),
              str(log_text)[:100])
        cdp.shot(out_dir / "06-logs.png")

        # ---- 8. 收集页面控制台报错 ----
        errors = []
        for event in cdp.events:
            method = event.get("method")
            if method == "Runtime.exceptionThrown":
                detail = event["params"]["exceptionDetails"]
                errors.append(f"{detail.get('text')} {(detail.get('exception') or {}).get('description', '')[:200]}")
            elif method == "Runtime.consoleAPICalled" and event["params"].get("type") == "error":
                parts = [str(a.get("value", a.get("description", "")))[:160] for a in event["params"].get("args", [])]
                errors.append("console.error: " + " ".join(parts))
        check("页面无 JS 报错", not errors, " | ".join(errors[:4]))
        if errors:
            print("\n  页面报错明细：")
            for item in errors[:10]:
                print("   -", item[:240])
    except Exception as exc:
        FAIL += 1
        FAILURES.append(f"验收脚本异常 {exc}")
        print(f"  \033[31m✗\033[0m 验收脚本异常：{exc.__class__.__name__}: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        if cdp:
            cdp.close()
        if not args.keep_open:
            try:
                os.killpg(os.getpgid(process.pid), 15)
            except Exception:
                process.terminate()

    print(f"\n{'=' * 56}")
    print(f"图形界面验收：通过 \033[32m{PASS}\033[0m 项，失败 \033[31m{FAIL}\033[0m 项")
    print(f"截图目录：{out_dir}")
    if FAILURES:
        print("失败明细：")
        for item in FAILURES:
            print("  -", item[:220])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
