#!/usr/bin/env python3
"""批量抓取各 iOS 版本的开发者镜像，供打包进应用（离线可用）。

镜像来源：doronz88/DeveloperDiskImage（pymobiledevice3 官方使用的仓库）

两类镜像：
1. **经典镜像**（iOS 16 及更早）：每个系统版本一套
   `DeveloperDiskImage.dmg` + `DeveloperDiskImage.dmg.signature`
2. **个性化镜像**（iOS 17 及以后，含 27.x）：**全系共用一套**
   `Image.dmg` + `BuildManifest.plist` + `Image.dmg.trustcache`
   实际签名在挂载时由 Apple TSS 按设备在线生成，因此不需要逐版本素材。

注意：raw.githubusercontent.com 在国内会被 SNI 阻断（4 个 IP 里只有部分可用），
所以这里逐个 IP 直连重试，并支持 jsDelivr 镜像兜底。

用法：
    ./.venv/bin/python tools/fetch_ddi.py                # 下载全部（16.0-16.7 + 个性化）
    ./.venv/bin/python tools/fetch_ddi.py --only 16.5    # 只下一个版本
    ./.venv/bin/python tools/fetch_ddi.py --check        # 只检查已有文件完整性
"""
from __future__ import annotations

import argparse
import http.client
import json
import shutil
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "resources" / "ddi"

REPO = "doronz88/DeveloperDiskImage"
BRANCH = "main"
# 实测：.111 / .108 可达，.109 / .110 是黑洞（先试可达的）
RAW_IPS = ["185.199.111.133", "185.199.108.133", "185.199.109.133", "185.199.110.133"]

# 经典镜像：iOS 16 全系（仓库里 16.x 到此为止）
CLASSIC_VERSIONS = ["16.0", "16.1", "16.2", "16.3", "16.4", "16.5", "16.6", "16.7"]
CLASSIC_FILES = ["DeveloperDiskImage.dmg", "DeveloperDiskImage.dmg.signature"]

# 个性化镜像：iOS 17+ 全系共用
PERSONALIZED_DIR = "PersonalizedImages/Xcode_iOS_DDI_Personalized"
PERSONALIZED_FILES = ["Image.dmg", "BuildManifest.plist", "Image.dmg.trustcache"]
PERSONALIZED_OUT = "personalized"

# 各文件的最小合理体积（用于判断下载是否完整）
MIN_SIZE = {
    "DeveloperDiskImage.dmg": 5 * 1024 * 1024,
    "DeveloperDiskImage.dmg.signature": 64,
    "Image.dmg": 10 * 1024 * 1024,
    "BuildManifest.plist": 100 * 1024,
    "Image.dmg.trustcache": 256,
}


CONNECT_TIMEOUT = 6.0     # TCP 建连超时：坏 IP 是黑洞，必须快速放弃
READ_TIMEOUT = 300.0      # 读数据超时：大文件（15MB）要留足时间


def _pipe(sock, host: str, path: str, timeout: float) -> bytes | None:
    """在已建立的 TCP 连接上完成 TLS + HTTP GET。"""
    context = ssl.create_default_context()
    tls = context.wrap_socket(sock, server_hostname=host)
    try:
        tls.settimeout(timeout)
        request = (
            f"GET /{REPO}/{BRANCH}/{path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: Mozilla/5.0\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        )
        tls.sendall(request.encode())
        chunks: list[bytes] = []
        while True:
            chunk = tls.recv(262144)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        try:
            tls.close()
        except Exception:
            pass
    if not raw:
        return None
    head, _, body = raw.partition(b"\r\n\r\n")
    if b"200" not in head.split(b"\r\n")[0]:
        return None
    return body


def fetch_via_ip(path: str, timeout: float = READ_TIMEOUT) -> bytes | None:
    """直连 GitHub Pages IP 拉取（绕过被阻断的 DNS 轮询）。

    关键：**先做一次短超时的 TCP 建连测试**。实测坏的 IP 是黑洞，
    HTTPSConnection 的 timeout 管不住 TCP 连接阶段，会挂 75 秒以上，
    导致整个下载流程看起来"卡死"。
    """
    import socket

    for ip in RAW_IPS:
        try:
            sock = socket.create_connection((ip, 443), timeout=CONNECT_TIMEOUT)
        except Exception:
            print(f"      （{ip} 连接超时，跳过）")
            continue
        try:
            data = _pipe(sock, "raw.githubusercontent.com", path, timeout)
            if data:
                return data
        except Exception as exc:
            print(f"      （{ip} 传输失败：{exc.__class__.__name__}）")
            continue
        finally:
            try:
                sock.close()
            except Exception:
                pass
    return None


def fetch_via_jsdelivr(path: str, timeout: float = 180.0) -> bytes | None:
    """jsDelivr CDN 兜底（国内通常可达）。"""
    url = f"https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}/{path}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            return response.read()
    except Exception:
        return None


def download(rel_path: str, dest: Path, force: bool = False) -> bool:
    name = dest.name
    minimum = MIN_SIZE.get(name, 100)
    if dest.exists() and dest.stat().st_size >= minimum and not force:
        print(f"    已有（{dest.stat().st_size / 1048576:.2f} MB），跳过")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = fetch_via_ip(rel_path)
    source = "GitHub IP"
    if not data:
        print("    GitHub 直连失败，改用 jsDelivr…")
        data = fetch_via_jsdelivr(rel_path)
        source = "jsDelivr"
    if not data or len(data) < minimum:
        print(f"    ❌ 下载失败（{len(data) if data else 0} 字节，期望 ≥ {minimum}）")
        return False
    dest.write_bytes(data)
    print(f"    ✅ {len(data) / 1048576:.2f} MB（{source}）")
    return True


def verify() -> int:
    """检查镜像完整性。

    **必备**：个性化镜像（iOS 17+ 全系，含 27.x）。
    **可选**：iOS 16.x 经典镜像 —— Apple 按系统构建号签发，社区镜像在多数新构建上
    会被设备拒签（实测 16.5/20F66 报 Failed to verify the disk image signature），
    因此不作为必备项，缺失只提示。
    """
    print("\033[1m检查镜像完整性\033[0m")
    required_total = 0
    required_missing: list[str] = []
    print("  【必备】个性化镜像（iOS 17 及以上全系共用）")
    for name in PERSONALIZED_FILES:
        path = OUT / PERSONALIZED_OUT / name
        ok = path.exists() and path.stat().st_size >= MIN_SIZE[name]
        if ok:
            required_total += path.stat().st_size
        else:
            required_missing.append(f"{PERSONALIZED_OUT}/{name}")
        size = f"{path.stat().st_size / 1048576:.2f} MB" if path.exists() else "缺失"
        print(f"    {'✅' if ok else '❌'} {name:35s} {size}")

    optional_total = 0
    optional_found: list[str] = []
    print("  【可选】iOS 16.x 经典镜像（构建号匹配时才有用）")
    for version in CLASSIC_VERSIONS:
        directory = OUT / version
        have = all(
            (directory / name).exists() and (directory / name).stat().st_size >= MIN_SIZE[name]
            for name in CLASSIC_FILES
        )
        if have:
            optional_found.append(version)
            optional_total += sum((directory / name).stat().st_size for name in CLASSIC_FILES)
        print(f"    {'✅' if have else '·'} {version}")

    print(f"\n  必备素材合计 {required_total / 1048576:.1f} MB"
          f"｜可选经典镜像 {len(optional_found)}/{len(CLASSIC_VERSIONS)} 个版本、{optional_total / 1048576:.1f} MB")
    if required_missing:
        print(f"  \033[31m必备素材缺失 {len(required_missing)} 个：{', '.join(required_missing)}\033[0m")
        return 1
    print("  \033[32m必备素材齐全（iOS 17 及以上可离线挂载）\033[0m")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None, help="只下载指定版本（如 16.5）")
    parser.add_argument("--check", action="store_true", help="只检查完整性")
    parser.add_argument("--force", action="store_true", help="强制重新下载")
    parser.add_argument("--personalized-only", action="store_true", help="只下载 iOS 17+ 共用镜像")
    parser.add_argument("--classic", action="store_true",
                        help="额外下载 iOS 16.x 经典镜像（注意：Apple 已按构建号签发，多数新构建会被拒签）")
    args = parser.parse_args()

    if args.check:
        return verify()

    print("\033[1m下载开发者镜像到应用内（供离线使用）\033[0m")
    print(f"  输出目录：{OUT}\n")

    ok_all = True
    if not args.personalized_only and (args.only or args.classic):
        versions = [args.only] if args.only else CLASSIC_VERSIONS
        for version in versions:
            print(f"[经典镜像] iOS {version}")
            for name in CLASSIC_FILES:
                rel = f"DeveloperDiskImages/{version}/{name}"
                if not download(rel, OUT / version / name, args.force):
                    ok_all = False

    if not args.only:
        print("[个性化镜像] iOS 17+ 全系共用（含 27.x）")
        for name in PERSONALIZED_FILES:
            rel = f"{PERSONALIZED_DIR}/{name}"
            if not download(rel, OUT / PERSONALIZED_OUT / name, args.force):
                ok_all = False

    print()
    verify()
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
