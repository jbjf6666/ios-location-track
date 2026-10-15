"""iOS 17+ 的 RSD 隧道管理（macOS / Windows 双平台）。

背景
----
iOS 17 起 DVT 服务不再走 usbmux，必须先在主机与设备之间建立 RSD 隧道。

两个平台的建立方式**本质不同**，这是本模块最需要注意的地方：

macOS
    用 ``remote start-tunnel --native``：借 Apple 自带 ``remoted`` 的隧道
    （不建内核 utun，因而不需要 root），打印出**内核可路由**的 ``HOST PORT``，
    主进程用普通 TCP 连上去即可 → 走"子进程 + 解析 stdout"的老路。

Windows
    没有 XPC/Objective-C，native 隧道不存在。改用 ``UserspaceRsdTunnel``：
    用纯 Python 的 PyTCP 用户态协议栈实现 iOS 17+ 隧道，**不需要管理员权限、
    不需要 Wintun 驱动**。

    ⚠️ 关键差异：用户态隧道的设备地址**只在本进程的协议栈里可达**
    （``rsd.is_in_process_tunnel is True``）。用普通 socket 去连 ``(host, port)``
    会直接失败（Windows 上表现为 ``OSError [WinError 1231] 网络位置不可达``）。
    pymobiledevice3 的做法是把一个 relay dialer 注入它返回的
    ``RemoteServiceDiscoveryService`` 对象（``open_connection=dial_plane.dial``），
    所以**必须把这个 rsd 对象本身交给 DVT 层**（``RealLocationProvider(rsd=...)``），
    而不是把 ``(host, port)`` 传出去另建连接。

    这也意味着 Windows 上 **不能**沿用 macOS 的"隧道助手子进程"架构：
    子进程的协议栈不能给父进程用。隧道必须在应用自己的事件循环里建立并持有。

实测（iPhone 11 / iOS 26.6.1，Windows 11 x64）：
    隧道建立耗时约 0.3 秒，无需管理员权限，无需安装 Wintun 驱动。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from . import platform as plat
from . import protocol

POLL_INTERVAL = 0.25
START_TIMEOUT = 75.0

#: Windows 上不需要提权；macOS 的 sudo 兜底路径保留
_HINT_MAC_FAILED = (
    "无法建立隧道：iOS 17 及以上需要 RSD 隧道。请确认：① 设备已用数据线连接并解锁；"
    "② 已在手机上开启「开发者模式」；③ 关闭爱思助手等同类工具（同一个 RSD 连接会被互相挤掉）；"
    "④ 若仍失败，可尝试管理员模式：在终端执行 sudo ./run.sh"
)
_HINT_MAC_FAILED_SHORT = "隧道建立失败：请关闭同类工具后重试，或用 sudo ./run.sh 以管理员模式启动"

_HINT_WIN_FAILED = (
    "无法建立隧道：iOS 17 及以上需要 RSD 隧道。请确认：① 设备已用数据线连接并解锁；"
    "② 已在手机上开启「开发者模式」并点过「信任此电脑」；"
    "③ 关闭爱思助手 / i4Tools 等同类工具（同一个 RSD 连接会被互相挤掉）；"
    "④ 若提示缺少 Apple Mobile Device Support，请运行「修复驱动」。"
)
_HINT_WIN_FAILED_SHORT = "隧道建立失败：请确认设备已解锁并信任本电脑，且已关闭同类工具"

_HINT_NATIVE_FAILED = _HINT_WIN_FAILED if plat.IS_WINDOWS else _HINT_MAC_FAILED
_HINT_NATIVE_FAILED_SHORT = (
    _HINT_WIN_FAILED_SHORT if plat.IS_WINDOWS else _HINT_MAC_FAILED_SHORT
)


class TunnelManager:
    """建立/持有/释放 RSD 隧道，并对外暴露 (host, port) 与（Windows）rsd 对象。"""

    def __init__(self, venv_python: str | None = None, log=None, on_change=None,
                 runner=None) -> None:
        self._lock = threading.RLock()
        self.state = "idle"  # idle|starting|connected|error|unsupported
        self.message = ""
        self.host: str | None = None
        self.port: int | None = None
        self.udid: str | None = None
        self.mode: str | None = None  # native|userspace|manual|sudo
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._needs_root = False
        self._log = log
        self._on_change = on_change
        #: Windows：应用的事件循环运行器（core/server.py:AsyncRunner）。
        #: 用户态隧道必须建在这个循环上，并被本对象一直持有。
        self._runner = runner
        self._userspace_tunnel: Any = None
        self._rsd: Any = None
        self.venv_python = venv_python or plat.python_for_subprocess()

    # ---------------- 状态 ----------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "message": self.message or _default_message(self.state),
                "rsd": f"{self.host} {self.port}" if self.host and self.port else None,
                "mode": self.mode,
                "udid": self.udid,
                # 前端据此提示"隧道只在本进程内可用"，不要尝试用外部工具连它
                "in_process": bool(self._rsd is not None
                                   and getattr(self._rsd, "is_in_process_tunnel", False)),
                "platform": plat.platform_label(),
            }

    def rsd_object(self) -> Any:
        """返回**已连接**的 RSD 对象（Windows 用户态隧道必需）。

        DVT 层必须复用它：它的拨号器绑定在本进程的用户态协议栈上。
        """
        with self._lock:
            return self._rsd

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
        self._set_state("starting", "正在建立 RSD 隧道…", udid=udid, mode=None,
                        host=None, port=None)
        if plat.IS_WINDOWS:
            return self._start_userspace(udid)
        return self._start_subprocess(udid, allow_sudo)

    # ---------------- Windows：进程内用户态隧道 ----------------
    def _start_userspace(self, udid: str | None) -> dict[str, Any]:
        """Windows：在应用事件循环上建立 userspace 隧道，并持有 rsd 对象。"""
        if self._runner is None:
            self._set_state(
                "error",
                "内部错误：Windows 用户态隧道需要事件循环运行器（runner），但未注入",
                mode="userspace",
            )
            return self.snapshot()
        try:
            from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        except Exception as exc:
            self._set_state("unsupported", f"无法加载 userspace 隧道模块：{exc}",
                            mode="userspace")
            return self.snapshot()

        self._emit("info", "正在建立 userspace RSD 隧道（进程内 PyTCP 协议栈，无需管理员权限）…")

        async def _open() -> tuple[Any, Any]:
            tunnel = UserspaceRsdTunnel(serial=udid, autopair=True,
                                        remotepairing_fallback=True)
            rsd = await tunnel.aopen()
            return tunnel, rsd

        started = time.monotonic()
        try:
            tunnel, rsd = self._runner.run(_open(), timeout=START_TIMEOUT)
        except Exception as exc:
            message = f"隧道建立失败：{exc}"
            if "already active" in str(exc):
                message = ("本进程已有一条 userspace 隧道（PyTCP 协议栈是进程内全局单例），"
                           "请先断开后再重试")
            self._set_state("error", message, mode="userspace")
            self._emit("error", f"[tunnel] {message}")
            return self.snapshot()

        address = tuple(getattr(rsd.service, "address", ()) or ())
        if len(address) < 2:
            try:
                self._runner.run(_close_userspace(tunnel), timeout=20)
            except Exception:
                pass
            self._set_state("error", "隧道已建立但未拿到 RSD 地址", mode="userspace")
            return self.snapshot()

        host, port = str(address[0]), int(address[1])
        in_process = bool(getattr(rsd, "is_in_process_tunnel", False))
        with self._lock:
            self._userspace_tunnel = tunnel
            self._rsd = rsd
        elapsed = time.monotonic() - started
        detail = (f"隧道已建立（userspace，{elapsed:.1f}s）：{host} 端口 {port}"
                  + ("（进程内，仅供本程序使用）" if in_process else ""))
        self._emit("info", f"[tunnel] {detail}")
        self._set_state("connected", detail, host=host, port=port, mode="userspace")
        return self.snapshot()

    # ---------------- macOS：子进程 + 解析 stdout ----------------
    def _start_subprocess(self, udid: str | None, allow_sudo: bool) -> dict[str, Any]:
        # 设备在 iOS 17+ 上只有一条 RSD 连接：残留的旧隧道进程会与新的互相挤掉，
        # 因此启动前先清理上一轮遗留的隧道进程（本工具异常退出时可能留下）。
        if not allow_sudo:
            self._kill_stale_tunnels()
        command, mode = self._build_command(udid=udid, allow_sudo=allow_sudo)
        if command is None:
            self._set_state("unsupported", _HINT_NATIVE_FAILED, mode=None)
            return self.snapshot()

        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")  # 不加的话 stdout 是块缓冲，HOST PORT 读不到
        env.setdefault("PYTHONPATH", str(plat.resolve_roots()[0]))
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
                **plat.spawn_detached_kwargs(),
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
        self._reader = threading.Thread(target=self._read_output, args=(process,),
                                        name="tunnel-reader", daemon=True)
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
                message = ("管理员隧道启动失败：请查看具体报错后重试"
                           if plat.IS_WINDOWS else
                           "管理员隧道启动失败：请在终端手动执行 sudo ./run.sh 查看具体报错")
            self.stop()
            self._set_state("error", message, mode=mode)
        return self.snapshot()

    def _build_command(self, udid: str | None, allow_sudo: bool) -> tuple[list[str] | None, str]:
        """拼出"建隧道"命令（**仅 macOS 使用**；Windows 走进程内 API）。"""
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
        if plat.IS_MACOS:
            inner = " ".join(_shell_quote(part) for part in command)
            script = (f'do shell script "{inner} > /tmp/iosloc-tunnel.log 2>&1 &" '
                      f"with administrator privileges")
            return ["osascript", "-e", script], "sudo"
        # Windows 的 userspace 隧道不需要提权；走到这里说明上层误用了 sudo 路径
        return command, "native"

    def _kill_stale_tunnels(self) -> None:
        """清理**本工具自己**遗留在后台的 start-tunnel 进程。

        iOS 17+ 设备只有一条 RSD 连接：残留进程会与新建的隧道互相挤掉。
        为避免误杀用户手动起的隧道或其它工具，只处理本项目记录在 pid 文件里的进程，
        并且会先校验命令行确实属于本工具。
        """
        pids = _read_pidfile()
        if not pids:
            return
        alive: list[int] = []
        for pid in pids:
            if not plat.pid_alive(pid):
                continue
            command = plat.process_cmdline(pid) or ""
            if _looks_like_our_tunnel(command):
                alive.append(pid)
        _write_pidfile([])
        if not alive:
            return
        self._emit("warn", f"发现 {len(alive)} 个上次遗留的隧道进程，正在清理…")
        for pid in alive:
            plat.terminate_process(pid, force=False, timeout=3.0)

    def _cli_command(self) -> list[str] | None:
        """返回"建隧道"命令的前缀（源码运行时的 macOS 路径）。"""
        import sys

        if getattr(sys, "frozen", False):
            return [sys.executable, "tunnel-helper"]
        candidate = plat.python_for_subprocess()
        if candidate:
            try:
                probe = subprocess.run(
                    [candidate, "-c",
                     "import pymobiledevice3; print('ok')"],
                    capture_output=True, text=True, timeout=25,
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
            self._set_state("error",
                            f"隧道进程已退出（代码 {code}）：{_HINT_NATIVE_FAILED_SHORT}")

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
        self._set_state("connected", f"已使用手动指定的 RSD：{host} 端口 {iport}",
                        host=host, port=iport, mode="manual", udid=udid)
        return self.snapshot()

    def stop(self) -> None:
        # ---- Windows：释放进程内用户态隧道 ----
        with self._lock:
            tunnel = self._userspace_tunnel
            self._userspace_tunnel = None
            self._rsd = None
        if tunnel is not None:
            try:
                if self._runner is not None:
                    self._runner.run(_close_userspace(tunnel), timeout=25)
                else:  # pragma: no cover
                    tunnel.close()
            except Exception as exc:
                self._emit("warn", f"释放用户态隧道时出错（可忽略）：{exc}")

        # ---- macOS：结束子进程 ----
        with self._lock:
            process = self._process
            self._process = None
        if process is not None and process.poll() is None:
            plat.terminate_process_tree(process)

        with self._lock:
            self.host = None
            self.port = None
            self.mode = None
            if self.state in ("connected", "starting", "error", "unsupported"):
                self.state = "idle"
                self.message = _default_message("idle")
        _write_pidfile([])


async def _close_userspace(tunnel: Any) -> None:
    """释放 userspace 隧道。

    注意：``UserspaceRsdTunnel.close()`` 是同步壳，内部会 ``aclose()`` 但**不 await**，
    直接调用会留下未 await 的协程并且隧道不会真正释放 —— 必须 await ``aclose()``。
    """
    aclose = getattr(tunnel, "aclose", None)
    if aclose is not None:
        await aclose()
        return
    close = getattr(tunnel, "close", None)
    if close is not None:
        result = close()
        if hasattr(result, "__await__"):
            await result


def _looks_like_our_tunnel(command: str) -> bool:
    """判断某个进程是否"本工具建立的隧道进程"，避免误杀。

    macOS 子进程模式：``... -m pymobiledevice3 remote start-tunnel --native ...``
    打包后的自调用：``iOSLocationTrack.exe tunnel-helper ...``
    """
    if not command:
        return False
    lowered = command.lower()
    if "tunnel-helper" in lowered:
        return True
    return "pymobiledevice3" in lowered and "start-tunnel" in lowered


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
    return os.path.join(str(plat.data_dir()), ".tunnel.pids")


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


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _project_root() -> str:
    """数据目录根：源码运行用项目目录，打包后只读，改用用户可写目录。"""
    import sys

    if getattr(sys, "frozen", False):
        return str(plat.data_dir())
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable_data_dir() -> str:
    return str(plat.data_dir())


def _detect_python() -> str | None:
    return plat.python_for_subprocess()


def tunnel_supported() -> bool:
    """Windows（userspace）与 macOS（native）都支持自动建隧道。"""
    return plat.IS_WINDOWS or plat.IS_MACOS


def mounter_hint(udid: str | None = None) -> str:
    """给用户的"挂载开发者镜像"命令行提示（按平台给出可直接粘贴的命令）。"""
    tail = f" --udid {udid}" if udid else ""
    if plat.IS_WINDOWS:
        return (f".venv\\Scripts\\python.exe -m pymobiledevice3 mounter auto-mount{tail}")
    return f"./.venv/bin/python -m pymobiledevice3 mounter auto-mount{tail}"


def python_hint() -> str:
    """解释器路径提示（错误文案里用）。"""
    return ".venv\\Scripts\\python.exe" if plat.IS_WINDOWS else "./.venv/bin/python"


def run_hint() -> str:
    """启动脚本提示。"""
    return ".\\scripts\\run.ps1" if plat.IS_WINDOWS else "./run.sh"
