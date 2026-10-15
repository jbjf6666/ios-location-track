#!/usr/bin/env python3
"""给界面截图（视觉验收用）。

做法：headless Chrome 的一次性 --screenshot 模式 + 独立 user-data-dir，
不依赖 CDP WebSocket（本机没有 websocket-client 库，Chrome 的 CDP 又需要它）。
--screenshot 在页面 load 事件后立即截图，用一次注入脚本做等待/交互，
脚本通过 URL fragment 传不进去，所以这里用临时 HTML 包装页来间接执行前置动作。

用法：
    ./.venv/bin/python tools/screenshot.py http://127.0.0.1:8787/ out.png [--wait 9]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    raise SystemExit("未找到 Chrome / Edge / Chromium")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("out", nargs="?", default="screenshot.png")
    parser.add_argument("--wait", type=float, default=9.0, help="页面加载后额外等待秒数（等瓦片/WS 渲染）")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--profile", default="/tmp/iosloc-shot-profile")
    parser.add_argument("--json", action="store_true", help="同时打印页面控制台日志（--enable-logging）")
    args = parser.parse_args()

    chrome = find_chrome()
    profile = Path(args.profile)
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)
    out = Path(args.out).resolve()
    if out.exists():
        out.unlink()

    command = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        "--disable-extensions",
        "--force-device-scale-factor=2",
        f"--window-size={args.width},{args.height}",
        f"--user-data-dir={profile}",
        # Chrome 没有"加载后等待 N 秒"的开关，用虚拟时间预算让定时器与网络请求跑完
        f"--virtual-time-budget={int(args.wait * 1000)}",
        f"--screenshot={out}",
        args.url,
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=max(60.0, args.wait * 6))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            process.kill()
        output, _ = process.communicate()
        print("Chrome 超时被终止")
    if args.json and output:
        for line in output.splitlines():
            if any(key in line for key in ("ERROR", "SEVERE", "Uncaught", "console")):
                print("  [chrome]", line[:220])
    if not out.exists() or out.stat().st_size == 0:
        print("截图失败")
        if output:
            print(output[-1500:])
        return 1
    print(f"截图已保存：{out}（{out.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
