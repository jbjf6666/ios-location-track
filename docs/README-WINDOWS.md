# Windows 版说明（iOS 虚拟定位轨迹工具）

> 本文档面向 Windows 用户与维护者。
> macOS 侧说明见 [README.md](../README.md)；移植决策与背景见 [WINDOWS_PORT.md](WINDOWS_PORT.md)。

---

## 1. 三分钟上手

1. 运行 `iOSLocationTrack-1.0.0-setup.exe` 安装，或直接双击绿色版
   `dist\iOSLocationTrack\iOSLocationTrack.exe`。
2. **程序会请求管理员权限**（UAC 弹窗点「是」）—— 用于首次自动安装 Apple 驱动。
3. 弹出**「环境准备」**窗口，逐项列出所需组件：
   - 已就绪的项目显示绿色 ✓，右侧按钮变成「已就绪」（不可点）
   - 缺失的项目显示红色 ✗，右侧是可点的「安装 / 下载」按钮
   - **必需项全部就绪后，「进入程序」才会变亮**
4. 点「进入程序」→ 浏览器以无地址栏应用窗口打开界面。
5. 用数据线连接 iPhone → 解锁 → 手机上点「信任」→ 界面点「连接设备」。

> 从源码运行：`.\scripts\run.ps1`（会自动建 venv、装依赖、补镜像素材）。

---

## 2. 「环境准备」窗口检查哪些项

| 项目 | 必需 | 检查方式 | 缺失时的「安装」做什么 |
| --- | --- | --- | --- |
| Python 运行时 | ✅ | 版本号 | —（已随 exe 打包） |
| pymobiledevice3 | ✅ | 导入 + 版本 | —（已随 exe 打包） |
| **Apple Mobile Device Support** | ✅ | 服务状态 + 27015 协议往返 | 自动下载安装（约 40~200 MB） |
| RSD 隧道依赖 | ✅ | `pytun_pmd3` / `pmd_pytcp` 等导入 | —（已随 exe 打包） |
| 开发者镜像 | ✅ | `resources/ddi/personalized/` 三个文件 | 下载个性化镜像（约 15 MB） |
| 管理员权限 | ✅ | `IsUserAnAdmin` | 右键「以管理员身份运行」 |
| iPhone 连接 | ➖ 可选 | usbmuxd `ListDevices` | —（接上手机即可） |
| 网络连通 | ➖ 可选 | TCP 连 `gs.apple.com:443` | —（挂载镜像时 Apple 需在线签名） |

**判定 usbmuxd「能用」的唯一权威信号是 127.0.0.1:27015 上的协议往返**，不是端口能连上。
`ReadBUID` 不需要插设备，因此可以在没有手机的情况下单独验证服务是否正常。

### 想看文字版自检

```powershell
# 绿色版
.\dist\iOSLocationTrack\iOSLocationTrack.exe --check-driver
# 源码
.venv\Scripts\python.exe main.py --check-driver
```

输出示例：

```
平台：Windows（管理员：是）
服务：RUNNING（Apple Mobile Device Service）
版本：Apple Mobile Device Support 20.0.0.35
usbmuxd(127.0.0.1:27015)：可通信
设备：1 台
  · 00008030-001551840A02402E（USB）
结论：Apple Mobile Device Service 正在运行，usbmuxd 可通信
```

---

## 3. 自动安装 Apple Mobile Device Support

「环境准备」窗口里点 Apple 那一行的「安装」，或在命令行跑：

```powershell
main.py --install-driver          # 源码
iOSLocationTrack.exe --install-driver   # 绿色版
```

安装策略按顺序自动降级（`core/apple_driver.py`）：

| 顺序 | 策略 | 说明 |
| --- | --- | --- |
| 0 | 启动已有服务 | 服务已装但没运行时，`sc start` 一下就够（最常见、最便宜） |
| 1 | 独立 AMDS MSI | Apple CDN 上的 `AppleMobileDeviceSupport64.msi`（约 38 MB） |
| 2 | 完整 iTunes 包 | `https://www.apple.com/itunes/download/win64`（约 193 MB） |
| 3 | winget | `Apple.AppleMobileDeviceSupport` / `Apple.iTunes` |

成功退出码：`0`、`3010`、`1641`、`1638`（MSI 语义，3010/1641 表示需要重启）。

### 必须知道的几个坑（都已在真机取证）

1. **服务名是带空格的 `Apple Mobile Device Service`**。
   `AppleMobileDeviceService` 只是 exe 文件名，`sc query AppleMobileDeviceService` 会报 1060。
2. **Windows 上没有 `\\.\pipe\usbmuxd`**，usbmuxd 只暴露为 TCP `127.0.0.1:27015`。
3. **不要用 `usbaapl64` 判断驱动是否存在**。AMDS 的 MSI 里 **0 个 `.inf`/`.sys`/`.cat`**，
   它**不提供 USB 驱动**；现代机器上 iPhone 实际绑定 `appleusb.inf`
   （服务名 `WINUSB` / `usbccgp`）。用 `usbaapl64` 判断会在健康机器上误报「缺失」。
4. **AMDS 的 ProductCode 每个版本都变**，稳定的标识是 UpgradeCode
   `{5CF0656E-F3B8-11DB-BA81-804E56D89593}`（x86/x64 通用）。
5. **Apple 没有稳定的「latest」直链**；`iTunes64Setup.exe` 的地址带构建号。因此
   本项目对能固定 SHA256 的文件做校验，校验不过会明确报错而不是装上一个来路不明的包。
6. **Microsoft Store 版 iTunes 与经典版互斥**：经典安装包的 `LaunchCondition` 里写死了
   `NOT ITUNES_STORE_APP_INSTALLED`。另外 Store 版的守护进程**不支持 Wi-Fi 发现设备**。
   pymobiledevice3 官方也推荐经典版。

---

## 4. 与 macOS 版的架构差异（**重要**）

移植时最容易踩的坑：**两个平台的 RSD 隧道建立方式本质不同**，
所以 `core/tunnel.py` 里 Windows 与 macOS 走的是两条完全不同的路径。

| | macOS | Windows |
| --- | --- | --- |
| 隧道方式 | `remote start-tunnel --native`（借 Apple `remoted`） | `UserspaceRsdTunnel`（纯 Python PyTCP 用户态协议栈） |
| 需要 root/管理员 | 不需要 | **不需要** |
| 需要第三方驱动 | 否 | **否**（不需要 Wintun） |
| RSD 地址可达范围 | 内核可路由，**任何进程**都能连 | **仅本进程**可达（用户态协议栈内） |
| 实现方式 | 子进程 + 解析 stdout 的 `HOST PORT` | 在本应用的事件循环里建立并持有 rsd 对象 |
| 实测耗时 | — | **约 0.3 秒** |

### 为什么 Windows 不能用「隧道助手子进程」那套

macOS 版把隧道跑在子进程里，打印 `HOST PORT`，主进程用普通 TCP 连上去。
Windows 上这样行不通：

- 用户态隧道的设备地址**只存在于建立它的那个进程的协议栈里**；
- pymobiledevice3 的做法是把一个 relay dialer 注入它返回的
  `RemoteServiceDiscoveryService` 对象（`open_connection=dial_plane.dial`）；
- 拿 `(host, port)` 另建一个 RSD 会退回 stdlib dialer，直接打到操作系统网络栈上，
  结果是 `OSError [WinError 1231] 网络位置不可达`（本项目实测复现过）。

**正确做法**：把隧道返回的 **rsd 对象本身**注入给 DVT 层。
`RealLocationProvider` 早就预留了 `rsd=` 注入口（`core/location_provider.py`），
`core/server.py:provider_for()` 在 Windows 上走的就是这条路。

### 关于 Wintun

`docs/WINDOWS_PORT.md` 让先装 Wintun，**实测不需要**：

- userspace 隧道走纯 Python 的 PyTCP 协议栈，不创建内核网卡；
- 而且 `pytun_pmd3` 这个依赖**自带**各架构的 `wintun.dll`
  （`pytun_pmd3/wintun/bin/amd64/wintun.dll`），本来也不用另外下载。

### 实测环境

| 项 | 值 |
| --- | --- |
| 系统 | Windows 11 家庭版 中文版（10.0.26200，x64） |
| Python | 3.12.14 |
| pymobiledevice3 | 11.31.0 |
| 设备 | iPhone 11（`iPhone12,1`），**iOS 26.6.1** |
| Apple Mobile Device Support | 20.0.0.35 |
| iTunes | 12.13.11.1（经典版） |

连接耗时实测 **0.8 秒**（隧道 0.3s + 镜像/DVT 会话）。

---

## 5. 数据与日志位置

| 内容 | 路径 |
| --- | --- |
| 配置 / 瓦片缓存 / 日志 | `%LOCALAPPDATA%\iOSLocationTrack\` |
| 应用日志 | `%LOCALAPPDATA%\iOSLocationTrack\logs\app.log` |
| MSI 安装日志 | `%LOCALAPPDATA%\iOSLocationTrack\logs\msi-*.log` |
| 下载的安装包缓存 | `%LOCALAPPDATA%\iOSLocationTrack\installers\` |
| 开发者镜像缓存 | `%USERPROFILE%\.pymobiledevice3\Xcode_iOS_DDI_Personalized\` |

`app.log` 是 UTF-8、Tab 分隔：`时间戳 \t 级别 \t 消息`。
用记事本或 `Get-Content -Encoding UTF8` 打开（PowerShell 5.1 默认按 ANSI 读会乱码）。

---

## 6. 构建

```powershell
# 一键：图标 → 镜像素材 → 测试 → PyInstaller → Inno Setup
.\scripts\build.ps1

# 只打 exe
.\scripts\build.ps1 -NoInstaller

# 跳过测试快速重建
.\scripts\build.ps1 -SkipTests -SkipIcon -SkipDdi
```

产物：

| 文件 | 说明 |
| --- | --- |
| `dist\iOSLocationTrack\iOSLocationTrack.exe` | 绿色版 onedir，整个目录一起拷走 |
| `dist\installer\iOSLocationTrack-1.0.0-setup.exe` | 安装包（Inno Setup 6） |

**前置条件：**

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ pymobiledevice3 pillow pyinstaller
winget install -e --id JRSoftware.InnoSetup     # 可选，只影响安装包
```

### 打包时踩过的坑（都已修好，改 spec 时注意别踩回去）

1. **不要排除 `distutils` / `setuptools` / `wheel` / `pip`**。
   PyInstaller 与 setuptools 的 hook 会强制导入或别名它们，排除后构建直接失败：
   ```
   ValueError: Target module "distutils" already imported as ExcludedModule('distutils',)
   ValueError: Target module "wheel"     already imported as ExcludedModule('wheel',)
   ```
2. **不要排除 `tkinter`** ——「环境准备」窗口靠它（见下）。
3. **`copy_metadata` 必须带上** `apple_compress` 等包，否则挂载镜像时报
   `No package metadata was found for apple_compress`。
4. **不要排除 `questionary` / `prompt_toolkit` / `pygments`**，
   它们是 pymobiledevice3 运行期导入的；排掉后真机功能会静默失效
   （表现为设备发现退化成 libimobiledevice）。
5. **`--windowed` 时 `sys.stdout` 是 `None`**，打印前必须判空。
6. **`.ps1` 必须带 UTF-8 BOM**。Windows PowerShell 5.1 只有在文件开头看到 BOM 时才按
   UTF-8 解析脚本，否则按系统 ANSI 代码页（中文机器上是 GBK）读 —— 结果是中文全部乱码，
   并且**脚本直接语法报错**（`The string is missing the terminator`）。
   `scripts\run.ps1` / `scripts\build.ps1` 都已存为 UTF-8 with BOM。
   编辑这两个文件时务必保持 BOM，否则它们会突然无法运行。

### 启动时请求管理员权限

`Location.spec` 里 Windows 分支设了 `uac_admin=True`，PyInstaller 会把
`requestedExecutionLevel level="requireAdministrator"` 写进 exe 清单。
用 `strings` 或在 exe 里搜 `requireAdministrator` 可以验证。

> 注：userspace 隧道**本身不需要管理员权限**；提权只是为了首次运行能自动安装
> Apple Mobile Device Support，免去用户手动右键提权。

---

## 7. 验收

```powershell
# 无需设备（136 项）
.venv\Scripts\python.exe tests\selftest.py        # 83 项：协议/几何/播放/配置/瓦片/端到端
.venv\Scripts\python.exe tests\drift_sim.py       # 40 项：真实感模型
.venv\Scripts\python.exe tests\offline_tiles.py   # 13 项：瓦片离线缓存

# 平台可行性（真机，7 项）
.venv\Scripts\python.exe tests\win_e2e.py --confirm

# 应用层（真机，9 项；走 App 的真实装配路径）
.venv\Scripts\python.exe tests\win_app_e2e.py --confirm

# 无设备跑界面
.venv\Scripts\python.exe main.py --mock
```

真机验收 6 步（对应 `WINDOWS_PORT.md` 第 7 节）：

1. `pymobiledevice3 usbmux list` 能看到设备 ✅
2. 挂载开发者镜像成功 ✅
3. 「连接设备」→ 隧道建立 → DVT 会话就绪 ✅
4. 播放轨迹 → 手机定位真的移动 ✅
5. 「停止」→ 手机恢复真实定位 ✅
6. 应用层 9/9、平台层 7/7 全绿 ✅

---

## 8. 已知限制与注意事项

| 限制 | 说明 |
| --- | --- |
| **一个进程只能有一条 userspace 隧道** | PyTCP 协议栈是进程内全局单例。所以别双开本程序。 |
| **同一时间只能有一个工具占 RSD 连接** | 爱思助手 / i4Tools / Xcode 会互相挤掉，用之前先关掉。 |
| **iOS 16 及更早不支持** | 经典镜像需 Apple 按构建号签发，第三方镜像会被设备拒签。本项目面向 iOS 17+。 |
| **DVT 只能改经纬度** | `simulateLocationWithLatitude:longitude:` 只有两个参数；海拔、精度、传感器数据由 iOS 合成，无法注入。 |
| **挂载镜像需要联网** | 个性化镜像由 Apple TSS 按设备在线签发。 |
| **隧道可能静默断开** | Windows 用户态协议栈会偶发断开，设备会自己恢复真实定位而界面看起来正常。已加**保活**：设了点但停止播放时，每 15 秒重发一次（`core/location_provider.py:KEEPALIVE_INTERVAL`）。 |
| **iOS 26.4.x 的镜像挂载可能干扰 DVT** | 有同类工具报告 26.4.1 上传镜像会中断隧道，导致后续 DVT 报 `InvalidService`。本机 iOS 26.6.1 实测正常。若遇到，先在别的工具里挂好镜像再连。 |
| **未签名 exe 会有 SmartScreen 提示** | 没有代码签名证书。点「更多信息 → 仍要运行」。 |
| **Windows 10 1809 以下不支持** | 安装包 `MinVersion=10.0`。 |

---

## 9. 文件对照（Windows 相关改动）

| 文件 | 说明 |
| --- | --- |
| `core/platform.py` | **新增**。平台抽象层：数据目录、浏览器探测、进程管理、提权、信号 |
| `core/apple_driver.py` | **新增**。Apple Mobile Device Support 检测/安装 + 开发者镜像下载 |
| `core/setup_window.py` | **新增**。「环境准备」窗口（tkinter） |
| `core/tunnel.py` | **重写**。Windows 走进程内 userspace 隧道；macOS 保持子进程 native 隧道 |
| `core/server.py` | 注入 runner 给 TunnelManager；`provider_for()` 注入 rsd 对象；日志落盘；资源目录与数据目录分离 |
| `core/location_provider.py` | 平台化错误提示；**新增保活**（`_keepalive_loop`） |
| `main.py` | 「环境准备」窗口、Apple 驱动后台检查、`--check-driver` / `--install-driver`、跨平台浏览器与信号 |
| `Location.spec` | 平台分支；Windows 用 `.ico` + `uac_admin=True` + `version_info.txt`；**单一 spec，不分叉** |
| `tools/make_ico.py` | **新增**。由已有 PNG 图标集生成多尺寸 `.ico` |
| `tools/probe_tunnel_win.py` | **新增**。隧道可行性诊断脚本 |
| `scripts/run.ps1` / `build.ps1` | **新增**。Windows 启动 / 构建 |
| `installer/iosloc.iss` | **新增**。Inno Setup 安装包脚本 |
| `installer/version_info.txt` | **新增**。exe 版本资源 |
| `installer/languages/ChineseSimplified.isl` | **新增**。Inno Setup 6.7 起不再自带中文，随仓库携带 |
| `tests/win_e2e.py` / `win_app_e2e.py` | **新增**。真机验收（平台层 / 应用层） |

macOS 侧的构建链（`tools/make_dmg.py`、`scripts/run.sh`、`build_app.sh`、
`Location.spec` 的 BUNDLE 分支）**全部保留**，同一份代码库两个平台都能构建。
