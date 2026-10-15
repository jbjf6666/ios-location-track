# iOS 虚拟定位轨迹工具（macOS）

[**中文**](README.md) | [English](README.en.md)

在 Mac 上给 iPhone / iPad 做**虚拟定位**与**轨迹模拟**的桌面工具：在地图上绘制路线、添加途经点、按真实速度沿轨迹驱动手机定位。

支持 **iOS 17 ~ iOS 27.2**（RSD 隧道 + DVT `SimulateLocation`），应用内内置了全系共用的开发者镜像素材，**不需要安装 Python 环境**。

界面为本机网页应用，用 Chrome / Edge 的应用窗口打开（无地址栏，像原生 App），也可以直接用浏览器访问 `http://127.0.0.1:8787`。地图、轨迹、播放引擎、设备通信全部在本机完成，只有地图瓦片和（可选的）路线规划需要联网。

---

## 1. 快速开始

### 方式一：直接下载安装包（推荐）

从 [Releases](https://github.com/jbjf6666/ios-location-track/releases) 下载 `iOSLocationTrack-1.0.0.dmg`：

1. 打开 DMG，把 `虚拟定位轨迹.app` 拖进 `Applications`
2. 首次打开若提示「来自身份不明的开发者」→ 右键 → 打开 → 再点「打开」

**不需要安装 Python、不需要任何依赖**，Python 运行时与全部依赖都已打包在应用内（约 80 MB）。
数据（配置、瓦片缓存）写到 `~/Library/Application Support/iOSLocationTrack/`。

> 只支持 **Apple Silicon（M 系列）**。Intel Mac 可用下方源码方式，或在 Intel 机器上执行 `./scripts/build_app.sh`（脚本通用）。

### 方式二：源码运行

```bash
git clone https://github.com/jbjf6666/ios-location-track.git
cd ios-location-track

# 首次运行会自动创建虚拟环境并安装依赖（走国内镜像）
./scripts/run.sh
```

启动后会自动打开界面。命令行等价方式：

```bash
./.venv/bin/python main.py                 # 正常启动（自动打开界面）
./.venv/bin/python main.py --mock           # 无设备模拟模式（演示/验收轨迹与播放）
./.venv/bin/python main.py --port 8899 --no-browser
./.venv/bin/python main.py --window browser # 用默认浏览器打开而不是应用窗口
```

### 三种运行形态

| 形态 | 路径 | 是否需要环境 | 适用场景 |
| --- | --- | --- | --- |
| **DMG 安装包** | Release 附件（45 MB） | **不需要** | 分发 / 换机器 |
| **独立版** | `dist/虚拟定位轨迹.app`（约 80 MB 展开） | **不需要**：Python、依赖、前端、图标、镜像素材全在包内 | 双击即用 |
| 源码版 | `./scripts/run.sh` | 需要 Python 3.11+（首次自动装依赖） | 改代码、调试 |

```bash
# 重新打包（一步完成：图标 → 打包 → ad-hoc 签名 → 制作 DMG → 验证）
./scripts/build_app.sh
```

DMG 里放了应用本体、`Applications` 快捷方式（拖进去即安装）和一份安装说明。
如需自定义 DMG 外观：改 `tools/make_dmg.py` 顶部的常量。

> 首次运行会请求「控制 Chrome/Edge」的权限（用于以应用窗口方式打开界面）。

---

## 2. 支持的系统

| 系统 | 支持 | 说明 |
| --- | --- | --- |
| **iOS 17 ~ 27.2** | ✅ | 走 RSD 隧道 + DVT。应用内置全系共用的个性化镜像素材 |
| iOS 16 及更早 | ❌ | 其开发者镜像必须由 Apple 按系统构建号签发，第三方镜像会被设备拒签（实测 `Failed to verify the disk image signature`，换 libimobiledevice 同样失败） |

**为什么一套镜像就够**：iOS 17 起 Apple 改用「个性化」开发者镜像 —— 一份基础镜像（`Image.dmg`）+ `BuildManifest.plist`，**全系统版本共用**；挂载时由 Apple TSS 服务器按当前设备在线签发，因此不需要逐版本素材。本工具把这套素材打包在内（约 16 MB），点「挂载开发者镜像」即可离线使用（仅签名步骤需要联网）。

---

## 3. 设备准备（关键）

| 要求 | 说明 |
| --- | --- |
| 数据线连接 | 建议用 USB；Wi-Fi 连接在传输大文件时容易断 |
| 解锁并「信任」 | 手机弹出「要信任此电脑吗」必须点**信任**并输入锁屏密码 |
| 开启开发者模式 | 设置 → 隐私与安全性 → **开发者模式** → 打开 → **重启手机** → 重启后确认开启 |
| 挂载开发者镜像 | 点界面上的「挂载开发者镜像」（应用已内置素材），或用 Xcode 连接一次自动完成 |
| 关闭同类工具 | 爱思助手 / i4Tools / Xcode 等会与本工具争抢同一条 RSD 连接，**同一时间只能开一个** |

### 挂载开发者镜像

界面上点 **「挂载开发者镜像」** 即可，过程实时输出到右侧运行日志。命令行等价方式：

```bash
# 查看是否已挂载（返回 [] 表示没挂）
./.venv/bin/python -m pymobiledevice3 mounter list

# 自动挂载
./.venv/bin/python -m pymobiledevice3 mounter auto-mount --udid <你的UDID>
```

也可以直接用 Xcode：连接设备 → Window → Devices and Simulators → 等待 "Preparing device for development" 完成。

> macOS 装有 Xcode 时，系统自带的 `CoreDeviceService` 通常会在设备接入后**自动挂载**
> （`/Library/Developer/DeveloperDiskImages/`），此时无需任何操作。

> 打包版（.app）内部用 pymobiledevice3 的 **Python API** 完成挂载，不依赖任何命令行工具。

### 关于 iOS 17+ 的隧道（RSD）

iOS 17 起，虚拟定位服务不再走 usbmux，必须先建立 **RSD 隧道**。本工具有三种方式：

1. **免 root（默认，推荐）**：自动执行 `pymobiledevice3 remote start-tunnel --native --script-mode`，借用 Apple 自带 `remoted` 的隧道。打包版通过 `tunnel-helper` 自调用实现，**不依赖外部 Python**。
2. **管理员模式**：界面上点「以管理员权限重建隧道」（会弹系统密码框），或直接 `sudo ./scripts/run.sh`。
3. **手动填 RSD**：界面「手动 RSD」按钮，填入 `HOST PORT`（例如从其它工具或 `tunneld` 拿到）。

> `remote tunneld`、`lockdown start-tunnel`、经典 `remote start-tunnel` 都**必须 root**，本工具不会静默提权。

---

## 4. 界面功能

### 顶部：设备区
- 设备名 / 型号 / iOS 版本 / 连接方式（USB/网络）
- **隧道**状态、**虚拟定位**状态、**开发者模式**状态
- 「刷新设备」「连接设备」「断开」

### 左侧：轨迹设置
- **出行模式**：驾车 / 骑行 / 步行 / 飞行（决定基准速度与是否走真实路网）
- **播放倍速**：0.1x ~ 50x（只影响轨迹推进速度，不影响下发频率）
- **循环播放**：到终点自动回到起点
- **真实感模拟**（可随时开关、播放中也能改）：
  - **定位漂移**：模拟 GPS 误差导致的缓慢游走，半径 0~50 米（默认 4 米）。
    用多尺度平滑噪声实现——沿轨迹连续变化、有惯性，**不会出现随机跳点**。
  - **速度浮动**：瞬时速度在设定值上下平滑波动，幅度 0~60%（默认 ±8%）。
  - **发送节奏抖动**（默认开）：真实手机 GPS 不是精确每秒一次，而是 0.85~1.15 秒抖动、
    偶发丢一次采样（约 2%）。间隔完全均匀是最容易被数据侧识别的特征之一。
  - **速度由位移推算**（默认开）：上报速度 = 实际位移 ÷ 实际时间间隔，与 iOS 从坐标序列
    推算 `CLLocation.speed` 的方式一致，因此**速度、位移、时间戳三者自洽**。
  - **注入粗差跳点**（默认关）：按概率插入多径/城市峡谷导致的坏点，
    用来测你自己 App 的 `horizontalAccuracy` 过滤与跳点剔除逻辑是否生效。
  - 所有开关与幅度都会持久化，下次启动沿用。
- **快速定位到坐标**：输入经纬度直接跳转，可收藏常用位置
- **路线概览**：总距离、预计耗时、路径点数、途经点数、路线引擎（OSRM 真实路网 / 直线）
- **途经点列表**：拖拽排序、双击改坐标、删除、定位到该点
- **本地保存的路线**：保存 / 载入 / 删除（浏览器本地存储）
- **底图**：高德矢量（GCJ-02，带中文标注）/ 高德卫星 / 全球卫星(Esri) / OpenStreetMap
- 操作：「加载 OSRM 路线」「清空」「导出 GPX」「导入 GPX」「恢复真实定位」
- 瓦片缓存：显示缓存目录与占用，「清理瓦片缓存」

### 地图工具条
- **选择**：拖动地图、拖拽途经点、点击编辑
- **添加途经点**：在地图上连续点击加点（最常用的绘制方式）
- **折线绘制**：连续点击画折线，双击或 Esc 结束
- **撤销上一步**、**删除选中点**、**全览**

### 底部：播放条
- 播放 / 暂停 / 继续 / 停止
- 进度条（可拖动跳转，即 seek）
- 已用时间、剩余时间、序号
- 实时速度、当前坐标、倍速

### 右侧：运行日志
实时显示设备连接、隧道建立、镜像挂载、每次定位下发的日志，便于排查问题。

---

## 5. 典型用法

**A. 沿真实道路跑一条轨迹**

1. 连接设备 → 「连接设备」（iOS 17+ 会自动建隧道，最长约 1 分钟）
2. 点「添加途经点」，在地图上点起点、中间点、终点（至少 2 个）
3. 点「加载 OSRM 路线」→ 生成真实路网折线（无网络时自动退化为直线）
4. 调「播放倍速」（例如 20x 可以在几秒内跑完 10 公里）
5. 点「播放」→ 手机定位会沿轨迹移动；地图上定位点与进度实时更新
6. 点「停止」→ 手机立刻恢复真实定位

**B. 单点定位（钉在某处不动）**

在「选择」工具下双击地图任意位置，或点「快速定位到坐标」输入经纬度。用完点「恢复真实定位」。

**C. 导入已有轨迹**

点「导入 GPX」选择 `.gpx` 文件 → 轨迹出现在地图上 → 播放。导出的 GPX 也能导入 Google Earth、Strava 等工具。

**D. 没有设备时预览效果**

```bash
./.venv/bin/python main.py --mock
```
界面、轨迹绘制、播放引擎、进度条全部可用，只是定位不会真正下发到手机（顶部会显示「当前为模拟模式」）。

---

## 6. 命令行工具与自检

```bash
# 列出设备 + 开发者模式 + DVT 可用性
./.venv/bin/python tools/probe_device.py
./.venv/bin/python tools/probe_device.py --probe      # 实际连一次 DVT

# 单点虚拟定位（命令行版）
./.venv/bin/python tools/probe_location.py --lat 39.9088 --lon 116.3975
./.venv/bin/python tools/probe_location.py --clear     # 恢复真实定位

# 离线自检：83 项单元 + 端到端（不需要真机）
./.venv/bin/python tests/selftest.py

# 真实感模拟（GPS 漂移 + 速度浮动 + 节奏抖动，40 项）
./.venv/bin/python tests/drift_sim.py

# 瓦片本地缓存（13 项） / 浏览器级离线可用（9 项）
./.venv/bin/python tests/offline_tiles.py
./.venv/bin/python tests/offline_browser.py

# 图形界面验收（20 项，自动起浏览器）
./.venv/bin/python tests/ui_acceptance.py

# 独立版打包产物验证：干净环境启动 + 全部接口（22 项）
./.venv/bin/python tools/verify_bundle.py

# 真机端到端：会真的改变手机定位，结束后自动恢复
./.venv/bin/python tests/real_device_e2e.py --confirm --duration 5 --speed 20

# 给界面截图（视觉验收）
./.venv/bin/python tools/screenshot.py http://127.0.0.1:8787/ /tmp/ui.png --wait 9
```

打包相关：

```bash
./.venv/bin/python tools/make_icon.py     # 生成应用图标（程序化绘制）
./.venv/bin/python tools/fetch_ddi.py     # 获取开发者镜像素材
./.venv/bin/python tools/make_dmg.py      # 制作 DMG
```

---

## 7. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 界面显示「本地服务未连接」 | 服务端没起来或端口被占。看终端是否有报错；换端口 `--port 8899` |
| 未检测到设备 | 数据线 + 解锁 + 点「信任」 |
| 提示「需要开发者模式」 | 设置 → 隐私与安全性 → 开发者模式 → 打开并重启手机 |
| 「挂载开发者镜像」失败，提示签名被拒 | 设备是 iOS 16 或更早（不支持）；或网络不通导致 TSS 签名失败，检查网络后重试 |
| 连接设备报「隧道」相关错误 | ① 关闭爱思助手等同类工具（会抢 RSD 连接）；② 点「以管理员权限重建隧道」；③ 确认开发者镜像已挂载 |
| 定位不生效（手机没动） | 确认「虚拟定位」显示"模拟中"；iOS 17+ 必须隧道正常 |
| 地图一片空白 | 瓦片源被网络限制，换底图（高德卫星 / Esri）；**已浏览过的区域断网也能显示**（瓦片落在 `~/.ios-location-tiles`，上限 1GB，LRU 淘汰） |
| 隧道建好后手机定位立刻恢复 | 有别的程序（爱思/i4Tools/Xcode）在抢同一条 RSD 连接，关掉它们 |

---

## 8. 技术说明（实现要点）

- **通信**：Python 标准库 `http.server` + 手写 WebSocket（RFC 6455），无第三方 Web 框架；设备侧用 `pymobiledevice3`。
- **虚拟定位**：DVT 服务 `com.apple.instruments.dtservicehub`（iOS 17+，走 RSD 隧道），调用 `LocationSimulation.set(lat, lon)` / `clear()`。
  **连接必须长连接保持**——DVT 会话一断，模拟定位立即失效，这决定了播放引擎不能每发一个点就重连。
- **播放引擎**：按弧长积分推进；速度剖面按转弯角度生成（急弯自动减速）再做滑动平均，因此加减速自然。独立线程 + 单一在途请求，保证下发顺序严格不乱。
- **真实感模型**：位置漂移用**多尺度值噪声**（慢漂 120m + 中尺度 30m + 快抖 15m 相关长度，接近真实 GPS 误差的幂律谱），幅值标定到设定半径并做上限裁剪（1.8 倍）；相邻上报点之间平滑过渡，不会出现随机跳点造成的毛刺。
- **坐标**：内部与后端**一律 WGS84**；高德瓦片是 GCJ-02，由前端在请求瓦片与落点时做转换，避免"地图上的点和实际定位差 500 米"。
- **瓦片**：服务端代理 + 磁盘缓存（默认 `~/.ios-location-tiles`，上限 1GB，LRU 淘汰）。会自动识别并丢弃服务商返回的"全透明空瓦片"。
- **路线**：优先 OSRM 真实路网（`router.project-osrm.org`），无网络时退化为直线并在地图上标注引擎类型。
- **独立打包**：PyInstaller onedir + ad-hoc 签名。隧道建立通过「自调用」实现（`<应用自身> tunnel-helper`），因此打包后不需要机器上有 Python。
- **接口契约**：见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

---

## 9. 已知限制

- **DVT 只能改经纬度**：`simulateLocationWithLatitude:longitude:` 仅接受两个参数。`horizontalAccuracy`、`verticalAccuracy`、`altitude`、`floor`、`sourceInformation.isSimulatedBySoftware` 以及加速度计/陀螺仪/计步器由 iOS 自行合成或来自硬件，**DVT 通道无法注入**。这是所有同类工具（含 Xcode 自带的模拟定位）的共同边界。
- **部分 App 不受影响**：使用独立定位通道、或在服务端做运动学交叉校验的 App 可能不会跟随模拟定位，或跟随了但数据被判定无效。
- **同一时间只能有一个工具占隧道**：iOS 17+ 设备只有一条 RSD 连接，爱思助手/Xcode 与本工具会互相挤掉。
- 首次挂载镜像的签名步骤需要能访问 Apple TSS 服务器。
- 地图瓦片来自公开服务，仅供个人测试使用，请遵守相应服务条款。

---

## 10. 合法使用

本工具用于**软件测试、开发调试、位置相关功能的回归验证**等正当用途，例如：

- 测试 App 在不同城市/国家的展示与逻辑
- 验证轨迹回放、电子围栏、打卡类功能的边界情况
- 演示与教学

请勿用于欺骗他人、伪造行程记录、规避考勤或任何违法违规用途。使用本工具产生的一切后果由使用者承担。

特别说明：本工具**不提供**针对第三方应用风控/反作弊的绕过能力。如果你需要测试依赖海拔、气压计、计步器等硬件数据的功能，唯一可靠的方式是在你自己 App 的 Debug 构建里加入定位注入入口、自行构造完整的 `CLLocation`。
