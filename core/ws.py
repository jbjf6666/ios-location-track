"""极简 WebSocket 服务端实现（RFC 6455）：零第三方依赖。

设计：
- ThreadingHTTPServer 的 handler 检测到 Upgrade: websocket 后调用 serve()，
  该函数劫持 socket 并阻塞在当前线程里跑消息循环。
- 发送可能来自任意线程（HTTP 线程、播放引擎线程），因此内部用锁 + deque 保证串行写。
- 服务端只推送文本帧，不接收分片消息（浏览器不会对短 JSON 做分片）。
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import time
from collections import deque
from typing import Any, Callable

from . import protocol

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME = 8 * 1024 * 1024


def is_websocket_upgrade(headers: Any) -> bool:
    upgrade = (headers.get("Upgrade") or "").strip().lower()
    connection = (headers.get("Connection") or "").lower()
    return upgrade == "websocket" and "upgrade" in connection


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class WSConnection:
    """一条已升级的连接。线程安全的 send_json。"""

    def __init__(self, sock: socket.socket, addr: tuple[str, int], require_masked: bool = True) -> None:
        self.sock = sock
        self.addr = addr
        self.require_masked = require_masked
        self.id = f"ws-{int(time.time() * 1000) % 100000}-{os.urandom(2).hex()}"
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._buf = b""
        self._frag_op: int | None = None
        self._frag_data = bytearray()
        self.bytes_sent = 0
        self.bytes_recv = 0

    # ---------------- 发送 ----------------
    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self._closed.is_set():
            return
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        frame = bytes(header) + payload
        with self._send_lock:
            if self._closed.is_set():
                return
            try:
                self.sock.sendall(frame)
                self.bytes_sent += len(frame)
            except OSError:
                self._closed.set()

    def send_text(self, text: str) -> None:
        self.send_frame(OP_TEXT, text.encode("utf-8"))

    def send_json(self, obj: dict[str, Any]) -> None:
        self.send_text(protocol.dumps(obj))

    def ping(self) -> None:
        self.send_frame(OP_PING)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed.is_set():
            return
        payload = struct.pack("!H", code) + reason.encode("utf-8")
        try:
            self.send_frame(OP_CLOSE, payload)
        finally:
            self._closed.set()
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # ---------------- 接收 ----------------
    def _recv_exact(self, n: int) -> bytes | None:
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except (OSError, socket.timeout):
                return None
            if not chunk:
                return None
            self._buf += chunk
            self.bytes_recv += len(chunk)
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read_frame(self) -> tuple[int, bool, bytes] | None:
        head = self._recv_exact(2)
        if head is None:
            return None
        b1, b2 = head[0], head[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            ext = self._recv_exact(2)
            if ext is None:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._recv_exact(8)
            if ext is None:
                return None
            length = struct.unpack("!Q", ext)[0]
        if length > MAX_FRAME:
            self.close(1009, "frame too large")
            return None
        mask = b""
        if masked:
            mask = self._recv_exact(4) or b""
            if len(mask) != 4:
                return None
        elif self.require_masked:
            # RFC 6455：客户端发来的帧必须带掩码，否则属协议错误
            self.close(1002, "unmasked frame from client")
            return None
        payload = self._recv_exact(length) if length else b""
        if payload is None:
            return None
        if masked and payload:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, fin, payload

    def run(self, on_message: Callable[["WSConnection", dict[str, Any]], None],
            on_close: Callable[["WSConnection"], None] | None = None) -> None:
        """消息循环，阻塞直到对端关闭或出错。"""
        try:
            self.sock.settimeout(None)
        except OSError:
            pass
        try:
            while not self._closed.is_set():
                frame = self._read_frame()
                if frame is None:
                    break
                opcode, fin, payload = frame
                if opcode == OP_CLOSE:
                    self.send_frame(OP_CLOSE, payload[:2] if len(payload) >= 2 else b"")
                    break
                if opcode == OP_PING:
                    self.send_frame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode in (OP_TEXT, OP_BIN):
                    if fin:
                        data = payload
                    else:
                        self._frag_op = opcode
                        self._frag_data = bytearray(payload)
                        continue
                elif opcode == OP_CONT:
                    if self._frag_op is None:
                        continue
                    self._frag_data.extend(payload)
                    if len(self._frag_data) > MAX_FRAME:
                        self.close(1009, "fragmented message too large")
                        return None
                    if not fin:
                        continue
                    data = bytes(self._frag_data)
                    self._frag_op = None
                    self._frag_data = bytearray()
                else:
                    continue
                if not data:
                    continue
                try:
                    msg = protocol.loads(data)
                except Exception:
                    self.send_json({"type": "error", "error": "消息不是合法 JSON", "code": "bad_json"})
                    continue
                if isinstance(msg, dict):
                    on_message(self, msg)
        finally:
            self._closed.set()
            try:
                self.sock.close()
            except OSError:
                pass
            if on_close is not None:
                try:
                    on_close(self)
                except Exception:
                    pass


def handshake(sock: socket.socket, headers: Any) -> WSConnection | None:
    """完成 HTTP → WebSocket 升级。成功返回 WSConnection。

    注意：只有当客户端请求了 Sec-WebSocket-Protocol 时才能回该头（RFC 6455）。
    否则浏览器会因为"服务端选择了未请求的子协议"而直接判定握手失败。
    """
    key = headers.get("Sec-WebSocket-Key")
    if not key:
        try:
            sock.sendall(
                b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
        except OSError:
            pass
        return None
    version = (headers.get("Sec-WebSocket-Version") or "").strip()
    if version and version != "13":
        # RFC 6455：只支持版本 13，其它版本必须明确拒绝，不能装作握手成功
        try:
            sock.sendall(
                b"HTTP/1.1 426 Upgrade Required\r\nSec-WebSocket-Version: 13\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
        except OSError:
            pass
        return None
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_key(key.strip())}",
    ]
    requested = headers.get("Sec-WebSocket-Protocol")
    if requested:
        offered = [item.strip() for item in requested.split(",") if item.strip()]
        # 只回客户端提供的第一个协议，绝不回它没要求过的值
        if offered:
            lines.append(f"Sec-WebSocket-Protocol: {offered[0]}")
    response = "\r\n".join(lines) + "\r\n\r\n"
    try:
        sock.sendall(response.encode("ascii"))
    except OSError:
        return None
    addr = sock.getpeername() if hasattr(sock, "getpeername") else ("?", 0)
    if not isinstance(addr, tuple):
        addr = ("?", 0)
    return WSConnection(sock, (str(addr[0]), int(addr[1])))


class Hub:
    """连接注册表 + 广播。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: deque[WSConnection] = deque()

    def add(self, conn: WSConnection) -> None:
        with self._lock:
            self._conns.append(conn)

    def remove(self, conn: WSConnection) -> None:
        with self._lock:
            try:
                self._conns.remove(conn)
            except ValueError:
                pass

    def snapshot(self) -> list[WSConnection]:
        with self._lock:
            return [c for c in self._conns if not c.closed]

    @property
    def count(self) -> int:
        return len(self.snapshot())

    def broadcast(self, obj: dict[str, Any]) -> int:
        payload = protocol.dumps(obj)
        sent = 0
        for conn in self.snapshot():
            try:
                conn.send_text(payload)
                sent += 1
            except Exception:
                self.remove(conn)
        return sent

    def close_all(self) -> None:
        for conn in self.snapshot():
            try:
                conn.close(1001, "server shutdown")
            except Exception:
                pass
        with self._lock:
            self._conns.clear()
