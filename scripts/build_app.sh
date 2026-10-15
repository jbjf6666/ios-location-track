#!/usr/bin/env bash
# 一步完成：生成图标 → 打包独立 .app → ad-hoc 签名 → 制作 DMG → 验证
# 产物：dist/虚拟定位轨迹.app（独立运行）、dist/虚拟定位轨迹-<版本>.dmg（分发用）
set -euo pipefail
cd "$(dirname "$0")/.."
PY=".venv/bin/python"
VERSION="1.0.0"
APP="dist/虚拟定位轨迹.app"
DMG="dist/虚拟定位轨迹-${VERSION}.dmg"

if [ ! -x "$PY" ]; then
  echo "未找到 $PY，请先运行 ./scripts/run.sh 完成依赖安装"
  exit 1
fi

# 1) 依赖：PyInstaller（打包用）
if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
  echo "安装 PyInstaller…"
  "$PY" -m pip install --quiet --disable-pip-version-check \
    -i "${IOSLOC_PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}" pyinstaller
fi

# 2) 图标
if [ ! -f resources/AppIcon.icns ]; then
  echo "生成图标…"
  "$PY" tools/make_icon.py
fi

# 3) 开发者镜像素材（iOS 17+ 全系共用；体积大，不入 git，首次构建时下载）
if [ ! -f resources/ddi/personalized/Image.dmg ]; then
  echo "下载 iOS 17+ 个性化镜像素材（约 16MB，仅需一次）…"
  "$PY" tools/fetch_ddi.py || {
    echo "素材下载失败。可手动放到 resources/ddi/personalized/ 后重试："
    echo "  Image.dmg / BuildManifest.plist / Image.dmg.trustcache"
    exit 1
  }
fi

# 4) 打包
echo "打包 .app（约 2-4 分钟）…"
"$PY" -m PyInstaller --clean --noconfirm --log-level WARN Location.spec

# 5) ad-hoc 签名：Apple Silicon 上未签名的 arm64 应用可能被系统直接拒绝运行
echo "ad-hoc 签名…"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP" && echo "  签名校验通过"

# 6) 制作 DMG
echo
"$PY" tools/make_dmg.py

# 7) 验证独立运行
echo
"$PY" tools/verify_bundle.py

echo
echo "================================================"
echo "完成："
echo "  独立应用：$APP"
echo "  分发镜像：$DMG"
echo "================================================"
