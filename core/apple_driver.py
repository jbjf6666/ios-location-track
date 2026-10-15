r"""Apple Mobile Device Support（Windows 上的 usbmuxd 等价物）检测与自动安装。

**为什么需要这个模块**

macOS 自带 usbmuxd；Windows 上 usbmuxd 由 Apple 的 "Apple Mobile Device Service"
提供（随 iTunes / Apple Mobile Device Support 安装）。它监听 **127.0.0.1:27015**，
pymobiledevice3 通过它发现 iPhone。没有它，`pymobiledevice3 usbmux list` 一台设备也看不到。

**关键事实（已在真机取证，见 docs/DEVICE_NOTES_WINDOWS.md）**

1. 服务名是带空格的 ``Apple Mobile Device Service``；``AppleMobileDeviceService`` 只是
   exe 文件名，`sc query AppleMobileDeviceService` 会报 1060。
2. Windows 上 **没有** ``\\.\pipe\usbmuxd``，只有 TCP 27015。
3. ProductCode 不稳定（每个版本都变），**UpgradeCode 稳定**：
   ``{5CF0656E-F3B8-11DB-BA81-804E56D89593}``（x86/x64 通用）。
4. **AMDS 不提供 USB 驱动**。它的 MSI 里 0 个 .inf/.sys/.cat；现代机器上 iPhone 实际绑定
   的是 ``appleusb.inf``（服务 WINUSB / usbccgp）。所以**不要**用 `usbaapl64` 判断驱动是否存在
   —— 健康的机器上这个判断会误报"缺失"。
5. 判断"到底能不能用"的**唯一权威信号**是 27015 上的协议往返（``ReadBUID`` 不需要插设备）。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import plistlib
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import platform as plat

#: 服务名（带空格！）
SERVICE_NAME = "Apple Mobile Device Service"
#: AMDS 的稳定标识（UpgradeCode，x86/x64 通用，跨版本不变）
AMDS_UPGRADE_CODE = "{5CF0656E-F3B8-11DB-BA81-804E56D89593}"
#: AMDS 安装包在注册表 Uninstall 下的显示名
AMDS_DISPLAY_NAME = "Apple Mobile Device Support"

USBMUX_HOST = "127.0.0.1"
USBMUX_PORT = 27015

# ---------------------------------------------------------------------------
# 官方下载地址（2026-10 实测可达）
# ---------------------------------------------------------------------------
ITUNES_WIN64_URL = "https://www.apple.com/itunes/download/win64"
ITUNES_WIN32_URL = "https://www.apple.com/itunes/download/win32"

#: 独立 AMDS MSI（约 38 MB，比整个 iTunes 包 193 MB 小得多）
AMDS_MSI_X64_URL = (
    "https://swcdn.apple.com/content/downloads/20/49/047-76422/"
    "qcw2a7028lr8yp4rdkrfyzkvlhi9q1gg2g/AppleMobileDeviceSupport64.msi"
)
AMDS_MSI_X64_SHA256 = "b60533fb54e7bd81ffc52d99678a9cce04e58cb54a76281bd7b1309f20d360e9"
AMDS_MSI_X86_URL = (
    "https://swcdn.apple.com/content/downloads/54/12/047-76417/"
    "rftfhqmqnu0afv25sdr2iosg38mxkqa388/AppleMobileDeviceSupport.msi"
)
AMDS_MSI_X86_SHA256 = "590e4b21d2aa522cfe789cc3fea95421b6141ddfa36e06525ae9715a5b5859e3"

#: Apple Application Support（AMDS 的前置依赖，安装顺序由 iTunes 包自己决定）
AAS_MSI_X64_URL = (
    "https://swcdn.apple.com/content/downloads/63/47/001-39934/"
    "955xl2euy0wb5j2z4oddbbd39ln1sthedn/AppleApplicationSupport64.msi"
)

#: msiexec / 安装程序的"成功"退出码
SUCCESS_CODES = {0, 3010, 1641, 1638}
CODE_MEANING = {
    0: "成功",
    3010: "成功（需要重启才能完全生效）",
    1641: "成功（安装程序已自动重启）",
    1638: "已安装该产品的其它版本（视为已就绪）",
    1603: "安装失败（致命错误）",
    1618: "另一个安装正在进行中，请稍后重试",
    1619: "安装包无法打开（下载可能不完整）",
    1620: "安装包无法打开（文件损坏）",
    1260: "组策略禁止了本次安装",
    5: "拒绝访问（需要管理员权限）",
}

_TIMEOUT_PROBE = 5.0


# ---------------------------------------------------------------------------
# 底层工具
# ---------------------------------------------------------------------------
def _no_window() -> dict[str, Any]:
    if plat.IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def _run(command: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """执行外部命令，返回 (returncode, 合并输出)。失败不抛异常。"""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, **_no_window()
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, "命令超时"
    except FileNotFoundError:
        return -2, f"找不到命令：{command[0]}"
    except Exception as exc:  # pragma: no cover
        return -3, repr(exc)


def _decode(data: bytes) -> str:
    """Windows 中文控制台输出多为 GBK，尽量解出来。"""
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------
def service_state() -> dict[str, Any]:
    """查询 ``Apple Mobile Device Service`` 的状态。"""
    if not plat.IS_WINDOWS:
        return {"applicable": False, "exists": False, "running": False, "state": "n/a"}
    code, output = _run(["sc", "query", SERVICE_NAME], timeout=15)
    text = output.upper()
    exists = code == 0 and "1060" not in output
    running = "RUNNING" in text
    state = "unknown"
    for token in ("RUNNING", "STOPPED", "START_PENDING", "STOP_PENDING", "PAUSED"):
        if token in text:
            state = token
            break
    if not exists:
        state = "not_installed"
    return {
        "applicable": True,
        "exists": exists,
        "running": running,
        "state": state,
        "raw": output.strip(),
    }


def start_service() -> tuple[bool, str]:
    """尝试启动服务。已有的服务只是没启动，是"看不到设备"最常见的原因。"""
    if not plat.IS_WINDOWS:
        return False, "非 Windows 平台"
    code, output = _run(["sc", "start", SERVICE_NAME], timeout=60)
    if code == 0 or "1056" in output:  # 1056 = 已在运行
        return True, "服务已启动"
    return False, output.strip() or f"sc start 返回 {code}"


def usbmux_probe(
    message_type: str = "ReadBUID",
    host: str = USBMUX_HOST,
    port: int = USBMUX_PORT,
    timeout: float = _TIMEOUT_PROBE,
) -> dict[str, Any]:
    """在 27015 上做一次 usbmuxd 协议往返 —— **判断可用性的唯一权威信号**。

    ``ReadBUID`` 不需要插设备，因此可以在"有没有手机"之外单独验证服务是否真的工作。

    协议：16 字节小端头 ``<IIII`` = (length, version=1, message=8, tag)，
    然后是 XML plist；``length`` 含头。
    """
    body = {
        "MessageType": message_type,
        "ProgName": "iOSLocationTrack",
        "ClientVersionString": "iOSLocationTrack 1.0",
        "kLibUSBMuxVersion": 3,
    }
    try:
        payload = plistlib.dumps(body)
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"构造 plist 失败：{exc}"}

    header = struct.pack("<IIII", 16 + len(payload), 1, 8, 1)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(header + payload)
            raw = b""
            while len(raw) < 4:
                chunk = sock.recv(4 - len(raw))
                if not chunk:
                    break
                raw += chunk
            if len(raw) < 4:
                return {"ok": False, "error": "连接被对端关闭（无响应头）"}
            total = struct.unpack("<I", raw[:4])[0]
            if not (16 <= total <= 4 * 1024 * 1024):
                return {"ok": False, "error": f"响应长度异常：{total}"}
            while len(raw) < total:
                chunk = sock.recv(total - len(raw))
                if not chunk:
                    break
                raw += chunk
    except ConnectionRefusedError:
        return {"ok": False, "error": f"{host}:{port} 拒绝连接（服务未运行或未安装）"}
    except socket.timeout:
        return {"ok": False, "error": f"{host}:{port} 连接超时"}
    except OSError as exc:
        return {"ok": False, "error": f"{host}:{port} 不可达：{exc}"}

    try:
        length, version, message, tag = struct.unpack("<IIII", raw[:16])
        parsed = plistlib.loads(raw[16:total])
    except Exception as exc:
        return {"ok": False, "error": f"解析响应失败：{exc}"}

    # 成功的两种形态：{"MessageType":"Result","Number":0} 或直接带数据（如 ListDevices）
    if isinstance(parsed, dict):
        number = parsed.get("Number")
        if parsed.get("MessageType") == "Result" and number not in (0, None):
            return {
                "ok": False,
                "error": f"usbmuxd 返回错误码 {number}",
                "plist": parsed,
            }
    return {
        "ok": True,
        "plist": parsed,
        "header": {"length": length, "version": version, "message": message, "tag": tag},
    }


def list_devices() -> dict[str, Any]:
    """通过 usbmuxd 列出设备（USB + Wi-Fi）。"""
    result = usbmux_probe("ListDevices")
    if not result.get("ok"):
        return result
    devices = (result.get("plist") or {}).get("DeviceList") or []
    out = []
    for item in devices:
        props = item.get("Properties") or {}
        out.append(
            {
                "device_id": item.get("DeviceID"),
                "connection": props.get("ConnectionType", "?"),
                "product_id": props.get("ProductID"),
                "serial": props.get("SerialNumber", ""),
            }
        )
    return {"ok": True, "devices": out, "count": len(out)}


def _registry_views():
    """枚举 32/64 位两个注册表视图的 Uninstall 键（32 位 AMDS 落在 WOW6432Node）。"""
    import winreg  # type: ignore[import-not-found]

    root = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for sub in ("", r"\WOW6432Node"):
                path = root + sub if sub else root
                try:
                    with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as key:
                        count = winreg.QueryInfoKey(key)[0]
                        for index in range(count):
                            try:
                                name = winreg.EnumKey(key, index)
                                with winreg.OpenKey(key, name) as entry:
                                    yield entry
                            except OSError:
                                continue
                except OSError:
                    continue


def installed_products() -> list[dict[str, Any]]:
    """从注册表读出 Apple 相关产品的版本信息（仅用于展示/诊断）。"""
    if not plat.IS_WINDOWS:
        return []
    wanted = ("apple mobile device support", "itunes", "apple application support",
              "apple devices", "bonjour")
    found: dict[str, dict[str, Any]] = {}
    for entry in _registry_views():
        try:
            import winreg  # type: ignore[import-not-found]

            display = str(winreg.QueryValueEx(entry, "DisplayName")[0])
        except OSError:
            continue
        low = display.lower()
        if not any(w in low for w in wanted):
            continue
        record: dict[str, Any] = {"display_name": display}
        for field in ("DisplayVersion", "Publisher", "InstallLocation", "UninstallString"):
            try:
                import winreg  # type: ignore[import-not-found]

                value = winreg.QueryValueEx(entry, field)[0]
                record[field.lower()] = str(value)
            except OSError:
                continue
        # 用 UpgradeCode 精确识别 AMDS（ProductCode 不稳定，不能硬编码）
        try:
            import winreg  # type: ignore[import-not-found]

            props = winreg.OpenKey(entry, "InstallProperties")
            with props:
                upgrade = str(winreg.QueryValueEx(props, "UpgradeCode")[0])
                record["upgrade_code"] = upgrade
        except OSError:
            pass
        key = f"{display}|{record.get('displayversion','')}"
        found[key] = record
    return sorted(found.values(), key=lambda r: r["display_name"])


def amds_product() -> dict[str, Any] | None:
    """返回已安装的 AMDS 记录（按 UpgradeCode 或显示名匹配）。"""
    for item in installed_products():
        if (item.get("upgrade_code") or "").upper() == AMDS_UPGRADE_CODE.upper():
            return item
        if (item.get("display_name") or "").strip().lower() == AMDS_DISPLAY_NAME.lower():
            return item
    return None


def usb_driver_status() -> dict[str, Any]:
    """检查 iPhone 的 USB 驱动绑定情况。

    **不要**用 ``usbaapl`` 判断是否存在 —— 现代机器上健康设备绑定的是 ``WINUSB``/
    ``usbccgp``（驱动文件 ``appleusb.inf``）。
    """
    if not plat.IS_WINDOWS:
        return {"applicable": False}
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return {"applicable": True, "checked": False, "error": "找不到 PowerShell"}
    script = (
        "Get-PnpDevice -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InstanceId -like '*VID_05AC*' } | "
        "Select-Object Status,Class,FriendlyName,InstanceId,Service | ConvertTo-Json -Compress"
    )
    code, output = _run([ps, "-NoProfile", "-NonInteractive", "-Command", script], timeout=45)
    devices: list[dict[str, Any]] = []
    if output.strip():
        try:
            data = json.loads(output.strip())
            devices = data if isinstance(data, list) else [data]
        except Exception:
            devices = []
    healthy_tokens = ("usbaapl", "winusb", "appleusb", "usbccgp")
    bound = [
        d for d in devices
        if any(t in str(d.get("Service") or "").lower() for t in healthy_tokens)
    ]
    # MTP（照片）设备绑定 WUDFWpdMtp，不能算作"定位可用"，但说明线材/端口 OK
    return {
        "applicable": True,
        "checked": True,
        "devices": [
            {
                "name": d.get("FriendlyName"),
                "status": d.get("Status"),
                "service": d.get("Service"),
                "instance": d.get("InstanceId"),
            }
            for d in devices
        ],
        "bound_count": len(bound),
        "any_apple_device": len(devices) > 0,
    }


def status(deep: bool = True) -> dict[str, Any]:
    """汇总状态。``deep=True`` 时做 27015 协议往返（权威可用性判定）。"""
    if not plat.IS_WINDOWS:
        return {
            "applicable": False,
            "ready": True,
            "summary": "非 Windows 平台，usbmuxd 由系统自带",
        }
    service = service_state()
    product = amds_product()
    probe: dict[str, Any] = {"ok": False, "error": "未检测"}
    devices: dict[str, Any] = {"ok": False, "count": 0, "devices": []}
    if deep:
        probe = usbmux_probe("ReadBUID")
        if probe.get("ok"):
            devices = list_devices()

    ready = bool(service["running"] and probe.get("ok"))
    if ready:
        summary = "Apple Mobile Device Service 正在运行，usbmuxd 可通信"
    elif not service["exists"]:
        summary = "未安装 Apple Mobile Device Support（需要安装才能发现 iPhone）"
    elif not service["running"]:
        summary = "Apple Mobile Device Service 已安装但未运行"
    else:
        summary = "服务在运行，但 usbmuxd（127.0.0.1:27015）不可通信"

    return {
        "applicable": True,
        "ready": ready,
        "summary": summary,
        "needs_install": not service["exists"] and product is None,
        "needs_start": bool(service["exists"] and not service["running"]),
        "service": service,
        "product": product,
        "probe": {k: v for k, v in probe.items() if k != "plist"},
        "devices": devices,
        "driver": usb_driver_status() if deep else {"checked": False},
        "is_admin": plat.is_admin(),
    }


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------
def _download(url: str, dest: Path, log: Callable[[str, str], None] | None = None,
              expected_sha256: str | None = None) -> tuple[bool, str]:
    """下载文件并可选校验 SHA256。支持断点续传（.part）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    def emit(level: str, message: str) -> None:
        if log:
            log(level, message)

    if dest.exists() and expected_sha256:
        if _sha256(dest) == expected_sha256.lower():
            return True, f"已存在且校验通过：{dest.name}"

    emit("info", f"下载 {url.split('/')[-1]} …")
    request = urllib.request.Request(url, headers={"User-Agent": "iOSLocationTrack/1.0"})
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    try:
        handlers: list[Any] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(request, timeout=60) as response, open(part, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            last_report = 0.0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if total and now - last_report > 2.0:
                    last_report = now
                    emit("info", f"  … {done/1048576:.1f}/{total/1048576:.1f} MB "
                                 f"({done*100//total}%)")
    except Exception as exc:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"下载失败：{exc}"

    if expected_sha256:
        actual = _sha256(part)
        if actual != expected_sha256.lower():
            part.unlink(missing_ok=True)
            return False, (f"SHA256 校验失败：期望 {expected_sha256[:16]}…，"
                           f"实际 {actual[:16]}…（文件可能已更新，请报告）")
    try:
        part.replace(dest)
    except OSError as exc:
        return False, f"保存失败：{exc}"
    return True, f"已下载：{dest}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _msiexec(msi: Path, log: Callable[[str, str], None] | None = None,
             extra: list[str] | None = None) -> tuple[int, str]:
    """静默安装一个 MSI，返回 (退出码, 输出)。"""
    logfile = plat.data_dir() / "logs" / f"msi-{msi.stem}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "msiexec", "/i", str(msi), "/qn", "/norestart", "/passive",
        f"/l*v", str(logfile),
    ]
    if extra:
        command += extra
    # /qn 与 /passive 同时给出时以 /qn 为准；这里保留 /qn 保证全静默
    command = [c for c in command if c != "/passive"]
    if log:
        log("info", f"执行：{' '.join(command)}")
    code, output = _run(command, timeout=1800)
    return code, output


def install_amds(log: Callable[[str, str], None] | None = None,
                 allow_download: bool = True) -> dict[str, Any]:
    """策略 B：只装 Apple Mobile Device Support（约 38 MB，推荐）。

    顺序：Apple Application Support x64 → AMDS x64。iTunes 包自己就是这个顺序。
    """
    def emit(level: str, message: str) -> None:
        if log:
            log(level, message)

    if not plat.IS_WINDOWS:
        return {"ok": False, "error": "仅 Windows 支持"}
    if not plat.is_admin():
        return {"ok": False, "error": "需要管理员权限才能安装 Apple Mobile Device Support",
                "needs_admin": True}
    if not allow_download:
        return {"ok": False, "error": "已禁用下载"}

    arch64 = struct.calcsize("P") * 8 == 64
    cache = plat.data_dir() / "installers"
    steps: list[dict[str, Any]] = []

    plans = []
    if arch64:
        plans.append(("Apple Application Support x64", AAS_MSI_X64_URL, None, True))
    url = AMDS_MSI_X64_URL if arch64 else AMDS_MSI_X86_URL
    sha = AMDS_MSI_X64_SHA256 if arch64 else AMDS_MSI_X86_SHA256
    plans.append(("Apple Mobile Device Support", url, sha, False))

    for label, url, sha, optional in plans:
        name = url.rsplit("/", 1)[-1]
        target = cache / name
        emit("info", f"=== {label} ===")
        ok, message = _download(url, target, log, sha)
        if not ok:
            if optional:
                emit("warn", f"{label} 下载失败（可选前置，继续）：{message}")
                steps.append({"step": label, "ok": False, "skipped": True, "message": message})
                continue
            return {"ok": False, "error": message, "steps": steps}
        emit("info", message)
        code, output = _msiexec(target, log)
        meaning = CODE_MEANING.get(code, f"退出码 {code}")
        emit("info" if code in SUCCESS_CODES else "error", f"{label}：{meaning}")
        steps.append({"step": label, "code": code, "ok": code in SUCCESS_CODES,
                      "message": meaning})
        if code not in SUCCESS_CODES:
            tail = "\n".join(output.strip().splitlines()[-8:])
            return {"ok": False, "error": f"{label} 安装失败：{meaning}",
                    "steps": steps, "output": tail}

    return {"ok": True, "steps": steps, "strategy": "amds_msi",
            "reboot_required": any(s.get("code") in (3010, 1641) for s in steps)}


def install_itunes(log: Callable[[str, str], None] | None = None) -> dict[str, Any]:
    """策略 A：装完整 iTunes 包（约 193 MB，Apple 官方推荐路径，最稳）。"""
    def emit(level: str, message: str) -> None:
        if log:
            log(level, message)

    if not plat.IS_WINDOWS:
        return {"ok": False, "error": "仅 Windows 支持"}
    if not plat.is_admin():
        return {"ok": False, "error": "需要管理员权限才能安装 iTunes", "needs_admin": True}

    cache = plat.data_dir() / "installers"
    target = cache / "iTunes64Setup.exe"
    ok, message = _download(ITUNES_WIN64_URL, target, log, None)
    if not ok:
        return {"ok": False, "error": message}
    emit("info", f"{message}（{target.stat().st_size/1048576:.0f} MB）")
    emit("warn", "安装期间 iTunes / Outlook 可能被自动关闭，请先保存工作")
    command = [str(target), "/quiet", "/norestart", "/log",
               str(plat.data_dir() / "logs" / "itunes-install.log")]
    emit("info", f"执行：{' '.join(command)}（可能需要几分钟）")
    code, output = _run(command, timeout=3600)
    meaning = CODE_MEANING.get(code, f"退出码 {code}")
    ok = code in SUCCESS_CODES
    emit("info" if ok else "error", f"iTunes 安装：{meaning}")
    return {"ok": ok, "strategy": "itunes_bundle", "code": code, "message": meaning,
            "reboot_required": code in (3010, 1641),
            "error": None if ok else f"iTunes 安装失败：{meaning}"}


def install_winget(log: Callable[[str, str], None] | None = None) -> dict[str, Any]:
    """策略 C：用 winget 装（依赖 winget 可用且能联网）。"""
    def emit(level: str, message: str) -> None:
        if log:
            log(level, message)

    if not plat.IS_WINDOWS:
        return {"ok": False, "error": "仅 Windows 支持"}
    winget = shutil.which("winget")
    if not winget:
        return {"ok": False, "error": "未找到 winget"}
    last: dict[str, Any] = {"ok": False, "error": "所有 winget 包都失败"}
    for package_id in ("Apple.AppleMobileDeviceSupport", "Apple.iTunes"):
        command = [winget, "install", "--id", package_id, "--exact", "--silent",
                   "--accept-package-agreements", "--accept-source-agreements",
                   "--disable-interactivity"]
        emit("info", f"执行：{' '.join(command)}")
        code, output = _run(command, timeout=3600)
        if code == 0:
            emit("info", f"{package_id} 安装完成")
            return {"ok": True, "strategy": f"winget:{package_id}"}
        emit("warn", f"{package_id} 失败（{code}）：{output.strip()[-300:]}")
        last = {"ok": False, "error": f"{package_id} 安装失败（退出码 {code}）",
                "output": output.strip()[-500:]}
    return last


def ensure(
    log: Callable[[str, str], None] | None = None,
    allow_install: bool = True,
    strategy: str = "auto",
) -> dict[str, Any]:
    """**主入口**：检测 → 必要时安装 → 复检。

    ``strategy``: ``auto``（推荐）| ``msi`` | ``itunes`` | ``winget`` | ``none``
    """
    def emit(level: str, message: str) -> None:
        if log:
            log(level, message)

    if not plat.IS_WINDOWS:
        return {"ok": True, "applicable": False, "action": "none",
                "status": status(deep=False)}

    before = status(deep=True)
    if before["ready"]:
        emit("info", "Apple Mobile Device Support 已就绪，无需安装")
        return {"ok": True, "applicable": True, "action": "none", "status": before}

    # 已安装但没运行 —— 先尝试启动（最常见、也最便宜）
    if before["needs_start"]:
        emit("warn", "服务已安装但未运行，尝试启动…")
        ok, message = start_service()
        emit("info" if ok else "warn", message)
        if ok:
            time.sleep(1.5)
            after_start = status(deep=True)
            if after_start["ready"]:
                return {"ok": True, "applicable": True, "action": "start_service",
                        "status": after_start}

    if not allow_install or strategy == "none":
        return {"ok": False, "applicable": True, "action": "none",
                "needs_install": True, "status": before,
                "error": before["summary"]}

    if not plat.is_admin():
        return {"ok": False, "applicable": True, "action": "blocked",
                "needs_admin": True, "status": before,
                "error": "安装 Apple Mobile Device Support 需要管理员权限，"
                         "请以管理员身份重新运行程序"}

    emit("warn", f"未检测到可用的 usbmuxd：{before['summary']}")
    emit("info", "开始自动安装 Apple Mobile Device Support（首次约 40~200 MB）…")

    attempts: list[dict[str, Any]] = []
    order = {
        "auto": [("msi", install_amds), ("itunes", install_itunes), ("winget", install_winget)],
        "msi": [("msi", install_amds)],
        "itunes": [("itunes", install_itunes)],
        "winget": [("winget", install_winget)],
    }.get(strategy) or []

    for name, function in order:
        emit("info", f"--- 安装策略：{name} ---")
        result = function(log)
        attempts.append({"strategy": name, **{k: v for k, v in result.items()
                                              if k not in ("steps",)}})
        if result.get("ok"):
            if name == "msi" and not result.get("ok"):
                continue
        # 无论成功失败都让服务有机会起来，然后复检
        if result.get("ok") or result.get("reboot_required"):
            service_state()
            start_service()
            time.sleep(2.0)
            after = status(deep=True)
            if after["ready"]:
                return {"ok": True, "applicable": True, "action": f"installed:{name}",
                        "status": after, "attempts": attempts,
                        "reboot_required": bool(result.get("reboot_required"))}
            if result.get("reboot_required"):
                return {"ok": False, "applicable": True, "action": f"installed:{name}",
                        "needs_reboot": True, "status": after, "attempts": attempts,
                        "error": "安装完成但需要重启 Windows 后 usbmuxd 才会生效"}
        if result.get("error"):
            emit("warn", f"策略 {name} 未成功：{result['error']}")

    final = status(deep=True)
    if final["ready"]:
        return {"ok": True, "applicable": True, "action": "installed",
                "status": final, "attempts": attempts}
    return {
        "ok": False, "applicable": True, "action": "failed", "status": final,
        "attempts": attempts,
        "error": "自动安装未能建立可用的 usbmuxd。可手动安装 iTunes："
                 "https://www.apple.com/itunes/download/win64",
    }


USBMUX_PIPE = r"\\.\pipe\usbmuxd"


# ---------------------------------------------------------------------------
# 开发者镜像（个性化镜像，iOS 17+ 全系共用）
# ---------------------------------------------------------------------------
#: 镜像来源：doronz88/DeveloperDiskImage（pymobiledevice3 官方使用的仓库）
DDI_REPO = "doronz88/DeveloperDiskImage"
DDI_BRANCH = "main"
DDI_DIR = "PersonalizedImages/Xcode_iOS_DDI_Personalized"
#: 文件名 → 最小合理体积（用于判断下载是否完整）
DDI_FILES = {
    "Image.dmg": 10 * 1024 * 1024,
    "BuildManifest.plist": 100 * 1024,
    "Image.dmg.trustcache": 256,
}
#: 本地存放位置（相对项目/资源根）
DDI_SUBDIR = ("resources", "ddi", "personalized")


def ddi_dir(root: Path | str | None = None) -> Path:
    """返回开发者镜像的本地目录。"""
    base = Path(root) if root else plat.resolve_roots()[0]
    return base.joinpath(*DDI_SUBDIR)


def ddi_status(root: Path | str | None = None) -> dict[str, Any]:
    """检查本地开发者镜像素材是否齐全。"""
    directory = ddi_dir(root)
    files: dict[str, Any] = {}
    missing: list[str] = []
    total = 0
    for name, minimum in DDI_FILES.items():
        path = directory / name
        if path.exists():
            size = path.stat().st_size
            ok = size >= minimum
            total += size
            files[name] = {"exists": True, "size": size, "ok": ok}
            if not ok:
                missing.append(name)
        else:
            files[name] = {"exists": False, "size": 0, "ok": False}
            missing.append(name)
    return {
        "ok": not missing,
        "dir": str(directory),
        "files": files,
        "missing": missing,
        "total_mb": round(total / 1048576, 2),
    }


def _ddi_urls(name: str) -> list[str]:
    path = f"{DDI_DIR}/{name}"
    return [
        f"https://raw.githubusercontent.com/{DDI_REPO}/{DDI_BRANCH}/{path}",
        f"https://cdn.jsdelivr.net/gh/{DDI_REPO}@{DDI_BRANCH}/{path}",
    ]


def download_developer_image(
    log: Callable[[str, str], None] | None = None,
    root: Path | str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """下载缺失的个性化开发者镜像（约 15 MB）。

    按「GitHub 直连 → jsDelivr 镜像」的顺序重试，与 tools/fetch_ddi.py 的策略一致。
    供「环境准备」窗口的「安装」按钮调用。
    """
    def emit(level: str, message: str) -> None:
        if log:
            log(level, message)

    directory = ddi_dir(root)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"无法创建目录 {directory}：{exc}"}

    current = ddi_status(root)
    if current["ok"] and not force:
        return {"ok": True, "action": "none", "status": current,
                "detail": f"已就绪（{current['total_mb']} MB）"}

    for name, minimum in DDI_FILES.items():
        target = directory / name
        if not force and target.exists() and target.stat().st_size >= minimum:
            emit("info", f"{name} 已存在，跳过")
            continue
        last_error = ""
        downloaded = False
        for url in _ddi_urls(name):
            ok, message = _download(url, target, log, None)
            if ok and target.exists() and target.stat().st_size >= minimum:
                emit("info", f"{name}：{target.stat().st_size/1048576:.2f} MB")
                downloaded = True
                break
            last_error = message
            emit("warn", f"{name} 从 {url.split('/')[2]} 下载失败，尝试备用源…")
        if not downloaded:
            return {"ok": False, "error": f"{name} 下载失败：{last_error}",
                    "status": ddi_status(root)}

    after = ddi_status(root)
    if not after["ok"]:
        return {"ok": False, "error": "镜像文件仍不完整：" + "、".join(after["missing"]),
                "status": after}
    return {"ok": True, "action": "downloaded", "status": after,
            "detail": f"已下载（{after['total_mb']} MB）"}
