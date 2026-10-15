#!/usr/bin/env python3
"""生成应用图标（程序化绘制，无需设计文件）。

设计：
- 深色圆角方块底（与界面深色主题一致），叠加轻微纵向渐变
- 一条虚线轨迹（用点表示途经点），从左上延伸到中心的定位针
- 定位针用「描边 + 内部圆点」造型，16px 下仍然可辨识
- 右下角一枚小圆点表示"当前位置"的脉冲

输出：resources/AppIcon.iconset/*.png + resources/AppIcon.icns
用法：./.venv/bin/python tools/make_icon.py
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("需要 Pillow：./.venv/bin/python -m pip install pillow")
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "resources"
ICONSET = OUT_DIR / "AppIcon.iconset"
ICNS = OUT_DIR / "AppIcon.icns"

# 主题色（与界面一致）
BG_TOP = (26, 36, 52)
BG_BOTTOM = (14, 20, 30)
ACCENT = (59, 130, 246)      # 蓝
ACCENT_HI = (96, 165, 250)
ROUTE = (148, 163, 184)      # 轨迹灰
PIN = (248, 113, 113)        # 定位针红
PIN_HI = (254, 202, 202)

# macOS 图标规范：内容四周留出约 10% 安全边距，圆角半径约 22.37%
SAFE_RATIO = 0.085
CORNER_RATIO = 0.2237


def _bezier(points, steps=200):
    """二次贝塞尔采样（用于画出平滑的轨迹线）。"""
    (x1, y1), (x2, y2), (x3, y3) = points
    out = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        out.append((
            mt * mt * x1 + 2 * mt * t * x2 + t * t * x3,
            mt * mt * y1 + 2 * mt * t * y2 + t * t * y3,
        ))
    return out



def _stroke(draw, points, width, color):
    """用填充多边形画出平滑的粗线（PIL 的宽 line 逐段绘制会出现"肋纹"）。"""
    if len(points) < 2:
        return
    half = width / 2.0
    left, right = [], []
    n = len(points)
    for i, (x, y) in enumerate(points):
        # 用前后点估计切线方向
        px, py = points[max(0, i - 1)]
        nx, ny = points[min(n - 1, i + 1)]
        dx, dy = nx - px, ny - py
        length = math.hypot(dx, dy)
        if length < 1e-9:
            dx, dy, length = 1.0, 0.0, 1.0
        ux, uy = -dy / length, dx / length      # 法线
        left.append((x + ux * half, y + uy * half))
        right.append((x - ux * half, y - uy * half))
    draw.polygon(left + right[::-1], fill=color)
    # 两端补圆头
    r = width / 2.0
    for (ex, ey) in (points[0], points[-1]):
        draw.ellipse((ex - r, ey - r, ex + r, ey + r), fill=color)


def draw_icon(size: int) -> Image.Image:
    """在 size×size 画布上绘制图标（4 倍超采样后缩小，边缘平滑）。"""
    ss = 4
    canvas = size * ss
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))

    margin = int(canvas * SAFE_RATIO)
    box = (margin, margin, canvas - margin, canvas - margin)
    radius = int((box[2] - box[0]) * CORNER_RATIO)
    inner = box[2] - box[0]

    # 1) 圆角底 + 纵向渐变（对角向，更像 macOS 应用图标）
    gradient = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(gradient)
    for y in range(box[1], box[3]):
        t = (y - box[1]) / max(1, box[3] - box[1])
        color = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        gdraw.line([(box[0], y), (box[2], y)], fill=color + (255,))
    mask = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    img.paste(gradient, (0, 0), mask)
    draw = ImageDraw.Draw(img)

    line_w = max(2, int(inner * 0.048))

    # 2) 平滑轨迹线：从左下到右上的贝塞尔曲线（实线 + 途经点圆点，小尺寸下最清晰）
    curve = _bezier([
        (box[0] + inner * 0.14, box[1] + inner * 0.82),
        (box[0] + inner * 0.44, box[1] + inner * 0.76),
        (box[0] + inner * 0.60, box[1] + inner * 0.56),
    ])
    _stroke(draw, curve, line_w, ROUTE + (215,))

    # 途经点小圆（沿轨迹均匀分布，表达"添加途经点"这一核心功能）
    dot_r = line_w * 0.78
    for frac in (0.18, 0.45):
        wx, wy = curve[int(len(curve) * frac)]
        draw.ellipse((wx - dot_r, wy - dot_r, wx + dot_r, wy + dot_r), fill=ACCENT_HI + (255,))

    # 3) 定位针（水滴形 + 白色内圆）
    pin_cx = box[0] + inner * 0.68
    pin_cy = box[1] + inner * 0.38
    pin_r = inner * 0.185
    tip_y = pin_cy + pin_r * 2.16
    # 针身：圆 + 收窄到底部的三角，拼成水滴
    draw.ellipse((pin_cx - pin_r, pin_cy - pin_r, pin_cx + pin_r, pin_cy + pin_r), fill=PIN + (255,))
    draw.polygon([
        (pin_cx - pin_r * 0.60, pin_cy + pin_r * 0.78),
        (pin_cx + pin_r * 0.60, pin_cy + pin_r * 0.78),
        (pin_cx, tip_y),
    ], fill=PIN + (255,))
    # 白色内圆：小尺寸下也能看出是"定位标记"
    hole = pin_r * 0.40
    draw.ellipse((pin_cx - hole, pin_cy - hole, pin_cx + hole, pin_cy + hole), fill=(255, 255, 255, 255))

    # 4) 针下方落点阴影（让针"站"在地图上）
    sh_w = pin_r * 0.95
    sh_h = max(1, int(pin_r * 0.22))
    shadow = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (pin_cx - sh_w, tip_y - sh_h * 0.4, pin_cx + sh_w, tip_y + sh_h * 1.6),
        fill=(0, 0, 0, 90),
    )
    img = Image.alpha_composite(img, shadow)
    draw = ImageDraw.Draw(img)

    # 5) 当前位置：轨迹上的一个高亮圆点（在针的左下侧）
    px, py = curve[int(len(curve) * 0.72)]
    pr = line_w * 0.72
    draw.ellipse((px - pr * 1.8, py - pr * 1.8, px + pr * 1.8, py + pr * 1.8),
                 fill=ACCENT + (60,))
    draw.ellipse((px - pr, py - pr, px + pr, py + pr), fill=ACCENT_HI + (255,))

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(ICONSET, ignore_errors=True)
    ICONSET.mkdir(parents=True, exist_ok=True)

    # iconset 需要的尺寸组合
    specs = [
        (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
    ]
    cache: dict[int, Image.Image] = {}
    for size, name in specs:
        if size not in cache:
            cache[size] = draw_icon(size)
        cache[size].save(ICONSET / name)
    print(f"已生成 {len(specs)} 个尺寸 → {ICONSET}")

    # 预览图（512）
    preview = OUT_DIR / "AppIcon-preview.png"
    cache[512].save(preview)
    print(f"预览图 → {preview}")

    # 合成 icns
    try:
        subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)], check=True)
        print(f"已生成 {ICNS}（{ICNS.stat().st_size // 1024} KB）")
    except Exception as exc:
        print(f"iconutil 失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
