"""跨平台适配层（macOS / Windows / Linux）。

见 docs/WINDOWS_PORT.md 第 9 节：平台相关代码集中到本模块，其余模块只依赖这里，
从而保持**单一代码库**，不给 Windows 分叉出第二份源码。

约定：
- 所有公开函数在任何平台上都必须可用（不存在的能力返回 None / 空列表，不抛异常）。
- 需要"管理员/root"的操作只在这里封装，便于在界面上给出对应提示。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

#: 数据目录名（Windows: %LOCALAPPDATA%\iOSLocationTrack；macOS: ~/Library/Application Support/iOSLocationTrack）
APP_DIR_NAME = "iOSLocationTrack"
#: Chrome/Edge `--app=` 模式使用的独立用户数据目录
APP_WINDOW_PROFILE = ".ios-location-appwindow"


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def data_dir() -> Path:
    """打包运行时的可写数据目录（配置、瓦片缓存、隧道 pid 文件都写这里）。"""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        target = root / APP_DIR_NAME
    elif IS_MACOS:
        target = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"
        target = root / APP_DIR_NAME
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback = Path.home() / f".{APP_DIR_NAME.lower()}"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Path.home()
        return fallback
    return target


def resolve_roots() -> tuple[Path, Path]:
    """返回 (资源根, 数据根)。

    - 源码运行：两者都是项目目录
    - 打包后：资源在 PyInstaller 解包目录（只读），数据写到用户可写目录
    """
    if getattr(sys, "frozen", False):
        resources = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return resources, data_dir()
    root = Path(__file__).resolve().parent.parent
    return root, root


def venv_python(root: Path | str | None = None) -> Path | None:
    """返回项目 venv 内的解释器路径（不存在返回 None）。"""
    base = Path(root) if root else resolve_roots()[0]
    candidates = (
        [base / ".venv" / "Scripts" / "python.exe", base / ".venv" / "Scripts" / "python"]
        if IS_WINDOWS
        else [base / ".venv" / "bin" / "python", base / ".venv" / "bin" / "python3"]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def python_for_subprocess(root: Path | str | None = None) -> str:
    """选一个能 `import pymobiledevice3` 的解释器：优先项目 venv，其次当前解释器。"""
    found = venv_python(root)
    if found is not None:
        return str(found)
    return sys.executable


def module_command(root: Path | str | None = None) -> list[str]:
    """运行 `pymobiledevice3` CLI 的命令前缀。"""
    return [python_for_subprocess(root), "-m", "pymobiledevice3"]


# --------------------------------------------------------------------------
# 界面窗口（Chrome/Edge 的 --app= 模式）
# --------------------------------------------------------------------------
def _windows_browser_candidates() -> list[Path]:
    out: list[Path] = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    rel = [
        (pf, r"Google\Chrome\Application\chrome.exe"),
        (pf86, r"Google\Chrome\Application\chrome.exe"),
        (local, r"Google\Chrome\Application\chrome.exe"),
        (pf, r"Microsoft\Edge\Application\msedge.exe"),
        (pf86, r"Microsoft\Edge\Application\msedge.exe"),
        (pf, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        (pf86, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        (local, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        (local, r"Chromium\Application\chrome.exe"),
    ]
    for base, tail in rel:
        if base:
            out.append(Path(base) / tail)
    # 注册表 App Paths 兜底（覆盖非默认安装位置）
    try:
        import winreg  # type: ignore[import-not-found]

        for exe in ("chrome.exe", "msedge.exe", "brave.exe"):
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(
                            hive,
                            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}",
                            0,
                            winreg.KEY_READ | view,
                        ) as key:
                            value, _ = winreg.QueryValueEx(key, None)
                            if value:
                                out.append(Path(str(value).strip('"')))
                    except OSError:
                        continue
        # 卸载信息里找 Chrome 的 InstallLocation
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome",
                        0,
                        winreg.KEY_READ | view,
                    ) as key:
                        loc, _ = winreg.QueryValueEx(key, "InstallLocation")
                        if loc:
                            out.append(Path(str(loc)) / "chrome.exe")
                except OSError:
                    continue
    except Exception:
        pass
    return out


def browser_candidates() -> list[Path]:
    """可用于 `--app=` 无地址栏窗口的 Chromium 系浏览器，按优先级排序。"""
    if IS_WINDOWS:
        return _windows_browser_candidates()
    if IS_MACOS:
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    return [
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/usr/bin/microsoft-edge"),
        Path("/snap/bin/chromium"),
    ]


def find_app_browser() -> Path | None:
    """返回第一个真实存在的 Chromium 系浏览器。"""
    seen: set[str] = set()
    for candidate in browser_candidates():
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    for name in ("chrome", "msedge", "chromium", "brave"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def app_window_profile_dir() -> Path:
    return Path.home() / APP_WINDOW_PROFILE


# --------------------------------------------------------------------------
# 进程
# --------------------------------------------------------------------------
def spawn_detached_kwargs() -> dict[str, Any]:
    """Popen 关键字：让子进程独立于父进程（父进程被 Ctrl+C 时不连带杀掉隧道）。"""
    if IS_WINDOWS:
        # start_new_session 在 Windows 上被忽略；用 CREATE_NEW_PROCESS_GROUP +
        # DETACHED_PROCESS 达到同样效果，否则子进程会跟着控制台一起收到 CTRL_C_EVENT。
        flags = 0
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _no_window_kwargs() -> dict[str, Any]:
    """辅助进程（不弹黑窗）的 Popen 关键字。"""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def process_cmdline(pid: int) -> str | None:
    """返回进程命令行；进程不存在或查询失败返回 None。"""
    if pid <= 0:
        return None
    if IS_WINDOWS:
        return _windows_cmdline(pid)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    text = (result.stdout or "").strip()
    return text or None


def _windows_cmdline(pid: int) -> str | None:
    """Windows 上取进程命令行：优先 PowerShell CIM，其次 wmic。"""
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if ps:
        try:
            result = subprocess.run(
                [
                    ps, "-NoProfile", "-NonInteractive", "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                **_no_window_kwargs(),
            )
            text = (result.stdout or "").strip()
            if text:
                return text
        except Exception:
            pass
    wmic = shutil.which("wmic")
    if wmic:
        try:
            result = subprocess.run(
                [wmic, "process", "where", f"processid={int(pid)}", "get", "commandline", "/value"],
                capture_output=True,
                text=True,
                timeout=10,
                **_no_window_kwargs(),
            )
            for line in (result.stdout or "").splitlines():
                if line.strip().startswith("CommandLine="):
                    return line.split("=", 1)[1].strip() or None
        except Exception:
            pass
    return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        # OpenProcess + GetExitCodeProcess；用 tasklist 太慢，这里走 ctypes
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def terminate_process(pid: int, force: bool = False, timeout: float = 3.0) -> bool:
    """结束单个进程（不递归）。返回是否已确认退出。"""
    if pid <= 0 or pid == os.getpid():
        return False
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                **_no_window_kwargs(),
            )
        except Exception:
            return False
        for _ in range(int(max(1, timeout / 0.2))):
            if not pid_alive(pid):
                return True
            time.sleep(0.2)
        return not pid_alive(pid)
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    if not force:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    return not pid_alive(pid)


def terminate_process_tree(process: subprocess.Popen[Any] | None) -> None:
    """结束一个 Popen 及其子进程（隧道助手会派生子进程）。"""
    if process is None or process.poll() is not None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(process.pid)), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                **_no_window_kwargs(),
            )
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    else:
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
        if not IS_WINDOWS:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                pass
        else:
            terminate_process(process.pid, force=True)


def find_pid_by_cmdline(needles: list[str], image_names: list[str] | None = None) -> list[int]:
    """按命令行关键字找进程（Windows 用一次 CIM 批量查询，POSIX 用 pgrep）。

    仅用于清理本工具自己遗留的隧道进程。
    """
    if IS_WINDOWS:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            return []
        try:
            result = subprocess.run(
                [
                    ps, "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance Win32_Process | "
                    "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=25,
                **_no_window_kwargs(),
            )
        except Exception:
            return []
        import json

        try:
            data = json.loads((result.stdout or "").strip() or "[]")
        except Exception:
            return []
        if isinstance(data, dict):
            data = [data]
        found: list[int] = []
        me = os.getpid()
        for item in data:
            if not isinstance(item, dict):
                continue
            pid = int(item.get("ProcessId") or 0)
            if pid <= 0 or pid == me:
                continue
            name = str(item.get("Name") or "")
            cmd = str(item.get("CommandLine") or "")
            if image_names and not any(name.lower() == n.lower() for n in image_names):
                continue
            if any(needle.lower() in cmd.lower() for needle in needles):
                found.append(pid)
        return found
    pids: list[int] = []
    for needle in needles:
        try:
            result = subprocess.run(
                ["pgrep", "-f", needle], capture_output=True, text=True, timeout=5
            )
        except Exception:
            continue
        for line in (result.stdout or "").split():
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid != os.getpid() and pid not in pids:
                pids.append(pid)
    return pids


# --------------------------------------------------------------------------
# 权限
# --------------------------------------------------------------------------
def is_admin() -> bool:
    """当前进程是否具备管理员/root 权限。"""
    if IS_WINDOWS:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def admin_hint() -> str:
    """界面上展示的"怎么以管理员身份运行"文案。"""
    if IS_WINDOWS:
        return "请右键程序图标选择「以管理员身份运行」（userspace 隧道需要安装/启用 Wintun 虚拟网卡）"
    return "请在终端执行 sudo ./run.sh 以管理员模式启动"


def relaunch_elevated(argv: list[str] | None = None) -> bool:
    """以提权方式重新启动自身。返回是否成功发起（不等待结果）。"""
    args = argv if argv is not None else sys.argv
    if IS_WINDOWS:
        try:
            import ctypes

            if getattr(sys, "frozen", False):
                exe, params = sys.executable, subprocess.list2cmdline(args[1:])
            else:
                exe = sys.executable
                params = subprocess.list2cmdline([str(Path(args[0]).resolve()), *args[1:]])
            # SW_SHOWNORMAL = 1
            rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                None, "runas", exe, params, None, 1
            )
            return int(rc) > 32
        except Exception:
            return False
    if IS_MACOS:
        inner = " ".join(f"'{a}'" for a in args)
        script = f'do shell script "{inner}" with administrator privileges'
        try:
            subprocess.Popen(["osascript", "-e", script])
            return True
        except Exception:
            return False
    return False


# --------------------------------------------------------------------------
# 信号
# --------------------------------------------------------------------------
def termination_signals() -> list[int]:
    """可以安全注册的退出信号（Windows 没有 SIGHUP）。"""
    names = ["SIGINT", "SIGTERM"]
    if not IS_WINDOWS:
        names.append("SIGHUP")
    out: list[int] = []
    for name in names:
        value = getattr(signal, name, None)
        if isinstance(value, int):
            out.append(value)
    return out


def open_in_file_manager(path: Path) -> bool:
    """在系统文件管理器里打开一个目录。"""
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif IS_MACOS:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False


def platform_label() -> str:
    if IS_WINDOWS:
        return "Windows"
    if IS_MACOS:
        return "macOS"
    return "Linux"
