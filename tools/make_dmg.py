#!/usr/bin/env python3
"""把打包好的 .app 制作成可分发的 DMG 安装包。

产物：dist/iOSLocationTrack-1.1.0_mac.dmg
内容：应用本身 + Applications 快捷方式（拖进去即安装）+ 中文安装说明

特点：
- UDZO 压缩（zlib），体积最小且兼容性好
- 固定卷名、固定图标位置（打开 DMG 后两个图标位置整齐）
- 自定义背景图（程序化生成，无需设计稿）
- 生成后自动校验挂载与内容

用法：./.venv/bin/python tools/make_dmg.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "虚拟定位轨迹"
VERSION = "1.1.0"
APP = ROOT / "dist" / f"{APP_NAME}.app"
# 产物名带 _mac 后缀，与 Windows 的 _win 对应，Release 页面上一眼能分清平台。
# 用 ASCII 文件名：GitHub Release 会把非 ASCII 字符剥掉（v1.0.0 首次上传时踩过）。
DMG = ROOT / "dist" / f"iOSLocationTrack-{VERSION}_mac.dmg"
VOLUME = APP_NAME
BG_WIDTH, BG_HEIGHT = 600, 400


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def make_background(path: Path) -> None:
    """程序化生成 DMG 背景图（深色，与应用图标风格一致）。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    img = Image.new("RGB", (BG_WIDTH, BG_HEIGHT), (24, 30, 42))
    draw = ImageDraw.Draw(img)
    # 顶部渐变更深一点，营造层次
    for y in range(BG_HEIGHT // 2):
        t = y / (BG_HEIGHT / 2)
        color = (int(24 + 10 * t), int(30 + 12 * t), int(42 + 16 * t))
        draw.line([(0, y), (BG_WIDTH, y)], fill=color)
    # 一条虚线轨迹指向右侧（呼应应用图标）
    for x in range(70, 520, 18):
        draw.line([(x, 300), (x + 9, 300)], fill=(70, 90, 120), width=3)
    # 中间箭头提示"拖过去"
    draw.line([(250, 190), (330, 190)], fill=(120, 150, 190), width=4)
    draw.polygon([(330, 178), (352, 190), (330, 202)], fill=(120, 150, 190))
    img.save(path)


def main() -> int:
    if not APP.exists():
        print(f"未找到 {APP}，请先运行 ./scripts/build_app.sh")
        return 1

    py = sys.executable
    print("\033[1m制作 DMG 安装包\033[0m")
    print(f"  应用：{APP}")
    app_size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file()) / 1048576
    print(f"  应用体积：{app_size:.1f} MB")

    # 注意：/tmp 在系统只读卷上（可用空间很小），而 UDRW 中间镜像需要约 2 倍应用体积，
    # 因此临时目录必须放在数据卷（用户目录）下。
    staging_root = Path.home() / "Library" / "Caches" / "iosloc-dmg"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="build-", dir=str(staging_root)))
    try:
        # 1) 准备素材
        print("\n1) 准备 DMG 内容")
        target_app = staging / f"{APP_NAME}.app"
        shutil.copytree(APP, target_app, symlinks=True)
        # Applications 快捷方式：用户把 app 拖进去即完成安装
        os.symlink("/Applications", staging / "Applications")
        bg = staging / ".background.png"
        make_background(bg)
        readme = staging / "安装说明.txt"
        readme.write_text(
            f"""{APP_NAME} v{VERSION}
========================================

安装：
  把「{APP_NAME}.app」拖到右侧的 Applications 文件夹即可。

首次打开：
  本工具未做 Apple 公证，若提示"来自身份不明的开发者"，
  请在应用上点右键 → 打开 → 再点"打开"。
  首次运行还会请求"控制 Chrome/Edge"权限（用于以应用窗口方式打开界面）。

需要什么：
  · macOS 12 或更高（Apple Silicon / M 系列芯片）
  · 一根数据线 + 一台 iOS 设备（iOS 17 及以上需开启开发者模式并挂载开发者镜像）
  · 不需要安装 Python 或任何依赖，全部已打包在应用内

数据位置：
  ~/Library/Application Support/iOSLocationTrack/
  （配置与地图瓦片缓存；删除该目录即可重置）

完整使用说明见应用内的 README，或访问项目目录下的 README.md
""",
            encoding="utf-8",
        )
        print(f"   已放入：应用 + Applications 快捷方式 + 安装说明 + 背景图")

        # 2) 生成可读写 DMG 以便布局图标
        temp_dmg = staging / "temp.dmg"
        print("\n2) 生成磁盘映像并设置窗口布局")
        result = run([
            "hdiutil", "create", "-srcfolder", str(staging), "-volname", VOLUME,
            "-fs", "HFS+", "-format", "UDRW", "-ov", str(temp_dmg),
        ])
        if result.returncode != 0:
            print("  hdiutil create 失败：", result.stderr[-500:])
            return 1

        mount_point = Path(f"/Volumes/{VOLUME}")
        if mount_point.exists():
            run(["hdiutil", "detach", str(mount_point), "-force"])
        result = run(["hdiutil", "attach", str(temp_dmg), "-readwrite", "-noverify", "-noautoopen"])
        if result.returncode != 0:
            print("  挂载失败：", result.stderr[-400:])
            return 1
        time.sleep(2)

        # 用 AppleScript 设置 Finder 视图（图标位置 / 背景 / 大小）
        script = f'''
        tell application "Finder"
            tell disk "{VOLUME}"
                open
                set current view of container window to icon view
                set toolbar visible of container window to false
                set statusbar visible of container window to false
                set the bounds of container window to {{200, 150, 800, 550}}
                set viewOptions to the icon view options of container window
                set arrangement of viewOptions to not arranged
                set icon size of viewOptions to 110
                try
                    set background picture of viewOptions to file ".background.png"
                end try
                set position of item "{APP_NAME}.app" of container window to {{150, 190}}
                set position of item "Applications" of container window to {{450, 190}}
                set position of item "安装说明.txt" of container window to {{300, 320}}
                update without registering applications
                delay 1
                close
            end tell
        end tell
        '''
        result = run(["osascript", "-e", script])
        if result.returncode != 0:
            print("  布局脚本部分失败（不影响使用）：", result.stderr.strip()[:200])

        # 收尾：确保 Finder 写入的 .DS_Store 落盘
        run(["sync"])
        time.sleep(1)
        detach = run(["hdiutil", "detach", str(mount_point)])
        if detach.returncode != 0:
            run(["hdiutil", "detach", str(mount_point), "-force"])
            time.sleep(1)

        # 3) 压缩成最终 DMG
        print("\n3) 压缩为最终 DMG")
        if DMG.exists():
            DMG.unlink()
        result = run([
            "hdiutil", "convert", str(temp_dmg), "-format", "UDZO",
            "-imagekey", "zlib-level=9", "-o", str(DMG),
        ])
        if result.returncode != 0:
            print("  压缩失败：", result.stderr[-400:])
            return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            shutil.rmtree(staging_root, ignore_errors=True)
        except Exception:
            pass

    # 4) 校验
    print("\n4) 校验 DMG")
    size_mb = DMG.stat().st_size / 1048576
    print(f"  文件：{DMG}")
    print(f"  体积：{size_mb:.1f} MB")
    verify_mount = Path(f"/Volumes/{VOLUME}")
    result = run(["hdiutil", "attach", str(DMG), "-nobrowse", "-readonly", "-noautoopen"])
    ok_mount = result.returncode == 0
    print(f"  挂载：{'成功' if ok_mount else '失败 ' + result.stderr[-200:]}")
    if ok_mount:
        try:
            time.sleep(2)
            contents = sorted(p.name for p in verify_mount.iterdir())
            print(f"  内容：{contents}")
            has_app = (verify_mount / f"{APP_NAME}.app").exists()
            has_link = (verify_mount / "Applications").is_symlink()
            has_readme = (verify_mount / "安装说明.txt").exists()
            binary = verify_mount / f"{APP_NAME}.app" / "Contents" / "MacOS" / "iOSLocationTrack"
            print(f"  应用存在：{has_app} | Applications 快捷方式：{has_link} | 安装说明：{has_readme}")
            if binary.exists():
                arch = run(["lipo", "-archs", str(binary)]).stdout.strip()
                print(f"  可执行架构：{arch}")
            # 校验签名/完整性（未签名时的常规检查）
            quarantine = run(["xattr", "-l", str(verify_mount / f"{APP_NAME}.app")]).stdout
            print(f"  扩展属性：{'有' if quarantine.strip() else '无'}")
        finally:
            run(["hdiutil", "detach", str(verify_mount), "-force"])

    print(f"\n\033[32m完成：{DMG}\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
