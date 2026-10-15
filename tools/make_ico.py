#!/usr/bin/env python3
"""由 macOS 侧已有的 PNG 图标集生成 Windows 用的多尺寸 .ico。

macOS 用 ``iconutil`` 产出 .icns；Windows 需要 .ico，用 Pillow 直接合成即可。
输入：resources/AppIcon.iconset/ 下的 PNG（16/32/64/128/256/512/1024）
输出：resources/AppIcon.ico

用法：
    .venv\\Scripts\\python.exe tools\\make_ico.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICONSET = ROOT / "resources" / "AppIcon.iconset"
OUTPUT = ROOT / "resources" / "AppIcon.ico"
PREVIEW = ROOT / "resources" / "AppIcon-256.png"

#: Windows 资源管理器/任务栏实际会用到的尺寸
SIZES = [16, 24, 32, 48, 64, 128, 256]

#: 每个目标尺寸优先使用的源文件（按分辨率从高到低找，避免放大糊掉）
CANDIDATES = {
    16: ["icon_16x16.png", "icon_16x16@2x.png", "icon_32x32.png"],
    24: ["icon_32x32.png", "icon_16x16@2x.png"],
    32: ["icon_32x32.png", "icon_32x32@2x.png", "icon_64x64.png"],
    48: ["icon_128x128.png", "icon_32x32@2x.png"],
    64: ["icon_64x64.png", "icon_32x32@2x.png", "icon_128x128.png"],
    128: ["icon_128x128.png", "icon_128x128@2x.png", "icon_256x256.png"],
    256: ["icon_256x256.png", "icon_256x256@2x.png", "icon_512x512.png"],
}

#: 兜底：任意大图，用 Pillow 缩放
FALLBACKS = ["icon_512x512@2x.png", "icon_512x512.png", "icon_256x256@2x.png",
             "icon_256x256.png", "icon_128x128@2x.png", "icon_128x128.png"]


def main() -> int:
    try:
        from PIL import Image
    except ImportError:
        print("需要 Pillow：.venv\\Scripts\\python.exe -m pip install pillow")
        return 1

    if not ICONSET.is_dir():
        print(f"找不到图标源目录：{ICONSET}")
        return 1

    available = {p.name: p for p in ICONSET.glob("*.png")}
    if not available:
        print(f"{ICONSET} 下没有 PNG")
        return 1

    fallback = next((ICONSET / n for n in FALLBACKS if n in available), None)
    if fallback is None:
        fallback = sorted(available.values(), key=lambda p: p.stat().st_size)[-1]

    images = []
    for size in SIZES:
        chosen = next((ICONSET / n for n in CANDIDATES[size] if n in available), None)
        source = chosen or fallback
        try:
            with Image.open(source) as img:
                img = img.convert("RGBA")
                if img.size != (size, size):
                    img = img.resize((size, size), Image.LANCZOS)
                images.append(img.copy())
            print(f"  {size:>3}x{size:<3} ← {source.name}")
        except Exception as exc:
            print(f"  {size:>3}x{size:<3} 失败：{exc}")

    if not images:
        print("没有生成任何尺寸")
        return 1

    # 以最大的一张作为基准，sizes= 里附上其余尺寸
    largest = max(images, key=lambda i: i.size[0])
    largest.save(
        OUTPUT,
        format="ICO",
        sizes=[img.size for img in images],
        append_images=[img for img in images if img is not largest],
    )
    print(f"\n已生成：{OUTPUT}（{OUTPUT.stat().st_size/1024:.1f} KB，{len(images)} 个尺寸）")

    # 顺便导出一张 PNG 供 Inno Setup 的向导图/文档使用
    largest.resize((256, 256), Image.LANCZOS).save(PREVIEW)
    print(f"已生成：{PREVIEW}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
