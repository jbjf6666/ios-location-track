# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（macOS / Windows 双平台）。

用法：
    macOS  : ./.venv/bin/python -m PyInstaller --clean --noconfirm Location.spec
    Windows: .venv\\Scripts\\python.exe -m PyInstaller --clean --noconfirm Location.spec

设计要点：
- **onedir**（不是 onefile）：启动更快，onefile 每次启动都要解包到临时目录，反而更慢。
- 前端资源（web/）与图标打进包内，运行时通过 sys._MEIPASS 定位。
- 排除 pymobiledevice3 带进来但本工具用不到的重型依赖（IPython / pytest 等），
  它们只被 pymobiledevice3 的 CLI 交互模式使用；本工具只用 Python API。
- **Windows 专属**：`uac_admin=True` —— 在 exe 清单里写入 requireAdministrator，
  启动时直接弹 UAC 提权。这样首次运行就能自动安装 Apple Mobile Device Support，
  不需要用户再手动"以管理员身份运行"。
  （注：userspace 隧道本身**不需要**管理员权限，提权只是为了装驱动。）

注意：Windows 与 macOS 的差异都集中在文件末尾的 `if IS_WINDOWS:` 分支里，
保持单一 spec，避免两份配置漂移。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

IS_WINDOWS = sys.platform == "win32"
ROOT = Path(SPECPATH).resolve()

# 有些包在运行期用 importlib.metadata 读自己的版本（例如 apple_compress 的 __version__，
# 它被 pyimg4 用于个性化镜像处理）。PyInstaller 默认不会打包 dist-info，
# 漏掉后挂载会报 "No package metadata was found for apple_compress"。
METADATA_PACKAGES = [
    "apple_compress", "pymobiledevice3", "pyimg4", "construct",
    "cryptography", "bpylist2", "ipsw_parser", "opack2",
    # Windows 用户态隧道依赖链
    "pytun_pmd3", "pmd_pytcp", "pmd_net_addr", "pmd_net_proto",
]

_metadata_datas: list = []
for _pkg in METADATA_PACKAGES:
    try:
        _metadata_datas += copy_metadata(_pkg)
    except Exception:
        pass

# pytun_pmd3 自带各架构的 wintun.dll（数据文件，PyInstaller 不会自动收集）。
# Windows 的 userspace 隧道走纯 Python PyTCP 栈，通常不需要它；但某些路径会用
# pytun_pmd3.TunTapDevice，带上以免运行期缺 dll。整包只有 ~1.5MB。
_extra_datas: list = []
try:
    _extra_datas += collect_data_files("pytun_pmd3", includes=["wintun/**"])
except Exception:
    pass

# 只排除"构建期工具"和"确定与运行无关的开发工具"。
# 依据（macOS 侧实测）：questionary / prompt_toolkit / pygments 被 pymobiledevice3
# 运行期导入，曾因误排导致打包版真机不可用；因此与 pymobiledevice3 相关的一律保留。
EXCLUDES = [
    # 注意：**不要**排除 distutils / pkg_resources / setuptools / wheel / pip
    # —— PyInstaller 与 setuptools 的 hook 会强制导入/别名它们，排除后构建直接失败：
    #   ValueError: Target module "distutils" already imported as ExcludedModule('distutils',)
    #   ValueError: Target module "wheel"     already imported as ExcludedModule('wheel',)
    # （两个坑都在本项目上实测复现过；wheel 那条在 Windows + setuptools 84 上触发。）
    # 它们体积很小，排除掉的收益远小于构建失败的风险。
    #
    # 开发/测试工具（pymobiledevice3 的交互式 CLI 才会用）
    "IPython", "ipython", "ipykernel", "jupyter", "jupyter_client", "jupyter_core",
    "ipywidgets", "notebook", "nbformat", "nbconvert",
    "pytest", "_pytest", "matplotlib", "matplotlib_inline",
    # 注意：**不要**排除 tkinter —— 「环境准备」窗口用它实现（标准库，随 exe 打包）。
]

# 运行期动态 import 的模块，PyInstaller 静态分析看不到
HIDDENIMPORTS = [
    "pymobiledevice3.remote.remote_service_discovery",
    "pymobiledevice3.remote.tunnel_service",
    "pymobiledevice3.services.dvt.instruments.dvt_provider",
    "pymobiledevice3.services.dvt.instruments.location_simulation",
    "pymobiledevice3.lockdown",
    "pymobiledevice3.usbmux",
    "pymobiledevice3.service_connection",
    "pymobiledevice3.exceptions",
    "pymobiledevice3.services.mobile_image_mounter",
    "developer_disk_image.repo",
    "core.apple_driver",
    "core.platform",
    "core.setup_window",
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
]
if IS_WINDOWS:
    HIDDENIMPORTS += [
        # Windows 走 in-process userspace 隧道（不是 macOS 的 native_tunnel）
        "pymobiledevice3.remote.userspace_tunnel",
        "pytun_pmd3",
        "pytun_pmd3.wintun",
        "pmd_pytcp",
        "pmd_net_addr",
        "pmd_net_proto",
        "winreg",
        "ctypes.wintypes",
    ]
else:
    HIDDENIMPORTS += ["pymobiledevice3.remote.native_tunnel"]

# 打进包内的资源
_datas = _metadata_datas + _extra_datas + [
    ("web", "web"),                                  # 前端（index.html / style.css / js/）
    # 开发者镜像素材（iOS 17+ 全系共用的个性化镜像；Image.dmg 约 15MB）
    ("resources/ddi", "resources/ddi"),
    ("docs/PROTOCOL.md", "docs"),
    ("README.md", "."),
]
if IS_WINDOWS:
    _datas += [
        ("resources/AppIcon.ico", "resources"),
        ("docs/README-WINDOWS.md", "docs") if (ROOT / "docs" / "README-WINDOWS.md").exists() else ("README.md", "docs"),
    ]
else:
    _datas += [("resources/AppIcon.icns", "resources")]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_datas,
    hiddenimports=HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if IS_WINDOWS:
    # ---------------- Windows：onedir + .exe（启动时请求管理员权限）----------------
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="iOSLocationTrack",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,            # 不弹黑窗；日志走界面与控制台无关
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ROOT / "resources" / "AppIcon.ico"),
        # ★ 启动时直接请求管理员权限（exe 清单 requestedExecutionLevel=requireAdministrator）。
        #   目的：首次运行即可自动安装 Apple Mobile Device Support，不需要用户手动右键提权。
        uac_admin=True,
        version=str(ROOT / "installer" / "version_info.txt")
        if (ROOT / "installer" / "version_info.txt").exists() else None,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="iOSLocationTrack",
    )
else:
    # ---------------- macOS：onedir + .app bundle ----------------
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="iOSLocationTrack",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ROOT / "resources" / "AppIcon.icns"),
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="iOSLocationTrack",
    )

    app = BUNDLE(
        coll,
        name="虚拟定位轨迹.app",
        icon=str(ROOT / "resources" / "AppIcon.icns"),
        bundle_identifier="com.local.ios-location-track",
        version="1.1.0",
        info_plist={
            "CFBundleName": "虚拟定位轨迹",
            "CFBundleDisplayName": "虚拟定位轨迹",
            "CFBundleShortVersionString": "1.1.0",
            "CFBundleVersion": "1.1.0",
            "CFBundlePackageType": "APPL",
            "CFBundleIconFile": "AppIcon",
            "CFBundleIconName": "AppIcon",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.developer-tools",
            "NSHumanReadableCopyright": "本地工具，用于自有 App 的定位功能测试",
            "NSAppleEventsUsageDescription": "用于以管理员权限建立 iOS 设备隧道。",
            "LSUIElement": False,
        },
    )
