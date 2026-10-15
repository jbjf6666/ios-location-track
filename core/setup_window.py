"""启动时的「环境准备」窗口（Windows）。

需求：每次打开 exe 都弹出一个窗口，列出所有需要的依赖：
  - 没装的在右侧有「安装」按钮
  - 装好的显示 ✓
  - 必需项全部就绪后，「进入程序」按钮才可点击

设计要点：
- 用标准库 **tkinter**（已随 Python 打包进 exe），因此不依赖机器上有浏览器/网络，
  也不依赖我们已经启动的 HTTP 服务 —— 它必须能作为"第一个界面"独立工作。
- 检测与安装都放在后台线程，通过队列回传 UI 线程（tkinter 不是线程安全的）。
- 任何异常都不能让程序打不开：tkinter 不可用时返回 None，由调用方降级处理。
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Callable

from . import apple_driver
from . import platform as plat

# 颜色（与 web 端保持同一套语义色）
COLOR_OK = "#1a7f37"
COLOR_BAD = "#c0392b"
COLOR_BUSY = "#b7791f"
COLOR_IDLE = "#6b7280"
COLOR_BG = "#ffffff"
COLOR_MUTED = "#57606a"

ICON_OK = "✓"
ICON_BAD = "✗"
ICON_BUSY = "…"
ICON_OPT = "·"
ICON_IDLE = "○"


class EnvItem:
    """一个待检查/待安装的依赖项。"""

    def __init__(
        self,
        key: str,
        label: str,
        description: str,
        check: Callable[[], tuple[bool, str]],
        install: Callable[[Callable[[str, str], None]], tuple[bool, str]] | None = None,
        required: bool = True,
        install_label: str = "安装",
        size_hint: str = "",
    ) -> None:
        self.key = key
        self.label = label
        self.description = description
        self.check = check
        self.install = install
        self.required = required
        self.install_label = install_label
        self.size_hint = size_hint


# ---------------------------------------------------------------------------
# 各个依赖项的检测 / 安装实现
# ---------------------------------------------------------------------------
def _check_python() -> tuple[bool, str]:
    import sys

    return True, f"已内置 Python {sys.version.split()[0]}"


def _check_pymobiledevice3() -> tuple[bool, str]:
    try:
        import pymobiledevice3

        version = getattr(pymobiledevice3, "__version__", None) or "已安装"
        return True, f"已内置 pymobiledevice3 {version}"
    except Exception as exc:
        return False, f"导入失败：{exc}"


def _check_tunnel_deps() -> tuple[bool, str]:
    """userspace 隧道依赖（纯 Python 协议栈，**不需要 Wintun 驱动**）。"""
    missing = []
    for module in ("pytun_pmd3", "pmd_pytcp", "pmd_net_addr"):
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    if missing:
        return False, "缺少：" + "、".join(missing)
    try:
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel  # noqa: F401

        return True, "已内置（纯 Python 用户态协议栈，无需 Wintun 驱动、无需管理员权限）"
    except Exception as exc:
        return False, f"隧道模块导入失败：{exc}"


def _check_apple_driver() -> tuple[bool, str]:
    status = apple_driver.status(deep=True)
    if not status.get("applicable", True):
        return True, "非 Windows 平台，系统自带 usbmuxd"
    if status.get("ready"):
        product = status.get("product") or {}
        version = product.get("displayversion")
        devices = (status.get("devices") or {}).get("count", 0)
        detail = "usbmuxd 可通信"
        if version:
            detail = f"v{version}，{detail}"
        if devices:
            detail += f"，已发现 {devices} 台设备"
        return True, detail
    return False, status.get("summary") or "不可用"


def _install_apple_driver(log: Callable[[str, str], None]) -> tuple[bool, str]:
    if not plat.is_admin():
        return False, "需要管理员权限（本程序启动时应已提权）"
    result = apple_driver.ensure(log=log, allow_install=True)
    if result.get("ok"):
        if result.get("reboot_required"):
            return True, "安装完成，但需要重启 Windows 才生效"
        return True, "安装完成"
    return False, result.get("error") or "安装失败"


def _check_developer_image() -> tuple[bool, str]:
    from . import apple_driver as ad

    status = ad.ddi_status()
    if status["ok"]:
        return True, f"已就绪（{status['total_mb']} MB，iOS 17 及以上全系共用）"
    return False, "缺少：" + "、".join(status["missing"])


def _install_developer_image(log: Callable[[str, str], None]) -> tuple[bool, str]:
    result = apple_driver.download_developer_image(log=log)
    if result.get("ok"):
        return True, result.get("detail") or "下载完成"
    return False, result.get("error") or "下载失败"


def _check_permission() -> tuple[bool, str]:
    if plat.is_admin():
        return True, "已以管理员身份运行"
    return False, "未提权（安装 Apple 驱动和管理员操作会失败）"


def _check_device() -> tuple[bool, str]:
    if not plat.IS_WINDOWS:
        return True, "非 Windows 平台"
    result = apple_driver.list_devices()
    if not result.get("ok"):
        return False, str(result.get("error") or "usbmuxd 不可用")
    count = result.get("count", 0)
    if count == 0:
        return False, "未检测到 iPhone（用数据线连接、解锁并点「信任」）"
    first = result["devices"][0]
    return True, f"{count} 台（{first.get('serial', '')}，{first.get('connection', '?')}）"


def _check_network() -> tuple[bool, str]:
    """Apple TSS 签名需要联网（挂载开发者镜像时用）。"""
    import socket

    hosts = [("gs.apple.com", 443), ("raw.githubusercontent.com", 443)]
    reachable = []
    for host, port in hosts:
        try:
            with socket.create_connection((host, port), timeout=6):
                reachable.append(host)
        except OSError:
            continue
    if reachable:
        return True, "可连通（" + "、".join(reachable) + "）"
    return False, "无法连接 Apple 服务器（挂载开发者镜像时需要联网签名）"


def build_items() -> list[EnvItem]:
    items = [
        EnvItem("python", "Python 运行时", "程序自带的解释器",
                _check_python, None, required=True),
        EnvItem("pymobiledevice3", "pymobiledevice3", "与 iOS 设备通信的核心库",
                _check_pymobiledevice3, None, required=True),
    ]
    if plat.IS_WINDOWS:
        items.append(EnvItem(
            "apple_driver", "Apple Mobile Device Support",
            "Windows 上的 usbmuxd（127.0.0.1:27015），用来发现 iPhone",
            _check_apple_driver, _install_apple_driver, required=True,
            install_label="安装", size_hint="约 40~200 MB",
        ))
    items.append(EnvItem(
        "tunnel_deps", "RSD 隧道依赖",
        "iOS 17+ 的用户态隧道（PyTCP）",
        _check_tunnel_deps, None, required=True,
    ))
    items.append(EnvItem(
        "developer_image", "开发者镜像",
        "iOS 17 及以上全系共用的个性化镜像素材",
        _check_developer_image, _install_developer_image, required=True,
        install_label="下载", size_hint="约 15 MB",
    ))
    items.append(EnvItem(
        "permission", "管理员权限",
        "安装驱动与系统级操作需要",
        _check_permission, None, required=True,
    ))
    items.append(EnvItem(
        "device", "iPhone 连接",
        "可选项：现在没插也可以，之后接上再点「重新检测」",
        _check_device, None, required=False,
    ))
    items.append(EnvItem(
        "network", "网络连通",
        "可选项：挂载开发者镜像时 Apple 需要在线签名",
        _check_network, None, required=False,
    ))
    return items


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------
class SetupWindow:
    """环境准备窗口。所有耗时操作都在后台线程里跑。"""

    def __init__(self, root: Any, items: list[EnvItem] | None = None,
                 title: str = "环境准备") -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.items = items if items is not None else build_items()
        self.state: dict[str, dict[str, Any]] = {
            item.key: {"ok": None, "detail": "等待检测…", "busy": False}
            for item in self.items
        }
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.installing = False
        self.proceeded = False
        self.auto_install = False
        self._rows: dict[str, dict[str, Any]] = {}

        root.title(f"{title} — iOS 虚拟定位轨迹工具")
        root.configure(bg=COLOR_BG)
        # 先建内容再定尺寸：否则 Tk 会按控件的"要求尺寸"把窗口撑高，
        # geometry() 设的高度会被覆盖，底部的「进入程序」按钮就被挤出屏幕。
        self._build(title)
        root.resizable(False, True)
        self._fit_to_screen(root)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._pump)
        self.refresh_all()

    # ---------------- 布局 ----------------
    @staticmethod
    def _fit_to_screen(window: Any, min_width: int = 820, min_height: int = 560) -> None:
        """按内容实际需要的高度定尺寸，并保证窗口完整落在屏幕内。"""
        window.update_idletasks()
        try:
            screen_w = window.winfo_screenwidth()
            screen_h = window.winfo_screenheight()
            # 控件"要求"的尺寸 + 一点余量
            need_w = max(min_width, window.winfo_reqwidth() + 24)
            need_h = window.winfo_reqheight() + 24
            width = min(need_w, max(640, screen_w - 60))
            height = min(need_h, max(min_height, screen_h - 100))
            x = max(0, (screen_w - width) // 2)
            y = max(0, (screen_h - height) // 3)
            window.geometry(f"{width}x{height}+{x}+{y}")
            window.minsize(min(760, width), min(520, height))
        except Exception:
            window.geometry("960x760")

    def _build(self, title: str) -> None:
        tk, ttk = self.tk, self.ttk
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        header = tk.Frame(self.root, bg=COLOR_BG)
        header.pack(fill="x", padx=20, pady=(16, 6))
        tk.Label(header, text=title, font=("Microsoft YaHei UI", 15, "bold"),
                 bg=COLOR_BG).pack(anchor="w")
        tk.Label(
            header,
            text="首次使用需要准备以下组件。缺失的项目点右侧按钮即可自动安装；"
                 "全部就绪后点「进入程序」。",
            font=("Microsoft YaHei UI", 9), fg=COLOR_MUTED, bg=COLOR_BG,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        ttk.Separator(self.root).pack(fill="x", padx=20, pady=(10, 4))

        body = tk.Frame(self.root, bg=COLOR_BG)
        body.pack(fill="both", expand=False, padx=20)
        body.columnconfigure(1, weight=1)

        for row, item in enumerate(self.items):
            icon = tk.Label(body, text=ICON_IDLE, font=("Segoe UI Symbol", 13, "bold"),
                            fg=COLOR_IDLE, bg=COLOR_BG, width=2)
            icon.grid(row=row, column=0, sticky="nw", pady=(6, 0))

            text = tk.Frame(body, bg=COLOR_BG)
            text.grid(row=row, column=1, sticky="ew", padx=(2, 10), pady=(6, 0))
            name = tk.Label(text, text=item.label, font=("Microsoft YaHei UI", 10, "bold"),
                            bg=COLOR_BG, anchor="w")
            name.pack(anchor="w")
            detail = tk.Label(text, text=item.description, font=("Microsoft YaHei UI", 8),
                              fg=COLOR_MUTED, bg=COLOR_BG, anchor="w", justify="left",
                              wraplength=560)
            detail.pack(anchor="w")

            button = ttk.Button(body, text=item.install_label, width=10,
                                command=lambda i=item: self.install_one(i))
            button.grid(row=row, column=2, sticky="ne", pady=(6, 0))
            if item.install is None:
                button.state(["disabled"])

            tag = tk.Label(body, text="必需" if item.required else "可选",
                           font=("Microsoft YaHei UI", 8),
                           fg=COLOR_BAD if item.required else COLOR_MUTED,
                           bg=COLOR_BG, width=5)
            tag.grid(row=row, column=3, sticky="ne", pady=(6, 0))

            self._rows[item.key] = {
                "icon": icon, "detail": detail, "button": button, "item": item
            }

        ttk.Separator(self.root).pack(fill="x", padx=20, pady=(12, 6))

        log_frame = tk.Frame(self.root, bg=COLOR_BG)
        log_frame.pack(fill="both", expand=True, padx=20)
        tk.Label(log_frame, text="安装日志", font=("Microsoft YaHei UI", 9, "bold"),
                 bg=COLOR_BG).pack(anchor="w")
        self.log = tk.Text(log_frame, height=6, wrap="word", font=("Consolas", 8),
                           bg="#f6f8fa", fg="#24292f", relief="flat",
                           highlightthickness=1, highlightbackground="#d0d7de")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True, pady=(3, 0))
        self.log.configure(state="disabled")

        footer = tk.Frame(self.root, bg=COLOR_BG)
        footer.pack(fill="x", padx=20, pady=(10, 16))

        self.status_label = tk.Label(footer, text="正在检测…",
                                     font=("Microsoft YaHei UI", 9), fg=COLOR_MUTED,
                                     bg=COLOR_BG)
        self.status_label.pack(side="left")

        self.confirm_button = ttk.Button(footer, text="进入程序", width=16,
                                        command=self._on_confirm)
        self.confirm_button.pack(side="right", padx=(8, 0))
        self.confirm_button.state(["disabled"])

        self.install_all_button = ttk.Button(footer, text="全部安装", width=12,
                                            command=self.install_all)
        self.install_all_button.pack(side="right", padx=(8, 0))

        self.recheck_button = ttk.Button(footer, text="重新检测", width=12,
                                        command=self.refresh_all)
        self.recheck_button.pack(side="right")

    # ---------------- 日志 ----------------
    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- 检测 ----------------
    def refresh_all(self) -> None:
        if self.installing:
            return
        for item in self.items:
            self.state[item.key] = {"ok": None, "detail": "检测中…", "busy": False}
            self._render(item.key)
        self._update_footer()
        threading.Thread(target=self._check_worker, name="env-check", daemon=True).start()

    def _check_worker(self) -> None:
        for item in self.items:
            try:
                ok, detail = item.check()
            except Exception as exc:
                ok, detail = False, f"检测异常：{exc}"
            self.events.put(("check", (item.key, ok, detail)))
        self.events.put(("checks_done", None))

    # ---------------- 安装 ----------------
    def install_one(self, item: EnvItem) -> None:
        if self.installing or item.install is None:
            return
        state = self.state[item.key]
        if state.get("ok") is True:
            return
        self.installing = True
        self.install_all_button.state(["disabled"])
        self.recheck_button.state(["disabled"])
        self._set_busy(item.key, "正在安装…")
        self.append_log(f"===== 开始安装：{item.label} =====")
        threading.Thread(target=self._install_worker, args=([item],),
                         name=f"install-{item.key}", daemon=True).start()

    def install_all(self) -> None:
        if self.installing:
            return
        pending = [i for i in self.items
                   if i.install is not None and self.state[i.key].get("ok") is not True]
        if not pending:
            self.append_log("没有需要安装的项目。")
            return
        self.installing = True
        self.install_all_button.state(["disabled"])
        self.recheck_button.state(["disabled"])
        for item in pending:
            self._set_busy(item.key, "排队中…")
        threading.Thread(target=self._install_worker, args=(pending,),
                         name="install-all", daemon=True).start()

    def _install_worker(self, items: list[EnvItem]) -> None:
        for item in items:
            self.events.put(("busy", (item.key, "正在安装…")))

            def log(level: str, message: str, _label: str = item.label) -> None:
                self.events.put(("log", f"[{_label}] {message}"))

            try:
                assert item.install is not None
                ok, detail = item.install(log)
            except Exception as exc:
                ok, detail = False, f"安装异常：{exc}"
            self.events.put(("install", (item.key, ok, detail)))
        self.events.put(("installs_done", None))

    # ---------------- UI 线程泵 ----------------
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "check":
                    key, ok, detail = payload
                    self.state[key] = {"ok": ok, "detail": detail, "busy": False}
                    self._render(key)
                elif kind == "checks_done":
                    self._update_footer()
                elif kind == "busy":
                    key, detail = payload
                    self.state[key]["busy"] = True
                    self.state[key]["detail"] = detail
                    self._render(key)
                elif kind == "install":
                    key, ok, detail = payload
                    self.state[key] = {"ok": ok, "detail": detail, "busy": False}
                    self._render(key)
                    self.append_log(("安装成功：" if ok else "安装失败：") + detail)
                elif kind == "installs_done":
                    self.installing = False
                    self.install_all_button.state(["!disabled"])
                    self.recheck_button.state(["!disabled"])
                    self._update_footer()
                    # 安装后可能影响其它项（例如驱动装好后设备就能发现了），重新检测一遍
                    self.refresh_all()
                elif kind == "log":
                    self.append_log(payload)
        except queue.Empty:
            pass
        if not self.proceeded:
            self.root.after(80, self._pump)

    # ---------------- 渲染 ----------------
    def _render(self, key: str) -> None:
        row = self._rows.get(key)
        if row is None:
            return
        state = self.state[key]
        item: EnvItem = row["item"]
        ok = state.get("ok")
        busy = state.get("busy")

        if busy:
            icon, color = ICON_BUSY, COLOR_BUSY
        elif ok is True:
            icon, color = ICON_OK, COLOR_OK
        elif ok is False:
            icon = ICON_BAD if item.required else ICON_OPT
            color = COLOR_BAD if item.required else COLOR_MUTED
        else:
            icon, color = ICON_IDLE, COLOR_IDLE

        row["icon"].configure(text=icon, fg=color)
        suffix = f"　（{item.size_hint}）" if item.size_hint and ok is not True else ""
        row["detail"].configure(text=f"{state.get('detail', '')}{suffix}",
                                fg=COLOR_OK if ok is True else COLOR_MUTED)

        button = row["button"]
        if ok is True:
            # 已就绪：按钮文字改成"已就绪"并禁用，避免用户误以为还要再装一次
            button.configure(text="已就绪")
            button.state(["disabled"])
        elif item.install is None:
            button.configure(text="—")
            button.state(["disabled"])
        elif busy:
            button.configure(text="安装中…")
            button.state(["disabled"])
        else:
            button.configure(text=item.install_label)
            button.state(["!disabled"])

    def _set_busy(self, key: str, detail: str) -> None:
        self.state[key]["busy"] = True
        self.state[key]["detail"] = detail
        self._render(key)

    def _update_footer(self) -> None:
        required = [i for i in self.items if i.required]
        self.state.get("__x__")
        pending = [i for i in required if self.state[i.key].get("ok") is not True]
        optional_bad = [i for i in self.items
                        if not i.required and self.state[i.key].get("ok") is False]

        if self.installing:
            self.status_label.configure(text="正在安装，请稍候…", fg=COLOR_BUSY)
        elif pending:
            names = "、".join(i.label for i in pending)
            self.status_label.configure(text=f"还差 {len(pending)} 项：{names}", fg=COLOR_BAD)
        else:
            text = "全部就绪，可以开始使用"
            if optional_bad:
                text += "（可选：" + "、".join(i.label for i in optional_bad) + " 未就绪）"
            self.status_label.configure(text=text, fg=COLOR_OK)

        if not pending and not self.installing:
            self.confirm_button.state(["!disabled"])
        else:
            self.confirm_button.state(["disabled"])

        any_missing = any(i.install is not None and self.state[i.key].get("ok") is not True
                          for i in self.items)
        if any_missing and not self.installing:
            self.install_all_button.state(["!disabled"])
        else:
            self.install_all_button.state(["disabled"])

    # ---------------- 结束 ----------------
    def _on_confirm(self) -> None:
        self.proceeded = True
        try:
            self.root.destroy()
        except Exception:
            pass

    def _on_close(self) -> None:
        self.proceeded = False
        try:
            self.root.destroy()
        except Exception:
            pass


def run_setup_window(title: str = "环境准备", auto_install_missing: bool = False) -> bool | None:
    """显示环境准备窗口。

    返回：
        True  —— 全部必需项就绪，用户点了「进入程序」
        False —— 用户关闭了窗口（调用方应退出）
        None  —— 无法显示窗口（tkinter 不可用/构建失败），调用方自行降级
    """
    import traceback

    try:
        import tkinter as tk
    except Exception as exc:
        print(f"  [提示] tkinter 不可用：{exc}")
        return None

    try:
        root = tk.Tk()
    except Exception as exc:
        print(f"  [提示] 无法创建窗口（可能没有图形界面）：{exc}")
        return None

    try:
        window = SetupWindow(root, title=title)
        if auto_install_missing:
            root.after(600, window.install_all)
        root.mainloop()
        return bool(window.proceeded)
    except Exception:
        # 这里必须把真实原因打出来，否则窗口不显示时无从排查
        print("  [错误] 「环境准备」窗口构建失败：")
        traceback.print_exc()
        try:
            root.destroy()
        except Exception:
            pass
        return None
