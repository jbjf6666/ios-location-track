"""iOS 17+ 的 RSD 隧道管理。

背景（见 docs/DEVICE_NOTES.md 的取证结论）：
- iOS 17 及以上，DVT 服务不再走 usbmux 代理，必须先在 Mac 与设备之间建立 RSD 隧道。
- 经典隧道（kernel utun）需要 root；`pymobiledevice3 remote start-tunnel` 在 macOS 上支持
  `--native`：借用 Apple 自带 remoted 的隧道，**不需要 root**，并把内核可路由的 RSD 地址打印出来
  （--script-mode 输出 "HOST PORT"），因此可以被其它进程直接使用。
- 该 native 隧道与 remoted 共用设备的单条 RSD 连接，会互相挤掉，所以同一时间只应有一个客户端
  （即：本工具运行时应关闭爱思助手等同类工具）。
- 如果 native 不可用，退化为 `osascript` 弹窗向用户索取管理员密码，再跑经典隧道。
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any

from . import protocol

POLL_INTERVAL = 0.25
START_TIMEOUT = 75.0
_HINT_NATIVE_FAILED = (
    "无法建立隧道：iOS 17 及以上需要 RSD 隧道。请确认：① 设备已用数据线连接并解锁；"
    "② 已在手机上开启「开发者模式」；③ 关闭爱思助手等同类工具（同一个 RSD 连接会被互相挤掉）；"
    "④ 若仍失败，可尝试管理员模式：在终端执行 sudo ./run.sh"
)
_HINT_NATIVE_FAILED_SHORT = "隧道建立失败：请关闭同类工具后重试，或用 sudo ./run.sh 以管理员模式启动"


class TunnelManager:
    """建立/持有/释放 RSD 隧道，并对外暴露 (host, port)。"""

    def __init__(self, venv_python: str | None = None, log=None, on_change=None) -> None:
        self._lock = threading.RLock()
        self.state = "idle"  # idle|starting|connected|error|unsupported
        self.message = ""
        self.host: str | None = None
        self.port: int | None = None
        self.udid: str | None = None
        self.mode: str | None = None  # native|sudo|manual
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._needs_root = False
        self._log = log
        self._on_change = on_change
        self.venv_python = venv_python or _detect_python()

    # ---------------- 状态 ----------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "message": self.message or _default_message(self.state),
                "rsd": f"{self.host} {self.port}" if self.host and self.port else None,
                "mode": self.mode,
                "udid": self.udid,
            }

    def _emit(self, level: str, message: str) -> None:
        if self._log:
            try:
                self._log(level, message)
            except Exception:
                pass

    def _set_state(self, state: str, message: str = "", **fields: Any) -> None:
        with self._lock:
            self.state = state
            self.message = message
            for key, value in fields.items():
                setattr(self, key, value)
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    @property
    def ready(self) -> bool:
        with self._lock:
            return bool(self.host and self.port) and self.state == "connected"

    def address(self) -> tuple[str, int] | None:
        with self._lock:
            if self.host and self.port:
                return self.host, self.port
            return None

    # ---------------- 启动 ----------------
    def ensure(self, udid: str | None, allow_sudo: bool = False) -> dict[str, Any]:
        """保证隧道可用。已连接且 UDID 未变时直接复用。"""
        with self._lock:
            if self.ready and (udid is None or self.udid in (None, udid)):
                return self.snapshot()
        self.stop()
        return self.start(udid=udid, allow_sudo=allow_sudo)

    def start(self, udid: str | None = None, allow_sudo: bool = False) -> dict[str, Any]:
        # 设备在 iOS 17+ 上只有一条 RSD 连接：残留的旧隧道进程会与新的互相挤掉，
        # 因此启动前先清理上一轮遗留的隧道进程（本工具异常退出时可能留下）。
        if not allow_sudo:
            self._kill_stale_tunnels()
        self._set_state("starting", "正在建立 RSD 隧道…", udid=udid, mode=None, host=None, port=None)
        command, mode = self._build_command(udid=udid, allow_sudo=allow_sudo)
        if command is None:
            self._set_state("unsupported", _HINT_NATIVE_FAILED, mode=None)
            return self.snapshot()

        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONPATH", str(_project_root()))
        self._emit("info", f"启动隧道（{mode} 模式）：{' '.join(_mask(command))}")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,
            )
        except Exception as exc:
            self._set_state("error", f"隧道进程启动失败：{exc}", mode=mode)
            return self.snapshot()

        with self._lock:
            self._process = process
            self.mode = mode
            self._needs_root = mode == "sudo"
        if mode != "sudo":
            _write_pidfile([process.pid])
        self._reader = threading.Thread(target=self._read_output, args=(process,), name="tunnel-reader", daemon=True)
        self._reader.start()

        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            with self._lock:
                if self.ready:
                    return self.snapshot()
                if self.state == "error":
                    return self.snapshot()
            if process.poll() is not None and not self.ready:
                break
            time.sleep(POLL_INTERVAL)

        if not self.ready:
            message = _HINT_NATIVE_FAILED_SHORT
            if mode == "sudo":
                message = "管理员隧道启动失败：请在终端手动执行 sudo ./run.sh 查看具体报错"
            self.stop()
            self._set_state("error", message, mode=mode)
        return self.snapshot()

    def _build_command(self, udid: str | None, allow_sudo: bool) -> tuple[list[str] | None, str]:
        """拼出"建隧道"命令。

        - 打包成 .app（sys.frozen）：`<本可执行文件> tunnel-helper --udid X`
          —— 这条路径不依赖机器上有 Python/pymobiledevice3；
        - 源码运行：`python -m pymobiledevice3 remote start-tunnel --native --script-mode`。
        """
        import sys

        if getattr(sys, "frozen", False):
            base = [sys.executable, "tunnel-helper"]
        else:
            cli = self._cli_command()
            if cli is None:
                return None, "native"
            base = [*cli, "remote", "start-tunnel", "--native", "--script-mode"]
        command = list(base)
        if udid:
            command += ["--udid", udid]
        if not allow_sudo:
            return command, "native"
        inner = " ".join(_shell_quote(part) for part in command)
        script = f'do shell script "{inner} > /tmp/iosloc-tunnel.log 2>&1 &" with administrator privileges'
        return ["osascript", "-e", script], "sudo"

    def _kill_stale_tunnels(self) -> None:
        """清理**本工具自己**遗留在后台的 start-tunnel 进程。

        iOS 17+ 设备只有一条 RSD 连接：残留进程会与新建的隧道互相挤掉。
        为了避免误杀用户在终端手动起的隧道或其它工具，这里只处理本项目上次运行时
        记录在 pid 文件里的进程（会先用 ps 校验命令行确实是 start-tunnel）。
        """
        pids = _read_pidfile()
        if not pids:
            return
        alive: list[int] = []
        for pid in pids:
            command = _command_of(pid)
            if command and "start-tunnel" in command:
                alive.append(pid)
        _write_pidfile([])
        if not alive:
            return
        self._emit("warn", f"发现 {len(alive)} 个上次遗留的隧道进程，正在清理…")
        for pid in alive:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except Exception:
                continue
            for _ in range(10):
                time.sleep(0.3)
                if _command_of(pid) is None:
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass

    def _cli_command(self) -> list[str] | None:
        """返回"建隧道"命令的前缀。

        - 打包成 .app 独立运行时（sys.frozen）：用本可执行文件 + `tunnel-helper` 子命令
          （递归调用自己），因此**不需要机器上装 Python 或 pymobiledevice3**；
        - 源码运行时：优先 venv 里的 `python -m pymobiledevice3`，其次 PATH 里的可执行文件。
        """
        import sys

        if getattr(sys, "frozen", False):
            return [sys.executable, "tunnel-helper"]
        candidate = _detect_python()
        if candidate:
            try:
                probe = subprocess.run(
                    [candidate, "-c", "import pymobiledevice3, sys; print(pymobiledevice3.__version__ if hasattr(pymobiledevice3,'__version__') else 'ok')"],
                    capture_output=True,
                    text=True,
                    timeout=25,
                )
                if probe.returncode == 0:
                    return [candidate, "-m", "pymobiledevice3"]
            except Exception:
                pass
        direct = shutil.which("pymobiledevice3")
        if direct:
            return [direct]
        return None

    # ---------------- 输出解析 ----------------
    def _read_output(self, process: subprocess.Popen[str]) -> None:
        pattern = re.compile(r"(?P<host>[0-9a-fA-F:.]{3,})\s+(?P<port>\d{2,5})")
        assert process.stdout is not None
        for line in process.stdout:
            text = line.strip()
            if not text:
                continue
            self._emit("info", f"[tunnel] {text}")
            if self.ready:
                continue
            found = _parse_rsd_line(text, pattern)
            if found:
                host, port = found
                self._set_state(
                    "connected",
                    f"隧道已建立（{self.mode or 'native'}）：{host} 端口 {port}",
                    host=host,
                    port=port,
                )
        with self._lock:
            code = process.poll()
        if code is not None and not self.ready:
            self._set_state("error", f"隧道进程已退出（代码 {code}）：{_HINT_NATIVE_FAILED_SHORT}")

    # ---------------- 手动 / 释放 ----------------
    def use_manual(self, host: str, port: int, udid: str | None = None) -> dict[str, Any]:
        host = (host or "").strip()
        try:
            iport = int(port)
        except (TypeError, ValueError):
            raise protocol.AppError("RSD 端口必须是数字", "bad_rsd")
        if not host:
            raise protocol.AppError("RSD 地址不能为空", "bad_rsd")
        self.stop()
        self._set_state("connected", f"已使用手动指定的 RSD：{host} 端口 {iport}", host=host, port=iport, mode="manual", udid=udid)
        return self.snapshot()

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is not None and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                try:
                    process.terminate()
                except Exception:
                    pass
            try:
                process.wait(timeout=4)
            except Exception:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    pass
        with self._lock:
            self.host = None
            self.port = None
            self.mode = None
            if self.state in ("connected", "starting", "error", "unsupported"):
                self.state = "idle"
                self.message = _default_message("idle")
        _write_pidfile([])


def _parse_rsd_line(text: str, pattern: re.Pattern[str]) -> tuple[str, int] | None:
    if text.startswith("--rsd "):
        parts = text.split()
        if len(parts) >= 3:
            try:
                return parts[1], int(parts[2])
            except ValueError:
                return None
    if "RSD Port" in text:
        return None
    match = pattern.fullmatch(text) or pattern.match(text)
    if not match:
        return None
    host = match.group("host")
    if ":" not in host and "." not in host:
        return None
    try:
        port = int(match.group("port"))
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None
    return host, port


def _default_message(state: str) -> str:
    return {
        "idle": "隧道未建立（iOS 17 及以上需要）",
        "starting": "正在建立隧道…",
        "connected": "隧道已连接",
        "error": _HINT_NATIVE_FAILED,
        "unsupported": "当前环境不支持自动建隧道",
    }.get(state, "")


def _mask(command: list[str]) -> list[str]:
    return command


def _pidfile_path() -> str:
    return os.path.join(_project_root(), ".tunnel.pids")


def _read_pidfile() -> list[int]:
    try:
        with open(_pidfile_path(), "r", encoding="utf-8") as handle:
            return [int(line.strip()) for line in handle if line.strip().isdigit()]
    except Exception:
        return []


def _write_pidfile(pids: list[int]) -> None:
    try:
        path = _pidfile_path()
        if not pids:
            if os.path.exists(path):
                os.unlink(path)
            return
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(str(pid) for pid in pids))
    except Exception:
        pass


def _command_of(pid: int) -> str | None:
    """返回进程命令行；进程不存在返回 None。"""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    text = (result.stdout or "").strip()
    return text or None


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _project_root() -> str:
    """数据目录根：源码运行用项目目录，打包后只读，改用用户可写目录。"""
    import sys

    if getattr(sys, "frozen", False):
        return _writable_data_dir()
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable_data_dir() -> str:
    """打包运行时可写的数据目录（~/Library/Application Support/...）。"""
    base = os.path.expanduser("~/Library/Application Support/iOSLocationTrack")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = os.path.expanduser("~")
    return base


def _detect_python() -> str | None:
    """返回项目 venv 的 python，找不到就退回当前解释器。"""
    import sys

    root = _project_root()
    for candidate in (
        os.path.join(root, ".venv", "bin", "python"),
        os.path.join(root, ".venv", "bin", "python3"),
    ):
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def tunnel_supported() -> bool:
    import platform

    return platform.system() == "Darwin"
