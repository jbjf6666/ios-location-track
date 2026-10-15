#!/usr/bin/env bash
# 在终端里用真实设备跑一次「单点虚拟定位」，用于快速验证设备链路是否打通。
#
# 用法：
#   ./scripts/test-location.sh                       # 默认定位到北京天安门
#   ./scripts/test-location.sh 39.9088 116.3975      # 指定纬度 经度
#   ./scripts/test-location.sh --clear               # 让设备恢复真实定位
set -euo pipefail
cd "$(dirname "$0")/.."
PY=".venv/bin/python"

if [ "${1:-}" = "--clear" ]; then
  exec "$PY" tools/probe_location.py --clear
fi

LAT="${1:-39.9088}"
LNG="${2:-116.3975}"
exec "$PY" tools/probe_location.py --lat "$LAT" --lon "$LNG"
