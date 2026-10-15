# 设备侧取证笔记（pymobiledevice3 / DVT 虚拟定位 / iOS 17+ 隧道）

> 本文由 **device-dev**（task-2）维护。所有 API 事实均取自**本机真实源码**，不是凭记忆。
> 冻结契约见 [PROTOCOL.md](PROTOCOL.md)，本文件不修改契约，只记录实现依据。
>
> 取证来源：先从阿里云镜像下载官方 wheel 并解包到 `/tmp/pmd3src/extracted/`，
> 再与安装进 `.venv` 的 `site-packages` 版本逐条比对（见第 1 节）。
> 引用格式：`文件路径:行号`，相对 `pymobiledevice3/` 包根目录。

---

## 1. 版本与 Python 3.12 依赖

| 项目 | 结果 |
| --- | --- |
| 包版本 | **11.29.1**（wheel：`pymobiledevice3-11.29.1-py3-none-any.whl`） |
| `Requires-Python` | `>=3.9`（wheel METADATA 第 24 行） |
| 官方 classifier | 明确列出 `Programming Language :: Python :: 3.12`（METADATA 第 21 行） |
| 本机解释器 | `.venv` → Python **3.12.14**（Homebrew `/opt/homebrew/opt/python@3.12`） |
| 安装源 | 阿里云镜像 `https://mirrors.aliyun.com/pypi/simple/`（pypi.org 在本机 12s 才响应，官方源不可用） |
| 安装结果 | ✅ 成功（`Successfully installed … pymobiledevice3-11.29.1 …`，退出码 0，日志 `/tmp/pmd3_install.log`） |
| 安装位置 | `/Users/jbjf/Desktop/location/.venv/lib/python3.12/site-packages` |
| 纯 Python | `py3-none-any`，无本地编译（个别依赖带 arm64 wheel，如 `qh3`、`cffi`） |

依赖里与隧道/DTX 相关的关键项（METADATA 第 25-68 行）：

- `cryptography>=43.0.0`、`construct>=2.9.29`、`bpylist2>=4.0.1` —— lockdown / DTX 编解码
- `qh3<2,>=1.0.0` —— QUIC（tunneld 与 RemotePairing 隧道）
- `pytun-pmd3>=3.0.3` —— 内核 utun 隧道（需要 root）
- `pmd-pytcp>=0.3.7; python_version >= "3.9"` —— **免 root 的纯 Python 用户态网络栈**（userspace 隧道）
- `sslpsk-pmd3>=1.0.3; python_version < "3.13"` —— 本机是 3.12，所以会装它

> 注意：Python 3.12 下 `TunnelProtocol.DEFAULT` = **QUIC**（`cli/remote.py:108-116`：
> "default: TCP on Python >=3.13, otherwise QUIC"），这是后面隧道方案取舍的依据之一。

---

## 2. iOS<17 路径：usbmux / lockdown 直连 DVT

### 2.1 拿 lockdown 客户端

`lockdown.py:1370-1382`：

```python
async def create_using_usbmux(
    serial: Optional[str] = None,
    identifier: Optional[str] = None,
    label: str = DEFAULT_LABEL,
    autopair: bool = True,
    connection_type: Optional[str] = None,
    pair_timeout: Optional[float] = None,
    local_hostname: Optional[str] = None,
    pair_record: Optional[dict[str, Any]] = None,
    pairing_records_cache_folder: Optional[Path] = None,
    port: int = SERVICE_PORT,
    usbmux_address: Optional[str] = None,
) -> UsbmuxLockdownClient:
```

- 返回值是 `UsbmuxLockdownClient`（`lockdown.py:1149`），继承 `LockdownClient`（`lockdown.py:102`）。
- 它是 `LockdownServiceProvider`（`lockdown_service_provider.py:15`）的子类，因此可以直接丢给 `DvtProvider`。
- `close()` 是 async：`lockdown.py:899-904` → `await self.service.close()`。
- 开发者模式查询：`lockdown.py:534-541` `async def get_developer_mode_status(self) -> bool`
  （读 `com.apple.security.mac.amfi` 域的 `DeveloperModeStatus`）。

### 2.2 usbmux 地址怎么拿

`usbmux.py`：

| 行号 | API | 说明 |
| --- | --- | --- |
| 129-132 | `@dataclass class MuxDevice(devid, serial, connection_type)` | usbmux 设备条目 |
| 452 | `async def create_mux(usbmux_address=None)` | 打开 usbmuxd 连接（async context manager） |
| 456 | `async def list_devices(usbmux_address=None) -> list[MuxDevice]` | 枚举设备 |
| 485 | `async def select_device(udid=..., connection_type=...)` | 选设备 |
| 500-507 | `async def select_devices_by_connection_type(connection_type, usbmux_address=None)` | 按 USB/Network 过滤 |

`usbmux_address` 参数适用于远程/自定义 usbmuxd（`USBMUXD_SOCKET_ADDRESS` 环境变量）；本机默认走
`/var/run/usbmuxd`。**我们不需要自己连 usbmux 拿地址**：`create_using_usbmux(serial=...)` 内部已完成
`ServiceConnection.create_using_usbmux(...)`（`lockdown.py:1404-1406`）。

### 2.3 DVT provider 怎么建

`services/dvt/instruments/dvt_provider.py:8-53`：

```python
class DvtProvider(DtxServiceProvider):
    SERVICE_NAME = "com.apple.instruments.remoteserver.DVTSecureSocketProxy"   # iOS 14-16
    RSD_SERVICE_NAME = "com.apple.instruments.dtservicehub"                    # iOS 17+
    OLD_SERVICE_NAME = "com.apple.instruments.remoteserver"                    # iOS < 14

    def __init__(self, lockdown: LockdownServiceProvider,
                 strip_ssl: Optional[bool] = None,
                 dtx: Optional[DTXConnection] = None) -> None: ...
```

**关键结论：iOS<17 与 iOS≥17 用的是同一个 `DvtProvider` 类**，服务名由
`DtxServiceProvider.service_name_for()` 自动选择（`dtx_service_provider.py:80-97`）：

```python
@classmethod
def service_name_for(cls, service_provider: LockdownServiceProvider) -> str:
    if isinstance(service_provider, RemoteServiceDiscoveryService):
        return cls.RSD_SERVICE_NAME if cls.RSD_SERVICE_NAME is not None else cls.SERVICE_NAME
    if cls.OLD_SERVICE_NAME is not None and Version(service_provider.product_version).major < 14:
        return cls.OLD_SERVICE_NAME
    return cls.SERVICE_NAME
```

- 传 `RemoteServiceDiscoveryService` → `com.apple.instruments.dtservicehub`（隧道，iOS17+）
- 传 `LockdownClient` → `com.apple.instruments.remoteserver.DVTSecureSocketProxy`（iOS14-16）
- 传 `LockdownClient` 且 `product_version < 14` → `com.apple.instruments.remoteserver`

SSL 剥离（老服务需要）也自动判断：`dtx_service_provider.py:99-111 _default_strip_ssl()`，
RSD 永远返回 `False`，lockdown 且 iOS<14 返回 `True`。

### 2.4 iOS<17 的另一条路（我们用作兜底）

`services/simulate_location.py:8-40` `DtSimulateLocation`，走老开发者服务
`com.apple.dt.simulatelocation`，自己手写 4 字节大端协议头：

```python
SERVICE_NAME = "com.apple.dt.simulatelocation"          # :17
async def clear(self):                                  # :23-26
    service = await self.lockdown.start_lockdown_developer_service(self.SERVICE_NAME)
    await service.sendall(struct.pack(">I", 1))
async def set(self, latitude, longitude):               # :28-40
    service = await self.lockdown.start_lockdown_developer_service(self.SERVICE_NAME)
    await service.sendall(struct.pack(">I", 0))
    await service.sendall(struct.pack(">I", len(lat)) + lat.encode())
    await service.sendall(struct.pack(">I", len(lon)) + lon.encode())
```

pymobiledevice3 的 CLI 把这条路标为 **iOS<17 专用**：`cli/developer/simulate_location.py:16`
（`help="Simulate GPS location on iOS versions below 17 ..."`），对应命令
`pymobiledevice3 developer simulate-location set -- LAT LON`。
注意它**每次 set 都新开一个服务连接**（源码注释 `services/simulate_location.py:13-14`：
"A fresh lockdown developer service connection is opened for each command."），所以它天然不需要长连接；
但需要设备已挂载开发者镜像（`start_lockdown_developer_service`，`lockdown_service_provider.py:162-170`
失败时会提示 `pymobiledevice3 mounter mount`）。

---

## 3. iOS≥17 路径：RSD 隧道 + DVT

### 3.1 RSD 客户端构造参数

`remote/remote_service_discovery.py:81-152`：

```python
class RemoteServiceDiscoveryService(LockdownServiceProvider):
    def __init__(
        self,
        address: tuple[str, int],                                  # (host, port)
        name: Optional[str] = None,
        open_connection: Optional[Callable[..., Any]] = None,      # 自定义拨号器（userspace 隧道注入）
        auxiliary_metadata: Optional[dict[str, dict[str, Any]]] = None,
        handshake_uuid: Optional[uuid.UUID] = None,
    ) -> None:
```

- **注意是位置参数 `address` 一个元组**，不是 `host=` / `port=` 两个关键字（`self.service = RemoteXPCConnection(address, ...)`，:141）。
- host/port 从**隧道**来：内核 utun 隧道（有 root）、macOS 原生隧道、或 tunneld 发布的地址。
- `RSD_PORT = 58783`（`remote_service_discovery.py:38`），Bonjour 广播的 RSD 端口。
- 是 async context manager：`__aenter__` → `connect()`（:558-563），`__aexit__` → `close()`（:552-556）。
- `connect()`（:303-342）：握手后填充 `peer_info`、`udid`、`product_type`，并尽力建立远程 lockdown
  （`com.apple.mobile.lockdown.remote.trusted` → `.untrusted`）。
- `product_version`（:178-185）从 `peer_info["Properties"]["OSVersion"]` 取，**connect 之后才可用**。

### 3.2 服务发现：没有 browse，直接查 peer_info

RSD 不用 lockdownd 的 StartService RPC。真正的 API：

| 行号 | API | 返回 |
| --- | --- | --- |
| 545-550 | `_get_service_entry(name)` | `peer_info["Services"][name]`，缺失抛 `InvalidServiceError` |
| 503-511 | `get_service_port(name) -> int` | 该服务的设备侧 TCP 端口 |
| 513-525 | `get_service_features(name)` | 服务声明的 features |
| 472-483 | `start_remote_service(name)` | 未连接的 `RemoteXPCConnection` |
| 409-451 | `start_lockdown_service(name)` | 完成 RSD check-in 的 `ServiceConnection` |
| 485-501 | `start_service(name)` | 按 `UsesRemoteXPC` 自动选上面两种 |

`peer_info["Services"]` 的每一项形如 `{"Port": <int>, "Properties": {...}}`。
**没有 `browse()` 方法**；Bonjour 浏览是 `remote/utils.py get_rsds()` 与 `cli/remote.py browse` 的事。

### 3.3 DVT provider 怎么建（iOS≥17）

**不需要** `DvtProvider` 之外的类，也没有 `RemoteServiceDiscoveryService.get_dvt_provider()` 这个方法
（我在 11.29.1 源码里 grep 过 `get_dvt_provider`，不存在）。正确做法是把 RSD 对象直接传给 `DvtProvider`：

```python
async with RemoteServiceDiscoveryService((host, port)) as rsd:
    async with DvtProvider(rsd) as dvt:          # 自动用 com.apple.instruments.dtservicehub
        ...
```

依据：`dtx_service_provider.py:93-94` 的 `isinstance(service_provider, RemoteServiceDiscoveryService)` 分支
+ `dvt_provider.py:34 RSD_SERVICE_NAME`。

---

## 4. DVT 虚拟定位的精确调用方式（最重要）

### 4.1 类与方法签名

`services/dvt/instruments/location_simulation.py:7-47`：

```python
class LocationSimulationService(DTXService):                       # :7
    IDENTIFIER = "com.apple.instruments.server.services.LocationSimulation"   # :8

    @dtx_method("simulateLocationWithLatitude:longitude:")          # :10
    async def simulate_location_with_latitude_longitude_(self, latitude: float, longitude: float) -> None: ...

    @dtx_method("stopLocationSimulation", expects_reply=False)      # :13
    async def stop_location_simulation(self) -> None: ...


class LocationSimulation(DtxService[LocationSimulationService], LocationSimulationBase):   # :17
    def __init__(self, dvt: DtxServiceProvider):                    # :26
        super().__init__(dvt)
        LocationSimulationBase.__init__(self)

    async def set(self, latitude: float, longitude: float) -> None:  # :30
        await self.service.simulate_location_with_latitude_longitude_(latitude, longitude)

    async def clear(self) -> None:                                   # :41
        await self.service.stop_location_simulation()
```

- 包路径：`pymobiledevice3.services.dvt.instruments.location_simulation.LocationSimulation`
- 类名**不是** `DvtLocationSimulator`（11.29.1 里没有这个名字），是 `LocationSimulation`。
- 方法名是 `set(latitude, longitude)` / `clear()`，**不是** `simulate_location(lat, lon)` / `clear_location()`。
- 构造参数是 **`DvtProvider`（`DtxServiceProvider`）**，不是 lockdown。

### 4.2 上下文管理器语义（决定我们怎么保持长连接）

`dtx_service.py:21-170`：

```python
class DtxService(Generic[_SVC_T]):
    def __init__(self, provider: DtxServiceProvider, service: Optional[_SVC_T] = None) -> None:  # :52
        self._provider = provider
        self._service = service
    async def connect(self) -> None:          # :140-151  打开 DTX channel（幂等）
    async def close(self) -> None:            # :153-158  **no-op**（通道生命周期归 provider 管）
    async def __aenter__(self) -> Self:       # :160-162  await self.connect()
    async def __aexit__(...):                 # :164-170  await self.close()
```

`dtx_service_provider.py:117-178`：

```python
def __init__(self, lockdown, strip_ssl=None, dtx=None)   # :117  （dtx 传入时 provider 不负责关它）
async def connect(self) -> None:                          # :149  打开传输 + DTX 握手（幂等）
async def close(self) -> None:                            # :167  await self._dtx.aclose()
async def __aenter__/__aexit__                            # :173-178
```

**结论（三条，全部有源码依据）**：

1. `LocationSimulation` 与 `DvtProvider` **都是 async context manager**，但也可以手动
   `await provider.connect()` → `await location.connect()`，然后一直复用（`connect()` 幂等，:154/:149）。
2. `LocationSimulation.close()` 是 **no-op**（`dtx_service.py:153-158`），真正的释放靠
   `DvtProvider.close()` 关闭 DTX 连接（`dtx_service_provider.py:167-171`）。
3. **必须显式调用 `clear()`**，否则设备会一直停留在模拟位置；`__aexit__` 不会帮你 clear。

### 4.3 「连接断掉定位就失效」——CLI 取证

`cli/developer/dvt/simulate_location.py:36-38`：

```python
async with DvtProvider(service_provider) as dvt, LocationSimulation(dvt) as location_simulation:
    await location_simulation.set(latitude, longitude)
    OSUTILS.wait_return()
```

`OSUTILS.wait_return()` 在 POSIX 上是阻塞等待信号（`osu/posix_util.py:129-131`）：

```python
def wait_return(self):
    print("Press Ctrl+C to send a SIGINT or use 'kill' command to send a SIGTERM")
    signal.sigwait([signal.SIGINT, signal.SIGTERM])
```

即官方 CLI **set 之后一直挂在连接上直到 Ctrl-C**。这直接证明：**DVT 虚拟定位依赖长连接，
连接断开后模拟位置即失效**。→ 我们的 `RealLocationProvider` 必须长连接（见第 6 节实现）。

### 4.4 可直接复制的 10 行示例（iOS≥17）

```python
import asyncio
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

async def main(host: str, port: int, lat: float, lon: float) -> None:
    async with RemoteServiceDiscoveryService((host, port)) as rsd:      # 隧道给的 RSD 地址
        async with DvtProvider(rsd) as dvt:                              # -> dtservicehub
            async with LocationSimulation(dvt) as sim:                   # 打开 DTX channel
                await sim.set(lat, lon)                                  # 单点定位
                await asyncio.sleep(30)                                  # 期间保持连接！
                await sim.clear()                                        # 恢复真实定位

asyncio.run(main("10.7.0.1", 51234, 39.908, 116.397))
```

iOS<17（无隧道）只需把第一行换成 lockdown：

```python
from pymobiledevice3.lockdown import create_using_usbmux
lockdown = await create_using_usbmux(serial=None)      # 第一台 USB 设备
# 之后 DvtProvider(lockdown) -> LocationSimulation 完全一样
```

### 4.5 CLI 等价命令

| 命令 | 适用 | 源码 |
| --- | --- | --- |
| `pymobiledevice3 developer dvt simulate-location set -- LAT LON` | **iOS 17+**（跑在隧道上） | `cli/developer/dvt/simulate_location.py:26-38` |
| `pymobiledevice3 developer dvt simulate-location clear` | iOS 17+ | 同上 :18-23 |
| `pymobiledevice3 developer simulate-location set -- LAT LON` | **iOS<17**（老服务） | `cli/developer/simulate_location.py:28-35` |
| `pymobiledevice3 developer simulate-location clear` | iOS<17 | 同上 :21-25 |

两者都在 `developer` 子命令树里（`cli/developer/__init__.py:64-71`：`cli.add_typer(dvt.cli)`、
`cli.add_typer(simulate_location.cli)`）。注意 **`dvt` 是父命令名**，
`developer simulate-location` 与 `developer dvt simulate-location` 是两条不同的命令。

---

## 5. iOS 17+ 隧道方案（逐条 + 是否需要 root）

### 5.1 结论速查表

| 方案 | 命令 | 需要 root？ | 跨进程可用？ | 源码依据 |
| --- | --- | --- | --- | --- |
| 经典内核隧道 | `sudo pymobiledevice3 remote start-tunnel --script-mode` | **是** | 是（打印 `HOST PORT`） | `cli/remote.py:326-429`，`is_admin` 检查在 :417-418 |
| lockdown 别名 | `sudo pymobiledevice3 lockdown start-tunnel --script-mode` | **是** | 是 | `cli/lockdown.py:211-222`（`@sudo_required`） |
| macOS 原生隧道 | `pymobiledevice3 remote start-tunnel --native --script-mode` | **否** | 是 | `cli/remote.py:297-323`、:367-376、:405-416 |
| 进程内 userspace 隧道 | `--userspace` / `UserspaceRsdTunnel` | **否** | **否**（仅同进程） | `userspace_tunnel.py:793-834`、`cli/cli_common.py:564-573` |
| tunneld 服务 | `sudo pymobiledevice3 remote tunneld` | **是**（`@sudo_required`） | 是（但客户端要按 tunneld 协议/dialer 接） | `cli/remote.py:93-125`、`tunneld/api.py:16` |

### 5.2 逐条取证

**(a) `lockdown start-tunnel` —— 需要 root**

`cli/lockdown.py:211-222`：

```python
@cli.command("start-tunnel")
@sudo_required
@async_command
async def cli_start_tunnel(service_provider: ServiceProviderDep, script_mode: ... = False) -> None:
    """start tunnel"""
    await async_cli_start_tunnel(service_provider, script_mode)
```

`@sudo_required` 的实现（`cli/cli_common.py:165-173`）：

```python
def sudo_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not OSUTILS.is_admin:
            raise AccessDeniedError()
        else:
            func(*args, **kwargs)
    return wrapper
```

即非 root 直接抛 `AccessDeniedError`（`exceptions.py:546-549` "Need extra permissions to execute this command"）。
它内部用 `TunnelProtocol.TCP`（`cli/lockdown.py:207`）。

**(b) `remote start-tunnel` —— 经典路径需要管理员，`--native` 免 root**

`cli/remote.py:326-429`，关键几段：

```python
@cli.command("start-tunnel")
@async_command
async def cli_start_tunnel(...):
    """start tunnel (classic root tunnel by default; --native piggybacks Apple's tunnel on macOS, no root)"""
    ...
    if native:
        ...
        await native_tunnel_task(udid, script_mode=script_mode)     # :415  免 root 分支
        return
    if not OSUTILS.is_admin:                                        # :417-418
        raise AccessDeniedError()
```

`native_tunnel_task`（`cli/remote.py:297-323`）会打印：

```
Identifier: <UDID>
RSD Address: <address>
RSD Port: <port>
Use the follow connection option:
--rsd <address> <port>
```

`--native` 的帮助文本（:367-376）明确写了 "no root, macOS only"，并提示它和 remoted 会互相抢占 RSD 连接
（"Its RSD connection and remoted's evict each other, so it is opt-in"）。实现见
`remote/native_tunnel.py:950 async def establish_native_rsd(serial=None)`。

**(c) `remote tunneld` —— 需要 root，且默认 QUIC**

`cli/remote.py:93-125`：

```python
@cli.command("tunneld")
@sudo_required                                    # :94  ← 需要 root
def cli_tunneld(
    host: ... = TUNNELD_DEFAULT_ADDRESS[0],       # :96
    port: ... = TUNNELD_DEFAULT_ADDRESS[1],       # :97
    protocol: ... = TunnelProtocol.DEFAULT,       # :116  Python 3.12 → QUIC
    ...
```

- 默认监听地址：`tunneld/api.py:16` `TUNNELD_DEFAULT_ADDRESS = ("127.0.0.1", 49151)`。
- `remote.py:108-116` 的 help：**"default: TCP on Python >=3.13, otherwise QUIC"** → 本机 3.12 默认 QUIC。
- tunneld 的价值是「一次 root 起服务，之后多个非特权客户端复用」；但客户端要按 tunneld 的
  传输协议接入（QUIC + 自定义 API），对我们的单进程应用来说复杂度明显高于 (a)/(b)。

**(d) 进程内 userspace 隧道 —— 完全免 root，但只在同进程可用**

`remote/userspace_tunnel.py:793-834` 的类文档：

```
class UserspaceRsdTunnel:
    """A no-root, in-process iOS 17+ RSD tunnel and its connected RSD, as one closeable handle.
    Replaces the kernel utun (which needs root/admin) with a pure-Python PyTCP stack ...
    Constraints:
    * One tunnel per process. ...
    * The device address is in-process only, reachable only from this process's userspace
      stack — never by an external tool. The RSD reports this via
      RemoteServiceDiscoveryService.is_in_process_tunnel ...
```

`remote_service_discovery.py:146-152`：

```python
@property
def is_in_process_tunnel(self) -> bool:
    """True when this RSD reaches the device through an in-process dialer (the userspace
    tunnel) rather than a kernel-routable interface. The device address (self.service.address)
    is then only reachable from THIS process ..."""
    return self.open_connection is not None
```

便利函数：`userspace_tunnel.py:998-1000`
`async def establish_userspace_rsd(serial=None, autopair=True, remotepairing_fallback=True) -> RemoteServiceDiscoveryService`。

**(e) 上游的自动升级逻辑（可作为我们的语义参照）**

`__main__.py:408-452`：当 iOS 17+ 设备上的 developer 命令因 `InvalidServiceError` /
`RSDRequiredError` 失败时，CLI 会**自动重试一次并建立默认隧道**，日志为：

```
Trying again over {the native tunnel|a no-root userspace tunnel|tunneld} since
it is a developer command on an iOS 17+ device
(override with PYMOBILEDEVICE3_DEFAULT_FALLBACK, or --tunnel/--userspace/--native)
```

默认优先级由 `cli/cli_common.py:433-454 default_transport_preference()` 决定：内置默认
`"userspace"`（免 root），可用环境变量 `PYMOBILEDEVICE3_DEFAULT_FALLBACK` 覆盖为 `native`/`tunneld`。

### 5.3 无 root 情况下最可行的方案（明确推荐）

**推荐 ①（跨进程，macOS，免 root）**：

```bash
./.venv/bin/python -m pymobiledevice3 remote start-tunnel --native --script-mode
# 输出：<RSD_HOST> <RSD_PORT>   → 交给 RealLocationProvider(rsd_host=..., rsd_port=...)
```

理由：无需 sudo；打印的地址是**内核可路由**的，任何进程都能用；正好匹配
「core/tunnel.py 提供 rsd_host/rsd_port」的架构。
代价：与系统的 `remoted` 抢占同一台设备的 RSD 连接，可能被顶掉（重新建隧道即可）。

**推荐 ②（有管理员权限时最稳）**：

```bash
sudo ./.venv/bin/python -m pymobiledevice3 remote start-tunnel --script-mode
# 输出：<RSD_HOST> <RSD_PORT>
```

这是官方经典路径，不依赖 macOS 的 remoted，长时间运行最稳定；`run.sh` 提示用户输入密码即可。

**推荐 ③（单进程应用，完全免 root）**：进程内 userspace 隧道
（`UserspaceRsdTunnel` / `establish_userspace_rsd`），但 RSD 地址**不能**跨进程传递；
如果要走这条路，必须把**已连接的 RSD 对象**直接注入 `RealLocationProvider(rsd=...)`
（我为这个场景预留了可选关键字参数，见第 6.3 节）。

**不推荐**：`remote tunneld`（要 root + Python 3.12 下默认 QUIC，客户端集成成本高）；
`lockdown start-tunnel`（要 root，且只是经典隧道的别名）。

---

## 6. 我们的实现（core/location_provider.py / core/location_mock.py）

### 6.1 与 lead 的接口约定

- 只使用 lead 提供的 `DeviceManager.list_devices()`（Device dict 字段见 PROTOCOL.md 2.1）与
  `DeviceManager.get_selected()`；`connect(device)` 用容错读取（dict 的 `.get()`），字段缺失不会崩。
- 我**不改** `core/device.py`、`core/tunnel.py`；需要的额外能力通过构造参数注入。
- RSD 地址由 lead 的 `core/tunnel.py` 提供。

### 6.2 冻结签名与返回结构

```python
class RealLocationProvider:
    def __init__(self, rsd_host=None, rsd_port=None, udid=None, *, rsd=None, lockdown=None,
                 connect_timeout=15.0, on_log=None, log=None)   # log= 是 on_log 的别名
    async def connect(self, device) -> dict     # 建立长连接会话
    def connect_sync(self, runner, device=None) -> dict   # 同步版（提交给调用方 runner）
    async def set_location(self, lat, lon) -> None
    async def clear_location(self) -> None
    async def close(self) -> None
    @property connected -> bool
    async def probe(self, device=None) -> dict  # {"ok","detail","ios_major",...}；device 可选
```

`MockLocationProvider` 同构：`MockLocationProvider(device=None, on_log=None, latency=None, log=None)`
+ `connect_sync(runner, device=None)`。

> **`log=` 与 `connect_sync()` 是按 lead 的 `core/server.py` 真实调用点加的兼容别名**
> （`core/server.py:147` `MockLocationProvider(log=self.log)`、`:148` `provider.connect_sync(self.runner)`、
> `:167-172` `RealLocationProvider(..., log=self.log)`）。冻结的五个方法签名一个都没改，只是**新增**。
> `connect_sync` 只调用传入 runner 的 `run(coro)`，**不新建事件循环/线程**（协程仍在 runner 自己的循环里跑）。

`connect()` 返回（mock 与真机同构，方便 lead 组装 `/api/devices/connect`）：

```json
{"ok": true, "mock": false, "udid": "...", "ios_version": "26.0", "ios_major": 26,
 "connection": "usb", "tunnel": "connected", "strategy": "dvt-rsd",
 "rsd": "10.7.0.1:51234", "pymobiledevice3": "11.29.1", "detail": "中文说明"}
```

### 6.3 三条策略与降级顺序

| strategy | 触发条件 | 实现 |
| --- | --- | --- |
| `dvt-rsd` | 注入了 `rsd=` 对象，或给了 `rsd_host`+`rsd_port` | `RemoteServiceDiscoveryService((host,port))` → `DvtProvider(rsd)` → `LocationSimulation` |
| `dvt-lockdown` | 无 RSD 且 iOS<17 | `create_using_usbmux(serial=udid)` → `DvtProvider(lockdown)` → `LocationSimulation` |
| `legacy` | `dvt-lockdown` 失败 | `DtSimulateLocation`（`com.apple.dt.simulatelocation`，**会真开一次服务校验可用性**） |

- **长连接**：`connect()` 只建一次；`set_location()` 复用同一 DTX channel（`await self._location.set(...)`），
  不重连。依据见 4.3（连接断开定位即失效）。
- **同一事件循环**：全部原生 async/await，无线程、无 `asyncio.run`。
- **中文异常**：所有失败包成 `LocationProviderError`，文案含修复建议；iOS≥17 无隧道时直接给出
  「请以管理员权限运行 run.sh」+「--native 免 root 隧道」两条路。
- **不崩的降级**：未安装 pymobiledevice3 时 `import core.location_provider` 仍然成功，
  `probe()` 返回 `ok=False` + 中文安装指引，而不是 `ModuleNotFoundError`。
- **不假成功**：`legacy` 策略在 `connect()` 阶段就真开一次
  `start_lockdown_developer_service()` 校验（源码依据：`services/simulate_location.py:19-22`
  的构造不做设备 I/O），真机实测踩到过这个坑，详见 8.1。

### 6.4 `probe()` 返回

```json
{"ok": false, "detail": "iOS 17 及以上必须先用 RSD 隧道…", "ios_major": 26, "strategy": null}
```

- `probe()` **永不抛异常**，供前端「测试连接」按钮直接展示 `detail`。
- 可选参数 `device`（与 `connect()` 同一个 Device dict）：传入后先做静态判断
  （未配对 / 未开开发者模式 / iOS 版本），能省掉一次设备往返并回填 `ios_major`。
- 无隧道 + iOS<17 时会**真的去连一次**（点「测试连接」就应该真的测）；
  无隧道 + iOS17+ 时只报隧道状态，不做无意义重连。

### 6.5 与 lead 的 `core/server.py` 真实联调（`--mock` 模式端到端）

我用 lead 的入口在 **8791** 端口起了一次 mock 服务（不干扰他 8799 上的实例），实测：

```console
$ ./.venv/bin/python main.py --mock --port 8791 --no-browser --window none

$ curl -s http://127.0.0.1:8791/api/health
{"ok":true,"version":"1.0.0","name":"iOS 虚拟定位轨迹工具","mock":true,"device_count":1,
 "backend":"mock","pymobiledevice3":true,"uptime":2.31,"ws_clients":0}

$ curl -s -X POST http://127.0.0.1:8791/api/devices/connect -d '{}'
{"ok":true,"device":{...},"tunnel":{...},
 "probe":{"ok":true,"detail":"模拟模式：DVT 虚拟定位可用（iOS 26，无需隧道）","ios_major":26}}

$ curl -s -X POST http://127.0.0.1:8791/api/location -d '{"lat":39.908,"lng":116.397}'
{"ok":true}

$ curl -s "http://127.0.0.1:8791/api/logs?limit=20"
… {"level":"success","msg":"[模拟模式] 已连接模拟设备 (iPhone 17 Pro)（udid=MOCK-0001，iOS 26.0）"}
  {"level":"info",   "msg":"[模拟模式] 模拟模式：定位下发仅记录日志，不会真正修改设备位置"}
  {"level":"info",   "msg":"[模拟模式] 下发定位 lat=39.908000, lon=116.397000（第 1 次，模拟）"}
  {"level":"success","msg":"已把设备定位到 39.908000, 116.397000"}
  {"level":"info",   "msg":"[模拟模式] 下发定位 lat=31.230400, lon=121.473700（第 2 次，模拟）"}
```

验证到：

1. `MockLocationProvider(log=self.log)` + `connect_sync(self.runner)` 在 lead 的真实装配下工作正常；
2. `probe` 的中文 detail 直接出现在 `/api/devices/connect` 响应里（前端「测试连接」可直接用）；
3. `set_location(lat, lon)` 的**参数顺序正确**（日志里 lat=39.908 / lon=116.397 没有互换）；
4. provider 的日志经 `log=` 回调进入 lead 的日志面板（`/api/logs` 可见）。

> 测试用的 8791 服务已关闭（`lsof` 确认端口释放），lead 的 8799 实例未受影响。

---

## 7. 自测命令与真实输出

以下命令均为 device-dev 在本机实际执行过、并复制真实输出的结果。

### 7.1 安装结果

```console
$ ./.venv/bin/python -m pip install --index-url https://mirrors.aliyun.com/pypi/simple/ \
      --timeout 30 --retries 2 pymobiledevice3
...
Successfully installed ASGIMiddlewareStaticFile-0.7.0 IPython-9.17.1 Pillow-12.3.0 aiofiles-25.1.0
... pymobiledevice3-11.29.1 pmd-pytcp-0.3.7 qh3-1.9.4 pytun-pmd3-3.0.3 cryptography-50.0.2
... sslpsk-pmd3-1.0.3 typer-0.27.3 typer-injector-0.3.0 ...
[exit 0]

$ ./.venv/bin/python -m pip show pymobiledevice3
Name: pymobiledevice3
Version: 11.29.1
Location: /Users/jbjf/Desktop/location/.venv/lib/python3.12/site-packages
```

`site-packages` 里的关键源码与用于取证的 wheel **逐字节一致**（`diff -q` 全部 SAME）：

```console
SAME  services/dvt/instruments/location_simulation.py
SAME  services/dvt/instruments/dvt_provider.py
SAME  dtx_service_provider.py
SAME  dtx_service.py
SAME  remote/remote_service_discovery.py
SAME  services/simulate_location.py
```

### 7.2 语法 / import 自检

```console
$ ./.venv/bin/python -m compileall core/location_provider.py core/location_mock.py tools/*.py
COMPILEALL OK

$ ./.venv/bin/python -c "import core.location_provider, core.location_mock; print('IMPORT OK')"
IMPORT OK
```

> 这两个模块在 **pymobiledevice3 尚未安装时**也照样 import 成功（懒加载 + 中文错误），
> 已在安装前实测过一次。

### 7.3 离线自测（无设备也全绿）

```console
$ ./.venv/bin/python tools/selftest_location.py
====================================================================
虚拟定位后端离线自测（无需 iOS 设备）
Python 3.12.14 | cwd=/Users/jbjf/Desktop/location
====================================================================

== 1. MockLocationProvider ==
  [通过] 初始 connected=False
  [通过] connect 返回 dict 且 ok=True —— 模拟设备已连接（iOS 26.0，无需隧道）
  [通过] connect 后 connected=True
  [通过] connect 返回中文说明
  [通过] set_location 延时 5~10ms —— 实测 9.90ms
  [通过] send_count 递增
  [通过] last_location 记录正确
  [通过] clear_location 后 last_location=None
  [通过] close 后 connected=False
  [通过] close 幂等
  [通过] mock probe ok=True —— {'ok': True, 'detail': '模拟模式：DVT 虚拟定位可用（iOS 26，无需隧道）', 'ios_major': 26}

== 2. RealLocationProvider 降级逻辑 ==
  pymobiledevice3 已安装：True（版本 11.29.1）
  [通过] 初始 connected=False
  [通过] 未连接下发抛 LocationProviderError —— 尚未连接设备，无法下发定位：请先调用 connect()（或点击界面上的「连接设备」）
  [通过] 未连接 clear_location 为 no-op
  [通过] 未配对设备报中文错误 —— 设备未与此电脑配对：请在 iPhone 上点击「信任此电脑」后重试…
  [通过] 未开开发者模式报中文错误 —— 设备未开启「开发者模式」：请在 iPhone 上打开 设置 → 隐私与安全性 → 开发者模式…
  [通过] iOS17+ 无隧道提示建隧道 —— 设备为 iOS 26.0，需要先建立 RSD 隧道才能虚拟定位。iOS 17 及以上必须先用 RSD 隧道…
  [通过] 越界纬度报中文错误 —— 经纬度超出合法范围：lat=123.0, lon=116.0
  [通过] probe 返回 ok/detail/ios_major
  [通过] probe detail 为中文
  [通过] probe(隧道不可达) 不抛异常且 ok=False —— 建立 RSD/DVT 会话失败：无法连接 RSD 隧道（连接被拒绝），隧道可能已断开或尚未建立
  [通过] 文案为中文
  [通过] probe(device) 未开开发者模式 → ok=False 且中文
  [通过] probe(device) 回填 ios_major=26
  [通过] probe(device) iOS17+ 无隧道 → 提示建隧道
  [通过] close 幂等且 connected=False
  [通过] 不可达隧道报中文错误 —— 建立 RSD/DVT 会话失败：无法连接 RSD 隧道（连接被拒绝），隧道可能已断开或尚未建立

== 3. 与 lead 的 core/server.py 集成 ==
  [通过] MockLocationProvider(log=...) 构造成功
  [通过] connect_sync(runner) 建立会话
  [通过] log= 回调收到日志 —— 2 条
  [通过] set_location(lat, lon) 经 runner 调用成功
  [通过] close 后 connected=False
  [通过] RealLocationProvider(log=...) 构造成功
  [通过] probe(device) 经 runner 调用不抛异常
  [通过] connect_sync 无隧道时抛中文异常 —— 设备为 iOS 26，需要先建立 RSD 隧道才能虚拟定位。iOS 17 及以上必须先用 RSD 隧道才能使用 DVT 虚拟定位…

====================================================================
通过 36 项，失败 0 项
全部通过 ✅
```

> 说明：上表是**最终一轮**的输出（36 项，含第 3 节照抄 `core/server.py` 用法的 8 条集成断言）；
> 早期未加这些断言时是 25 项，同样全绿。
> 第 3 节用的是 `core/server.py` 里 `AsyncRunner` 的同构替身（后台线程 + 独立事件循环），
> 因此也验证了「跨线程提交协程」这一真实运行模型下我的 provider 不会自己新建事件循环。

（`tools/selftest_location.py` 里断言了：错误信息必须含中文、必须是 `LocationProviderError` 而不是
`AttributeError`/`ModuleNotFoundError`。）

---

## 8. 真机事实（**本机实际接了两台 iPhone**）

> 任务书写的是「本机没有连接 iOS 设备」，但实测**有**。这条对验收影响很大，特此记录。

```console
$ ./.venv/bin/python -m pymobiledevice3 usbmux list
[
    {"BuildVersion": "20F66", "ConnectionType": "Network", "DeviceName": "iPhone 17 Pro Max",
     "Identifier": "00008120-0011713A1444C01E", "ProductType": "iPhone15,3", "ProductVersion": "16.5"},
    {"BuildVersion": "23G83", "ConnectionType": "USB", "DeviceName": "iPhone",
     "Identifier": "00008030-001551840A02402E", "ProductType": "iPhone12,1", "ProductVersion": "26.6.1"}
]
```

| UDID | 型号 | iOS | 连接 | 开发者模式 | 已挂载开发者镜像 |
| --- | --- | --- | --- | --- | --- |
| `00008120-0011713A1444C01E` | iPhone 17 Pro Max (iPhone15,3) | 16.5 | Network | ✅ 已开启 | ❌ `mounter list` → `[]` |
| `00008030-001551840A02402E` | iPhone (iPhone12,1) | **26.6.1** | USB | ❌ 未开启 | ❌ `mounter list` → `[]` |

`pymobiledevice3 doctor` 的真实结论（本机能力）：

```console
Host — this works:
  Device plugged in    iPhone (00008030-001551840A02402E), and usbmux lists it
  usbmux daemon        reachable, 2 device(s): …C01E over Network, …402E over USB
  Service access       opened a lockdown connection to 00008030-001551840A02402E
  Tunnel (native)      remotepairingd answered, 1 device(s)      ← 免 root 原生隧道在本机可用
  Tunnel (userspace)   pmd-pytcp 0.3.7                           ← 免 root 进程内隧道也可用

Host — not available here:
  Tunnel (tunneld)     not running on 127.0.0.1:49151

Device — worth knowing:
  Developer mode       disabled
  Developer image      no Personalized image is mounted
      so:  `developer` commands will fail even with a working tunnel
      fix: pymobiledevice3 mounter auto-mount
```

**结论：真机端到端现在无法跑通，原因不在代码**——两台设备都没有挂载开发者镜像，且 iOS 26.6.1 那台
没开开发者模式。要让真机路径生效，需要用户手动做两步（都会改动设备，device-dev 未擅自执行）：

1. iOS 26 那台开启「开发者模式」（`pymobiledevice3 amfi enable-developer-mode`，设备会重启并需解锁）；
2. `pymobiledevice3 mounter auto-mount` 挂载开发者镜像。

### 8.1 真机实测：iOS 16.5 设备（开发者模式开、无镜像、Network）

```console
$ ./.venv/bin/python tools/probe_device.py --udid 00008120-0011713A1444C01E --probe
pymobiledevice3 版本：11.29.1
Python：3.12.14

[真机] 建立 DVT 会话（iOS<17 直连）失败：设备未提供所需的开发者服务。iOS<17 通常是尚未挂载开发者镜像，
       请执行：./.venv/bin/python -m pymobiledevice3 mounter auto-mount；iOS 17+ 则需要 RSD 隧道…
       改用老版 simulatelocation 服务重试
=== 设备 1 ===
  UDID        : 00008120-0011713A1444C01E
  名称        : iPhone 17 Pro Max
  型号        : iPhone15,3
  iOS 版本    : 16.5 (major=16)
  连接方式    : network
  配对状态    : connected
  开发者模式  : 已开启
  DVT 可用性  : 不可用 —— iOS 16 < 17：可经 usbmux/lockdown 直连 DVT（或老版 simulatelocation 服务）
  实测连接    : 失败 —— 建立老版虚拟定位会话（com.apple.dt.simulatelocation）失败：设备未提供所需的开发者服务…
```

真机验证到的事实：

1. `create_using_usbmux(serial=...)` 对 **Network（Wi-Fi）** 连接的设备同样有效；
2. 无开发者镜像时，`DvtProvider(lockdown).connect()` 真的抛 `InvalidServiceError`（`str(e)` 就是
   `"InvalidService"`，不带 "Error" 后缀）；
3. **实测发现并修掉一个真 bug**：`DtSimulateLocation.__init__` 不做任何设备 I/O
   （`services/simulate_location.py:19-22`），所以「构造成功」不等于「服务可用」。
   最初版本因此出现「connect 报成功 → 第一次 set 才失败」的假成功；现在
   `_connect_lockdown_path()` 会真的 `await lockdown.start_lockdown_developer_service(...)` 校验一次
   （见 `core/location_provider.py` 的 `legacy` 分支），上面的输出就是修复后的**诚实失败**。

### 8.2 iOS 26.6.1 设备（USB、开发者模式未开）

> **⚠️ 本节是 20:47 的中间状态，已被后续进展取代（保留作为取证过程）。**
> 21:00 之后实测的最终事实：
> - 用户在手机上**开启了开发者模式**并重启，`amfi developer-mode-status` 返回 `true`；
> - 开发者镜像已挂载：`mounter list` 显示 `Personalized DeveloperDiskImage 642.16`、
>   `MountPath=/System/Developer`、`ProductBuildVersion=27A266a`
>   （由 Xcode 的 `CoreDeviceService` 用本机素材自动挂载）；
> - **虚拟定位链路已端到端跑通**：免 root 的 `remote start-tunnel --native --script-mode`
>   建立 RSD 隧道 → 打开 DVT `dtservicehub` → `LocationSimulation` 长连接 →
>   单点定位与轨迹播放均成功（`tests/real_gui_acceptance.py` 11/11、
>   `tests/real_device_e2e.py` 通过），停止后恢复真实定位；
> - 素材下载会被 **SNI 级阻断**（raw.githubusercontent.com 的 4 个 IP 只有
>   `185.199.111.133` 可用），离线抓取方案见 `tools/ddi_offline_fetch.sh`。

```console
$ ./.venv/bin/python tools/probe_device.py --udid 00008030-001551840A02402E --probe
=== 设备 1 ===
  UDID        : 00008030-001551840A02402E
  名称        : iPhone
  型号        : iPhone12,1
  iOS 版本    : 26.6.1 (major=26)
  连接方式    : usb
  配对状态    : connected
  开发者模式  : 未开启
  DVT 可用性  : 不可用 —— 未开启开发者模式：设置 → 隐私与安全性 → 开发者模式（iOS16+ 必须开启）
```

`--probe` 直接在**静态判断**阶段就拦下（开发者模式未开），不会再浪费一次设备往返。

---

## 9. CLI `--help` 实测输出（真实终端输出，逐字粘贴）

### 9.1 `pymobiledevice3 --help`（命令树）

```
 Usage: python -m pymobiledevice3 [OPTIONS] COMMAND [ARGS]...

 Swiss-army CLI for pairing, inspecting, backing up, and automating iOS devices.

╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ developer        Developer tooling for iOS devices (requires Developer Mode  │
│                  + mounted DeveloperDiskImage).                              │
│ lockdown         Pair/Unpair device or access other lockdown services        │
│ remote           Create and browse RemoteXPC tunnels (RSD/tunneld) for       │
│                  developer services.                                         │
│ usbmux           Inspect usbmuxd-connected devices and forward TCP ports to  │
│                  them.                                                       │
│ mounter          Mount/Umount DeveloperDiskImage or query related info       │
│ version          Query pymobiledevice3 version                               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 9.2 `developer dvt simulate-location --help`（iOS 17+，DVT 路径）

```console
$ ./.venv/bin/python -m pymobiledevice3 developer dvt simulate-location --help
 Usage: python -m pymobiledevice3 developer dvt simulate-location [OPTIONS] COMMAND [ARGS]...

 Simulate device location through DVT on iOS 17 and newer.

╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ clear  Clear the currently simulated location on iOS 17 and newer.           │
│ set    Set a simulated location through DVT on iOS 17 and newer.             │
│ play   Replay a GPX route through DVT on iOS 17 and newer.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 9.3 `developer simulate-location --help`（iOS<17，老服务路径）

```console
$ ./.venv/bin/python -m pymobiledevice3 developer simulate-location --help
 Usage: python -m pymobiledevice3 developer simulate-location [OPTIONS] COMMAND [ARGS]...

 Simulate GPS location on iOS versions below 17 (set, clear, or replay GPX routes).

╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ clear  Stop location simulation and resume real GPS on iOS versions below 17.│
│ set    Set a fixed simulated location on iOS versions below 17 (latitude,    │
│        longitude).                                                           │
│        Example: `set 40.690008 -74.045843` (Liberty Island).                 │
│ play   Replay a GPX route on iOS versions below 17; optionally disable       │
│        sleeps and add timing jitter.                                         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

> 两条命令的 help 文本（"iOS 17 and newer" / "iOS versions below 17"）与源码里的
> `cli/developer/dvt/simulate_location.py:13` 与 `cli/developer/simulate_location.py:16` 完全对应，
> 这也从 CLI 侧证明了「iOS<17 与 iOS≥17 是两条不同的服务」。

### 9.4 `remote start-tunnel --help`（注意 `--native` = 免 root）

```console
$ ./.venv/bin/python -m pymobiledevice3 remote start-tunnel --help
 Usage: python -m pymobiledevice3 remote start-tunnel [OPTIONS]

 start tunnel (classic root tunnel by default; --native piggybacks Apple's
 tunnel on macOS, no root)

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --script-mode  --no-script-mode   Print only HOST and port for scripts       │
│                                   instead of formatted output.               │
│                                   [default: no-script-mode]                  │
│ --protocol     -p  <tcp|quic>     Transport protocol for the tunnel.         │
│                                   [default: (TCP on Python >=3.13,          │
│                                   otherwise QUIC)]                           │
│ --native       --no-native        Piggyback Apple's remoted tunnel via       │
│                                   remotepairingd and publish its RSD         │
│                                   address instead of creating a new tunnel;  │
│                                   no root, macOS only. Its RSD connection    │
│                                   and remoted's evict each other, so it is   │
│                                   opt-in …                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 9.5 `remote tunneld --help`（默认 QUIC，且需要 root）

```console
$ ./.venv/bin/python -m pymobiledevice3 remote tunneld --help
 Usage: python -m pymobiledevice3 remote tunneld [OPTIONS]

 Start Tunneld service for remote tunneling

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --host                     <str>       Address to bind the tunneld server to.│
│                                        [default: 127.0.0.1]                  │
│ --port                     <int>       Port to bind the tunneld server to.   │
│                                        [default: 49151]                      │
│ --protocol     -p  <tcp|quic>          Transport protocol for tunneld        │
│                                        (default: TCP on Python >=3.13,       │
│                                        otherwise QUIC).                      │
│                                        [default: quic]        ← 3.12 下默认  │
│ --usb/--no-usb, --wifi/--no-wifi, --usbmux/--no-usbmux, --mobdev2/…          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 9.6 `lockdown start-tunnel --help`

```console
$ ./.venv/bin/python -m pymobiledevice3 lockdown start-tunnel --help
 Usage: python -m pymobiledevice3 lockdown start-tunnel [OPTIONS]

 start tunnel

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --script-mode  --no-script-mode  Show only HOST and port number to allow     │
│                                  easy parsing from external shell scripts    │
│ --help  -h                       Show this message and exit.                 │
╰──────────────────────────────────────────────────────────────────────────────╯
```

（它只有 `--script-mode`——因为 RSD 主机/端口由底层内核隧道直接决定；实现见
`cli/lockdown.py:193-222`，内部以 `TunnelProtocol.TCP` 调 `start_tunnel_task`。）

### 9.7 `remote browse --help`

```console
$ ./.venv/bin/python -m pymobiledevice3 remote browse --help
 Usage: python -m pymobiledevice3 remote browse [OPTIONS]

 browse RemoteXPC devices (remotepairingd on macOS by default, bonjour elsewhere)

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --timeout   <float>          Browse timeout (in seconds)  [default: 3]       │
│ --native    --no-native      Browse via Apple's remotepairingd (the remoted  │
│                              tunnel daemon) instead of bonjour; no root,     │
│                              macOS only …                                    │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### 9.8 无 root 实测：三条命令一律拒绝

```console
$ ./.venv/bin/python -m pymobiledevice3 remote tunneld
2026-10-14 20:33:29 … __main__[83672] ERROR This command requires root privileges. Consider retrying with "sudo".
[exit 1]

$ ./.venv/bin/python -m pymobiledevice3 remote start-tunnel
2026-10-14 20:33:29 … __main__[83689] ERROR This command requires root privileges. Consider retrying with "sudo".
[exit 1]

$ ./.venv/bin/python -m pymobiledevice3 lockdown start-tunnel
2026-10-14 20:33:33 … __main__[83717] ERROR This command requires root privileges. Consider retrying with "sudo".
[exit 1]
```

→ 与 5.3 的结论互相印证：**免 root 的跨进程隧道只有 `remote start-tunnel --native --script-mode`**
（本机 `doctor` 也确认 `Tunnel (native) remotepairingd answered, 1 device(s)`）。


---

## 10. 交付物清单与给 lead 的接线说明

### 10.1 我交付的文件（写作用域内，未碰任何别人的文件）

| 文件 | 内容 |
| --- | --- |
| `core/location_provider.py` | `RealLocationProvider`（`dvt-rsd` / `dvt-lockdown` / `legacy` 三策略，长连接，中文异常，`probe()`） |
| `core/location_mock.py` | `MockLocationProvider`（`--mock` 模式，接口与真机同构，5~10ms 模拟往返） |
| `tools/probe_device.py` | 列出设备 + iOS 版本 + 开发者模式 + DVT 可用性；`--probe --rsd HOST PORT` 实测建会话 |
| `tools/probe_location.py` | 命令行单点定位：`--lat --lon [--clear] [--rsd HOST PORT] [--duration N]` |
| `tools/selftest_location.py` | 离线自测（无设备，36 项断言，含与 `core/server.py` 的集成断言） |
| `docs/DEVICE_NOTES.md` | 本文件（全部取证） |

### 10.2 lead 侧的接线方式

```python
from core.location_provider import RealLocationProvider, LocationProviderError
from core.location_mock import MockLocationProvider

# 真机（RSD 地址来自 core/tunnel.py）
provider = RealLocationProvider(rsd_host=host, rsd_port=port, udid=device["udid"], on_log=push_log)
info = await provider.connect(device)        # {"ok":..., "strategy":"dvt-rsd", "detail": 中文}
await provider.set_location(lat, lon)        # 注意签名顺序是 (lat, lon)
await provider.clear_location()              # play.stop / location.clear 必须调用
await provider.close()

# mock 模式
provider = MockLocationProvider(on_log=push_log)
```

- `connect()` 返回的 dict 里 `tunnel` 字段可直接映射到 `Device.tunnel`；
  `info["detail"]` 适合直接作为 `/api/devices/connect` 的提示文案。
- 失败一律是 `LocationProviderError`，`str(e)` 就是给用户看的中文（可直接转成
  `{"ok":false,"error":str(e)}`）。`probe()` 则永不抛异常。
- iOS 17+ 且没有隧道时抛：「设备为 iOS X，需要先建立 RSD 隧道才能虚拟定位。…sudo ./run.sh…」，
  可直接用于 PROTOCOL.md 里的 `tunnel_hint`。
- **隧道首选**：`./.venv/bin/python -m pymobiledevice3 remote start-tunnel --native --script-mode`
  （macOS 免 root，输出 `HOST PORT`；本机 `doctor` 已验证 remotepairingd 可用）。
  有管理员权限时用 `sudo … remote start-tunnel --script-mode` 更稳（不与系统 remoted 抢连接）。
- 可选扩展（不破坏冻结签名）：`RealLocationProvider(…, rsd=<已连接的 RemoteServiceDiscoveryService>)`
  用于把**同进程**的免 root userspace 隧道（`UserspaceRsdTunnel`）直接注入；
  注意它的地址跨进程不可达，不能靠子进程打印 HOST/PORT 传递。
