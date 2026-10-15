# 虚拟定位轨迹工具 — 前后端接口契约（v1，冻结）

所有实现必须严格遵守本文件。**任何一方需要改动契约，必须先改本文件并通知对方。**

## 1. 进程与端口

- 单进程：Python HTTP/WebSocket 服务监听 `127.0.0.1:8787`（可用 `--port` 覆盖）。
- 静态前端由服务端从 `web/` 目录提供，入口 `GET /` → `web/index.html`。
- 无外部网络依赖（除地图瓦片与可选路线规划 OSRM）。

## 2. REST API

所有响应 `Content-Type: application/json; charset=utf-8`。失败统一返回：
`{"ok": false, "error": "<中文可读信息>", "code": "<machine_code>"}`，HTTP 状态码 400/404/409/500。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | `{"ok":true,"version":"1.0.0","mock":false,"device_count":1}` |
| GET | `/api/devices` | 设备列表（见 2.1） |
| POST | `/api/devices/select` | body `{"udid":"..."}` → `{"ok":true,"device":<Device>}` |
| POST | `/api/devices/connect` | 建立隧道/DVT 会话 → `{"ok":true,"device":<Device>,"tunnel":{"state":"connected","ios_major":26}}` |
| POST | `/api/devices/disconnect` | 释放会话 → `{"ok":true}` |
| GET | `/api/config` | `{"ok":true,"config":{...}}` |
| POST | `/api/config` | 局部更新，body 为部分字段 → `{"ok":true,"config":{...}}` |
| GET | `/api/settings/tiles` | 瓦片源列表 + 当前选择 |
| GET | `/api/tiles/{source}/{z}/{x}/{y}.png` | 瓦片代理 + 本地磁盘缓存；命中/回源均返回图片字节；全部源失败 → 404 + JSON 错误 |
| POST | `/api/route/build` | body `{"waypoints":[[lng,lat],...],"mode":"driving"}` → `{"ok":true,"route":<Route>,"engine":"osrm"\|"straight"}` |
| POST | `/api/gpx/export` | body `{"route":<Route>,"name":"..."}` → 返回 GPX 文本（`application/gpx+xml`） |
| POST | `/api/gpx/import` | body 为 GPX 文本（`text/plain` 或 multipart）→ `{"ok":true,"waypoints":[[lng,lat],...],"path":[...],"points":[...],"name":"...","count":N}`。三个数组内容相同：**全量坐标、不抽稀**，前端用 `path` 保形重放 |
| POST | `/api/tunnel` | body `{}` 建隧道（可选 `{"sudo":true}` 走管理员）；或 `{"host":"...","port":1234}` 手动指定 → `{"ok":true,"tunnel":{...}}` |

### 2.1 Device 对象

```json
{
  "udid": "00008110-000A1B2C3D4E801E",
  "name": "iPhone 15 Pro",
  "product_type": "iPhone16,1",
  "ios_version": "26.0",
  "ios_major": 26,
  "connection": "usb",
  "state": "connected",
  "tunnel": "idle",
  "simulating": false,
  "developer_mode": true,
  "tunnel_hint": null,
  "last_error": null
}
```

- `state`: `connected` | `unpaired` | `unavailable`
- `tunnel`: `idle` | `starting` | `connected` | `error` | `unsupported`
- `tunnel_hint`: iOS≥17 需要隧道而失败时的中文修复建议（例如「需要管理员权限建立隧道：sudo ./run.sh」），否则 `null`。
- `developer_mode`: iOS≥16 未开启开发者模式时为 `false`，前端需显著提示。

### 2.2 Route 对象

```json
{
  "id": "r-1712345678901",
  "name": "未命名轨迹",
  "mode": "driving",
  "waypoints": [[116.397, 39.908], [116.401, 39.912]],
  "path": [[116.397, 39.908], [116.3971, 39.9082]],
  "speeds": [8.3, 8.5],
  "distance": 1234.5,
  "duration": 148.6,
  "engine": "osrm",
  "loop": false,
  "speed_scale": 1.0
}
```

- `waypoints` 用户途经点（含起点/终点），`path` 是实际驱动用的密集坐标序列（`[lng,lat]`，WGS84）。
- `speeds`：与 `path` 等长的瞬时速度 m/s（仅用于展示与导出）；`speed_scale` 是播放倍速。
- `distance` 米；`duration` 秒（= 按 `speeds` 积分的结果）。

### 2.3 配置文件 `config.json`（与 `main.py` 同目录）

```json
{
  "tile_source": "amap",
  "tile_sources": {
    "amap":    {"name": "高德矢量", "url": "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}", "subdomains": ["1","2","3","4"], "max_zoom": 18, "attribution": "© 高德地图"},
    "amap_sat":{"name": "高德卫星", "url": "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}", "subdomains": ["1","2","3","4"], "max_zoom": 18, "attribution": "© 高德地图"},
    "tencent": {"name": "腾讯地图", "url": "https://rt{s}.map.gtimg.com/tile?z={z}&x={x}&y={y}&type=vector&styleid=3", "subdomains": ["0","1","2","3"], "max_zoom": 18, "attribution": "© 腾讯地图"},
    "arcgis_sat": {"name": "全球卫星", "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", "subdomains": [], "max_zoom": 19, "attribution": "Esri"}
  },
  "tile_cache_dir": "~/.ios-location-tiles",
  "tile_cache_max_mb": 1024,
  "send_hz": 5,
  "default_speed_scale": 1.0,
  "mode_defaults": {"driving": 13.9, "cycling": 5.5, "walking": 1.4, "flight": 60.0},
  "osrm_url": "https://router.project-osrm.org/route/v1"
}
```

**坐标系统一为 WGS84**。前端若使用高德瓦片，仅瓦片源不同（高德瓦片为 GCJ-02），显示用点必须做 WGS84→GCJ-02 转换后再落到瓦片上，反向转换用于取点。转换函数放在 `web/js/coord.js`，导出 `wgs84ToGcj02` / `gcj02ToWgs84`。**后端永远只接收/返回 WGS84。**

## 3. WebSocket API

- 地址：`ws://127.0.0.1:8787/ws`。UTF-8 文本帧，每条消息一个 JSON 对象。
- 客户端 → 服务端：`{"type": "<cmd>", "id": "<可选请求id>", ...}`
- 服务端 → 客户端：`{"type": "<event>", "id": "<回填请求id>", ...}`，`id` 存在时表示对某条命令的应答。

### 3.1 客户端命令

| type | 字段 | 说明 |
| --- | --- | --- |
| `hello` | `{"client":"web"}` | 连接后立即发送，服务端回 `welcome` |
| `ping` | `{}` | 回 `pong`，用于心跳（客户端每 10s 发一次） |
| `devices.refresh` | `{}` | 回 `devices` |
| `device.select` | `{"udid":""}` | 回 `device` + `tunnel` |
| `device.connect` | `{}` | 回 `device`/`tunnel`，可能先推多条 `log` |
| `device.disconnect` | `{}` | 回 `device`/`tunnel` |
| `location.set` | `{"lng":116.397,"lat":39.908}` | 单点定位，回 `ack`，随后推 `location` |
| `location.clear` | `{}` | 停止模拟、恢复真实定位，回 `ack` |
| `play.start` | `{"route":<Route>,"speed_scale":1.0,"loop":false,"start_index":0}` | 开始沿轨迹播放。**服务端以收到的 route 为准**（前端可只发 `path`/`speeds`）。回 `ack`；随后持续推 `status`/`location` |
| `play.pause` | `{}` | 回 `ack` |
| `play.resume` | `{}` | 回 `ack` |
| `play.stop` | `{}` | 停止播放并停止模拟（恢复真实定位），回 `ack` |
| `play.seek` | `{"progress":0.42}` | 0..1，回 `ack` 并推 `location` |
| `play.speed` | `{"speed_scale":2.0}` | 播放倍速 0.1..50，回 `ack` |
| `log.list` | `{"limit":200}` | 回 `logs` |
| `route.build` | `{"waypoints":[[lng,lat],...],"mode":"driving","name":"...","use_osrm":true,"loop":false,"speed_scale":1.0}` | 回 `ack{route,engine}`；便于前端在 WS 上一次完成规划 |
| `tunnel.ensure` | `{"sudo":false}` | 建立/复用隧道 → `ack{tunnel,need_tunnel}`。`need_tunnel:false` 表示该场景不需要隧道（mock 模式，或 iOS<17 走 usbmux 经典通道），前端应立即结束等待 |
| `tunnel.manual` | `{"host":"fd00::1","port":51234}` | 使用手动指定的 RSD → `ack{tunnel}` |
| `tiles.clear` | `{}` | 清空瓦片磁盘缓存 → `ack{removed:N}` |
| `config.update` | `{"config":{"send_hz":5,"tile_source":"amap"}}` | 局部更新配置 → `ack{config}` |

### 3.2 服务端事件

| type | 字段 | 说明 |
| --- | --- | --- |
| `welcome` | `{"version":"1.0.0","mock":false,"config":{...}}` | 连接就绪，附带当前配置与设备快照 |
| `pong` | `{"ts":1712345678.1}` | 心跳应答 |
| `ack` | `{"id":"<回填>","cmd":"play.start"}` 或 `{"id":"...","ok":false,"error":"..."}` | 命令应答 |
| `devices` | `{"devices":[<Device>...],"selected":"<udid\|null>"}` | 设备列表变化 |
| `device` | `{"device":<Device>\|null}` | 单设备状态变化 |
| `tunnel` | `{"state":"connected","ios_major":26,"message":"隧道已建立（RSD）","rsd":"fd00::1 端口 51234"}` | 隧道状态变化 |
| `location` | `{"lng":116.397,"lat":39.908,"source":"play"\|"manual","progress":0.42,"index":1234,"speed":8.31,"sent_at":1712345678.123}` | 每次实际下发定位后推送 |
| `status` | `{"playing":true,"paused":false,"progress":0.42,"index":1234,"total":3000,"elapsed":61.2,"remaining":88.0,"speed_scale":1.0,"loop":false,"distance_done":512.3,"lng":116.39,"lat":39.9,"seq":7}` | 播放状态，播放中约 2Hz 推送，状态变化时立即推送。`seq` 为自增序号，用于判断新旧状态 |
| `logs` | `{"lines":[{"ts":1712345678.1,"level":"info","msg":"..."}]}` | 日志回填 |
| `log` | `{"ts":...,"level":"info"\|"warn"\|"error"\|"success","msg":"..."}` | 实时日志 |
| `error` | `{"error":"中文信息","code":"..."}` | 服务端异常 |

### 3.3 语义约定

- `play.start` 时若尚未连接设备：立即回 `ack ok:false, error:"请先连接设备"`（mock 模式除外）。
- `play.stop` 与 `location.clear` 必须调用 DVT `clear_location`，让手机恢复真实定位，并额外推一条
  `{"type":"location","source":"cleared","lng":null,"lat":null}`，前端应让地图上的定位点消失。
- 播放循环 `loop=true` 时到终点自动回到起点，`status.loop` 保持 `true`。
- 服务端播放引擎按 `send_hz`（默认 5Hz）下发；`play.speed` 只影响轨迹推进速率，不影响下发频率。
- 逐次下发的日志按 5 秒聚合成一条「正在按轨迹下发定位（已下发 N 次）」，不会 5Hz 刷屏。
- 所有 `lng/lat` 单位度，WGS84，保留 6 位小数。
- `hello` 的应答是 `ack`（`welcome` 的数据并进 ack，**没有独立的 `type:"welcome"` 消息**）：
  `{type:"ack", id, cmd:"hello", version, name, mock, config, devices, selected, tunnel, backend, pymobiledevice3}`。
- WebSocket 子协议：只有客户端在 `Sec-WebSocket-Protocol` 里请求了值时才回该头（RFC 6455 要求），
  否则浏览器会判定握手失败。

## 4. 无设备模拟模式（验收必备）

启动参数 `--mock` 或环境变量 `IOSLOC_MOCK=1` 时：

- `/api/devices` 返回一台假设备：`udid="MOCK-0001"`, `name="模拟设备 (iPhone 17 Pro)"`, `ios_version="26.0"`, `ios_major=26`, `state="connected"`, `tunnel="connected"`。
- 定位下发不报错，仅记录日志并照常推送 `location` 事件。
- 其余行为（播放引擎、进度、GPX、瓦片缓存）与真机完全一致，用于端到端验证。

## 5. 模块与文件所有权（写作用域）

| 文件 | 负责人 |
| --- | --- |
| `core/protocol.py` | lead |
| `core/ws.py` | lead |
| `core/server.py` | lead |
| `core/route_engine.py` | lead |
| `core/tilecache.py` | lead |
| `core/device.py` | lead |
| `core/tunnel.py` | lead |
| `core/location_provider.py` | device-teammate |
| `core/location_mock.py` | device-teammate |
| `core/playback.py` | lead |
| `core/config_store.py` | lead |
| `main.py` | lead |
| `web/index.html`, `web/style.css` | web-teammate |
| `web/js/*.js` | web-teammate |
| `tools/*.py` | device-teammate |
| `scripts/*`, `README.md` | lead |

任何人不得直接改别人拥有的文件；需要改动就发消息商量。
