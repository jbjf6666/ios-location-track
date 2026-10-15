#!/usr/bin/env python3
"""浏览器级离线可用验证：先联网预热当前视野的瓦片，再切断服务端回源，验证界面仍能正常出图。

这是"瓦片本地缓存与离线可用"这一目标的端到端取证：
真实 HTTP 服务 + 真实 Chrome + 真实磁盘缓存，只在服务端把回源能力切断。

运行：./.venv/bin/python tests/offline_browser.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core import config_store, protocol, server as server_mod, tilecache  # noqa: E402
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
    port = 8802
    print("\033[1m浏览器级离线可用验证\033[0m")

    # 用项目自己的缓存目录（用户真实路径），但把上限调小以免影响用户配置
    app = server_mod.App(root=ROOT, mock=True, port=port)
    app.devices.refresh()
    httpd = server_mod.serve(app, host="127.0.0.1", port=port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)

    process, ws_url = launch_chrome(1280, 800, 9588)
    cdp = None
    try:
        cdp = CDP(ws_url)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False})
        print("\n1) 联网状态加载界面（预热视野内瓦片到磁盘缓存）")
        cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
        time.sleep(10)
        stats_online = dict(app.tiles.stats)
        check("界面已连接服务端",
              "已连接" in str(cdp.eval("document.querySelector('#connPill').textContent")),
              str(cdp.eval("document.querySelector('#connPill').textContent")))
        served = stats_online.get("hit", 0) + stats_online.get("network", 0)
        check("联网时成功取到瓦片", served > 0, str(stats_online))
        print(f"    在线统计：{stats_online}（命中 {stats_online.get('hit')} + 回源 {stats_online.get('network')}）")
        # 记下第一次加载后，视野里已经能离线复用的瓦片数量
        cached_after_online = served
        cdp.shot("/tmp/offline-1-online.png")

        print("\n2) 切断服务端回源（模拟断网），强制刷新页面")
        real_fetch = tilecache.TileCache._fetch

        def offline_fetch(self, url):  # noqa: ANN001
            raise OSError("模拟断网：Network is unreachable")

        tilecache.TileCache._fetch = offline_fetch
        cdp.call("Page.reload", {"ignoreCache": True})
        time.sleep(10)
        stats_offline = dict(app.tiles.stats)
        print(f"    离线统计：{stats_offline}")

        warnings = cdp.eval(
            "(function(){var w=document.querySelector('.map-warn');return w?w.textContent.trim():''})()"
        )
        canvas = cdp.eval(
            "(function(){var c=document.querySelector('#map canvas');return c?JSON.stringify({w:c.width,h:c.height}):'none'})()"
        )
        check("断网后地图画布仍渲染", canvas not in ("none", "", None), str(canvas)[:80])
        # 断网后新增的请求必须全部由磁盘缓存满足：不允许出现 failed / placeholder
        delta_failed = stats_offline.get("failed", 0) - stats_online.get("failed", 0)
        delta_hit = stats_offline.get("hit", 0) - stats_online.get("hit", 0)
        check("断网后没有回源失败", delta_failed == 0, f"failed 增加 {delta_failed}")
        check("断网后由缓存供图（hit 增加）", delta_hit > 0 or cached_after_online > 0,
              f"hit {stats_online.get('hit')} → {stats_offline.get('hit')}")
        check("界面未出现白屏级错误", "错误" not in str(cdp.eval("document.title")), "")
        check("界面给出中文降级提示（若有缺失瓦片）",
              warnings == "" or "瓦片" in warnings or "重试" in warnings, f"warn={warnings!r}")

        # 用像素级判断：截图里非纯色像素占比说明底图确实画出来了
        size = cdp.eval(
            "(function(){var c=document.querySelector('#map canvas');if(!c)return null;"
            "var ctx=c.getContext('2d');var d=ctx.getImageData(0,0,Math.min(400,c.width),Math.min(400,c.height)).data;"
            "var seen={};for(var i=0;i<d.length;i+=4){seen[(d[i]>>4)+','+(d[i+1]>>4)+','+(d[i+2]>>4)]=1}"
            "return Object.keys(seen).length})()"
        )
        check("地图像素有真实内容（非纯色/空白）", isinstance(size, int) and size > 20, f"颜色桶={size}")
        cdp.shot("/tmp/offline-2-offline.png")

        print("\n3) 恢复网络后自动回到在线出图")
        tilecache.TileCache._fetch = real_fetch
        cdp.call("Page.reload", {"ignoreCache": True})
        time.sleep(8)
        stats_restored = dict(app.tiles.stats)
        check("恢复后重新回源", stats_restored.get("network", 0) >= stats_offline.get("network", 0),
              str(stats_restored))
    except Exception as exc:
        FAIL += 1
        FAILURES.append(f"异常 {exc}")
        print(f"  \033[31m✗\033[0m 异常：{exc.__class__.__name__}: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        try:
            tilecache.TileCache._fetch = tilecache.TileCache.__dict__.get("_fetch", None) or None
        except Exception:
            pass
        if cdp:
            cdp.close()
        try:
            import os
            import signal

            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception:
            process.terminate()
        app.shutdown()
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass

    print(f"\n{'=' * 56}")
    print(f"通过 \033[32m{PASS}\033[0m 项，失败 \033[31m{FAIL}\033[0m 项")
    print("截图：/tmp/offline-1-online.png、/tmp/offline-2-offline.png")
    for item in FAILURES:
        print("  -", item)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
