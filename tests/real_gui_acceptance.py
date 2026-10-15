#!/usr/bin/env python3
"""真机图形界面验收：在真实设备模式下驱动界面完成绘制→规划→播放，并截图留证。

与 tests/real_device_e2e.py 的区别：这个是**通过浏览器点击界面**完成的（端到端，
包含前端），而 real_device_e2e.py 是直接走 WS 协议。

    ./.venv/bin/python tests/real_gui_acceptance.py --confirm --url http://127.0.0.1:8790/
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ui_acceptance import CDP, launch_chrome  # noqa: E402

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


def main() -> int:
    global PASS, FAIL
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true", help="确认会真的改变设备定位")
    parser.add_argument("--url", default="http://127.0.0.1:8790/")
    parser.add_argument("--out", default="/tmp/ui-real-acceptance")
    parser.add_argument("--port", type=int, default=9466)
    parser.add_argument("--wait-connect", type=float, default=90.0)
    args = parser.parse_args()
    if not args.confirm:
        print("真机测试会改变设备定位，请加 --confirm")
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print("\033[1m真机图形界面验收\033[0m")

    process, ws_url = launch_chrome(1440, 900, args.port)
    cdp = CDP(ws_url, timeout=180)
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1440, "height": 900, "deviceScaleFactor": 2, "mobile": False})
        cdp.call("Page.navigate", {"url": args.url})
        time.sleep(4.0)

        check("界面已连接服务端",
              "已连接" in str(cdp.eval("document.querySelector('#connPill').textContent")),
              str(cdp.eval("document.querySelector('#connPill').textContent")))
        device_text = str(cdp.eval("(function(){var d=document.querySelector('#deviceCard');return d?d.innerText.replace(/\\n/g,' | '):''})()"))
        check("设备卡片识别到真机", "iPhone" in device_text, device_text[:160])
        print(f"    设备卡片：{device_text[:120]}")
        cdp.shot(out_dir / "01-device-found.png")

        # 点「连接设备」
        cdp.eval("(function(){var b=document.querySelector('#btnDeviceConnect');if(b)b.click();return !!b})()")
        print("  → 正在连接设备（iOS 17+ 会建立 RSD 隧道，最长约 90 秒）…")
        connected = False
        deadline = time.time() + args.wait_connect
        while time.time() < deadline:
            time.sleep(1.0)
            card = str(cdp.eval("(function(){var d=document.querySelector('#deviceCard');return d?d.innerText:''})()"))
            if "模拟中" in card or "已连接" in card and "虚拟定位" in card:
                if "模拟中" in card or "就绪" in card:
                    connected = True
                    break
        card_final = str(cdp.eval("(function(){var d=document.querySelector('#deviceCard');return d?d.innerText.replace(/\\n/g,' | '):''})()"))
        check("设备连接成功（卡片显示虚拟定位状态）", connected, card_final[:200])
        print(f"    连接后卡片：{card_final[:160]}")
        cdp.shot(out_dir / "02-connected.png")

        # 绘制 3 个途经点
        cdp.eval("(function(){var b=document.querySelector('#toolAdd');if(b)b.click();return !!b})()")
        time.sleep(0.4)
        box = json.loads(cdp.eval("(function(){var r=document.querySelector('#map').getBoundingClientRect();"
                                  "return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height})})()"))
        for fx, fy in ((0.28, 0.30), (0.50, 0.48), (0.74, 0.36)):
            cdp.click(box["x"] + box["w"] * fx, box["y"] + box["h"] * fy)
            time.sleep(0.45)
        wp = cdp.eval("(function(){return document.querySelectorAll('#wpList .wp-item, #wpList li, #wpList [data-index]').length})()")
        check("地图点选生成途经点", int(wp or 0) >= 3, f"途经点={wp}")

        # 规划路线
        cdp.eval("(function(){var b=document.querySelector('#btnBuildRoute');if(b)b.click();return !!b})()")
        distance = "—"
        for _ in range(40):
            time.sleep(0.5)
            distance = str(cdp.eval("document.querySelector('#statDistance').textContent"))
            if distance not in ("—", "-", "", "0"):
                break
        check("OSRM 路线规划成功", distance not in ("—", "-", "", "0"), f"总距离={distance!r}")
        cdp.shot(out_dir / "03-route.png")

        # 倍速滑块是对数刻度（0.1x ~ 50x），按 Geo.speedToSlider 反算 1.0x 对应的滑块位置
        slider_value = int(round((__import__("math").log(1.0 / 0.1) / __import__("math").log(50.0 / 0.1)) * 1000))
        cdp.eval("(function(){var s=document.querySelector('#speedRange');if(!s)return 'no';"
                 f"s.value={slider_value};s.dispatchEvent(new Event('input',{{bubbles:true}}));"
                 "s.dispatchEvent(new Event('change',{bubbles:true}));return s.value})()")
        time.sleep(0.8)
        speed_label = str(cdp.eval("document.querySelector('#speedValue').textContent"))
        check("倍速已设为 1.0x（对数滑块）", "1.0x" in speed_label or "1x" in speed_label, speed_label)

        # 播放
        cdp.eval("(function(){var b=document.querySelector('#btnPlay');if(b)b.click();return !!b})()")
        progressed = False
        progress_text = ""
        for _ in range(30):
            time.sleep(0.5)
            progress_text = str(cdp.eval("document.querySelector('#chipProgress').textContent"))
            coord = str(cdp.eval("document.querySelector('#chipCoord').textContent"))
            if any(ch.isdigit() for ch in progress_text) and "0%" not in progress_text and "0 %" not in progress_text:
                progressed = True
                break
        check("真机播放推进", progressed, f"progress={progress_text!r}")
        coord = str(cdp.eval("document.querySelector('#chipCoord').textContent"))
        check("界面显示实时下发坐标", any(ch.isdigit() for ch in coord), f"coord={coord!r}")
        print(f"    实时状态：{progress_text} | {coord}")
        cdp.shot(out_dir / "04-playing-real-direct.png")

        # 日志面板展开，确认有真实下发日志
        cdp.eval("(function(){var b=document.querySelector('#btnToggleLog');if(b)b.click();return !!b})()")
        time.sleep(1.2)
        log_text = str(cdp.eval("(function(){var p=document.querySelector('#logBody');return p?p.innerText:''})()"))
        check("日志面板记录了真机下发", "下发" in log_text or "定位" in log_text, log_text[-200:].replace("\n", " | "))
        cdp.shot(out_dir / "05-logs-real.png")

        # 停止并恢复真实定位
        cdp.eval("(function(){var b=document.querySelector('#btnStop');if(b)b.click();return !!b})()")
        time.sleep(2.5)
        state = str(cdp.eval("document.querySelector('#chipPlayState').textContent"))
        check("停止后回到未播放", "未播放" in state or "已停止" in state or "已结束" in state, state)
        cdp.shot(out_dir / "06-stopped.png")

        errors = []
        for event in cdp.events:
            if event.get("method") == "Runtime.exceptionThrown":
                detail = event["params"]["exceptionDetails"]
                errors.append(f"{detail.get('text')} {(detail.get('exception') or {}).get('description', '')[:160]}")
            elif event.get("method") == "Runtime.consoleAPICalled" and event["params"].get("type") == "error":
                parts = [str(a.get("value", a.get("description", "")))[:140] for a in event["params"].get("args", [])]
                errors.append("console.error: " + " ".join(parts))
        check("真机流程中页面无 JS 报错", not errors, " | ".join(errors[:3]))
    except Exception as exc:
        FAIL += 1
        FAILURES.append(f"异常 {exc}")
        print(f"  \033[31m✗\033[0m 异常：{exc.__class__.__name__}: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        try:
            cdp.close()
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(process.pid), 15)
        except Exception:
            process.terminate()

    print(f"\n{'=' * 56}")
    print(f"真机图形界面验收：通过 \033[32m{PASS}\033[0m 项，失败 \033[31m{FAIL}\033[0m 项")
    print(f"截图目录：{out_dir}")
    for item in FAILURES:
        print("  -", item[:200])
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
