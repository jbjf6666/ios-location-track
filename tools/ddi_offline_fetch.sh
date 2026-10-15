#!/bin/bash
# 离线抓取 DeveloperDiskImage 素材（绕过 raw.githubusercontent.com 的 SNI 阻断）
#
# 背景（2026-10-14 实测，本机 jbjf-Mac-mini）：
#   raw.githubusercontent.com 解析到 4 个 GitHub Pages IP，其中 3 个在 TLS ClientHello 后被 RST / 黑洞：
#     185.199.108.133  -> 连接超时（curl exit 28）
#     185.199.109.133  -> RST（curl exit 35）
#     185.199.110.133  -> RST（curl exit 35）
#     185.199.111.133  -> HTTP 200 正常
#   DNS 轮询会随机选中坏 IP，而 developer_disk_image/repo.py 的 requests.get 没有重试也没有超时，
#   所以单次 RST 就会让 mounter auto-mount 整体失败（ConnectionResetError(54)）。
#
# 本脚本：固定走可用 IP + 重试 + 校验字节数，把素材放到 pymobiledevice3 期望的位置。
# 用法：tools/ddi_offline_fetch.sh [输出目录]   默认输出 /tmp/ddi
set -uo pipefail

OUT="${1:-/tmp/ddi}"
GOOD_IPS=("185.199.111.133" "185.199.108.133" "185.199.109.133" "185.199.110.133")
BASE="https://raw.githubusercontent.com/doronz88/DeveloperDiskImage/main"
CACHE="$HOME/.pymobiledevice3/Xcode_iOS_DDI_Personalized"
PY="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/python"

mkdir -p "$OUT/16.5" "$CACHE"

# fetch <仓库相对路径> <期望字节数> <输出文件>
fetch() {
  local path="$1" want="$2" dest="$3"
  for ip in "${GOOD_IPS[@]}"; do
    echo "--- $path  (期望 $want 字节, 尝试 $ip)"
    if curl -sS -m 300 --retry 3 --retry-all-errors --resolve "raw.githubusercontent.com:443:$ip" \
         -o "$dest.part" -w "    http=%{http_code} size=%{size_download} t=%{time_total}s\n" "$BASE/$path"; then
      local got; got=$(stat -f%z "$dest.part" 2>/dev/null || echo 0)
      if [ "$got" = "$want" ]; then mv "$dest.part" "$dest"; echo "    OK ($ip)"; return 0; fi
      echo "    字节数不符 ($got != $want)，换 IP"
    fi
  done
  echo "!!! $path 全部 IP 失败"; return 1
}

# iOS 17+（personalized）素材 —— 例如 iPhone 11 / iOS 26.6.1
fetch "PersonalizedImages/Xcode_iOS_DDI_Personalized/Image.dmg"             15733248 "$OUT/Image.dmg"
fetch "PersonalizedImages/Xcode_iOS_DDI_Personalized/BuildManifest.plist"     801505 "$OUT/BuildManifest.plist"
fetch "PersonalizedImages/Xcode_iOS_DDI_Personalized/Image.dmg.trustcache"      1895 "$OUT/Image.dmg.trustcache"

# iOS <17（经典 developer image）素材 —— 例如 iOS 16.5
fetch "DeveloperDiskImages/16.5/DeveloperDiskImage.dmg"           7427565 "$OUT/16.5/DeveloperDiskImage.dmg"
fetch "DeveloperDiskImages/16.5/DeveloperDiskImage.dmg.signature"     128 "$OUT/16.5/DeveloperDiskImage.dmg.signature"

# 写进 pymobiledevice3 缓存：fetch_personalized_ddi() 会直接命中，彻底不再联网
# 注意缓存里的 trustcache 文件名必须是 Image.trustcache（不是 Image.dmg.trustcache）
cp "$OUT/BuildManifest.plist"    "$CACHE/BuildManifest.plist"
cp "$OUT/Image.dmg"              "$CACHE/Image.dmg"
cp "$OUT/Image.dmg.trustcache"   "$CACHE/Image.trustcache"

echo
echo "=== 校验缓存（ProductBuildVersion 必须等于 pymobiledevice3 的 LATEST_DDI_BUILD_ID）==="
"$PY" - <<'PY'
import plistlib, pathlib
from pymobiledevice3.services.mobile_image_mounter import LATEST_DDI_BUILD_ID, fetch_personalized_ddi
c = pathlib.Path.home()/".pymobiledevice3"/"Xcode_iOS_DDI_Personalized"
bm = plistlib.loads((c/"BuildManifest.plist").read_bytes())
print("缓存 BuildVersion :", bm["ProductBuildVersion"])
print("期望 LATEST_DDI   :", LATEST_DDI_BUILD_ID)
print("缓存有效(不会重下):", bm["ProductBuildVersion"] == LATEST_DDI_BUILD_ID)
PY

echo
echo "=== 接下来可以直接离线挂载（不再触碰 GitHub）==="
echo "  # iOS 17+ 设备（personalized，走缓存）"
echo "  ./.venv/bin/python -m pymobiledevice3 mounter auto-mount --udid <UDID>"
echo "  # iOS <17 设备（经典镜像，显式指定本地文件）"
echo "  ./.venv/bin/python -m pymobiledevice3 mounter mount-developer \\"
echo "      $OUT/16.5/DeveloperDiskImage.dmg $OUT/16.5/DeveloperDiskImage.dmg.signature --udid <UDID>"
