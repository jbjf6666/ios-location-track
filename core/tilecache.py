"""地图瓦片本地缓存代理。

- 前端只请求 /api/tiles/{source}/{z}/{x}/{y}.png，由本模块回源到瓦片服务商并落盘缓存。
- 缓存目录可配置（默认 ~/.ios-location-tiles），加载过的区域在断网时仍可显示。
- 无第三方依赖：urllib 回源 + 严格锁保证并发安全。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import protocol

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}

# 1x1 透明 PNG，回源彻底失败时返回，避免前端整块报错
PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f0300050001a5f645ea0000000049454e44ae426082"
)


@dataclass
class TileResult:
    body: bytes
    content_type: str
    status: int
    source: str  # "cache" | "network" | "placeholder" | "error"

    @property
    def ok(self) -> bool:
        return self.status == 200


class TileCache:
    def __init__(self, cache_dir: str | os.PathLike[str], max_mb: int = 1024, timeout: float = 12.0) -> None:
        self.dir = Path(cache_dir).expanduser()
        self.max_bytes = max(64, int(max_mb)) * 1024 * 1024
        self.timeout = timeout
        self._lock = threading.RLock()
        self._last_sweep = 0.0
        self._inflight: dict[str, threading.Lock] = {}
        self.stats = {"hit": 0, "miss": 0, "network": 0, "failed": 0, "placeholder": 0}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # ---------------- 内部 ----------------
    def _path_for(self, source: str, z: int, x: int, y: int, ext: str = ".png") -> Path:
        return self.dir / source / str(z) / str(x) / f"{y}{ext}"

    def _find_cached(self, source: str, z: int, x: int, y: int) -> Path | None:
        for ext in (".png", ".jpg", ".webp"):
            candidate = self._path_for(source, z, x, y, ext)
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

    def _write(self, source: str, z: int, x: int, y: int, ext: str, body: bytes) -> Path:
        path = self._path_for(source, z, x, y, ext)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(body)
            os.replace(tmp, path)
        return path

    def _sweep(self) -> None:
        """超过容量上限时按最后访问时间淘汰旧瓦片。"""
        if self.max_bytes <= 0:
            return
        with self._lock:
            now = time.time()
            if now - self._last_sweep < 60:
                return
            self._last_sweep = now
            entries: list[tuple[float, int, Path]] = []
            total = 0
            for root, _dirs, files in os.walk(self.dir):
                for name in files:
                    if not name.endswith((".png", ".jpg", ".webp")):
                        continue
                    path = Path(root) / name
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    total += st.st_size
                    entries.append((st.st_atime, st.st_size, path))
            if total <= self.max_bytes:
                return
            entries.sort(key=lambda item: item[0])
            target = total - int(self.max_bytes * 0.85)
            freed = 0
            for _atime, size, path in entries:
                if freed >= target:
                    break
                try:
                    path.unlink()
                    freed += size
                except OSError:
                    continue

    def _lock_for(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._inflight.get(key)
            if lock is None:
                lock = threading.Lock()
                self._inflight[key] = lock
            return lock

    # ---------------- 对外 ----------------
    def get_tile(self, source_key: str, source: dict[str, Any], z: int, x: int, y: int) -> TileResult:
        cached = self._find_cached(source_key, z, x, y)
        if cached is not None:
            try:
                blob = cached.read_bytes()
            except OSError:
                blob = b""
            if blob:
                if _is_blank_tile(blob):
                    # 之前可能把空瓦片缓存下来了：删掉并重新回源
                    try:
                        cached.unlink()
                    except OSError:
                        pass
                else:
                    self.stats["hit"] += 1
                    try:
                        os.utime(cached, None)
                    except OSError:
                        pass
                    return TileResult(blob, _mime_for(cached.suffix), 200, "cache")

        key = f"{source_key}/{z}/{x}/{y}"
        with self._lock_for(key):
            cached = self._find_cached(source_key, z, x, y)
            if cached is not None:
                try:
                    blob = cached.read_bytes()
                except OSError:
                    blob = b""
                if blob and not _is_blank_tile(blob):
                    self.stats["hit"] += 1
                    return TileResult(blob, _mime_for(cached.suffix), 200, "cache")

            self.stats["miss"] += 1
            url = build_url(source, z, x, y)
            if not url:
                self.stats["failed"] += 1
                return TileResult(PLACEHOLDER_PNG, "image/png", 404, "error")
            try:
                body, mime = self._fetch(url)
            except Exception:
                self.stats["failed"] += 1
                return TileResult(PLACEHOLDER_PNG, "image/png", 404, "error")
            if not body or _is_blank_tile(body):
                # 服务商返回空图（多为瓦片不存在或参数不对）：不写缓存，交给前端显示占位
                self.stats["failed"] += 1
                return TileResult(PLACEHOLDER_PNG, "image/png", 404, "error")
            ext = _EXT_BY_MIME.get(mime, ".png")
            self._write(source_key, z, x, y, ext, body)
            self.stats["network"] += 1
            self._sweep()
            return TileResult(body, mime, 200, "network")

    def _fetch(self, url: str) -> tuple[bytes, str]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": _referer_for(url),
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read()
            mime = (response.headers.get("Content-Type") or "image/png").split(";")[0].strip().lower()
        if mime not in _EXT_BY_MIME and not body.startswith(b"\xff\xd8") and not body.startswith(b"\x89PNG"):
            raise ValueError("返回的不是图片")
        return body, mime

    def clear(self) -> int:
        """清空缓存，返回删除的瓦片数。"""
        removed = 0
        with self._lock:
            for root, _dirs, files in os.walk(self.dir, topdown=False):
                for name in files:
                    try:
                        (Path(root) / name).unlink()
                        removed += 1
                    except OSError:
                        continue
        return removed

    def size_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.dir):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        return total


def _mime_for(suffix: str) -> str:
    return {".jpg": "image/jpeg", ".webp": "image/webp"}.get(suffix, "image/png")


def _is_blank_tile(body: bytes) -> bool:
    """识别「全透明 / 纯色」的空瓦片。

    某些瓦片服务在参数或图层不对时会返回 200 + 256x256 的全透明 PNG（体积很小），
    这类图直接给前端只会显示白块，因此这里当作失败处理。
    """
    if not body:
        return True
    if not body.startswith(b"\x89PNG"):
        return False  # JPEG 等格式不在此判断
    if len(body) > 6000:
        return False  # 体积正常的 PNG 基本都有内容
    try:
        import zlib

        if body[24] != 8:  # bit depth
            return False
        color_type = body[25]
        pos = 8
        idat = bytearray()
        while pos + 8 <= len(body):
            length = int.from_bytes(body[pos:pos + 4], "big")
            chunk_type = body[pos + 4:pos + 8]
            data = body[pos + 8:pos + 8 + length]
            if chunk_type == b"IDAT":
                idat.extend(data)
            elif chunk_type == b"IEND":
                break
            pos += 12 + length
        if not idat:
            return True
        raw = zlib.decompress(bytes(idat))
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 1)
        bpp = channels
        stride = 256 * bpp
        # 这里只做粗略判断：全 0（透明/黑）或单一字节值的图像视为空
        first = None
        for row in range(min(8, 256)):
            start = row * (stride + 1) + 1
            chunk = raw[start:start + stride]
            if not chunk:
                break
            if first is None:
                first = bytes(chunk)
            elif bytes(chunk) != first:
                return False
        if first is None:
            return True
        if color_type in (4, 6):
            # 带 alpha 通道：全部 alpha 为 0 视为全透明
            alpha_index = bpp - 1
            return all(first[i] == 0 for i in range(alpha_index, len(first), bpp))
        return len(set(first)) <= 1
    except Exception:
        return False


def _referer_for(url: str) -> str:
    try:
        parts = url.split("/")
        return f"{parts[0]}//{parts[2]}/"
    except Exception:
        return "https://www.amap.com/"


def build_url(source: dict[str, Any], z: int, x: int, y: int) -> str:
    """把 {z}/{x}/{y}/{s} 模板替换成真实 URL。"""
    template = str(source.get("url") or "")
    if not template:
        return ""
    subs = source.get("subdomains") or []
    if "{s}" in template:
        if subs:
            sub = subs[(x + y) % len(subs)]
        else:
            sub = source.get("subdomain_default", "1")
        template = template.replace("{s}", str(sub))
    max_zoom = int(source.get("max_zoom") or 19)
    z = min(z, max_zoom)
    try:
        return template.format(z=z, x=x, y=y, s="")
    except (KeyError, IndexError, ValueError):
        return ""


def validate_tile_request(z: Any, x: Any, y: Any) -> tuple[int, int, int]:
    try:
        iz, ix, iy = int(z), int(x), int(y)
    except (TypeError, ValueError) as exc:
        raise protocol.AppError("瓦片坐标必须是整数", "bad_tile") from exc
    if not (0 <= iz <= 22):
        raise protocol.AppError("缩放级别超出范围", "bad_tile")
    limit = 1 << iz
    if not (0 <= ix < limit and 0 <= iy < limit):
        raise protocol.AppError("瓦片坐标超出该缩放级别范围", "bad_tile")
    return iz, ix, iy
