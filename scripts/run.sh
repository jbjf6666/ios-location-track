#!/usr/bin/env bash
# iOS 虚拟定位轨迹工具 — 一键启动脚本
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

# 国内网络：默认走阿里云镜像（可用 IOSLOC_PIP_INDEX 覆盖）
PIP_INDEX="${IOSLOC_PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }
fail() { printf '\033[31m  %s\033[0m\n' "$1"; }

bold "iOS 虚拟定位轨迹工具"
info "目录：$ROOT"

# ---------- 1. Python ----------
if [ ! -x "$PY" ]; then
  PYTHON_BIN=""
  for candidate in python3.12 python3.13 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  done
  if [ -z "$PYTHON_BIN" ]; then
    fail "未找到 python3，请先安装 Python 3.11+（推荐：brew install python@3.12）"
    exit 1
  fi
  info "创建虚拟环境（$PYTHON_BIN）…"
  "$PYTHON_BIN" -m venv "$VENV"
fi
info "Python：$("$PY" -V 2>&1)"

# ---------- 2. 依赖 ----------
need_install=0
if ! "$PY" -c "import pymobiledevice3" >/dev/null 2>&1; then
  need_install=1
fi
if [ "$need_install" = "1" ]; then
  bold "首次运行：安装依赖（约 1-3 分钟，走国内镜像）"
  if ! "$PY" -m pip install --quiet --disable-pip-version-check -i "$PIP_INDEX" pymobiledevice3; then
    warn "国内镜像失败，改用官方源重试…"
    if ! "$PY" -m pip install --quiet --disable-pip-version-check pymobiledevice3; then
      fail "依赖安装失败。请手动执行："
      fail "  $PY -m pip install -i $PIP_INDEX pymobiledevice3"
      exit 1
    fi
  fi
  info "依赖安装完成"
fi
info "pymobiledevice3：$("$PY" -c "import pymobiledevice3,sys;print('已就绪')" 2>/dev/null || echo '不可用')"

# ---------- 3. 设备提示 ----------
if [ "${1:-}" != "--mock" ] && [ "${IOSLOC_MOCK:-0}" != "1" ]; then
  DEV_COUNT="$("$PY" - <<'EOF' 2>/dev/null || echo 0
import asyncio
try:
    from pymobiledevice3.usbmux import list_devices
    print(len(asyncio.run(list_devices())))
except Exception:
    print(0)
EOF
)"
  if [ "$DEV_COUNT" = "0" ]; then
    warn "未检测到 iOS 设备：请用数据线连接、解锁设备，并在手机上点「信任」"
  else
    info "检测到 $DEV_COUNT 台 iOS 设备"
  fi
  if [ "$(id -u)" != "0" ]; then
    info "提示：iOS 17 及以上需要 RSD 隧道，程序会优先使用免 root 的 native 隧道；"
    info "      若失败，可改用管理员模式：sudo ./scripts/run.sh"
  fi
fi

# ---------- 4. 启动 ----------
exec "$PY" "$ROOT/main.py" "$@"
