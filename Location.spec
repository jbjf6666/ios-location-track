# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把整个应用（Python 运行时 + 依赖 + 前端资源）打成一个可独立运行的 .app。

用法：./.venv/bin/python -m PyInstaller --clean --noconfirm Location.spec

设计要点：
- **onedir**（不是 onefile）：启动更快，且 .app 内部结构可被 macOS 正常识别；
  真要单文件也可以，但 onefile 每次启动都要解包到 /tmp，反而更慢。
- 前端资源（web/）与图标打进包内，运行时通过 sys._MEIPASS 定位。
- 排除 pymobiledevice3 带进来但本工具用不到的重型依赖（IPython / fastapi / uvicorn 等），
  它们只被 pymobiledevice3 的 CLI 交互模式使用；本工具只用 Python API。
  排除后体积从 ~190MB 降到 ~60MB 量级。
"""
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).resolve()

# 有些包在运行期用 importlib.metadata 读自己的版本（例如 apple_compress 的 __version__，
# 它被 pyimg4 用于个性化镜像处理）。PyInstaller 默认不会打包 dist-info，
# 漏掉后挂载会报 "No package metadata was found for apple_compress"。
METADATA_PACKAGES = [
    "apple_compress", "pymobiledevice3", "pyimg4", "construct",
    "cryptography", "bpylist2", "ipsw_parser", "opack2",
]

_metadata_datas: list = []
for _pkg in METADATA_PACKAGES:
    try:
        _metadata_datas += copy_metadata(_pkg)
    except Exception:
        pass

# pymobiledevice3 的 CLI 会拉起这些重型依赖，但本工具只用它的 Python API，全部排除
# 策略：**尽量完整保留**，只排除"构建期工具"和"确定与运行无关的开发工具"。
# 依据（实测）：questionary / prompt_toolkit / pygments 被 pymobiledevice3 运行期导入，
# 曾因误排导致打包版真机不可用；因此与 pymobiledevice3 相关的一切都保留。
# pip / setuptools / PyInstaller 只在构建期使用，运行 .app 时完全用不到。
EXCLUDES = [
    # 构建期工具（打包产物里绝不需要）
    # 注意：不要排除 distutils / pkg_resources —— PyInstaller 的 hook 会强制导入它们，
    # 排除后构建直接失败（ValueError: Target module "distutils" already imported as ExcludedModule）
    "pip", "wheel",
    "PyInstaller", "pyinstaller", "_pyinstaller_hooks_contrib",
    # 开发/测试工具（pymobiledevice3 的交互式 CLI 才会用）
    "IPython", "ipython", "ipykernel", "jupyter", "jupyter_client", "jupyter_core",
    "ipywidgets", "notebook", "nbformat", "nbconvert",
    "pytest", "_pytest", "matplotlib", "matplotlib_inline", "tkinter",
]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_metadata_datas + [
        ("web", "web"),                     # 前端（index.html / style.css / js/）
        ("resources/AppIcon.icns", "resources"),
        # 开发者镜像素材（iOS 17+ 全系共用的个性化镜像；iOS 16.x 的经典镜像按需放）
        ("resources/ddi", "resources/ddi"),
        ("docs/PROTOCOL.md", "docs"),
        ("README.md", "."),
    ],
    hiddenimports=[
        # 这些是运行期动态 import 的，PyInstaller 静态分析看不到
        "pymobiledevice3.remote.native_tunnel",
        "pymobiledevice3.remote.userspace_tunnel",
        "pymobiledevice3.remote.remote_service_discovery",
        "pymobiledevice3.remote.tunnel_service",
        "pymobiledevice3.services.dvt.instruments.dvt_provider",
        "pymobiledevice3.services.dvt.instruments.location_simulation",
        "pymobiledevice3.lockdown",
        "pymobiledevice3.usbmux",
        "pymobiledevice3.service_connection",
        "pymobiledevice3.exceptions",
        "developer_disk_image.repo",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

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
    console=False,          # 不弹终端窗口（日志写到文件）
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
    version="1.0.0",
    info_plist={
        "CFBundleName": "虚拟定位轨迹",
        "CFBundleDisplayName": "虚拟定位轨迹",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "CFBundlePackageType": "APPL",
        # Finder/Dock 取图标用；缺这个字段时系统有时会显示通用图标
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
