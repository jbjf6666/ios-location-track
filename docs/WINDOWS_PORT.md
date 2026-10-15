# Windows 移植交接文档

> 本文档由 macOS 侧的开发会话产出，用于把「iOS 虚拟定位轨迹工具」移植到 Windows。
> 仓库：https://github.com/jbjf6666/ios-location-track （macOS 版已发布，v1.0.0）
>
> **给 Windows 会话的建议**：先完整读一遍本文档，再动手。最大的风险点（iOS 17+ 隧道）
> 集中在第 3 节，**请先做那里的可行性验证再决定整体架构**——不要先写界面。

---

## 0. 一句话结论

**约 90% 的代码可以直接复用**（纯 Python + 纯前端，零平台依赖）。
真正需要重写的只有两处：**iOS 17+ 的隧道建立方式**（macOS 用 Apple 私有框架，Windows 必须换方案）
和**打包/分发链路**（DMG → 安装包）。窗口启动方式也需改。

总计约 70 行平台相关代码需要替换，其余 6000+ 行可原样移植。

---

## 1. 项目结构与可移植性对照表

| 文件 | 行数 | 可移植性 | 说明 |
| --- | --- | --- | --- |
| `web/` (index.html / style.css / js/*) | ~2500 | ✅ **完全可移植** | 纯前端，零改动。自绘 Canvas 地图，无第三方依赖 |
| `core/<br>playback.py` | 687 | ✅ 完全可移植 | 播放引擎（弧长积分、速度剖面、seek、循环） |
| `core/route_engine.py` | 576 | ✅ 完全可移植 | 路线规划（OSRM + 直线退化、途经点） |
| `core/geoutils.py` | 234 | ✅ 完全可移植 | 坐标换算（WGS84 / GCJ-02、haversine、抽稀） |
| `core/noise.py` | 208 | ✅ 完全可移植 | 真实感模型（多尺度值噪声、漂移、抖动、粗差） |
| `core/config_store.py` | 312 | ✅ 完全可移植 | 配置持久化（路径需调整） |
| `core/ws.py` | 326 | ✅ 完全可移植 | 手写 WebSocket（RFC 6455），基于 stdlib |
| `core/tilecache.py` | 323 | ✅ 完全可移植 | 瓦片代理 + 磁盘 LRU 缓存 |
| `core/protocol.py` | 160 | ✅ 完全可移植 | WS 协议常量与错误类型 |
| `core/location_mock.py` | 229 | ✅ 完全可移植 | 无设备模拟模式 |
| `core/device.py` | 427 | ⚠️ 基本可移植 | 设备发现用 `pymobiledevice3.usbmux`，Windows 上走 Apple Mobile Device 服务（iTunes 组件） |
| `core/location_provider.py` | 701 | ⚠️ 基本可移植 | DVT 会话逻辑跨平台；**只有 1 处** macOS 特定（错误提示文案里的 `./run.sh`） |
| `core/server.py` | 1352 | ⚠️ 少量改动 | HTTP + WS 服务、命令分发；约 15 处平台相关（窗口/路径/提示） |
| `core/tunnel.py` | 434 | ❌ **需重写** | 隧道建立，见第 3 节 |
| `main.py` | 284 | ⚠️ 需改 | 窗口启动（Chrome/Edge 路径）、数据目录 |
| `tools/make_dmg.py` | — | ❌ **纯 macOS** | 用 `hdiutil`，Windows 需换安装包方案 |
| `tools/make_icon.py` | — | ✅ 可移植 | Pillow 绘制，但 `.icns` 生成用 `iconutil`（macOS）→ Windows 需改成 `.ico` |
| `tools/verify_bundle.py` | — | ⚠️ 需改 | 校验逻辑通用，但 `codesign` / `.app` 结构是 macOS 的 |
| `Location.spec` | — | ⚠️ 需改 | PyInstaller 配置，Windows 要改成 `.exe` + `BUNDLE` → 无（改用 Inno Setup / NSIS） |
| `tests/*` | ~2000 | ✅ 大部分可移植 | `selftest.py` / `drift_sim.py` 纯逻辑，跨平台；`ui_acceptance.py` 需改浏览器路径 |
| `resources/ddi/` | 16 MB | ✅ 可移植 | 开发者镜像素材，与平台无关 |

---

## 2. Windows 环境准备

```
1. Python 3.11+（建议 3.12，与 macOS 侧一致）
2. iTunes（或至少 Apple Mobile Device Support）—— 提供 usbmuxd 等价服务，
   pymobiledevice3 依赖它才能通过 USB 发现 iOS 设备
   下载：Microsoft Store 版 iTunes，或 Apple 官网的 iTunes for Windows
3. Wintun 驱动（用于 userspace 隧道，见第 3 节）
   https://www.wintun.net/
4. Git
```

依赖安装：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install pymobiledevice3 pillow pyinstaller
```

> 注意：macOS 侧 `scripts/run.sh` 里写了国内 PyPI 镜像 `mirrors.aliyun.com`，
> Windows 侧可沿用（`pip.conf` 里已有配置，Windows 下需放到 `%APPDATA%\pip\pip.ini`）。

---

## 3. 核心难点：iOS 17+ 的 RSD 隧道（**必须先验证**）

### 3.1 为什么 macOS 的实现不能直接用

iOS 17 起，虚拟定位不再走 usbmux，必须先建立 RSD 隧道。macOS 侧走的是
`pymobiledevice3.remote.native_tunnel`，其实现**完全建立在 Apple 私有框架上**：

```python
_IS_DARWIN = platform.system() == "Darwin"
lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
# 然后调用 xpc_connection_create_mach_service、xpc_dictionary_* 等
# 以及 ObjC block：_NSConcreteGlobalBlock
```

这些 API 是 macOS 独有的（XPC + Objective-C runtime），**Windows 上不存在**。
pymobiledevice3 的 `establish_native_rsd()` 在非 macOS 平台会抛
`UserspaceTunnelUnavailableError`。

### 3.2 Windows 上的可行方案：userspace tunnel

pymobiledevice3 内置了跨平台的 userspace 隧道实现，Windows 是**明确支持的目标平台**：

```python
from pymobiledevice3.remote import userspace_tunnel
from pymobiledevice3.remote import tunnel_service

# 关键开关
tunnel_service.USE_USERSPACE_TUNNEL = True
```

实现要点（读源码可知）：

- 使用 `pytun_pmd3.TunTapDevice` 创建虚拟网卡（Windows 上底层是 Wintun 驱动）
- macOS/Linux 用 AF_UNIX socketpair；**Windows 没有 socketpair，改用 TUN 设备直连**
- 该包已在依赖列表中（`pytun_pmd3-3.0.3`），无需额外安装

### 3.3 验证步骤（**请先做完这一步再写别的代码**）

> **重要**：`--userspace` 不是 `remote start-tunnel` 的选项（该命令只有 `--native`）。
> 实测确认它挂在共享的 RSD 依赖上，**`lockdown start-tunnel` 与 `mounter auto-mount` 才接受它**：
>
> | 命令 | `--userspace` |
> | --- | --- |
> | `lockdown start-tunnel` | ✅ 支持 |
> | `mounter auto-mount` | ✅ 支持 |
> | `remote start-tunnel` | ❌ 不支持（只有 `--native`，且 `--native` 是 macOS 专用） |
> | `developer dvt simulate-location` | ❌ 不支持 |

```powershell
# 1) 设备能否被发现（需要 iTunes 提供的 Apple Mobile Device 服务）
python -m pymobiledevice3 usbmux list

# 2) 建立 userspace 隧道（Windows 上的正解）
python -m pymobiledevice3 lockdown start-tunnel --userspace --script-mode
```

期望输出形如 `HOST PORT`（例如 `fd00:...:1 54321`）—— 这正是 macOS 侧
`core/tunnel.py:_read_output()` 解析的格式，**保持这个格式，上层代码就不用改**。

**底层机制（读源码确认）**：`userspace_tunnel.UserspaceTunnel.aopen()` 内部会设置
`tunnel_service.USE_USERSPACE_TUNNEL = True`，从而让 `RemotePairingTunnel.start_tunnel()`
通过 `create_tun_device()` 工厂拿到 `UserspaceTun`（底层是 `pytun_pmd3.TunTapDevice` →
Windows 上是 Wintun），而不是内核隧道。所以**不需要任何 monkeypatch**，用上面的命令即可。

**限制**：PyTCP 的协议栈是进程内全局单例，**一个进程同时只能有一条 userspace 隧道**
（代码里会抛 `"a userspace tunnel is already active in this process"`）。
这对本工具没影响——播放引擎本来就是单一长连接，但要记得别在进程里重复建隧道。

**必须确认的事项**：

| 检查项 | 为什么重要 |
| --- | --- |
| 是否需要管理员权限 | Wintun 装驱动/建网卡通常需要；这决定要不要在界面上加提权按钮 |
| Windows 防火墙是否弹窗 | 首次建立隧道可能弹窗，需要写进文档 |
| 隧道建立后 `ping6` 是否通 | 验证虚拟网卡真的工作了 |
| 断开后网卡是否残留 | 残留会导致下次建立失败，需要清理逻辑 |

### 3.4 隧道模块的重写接口

`core/tunnel.py` 的 `TunnelManager` 类已经把隧道抽象好了，**保持以下公开接口不变**，
只替换内部实现即可，上层（`server.py` / `location_provider.py`）无需改动：

```python
class TunnelManager:
    def ready(self) -> bool: ...                    # 隧道是否就绪
    def address(self) -> tuple[str, int] | None: ... # (host, port)
    def ensure(self, udid: str | None, allow_sudo: bool = False) -> dict: ...
    def start(self, udid: str | None = None, allow_sudo: bool = False) -> dict: ...
    def use_manual(self, host: str, port: int, udid: str | None = None) -> dict: ...
    def stop(self) -> None: ...
    def snapshot(self) -> dict: ...                 # 状态快照，推给前端
```

**建议的实现方式**：照抄现有结构，把 `_build_command()` 换成：

```python
if sys.platform == "win32":
    # 注意：不是 remote start-tunnel，那个命令没有 --userspace
    base = [sys.executable, "-m", "pymobiledevice3",
            "lockdown", "start-tunnel", "--userspace", "--script-mode"]
```

打包后（`sys.frozen`）同样走自调用：

```python
base = [sys.executable, "tunnel-helper"]     # exe 调用自己的子命令
```

并在 `tunnel-helper` 内部用 Python API 建立 userspace 隧道：

```python
# 注意类名是 UserspaceRsdTunnel（不是 UserspaceTunnel）
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

tunnel = UserspaceRsdTunnel(serial=udid, autopair=True, remotepairing_fallback=True)
rsd = await tunnel.aopen()                    # -> RemoteServiceDiscoveryService
host, port = rsd.service.address
print(f"{host} {port}", flush=True)           # 保持这个格式，上层无需改动

# 进程退出前释放（否则虚拟网卡可能残留）
await tunnel.aclose()                         # 或 tunnel.close()
```

> 另外提供同步接口 `open()` / `close()`，以及 `UserspaceTun`（TUN 设备本身，
> 一般不需要直接用它）。

> **重要经验**：macOS 侧的 `tunnel-helper` 是「自调用」模式（打包后的 exe 调用自己
> 的 `tunnel-helper` 子命令），这样打包后不依赖外部 Python。Windows 侧建议照做。
> 实现见 `main.py:run_tunnel_helper()` 与 `core/tunnel.py:_build_command()`。

---

## 4. 其余需要改动的部分

### 4.1 窗口启动（`main.py`）

macOS 侧用 Chrome/Edge 的 `--app=` 模式打开无地址栏窗口：

```python
# macOS 路径
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
```

Windows 需改为：

```python
CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
# 或从注册表读：
# HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe
```

`--app={url} --window-size=1440,900` 参数在 Windows 上同样有效。

**另一个选择**：用 `pywebview`（Windows 上走 Edge WebView2）做成真正的原生窗口，
体验更接近 macOS 版。代价是多一个依赖。

### 4.2 数据目录（`main.py:_resolve_roots()`）

```python
# macOS: ~/Library/Application Support/iOSLocationTrack
# Windows 应改为：%LOCALAPPDATA%\iOSLocationTrack
os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
```

### 4.3 打包与分发

| macOS | Windows 对应 |
| --- | --- |
| `PyInstaller --windowed` + `.app` bundle | `PyInstaller --windowed` 生成 `.exe`（onedir 更稳） |
| `codesign --sign -`（ad-hoc 签名） | 无对应；未签名 exe 会有 SmartScreen 警告 |
| `hdiutil` 制作 DMG | **Inno Setup**（推荐，免费）或 NSIS 制作 `setup.exe` |
| `iconutil` 生成 `.icns` | Pillow 直接存 `.ico`（多尺寸：16/32/48/64/128/256） |

`tools/make_icon.py` 的绘制代码可完全复用，只需把最后的 `.icns` 输出改成 `.ico`：

```python
sizes = [16, 24, 32, 48, 64, 128, 256]
img.save("app.ico", sizes=[(s, s) for s in sizes])
```

### 4.4 需要删除或替换的文件

- `tools/make_dmg.py` —— 纯 macOS，直接删掉
- `tools/ddi_offline_fetch.sh` —— bash 脚本，Windows 无用
- `scripts/run.sh` / `scripts/build_app.sh` —— 改写为 `.ps1` 或 `.bat`
- `Location.spec` —— 改写：去掉 `BUNDLE()`，`icon` 改成 `.ico`，`console=False`

---

## 5. 设备侧注意事项（平台无关，但容易踩坑）

这些是 macOS 侧实测踩过的坑，Windows 上同样会遇到：

### 5.1 镜像挂载用 Python API，不要用 CLI

打包成 exe 后没有 `pymobiledevice3` 命令行入口，必须用 Python API：

```python
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobile_image_mounter import (
    MobileImageMounterService, auto_mount)

client = await create_using_usbmux(serial=udid, autopair=True,
                                   connection_type="USB", pair_timeout=5.0)
async with MobileImageMounterService(lockdown=client) as mounter:
    for image_type in ("Personalized", "Developer"):
        if await mounter.is_image_mounted(image_type):
            return  # 已挂载，直接返回（否则会抛 AlreadyMountedError）
await auto_mount(client, version=version)
```

**踩坑记录**：
- `AlreadyMountedError` 必须当成功处理，不是失败
- 不要只检查「镜像是否挂载」就认为可用——macOS 侧实测 iOS 16.5 上镜像显示已挂载但 DVT 服务不启动
- iOS 17+ **不要**用 lockdown 服务名探测 DVT（`com.apple.instruments.dtservicehub` 走 RSD，不在 lockdown 里）

### 5.2 必须打包包元数据

PyInstaller 默认不打包 `dist-info`，而部分包（如 `apple_compress`）在运行期用
`importlib.metadata` 读自己的版本，漏掉会报
`No package metadata was found for apple_compress`，导致镜像挂载失败：

```python
from PyInstaller.utils.hooks import copy_metadata
datas = []
for pkg in ("apple_compress", "pymobiledevice3", "pyimg4", "construct",
            "cryptography", "bpylist2", "opack2"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass
```

### 5.3 不要把 `distutils` 加进 excludes

PyInstaller 的 hook 会强制导入它，排除后**构建直接失败**：

```
ValueError: Target module "distutils" already imported as "ExcludedModule('distutils',)"
```

同理 `questionary` / `prompt_toolkit` / `pygments` 被 pymobiledevice3 运行期导入，
排除后真机功能会静默失效（设备发现退化成 libimobiledevice 兜底）。

### 5.4 支持的 iOS 版本范围

**iOS 17 ~ 27.2**，一套个性化镜像全系共用（`resources/ddi/personalized/`，约 16 MB）。
挂载时由 Apple TSS 在线签发，因此**只有签名步骤需要联网**。

**iOS 16 及更早不支持**——其开发者镜像必须由 Apple 按系统构建号签发，
第三方镜像会被设备拒签（已实测：`Failed to verify the disk image signature`，
换 libimobiledevice 同样失败，说明是设备端拒绝）。

---

## 6. 术语与设计约束（改代码前必读）

| 约束 | 原因 |
| --- | --- |
| **DVT 连接必须长连接保持** | 会话一断，模拟定位立即失效。所以播放引擎不能每发一个点就重连 |
| **DVT 只能改经纬度** | `simulateLocationWithLatitude:longitude:` 仅两个参数。`horizontalAccuracy`、`altitude`、`floor`、`isSimulatedBySoftware`、加速度计/陀螺仪/计步器由 iOS 合成或来自硬件，**无法注入** |
| **`last_point` 与 `last_reported` 必须分开** | 前者是轨迹上的当前点（供 status 用），后者是实际下发的点（用于推算速度）。混用会导致播放线程崩溃或速度算错 |
| **速度推算要用单调时钟** | macOS 侧曾因用 `time.time()` 且读取顺序错误，导致 dt 恒被钳到下限、速度偏大 20 倍 |
| **`update_tuning` 的参数默认值必须是哨兵** | 用 `None` 表示「不修改」会导致 `False` 也被当成「不修改」，开关永远关不掉 |
| **坐标内部一律 WGS84** | 高德瓦片是 GCJ-02，只在前端请求瓦片与落点时转换 |
| **同一时间只能有一个工具占隧道** | 设备只有一条 RSD 连接，i4Tools / Xcode / 本工具会互相挤掉 |

---

## 7. 验收标准（移植完成后请逐项跑通）

macOS 侧的测试套件大部分可直接复用，作为 Windows 版的验收基线：

```powershell
python tests\selftest.py          # 83 项：协议、几何、播放引擎、配置、瓦片（无需设备）
python tests\drift_sim.py         # 40 项：真实感模型（幅度、平滑性、开关、参数校验）
python tests\offline_tiles.py     # 13 项：瓦片缓存
python main.py --mock             # 界面能开、能画轨迹、能播放（无设备）
```

真机验收（**这一项是 Windows 版的核心风险点**）：

1. `pymobiledevice3 usbmux list` 能看到设备
2. 点「挂载开发者镜像」成功
3. 点「连接设备」→ 隧道建立 → DVT 会话就绪
4. 播放轨迹 → 手机定位真的移动
5. 点「停止」→ 手机恢复真实定位

---

## 8. 建议的 Windows 侧工作顺序

```
第 1 步：环境 + 可行性验证（最高优先级，1~2 小时）
  └─ 装 iTunes / pymobiledevice3 / Wintun
  └─ 跑通 `remote start-tunnel --userspace`，拿到 HOST PORT
  └─ 用 pymobiledevice3 的 DVT 直接下发一次定位，确认手机真的动了
  ★ 这一步不通，后面全白做。务必先做。

第 2 步：移植纯逻辑层（无风险，可并行）
  └─ 复制 core/ 下 10 个可移植模块 + web/ 整个目录
  └─ 跑 tests/selftest.py 与 tests/drift_sim.py 确认 123 项全绿

第 3 步：重写隧道层
  └─ 按第 3.4 节的接口实现 Windows 版 TunnelManager
  └─ 实现 `--tunnel-helper` 自调用模式（为打包做准备）

第 4 步：窗口与路径适配
  └─ main.py 的浏览器路径、数据目录

第 5 步：打包
  └─ PyInstaller → .exe（注意第 5.2、5.3 节的坑）
  └─ Inno Setup 制作安装包

第 6 步：完整验收（第 7 节）
```

---

## 9. 需要向 macOS 侧（或用户）确认的问题

1. **目标 Windows 版本**？（Windows 10 / 11，是否要支持 ARM64）
2. **是否需要内嵌浏览器**（pywebview + WebView2）还是沿用 Chrome/Edge `--app=` 模式？
3. **安装包形式**：Inno Setup `setup.exe` / 绿色免安装 zip？
4. **隧道方案确认**：如果 `--userspace` 在目标机器上不通，备选方案是
   让用户手动跑 `tunneld`（走「手动 RSD」入口，界面已支持），需要确认可接受性
5. **是否要保留 macOS 与 Windows 双平台同一代码库**？
   建议：把平台相关代码集中到 `core/platform_win.py` / `core/platform_mac.py`，
   用一个 `core/platform.py` 分发，避免分叉两份代码

---

## 10. 参考

- 仓库：https://github.com/jbjf6666/ios-location-track
- 协议契约：`docs/PROTOCOL.md`
- 设备侧取证记录：`docs/DEVICE_NOTES.md`（含 macOS 侧实测的完整排障过程）
- pymobiledevice3 文档：https://github.com/doronz88/pymobiledevice3
- userspace 隧道源码：`pymobiledevice3/remote/userspace_tunnel.py`
- 开发者镜像素材来源：https://github.com/doronz88/DeveloperDiskImage

---

## 11. ⚠️ 移植完成后的修正（**先读这一节**）

以下是 Windows 侧**实际动手后**发现本文档有误的地方。上文保留原样以便对照，
但**以本节为准**。详细说明见 [`README-WINDOWS.md`](README-WINDOWS.md)。

| # | 本文档原文 | 实际情况 |
| --- | --- | --- |
| 1 | §3.3 说 `userspace_tunnel.UserspaceTunnel.aopen()` | **类名不存在**，正确是 `UserspaceRsdTunnel`。§3.4 已改对，§3.3 的正文漏改。 |
| 2 | §8 第 1 步让跑 `remote start-tunnel --userspace` | **该命令没有 `--userspace` 选项**。实测 `lockdown start-tunnel --userspace` 在 Windows 上会以 `Connection was terminated abruptly` 失败。**正解是 Python API**：`UserspaceRsdTunnel(...).aopen()`。 |
| 3 | §2 / §8 把 **Wintun 驱动**列为必需前置 | **完全不需要**。userspace 隧道走纯 Python PyTCP 协议栈，不建内核网卡；而且 `pytun_pmd3` 依赖里**自带** `wintun/bin/amd64/wintun.dll`。无需下载、无需安装、无需管理员权限。 |
| 4 | §3.3 问「是否需要管理员权限 / 防火墙弹窗 / 网卡残留」 | 实测：**都不需要**。免管理员、无防火墙弹窗、无虚拟网卡（因此也没有残留问题）。隧道建立约 **0.3 秒**。 |
| 5 | §3.4 说「保持公开接口不变，只替换 `_build_command()`」 | **不够**。`core/tunnel.py` 还有一批 POSIX-only 代码：`os.killpg`/`os.getpgid`、`start_new_session=True`、`ps -p`、`osascript`、`.venv/bin/python`、`~/Library/Application Support`。这些已全部收进 `core/platform.py`。 |
| 6 | §3.4 建议 Windows 沿用「隧道助手子进程 + 解析 HOST PORT」 | **架构上行不通**。用户态隧道的设备地址**只在建立它的进程里可达**；拿 `(host, port)` 另建 RSD 会得到 `OSError [WinError 1231] 网络位置不可达`。必须把 `aopen()` 返回的 **rsd 对象本身**注入 DVT 层。 |
| 7 | §5.3 只提到别排除 `distutils` | **`wheel` 同理**：Windows + setuptools 84 上会报 `ValueError: Target module "wheel" already imported as ExcludedModule('wheel',)`。`setuptools` / `pip` 也建议一并保留。 |
| 8 | §1 说 `drift_sim.py` 等「纯逻辑，跨平台」 | `drift_sim.py` 与 `offline_tiles.py` **硬编码了 `/tmp/...`**。Windows 上虽然会落到 `C:\tmp`（有权限时能跑），但已改为 `tempfile.gettempdir()`。 |
| 9 | §1 称 `resources/ddi/` 是「16 MB 可移植」 | 实际入库的只有 `BuildManifest.plist` + 两个 trustcache（约 0.8 MB）；**`Image.dmg`（15.7 MB）被 `.gitignore` 排除**，新克隆必须跑 `tools/fetch_ddi.py`。已加进 `scripts\run.ps1` / `build.ps1` 自动补齐。 |
| 10 | §4.4 建议「删掉 `tools/make_dmg.py`、把 `run.sh` 改写成 `.ps1`」 | **没有删**。改成新增 `run.ps1` / `build.ps1` + `Location.spec` 平台分支，**单一份代码库两个平台都能构建**，避免分叉（§9.5 的目标）。 |

### 实测环境与结果

Windows 11 x64（10.0.26200）· Python 3.12.14 · pymobiledevice3 11.31.0 ·
iPhone 11（`iPhone12,1`）**iOS 26.6.1** · AMDS 20.0.0.35 · iTunes 12.13.11.1

- 隧道 → 镜像 → DVT → 下发定位 → 恢复真实定位：**全链路跑通**，连接耗时 0.8 秒
- `tests/selftest.py` 83/83、`drift_sim.py` 40/40、`offline_tiles.py` 13/13
- 新增真机验收：`tests/win_e2e.py` **7/7**、`tests/win_app_e2e.py` **9/9**
- 打包版 exe 实测：发现设备 → 0.3s 隧道 → DVT → 两次下发 → 恢复真实定位 → 干净退出

### 新增的 Windows 专属能力（本文档未提及）

1. **「环境准备」窗口**（每次启动弹出）：逐项列出依赖，缺失项右侧有「安装」按钮，
   必需项全部就绪后「进入程序」才可点。
2. **自动安装 Apple Mobile Device Support**：服务启动 → 独立 AMDS MSI → 完整 iTunes 包 → winget，
   逐级降级；成功码 `0/3010/1641/1638`。
3. **exe 清单写入 `requireAdministrator`**（`Location.spec` 的 `uac_admin=True`），启动即提权。
4. **保活机制**：隧道会静默断开并让设备偷偷恢复真实定位，因此设了点但停止播放时
   每 15 秒重发一次（`core/location_provider.py:KEEPALIVE_INTERVAL`）。
5. **日志落盘**：窗口程序没有控制台，日志写到
   `%LOCALAPPDATA%\iOSLocationTrack\logs\app.log`。
