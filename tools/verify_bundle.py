#!/usr/bin/env python3
"""验证打包后的 .app 能否**独立运行**（不依赖项目目录、不依赖 venv、不依赖系统 Python）。

检查项：
1. .app 结构完整（可执行文件、Info.plist、图标、前端资源）
2. 用干净环境启动（清空 PYTHON* 环境变量、不加载 .pth），端口能起来
3. 关键 API 可用：/api/health、/api/devices、/api/settings/tiles、/ 返回前端
4. 前端资源确实打包进去了（index.html / js / css 都能取到）
5. 瓦片代理可用（说明网络与缓存路径都对）
6. tunnel-helper 子命令可执行（真机连接依赖它；打包后不能再依赖 python -m pymobiledevice3）
7. 数据目录写在 ~/Library/Application Support/iOSLocationTrack（只读包外可写）

用法：./.venv/bin/python tools/verify_bundle.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "dist" / "虚拟定位轨迹.app"
BIN = APP / "Contents" / "MacOS" / "iOSLocationTrack"
PORT = 8830

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
    print("\033[1m打包产物独立运行验证\033[0m")
    if not APP.exists():
        print(f"  \033[31m✗ 未找到 {APP}，请先运行 PyInstaller\033[0m")
        return 1

    print("\n1) 包结构")
    check("可执行文件存在", BIN.exists(), str(BIN))
    check("Info.plist 存在", (APP / "Contents" / "Info.plist").exists())
    check("图标已嵌入", (APP / "Contents" / "Resources" / "AppIcon.icns").exists())
    web = APP / "Contents" / "Frameworks" / "web"
    check("前端资源已打包", (web / "index.html").exists(), str(web))
    check("前端 JS 已打包", (web / "js" / "app.js").exists())
    check("前端样式已打包", (web / "style.css").exists())
    size_mb = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file()) / 1048576
    print(f"    包体积：{size_mb:.1f} MB")

    print("\n2) Info.plist 关键字段")
    try:
        plist = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c", "Print", str(APP / "Contents" / "Info.plist")],
            capture_output=True, text=True, timeout=10,
        ).stdout
        check("包名正确", "虚拟定位轨迹" in plist)
        check("Bundle ID 正确", "com.local.ios-location-track" in plist)
        check("最低系统版本已声明", "LSMinimumSystemVersion" in plist)
    except Exception as exc:
        check("读取 Info.plist", False, str(exc)[:80])

    print("\n3) 干净环境启动（不依赖 venv / 项目目录）")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.path.expanduser("~"),
        "LANG": "zh_CN.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        # 明确清掉 Python 相关变量，证明不依赖外部环境
    }
    log = Path("/tmp/bundle-run.log")
    proc = subprocess.Popen(
        [str(BIN), "--mock", "--no-browser", "--window", "none", "--port", str(PORT)],
        env=env, stdout=log.open("wb"), stderr=subprocess.STDOUT,
        cwd="/tmp",  # 故意用 /tmp 作为工作目录，验证不依赖项目目录
        start_new_session=True,
    )
    base = f"http://127.0.0.1:{PORT}"
    ready = False
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.5)
    check("独立启动成功（不依赖 venv）", ready, f"见 {log}")
    if not ready:
        try:
            print("    启动日志：")
            print("   ", log.read_text(encoding="utf-8", errors="replace")[-800:])
        except Exception:
            pass
        proc.terminate()
        print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
        return 1

    try:
        print("\n4) 关键接口")
        with urllib.request.urlopen(base + "/api/health", timeout=5) as resp:
            health = json.loads(resp.read())
        check("GET /api/health", health.get("ok") is True, str(health)[:100])
        check("运行在打包模式（无 venv）", health.get("pymobiledevice3") is True, str(health)[:120])

        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            html = resp.read().decode("utf-8", "replace")
        check("GET / 返回前端页面", "<html" in html.lower() and "虚拟定位" in html, "")

        for path, label in [("/js/app.js", "app.js"), ("/js/map.js", "map.js"), ("/style.css", "style.css")]:
            with urllib.request.urlopen(base + path, timeout=5) as resp:
                body = resp.read()
            check(f"静态资源 {label} 可取", resp.status == 200 and len(body) > 1000,
                  f"{resp.status} {len(body)}B")

        with urllib.request.urlopen(base + "/api/devices", timeout=8) as resp:
            devices = json.loads(resp.read())
        check("GET /api/devices", "devices" in devices, str(devices)[:100])

        with urllib.request.urlopen(base + "/api/settings/tiles", timeout=8) as resp:
            tiles = json.loads(resp.read())
        check("GET /api/settings/tiles", bool(tiles.get("sources")), "")

        with urllib.request.urlopen(base + "/api/tiles/amap/12/3367/1580.png", timeout=25) as resp:
            tile = resp.read()
        check("瓦片代理可用", tile[:4] in (b"\x89PNG", b"\xff\xd8"), f"{len(tile)}B")

        print("\n5) 数据目录（包外可写）")
        data_dir = Path.home() / "Library" / "Application Support" / "iOSLocationTrack"
        check("数据目录已创建", data_dir.is_dir(), str(data_dir))
        check("config.json 已生成", (data_dir / "config.json").exists(), str(data_dir))
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(2)
        except Exception:
            proc.terminate()
        time.sleep(1)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass

    print("\n6) tunnel-helper 子命令（真机连接依赖它）")
    helper = subprocess.run(
        [str(BIN), "tunnel-helper", "--udid", "NONEXISTENT-UDID"],
        env=env, capture_output=True, text=True, timeout=90,
    )
    output = (helper.stdout or "") + (helper.stderr or "")
    # 不存在的设备必然失败，但必须是"能跑起来、给出中文/英文错误"，而不是崩溃或无此命令
    check("tunnel-helper 可执行（非崩溃）",
          helper.returncode in (0, 1, 2) and "Traceback" not in output,
          f"rc={helper.returncode} {output.strip()[:120]}")

    print(f"\n{'=' * 56}")
    print(f"通过 \033[32m{PASS}\033[0m 项，失败 \033[31m{FAIL}\033[0m 项")
    for item in FAILURES:
        print("  -", item)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
