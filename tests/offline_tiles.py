#!/usr/bin/env python3
"""瓦片离线可用验证：回源成功后断网，验证缓存仍能供图。

做法：用真实的 core.tilecache.TileCache，先把 urllib 换成"永远失败"的实现（模拟断网），
再请求已经缓存过的瓦片，断言仍然返回 200 + 合法图片；同时断言未缓存过的瓦片会优雅降级
（返回占位图 + status != 200，由上层回 404 让前端走重试）。

运行：./.venv/bin/python tests/offline_tiles.py
"""
from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config_store, tilecache  # noqa: E402

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
    print("\033[1m瓦片本地缓存 / 离线可用验证\033[0m")
    cache_dir = Path("/tmp/iosloc-offline-tiles")
    import shutil

    shutil.rmtree(cache_dir, ignore_errors=True)

    source_key = "amap"
    source = config_store.DEFAULT_TILE_SOURCES[source_key]
    # 北京天安门附近、z12 —— 本机实测可达
    z, x, y = 12, 3367, 1580

    cache = tilecache.TileCache(cache_dir, max_mb=64)

    print("\n1) 联网回源并落盘")
    online = cache.get_tile(source_key, source, z, x, y)
    check("回源返回真实图片", online.status == 200 and online.body[:4] in (b"\x89PNG", b"\xff\xd8"),
          f"status={online.status} source={online.source}")
    check("标记为 network", online.source == "network", online.source)
    check("已写入磁盘缓存", any(cache_dir.rglob("*")), "缓存目录为空")

    print("\n2) 断网（把 urllib 换成永远失败）后请求同一瓦片")
    real_urlopen = urllib.request.urlopen

    def offline(*_args, **_kwargs):
        raise OSError("模拟断网：Network is unreachable")

    urllib.request.urlopen = offline
    try:
        cached = cache.get_tile(source_key, source, z, x, y)
        check("离线仍返回 200", cached.status == 200, f"status={cached.status}")
        check("来源为 cache", cached.source == "cache", cached.source)
        check("图片内容与联网时一致", cached.body == online.body,
              f"{len(cached.body)}B vs {len(online.body)}B")

        print("\n3) 断网 + 未缓存瓦片 → 优雅降级（不抛异常）")
        t0 = time.time()
        miss = cache.get_tile(source_key, source, z, x, y + 1)
        elapsed = time.time() - t0
        check("未命中不抛异常", True, "")
        check("返回占位图且 status!=200（上层回 404 让前端重试）",
              miss.status != 200 and miss.body[:4] == b"\x89PNG",
              f"status={miss.status} source={miss.source}")
        check("失败快速返回（<5s）", elapsed < 5.0, f"{elapsed:.2f}s")

        print("\n4) 缓存统计")
        stats = cache.stats
        check("命中计数增加", stats["hit"] >= 1, str(stats))
        check("统计到离线失败", stats["failed"] >= 1, str(stats))
    finally:
        urllib.request.urlopen = real_urlopen

    print("\n5) 恢复网络后仍可正常回源")
    cache2 = tilecache.TileCache(cache_dir, max_mb=64)
    other = cache2.get_tile(source_key, source, z, x + 1, y)
    check("恢复后回源成功", other.status == 200 and other.body[:4] in (b"\x89PNG", b"\xff\xd8"),
          f"status={other.status} source={other.source}")

    print("\n6) 容量上限淘汰不破坏可用性")
    small = tilecache.TileCache(cache_dir, max_mb=1)
    small._sweep()
    check("淘汰后仍能读取", small.get_tile(source_key, source, z, x, y).status in (200, 404))

    shutil.rmtree(cache_dir, ignore_errors=True)
    print(f"\n{'=' * 56}")
    print(f"通过 \033[32m{PASS}\033[0m 项，失败 \033[31m{FAIL}\033[0m 项")
    for item in FAILURES:
        print("  -", item)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
