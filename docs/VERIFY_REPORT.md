# 独立验证报告（task-4 · 对抗性审查）

- 验证人：**verifier**（独立于 lead / web-ui-dev / device-dev，只读项目文件）
- 验证时间：本机 macOS，Python 3.12.14（`.venv`），pymobiledevice3 11.29.1，Chrome headless
- 验证方式：**全部结论都由本人独立复跑/独立实现客户端得到**，未采信任何既有测试结论
- 我方实例端口：**8877**（8787 为 lead 实例，全程未触碰）；所有临时脚本在 `/tmp`，验证结束后已关闭全部自建进程（`lsof` 确认 8877 无监听）
- 真实设备：全程**只做代码级/只读枚举核对**，未设置定位、未改设备状态、未建隧道

---

## 0. 结论

**状态：有阻塞问题（不可直接交付）。**

功能主干是**真实可用的**：图形界面上手绘轨迹、加载路线、播放/暂停/继续/停止/进度、GPX 导入导出、真机 DVT 虚拟定位链路在本机跑通；82/82 自检与 19/19 图形界面验收我已独立复现通过。自绘地图引擎、坐标转换方向、静态文件穿越防护、WebSocket 分帧/并发广播等关键面均**未发现缺陷**。

但有 5 个必须修复才能交付的缺陷（D1–D5），其中 D2/D5 只需**一次 HTTP 请求**即可让地图永久空白或把手机定位钉住，D3 会在真实设备上让定位在两条轨迹间来回跳，D4 会让 iOS 17+ 慢隧道下「连接设备」永远报超时。

| 编号 | 严重度 | 一句话 | 位置 |
| --- | --- | --- | --- |
| D1 | 高 | `play.seek` 传 NaN → 之后所有推送都是**非法 JSON**（`NaN` 字面量），浏览器 `JSON.parse` 抛错，引擎进度被污染成 NaN | `core/playback.py:154-173` + `core/protocol.py:71-84` |
| D2 | 高 | `POST /api/config` 无类型校验 → 一次 `{"tile_sources":"x"}` 之后**所有瓦片请求永久 500**（地图空白），`{"send_hz":NaN}` 让 welcome 变非法 JSON | `core/config_store.py:120-142`、`core/server.py:699-703` |
| D3 | 高 | 播放线程僵尸化：`stop` 的 join 超时后旧线程仍活着，下一次 `start` 清掉 stop 标志 → **两个线程同时驱动设备** | `core/playback.py:144-152, 75-106` |
| D4 | 高 | 前端心跳只认 `pong`，不认任何其它入站消息 → 慢隧道（README 自己写「最长约 1 分钟」）下 40s 强制断线，`device.connect` 结果丢失、90s 报超时 | `web/js/ws.js:163-167, 246-262`、`web/js/app.js:845` |
| D5 | 高 | HTTP/WS 无 Origin/Host 校验、无 token → **任意网页**可改配置、单点定位、甚至关机 | `core/server.py:678-769` |

另有 6 个中危、7 个低危缺陷，见第 2 节。

---

## 1. 独立复现的官方测试（先证明"绿"）

| 命令 | 结果 | 备注 |
| --- | --- | --- |
| `./.venv/bin/python tests/selftest.py` | **通过 82 项，失败 0 项，用时 9.7s** | 自检自身用 8899 端口，未与 8787 冲突 |
| `./.venv/bin/python main.py --mock --no-browser --port 8877` | `{"ok":true,...,"mock":true,"device_count":1,...}` | 我自己起的实例 |
| `./.venv/bin/python tests/ui_acceptance.py --url http://127.0.0.1:8877/ --out /tmp/ui-verify` | **通过 19 项，失败 0 项**（真实 headless Chrome，截图 `/tmp/ui-verify`） | 页面无 JS 报错 |
| `./.venv/bin/python tools/selftest_location.py` | **通过 36 项，失败 0 项** | 定位层离线自检 |
| `./.venv/bin/python tools/screenshot.py http://127.0.0.1:8877/ /tmp/verify-shot.png --wait 8` | 退出码 0，`/tmp/verify-shot.png` 1920 KB | 但见 D18-g（成功时也打印"Chrome 超时被终止"） |

图形界面人工核验（截图 2880×1800）：顶部设备卡 + 深色专业风 + 自绘 canvas 高德底图（北京中文标注）+ 左侧轨迹设置 + 绘制工具条 + 底部播放条 + 右侧日志面板，**界面确实存在且可用**，满足"图形化界面"要求。

---

## 2. 缺陷清单（按严重程度，全部含复现命令与原始输出）

> 全部复现均在自建 8877 实例上完成；`curl` 示例可直接复制（把 8877 换成你自己的端口）。

### D1（高）`play.seek(progress=NaN)` → 非法 JSON 推流 + 引擎进度 NaN 化

- 位置：`core/playback.py:154-173`（`seek`）、`core/protocol.py:71-76`（`clamp` 不处理 NaN）、`core/protocol.py:83-84`（`json.dumps` 默认 `allow_nan=True`）
- 复现（自写 RFC6455 客户端，`/tmp/verify_adv_ws2.py` B10）：

```
w.send('{"type":"play.seek","progress":NaN,"id":"nan1"}')     # python json 接受 NaN 字面量
→ ack: {"type":"ack","id":"nan1","cmd":"play.seek","status":{"playing":true,"progress":nan,...}}   ← 没有报错
→ 之后 3 秒内 33 帧里有 18 帧含 NaN 字面量：
  {"type":"location","lng":NaN,"lat":NaN,"source":"play","progress":NaN,"index":0,"speed":13.9,"sent_at":1791982831.97}
```

- 期望：`progress` 非有限数应回 `400 {"ok":false,"error":"进度必须是 0~1 的数字"}`；且任何出站 JSON 都不允许出现 `NaN`/`Infinity`。
- 影响：`clamp(NaN,0,1)` 返回 NaN（`NaN<0`、`NaN>1` 都是 False），于是 `self.distance = total*NaN = NaN`，此后 `interpolate()` 产出 NaN 坐标、`status.progress=NaN`。浏览器端 `ws.js:131-141` 的 `JSON.parse` 对 `NaN` 字面量**直接抛 SyntaxError**，每帧丢弃并报「服务端消息不是合法 JSON」；`status` 帧同样失效，界面进度/坐标停摆。
  另：`POST /api/config {"send_hz":NaN}` 走同一条链（见 D2），说明该问题在**序列化边界**上普遍存在。
- 最小修复：`protocol.clamp` 前加 `if not math.isfinite(value): raise AppError(...)`（或 `seek` 里显式 `math.isfinite` 校验）；`protocol.dumps` 改 `json.dumps(..., allow_nan=False)` 作为兜底。
- 附带证据：真实设备路径上 `RealLocationProvider.set_location` 会因 `not (-90<=NaN<=90)` 为真而抛中文错误（`core/location_provider.py:520-521`），即真机不会下发 NaN，但会**静默把播放停掉**；mock 路径则把 NaN 原样记日志并推送。

### D2（高）`POST /api/config` 无类型/范围校验 → 瓦片接口永久 500、welcome 变非法 JSON

- 位置：`core/config_store.py:120-142`（`update()` 只校验 key 白名单，不校验 value 类型）、`core/server.py:699-703`
- 复现与原始输出：

```
$ curl -s -X POST --data '{"tile_sources":"x"}' -H "Content-Type: application/json" http://127.0.0.1:8877/api/config
{"ok":true,"config":{"tile_source":"amap",...,"tile_sources":"x",...}}          ← 接受了

$ curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8877/api/tiles/amap/1/0/0.png
HTTP 500
$ curl -s http://127.0.0.1:8877/api/tiles/amap/1/0/0.png
{"ok":false,"error":"服务端错误：string indices must be integers, not 'str'","code":"internal"}   ← 地图此后永久空白

$ curl -s -X POST --data '{"send_hz":NaN}' -H "Content-Type: application/json" http://127.0.0.1:8877/api/config
{"ok":true,...}          ← 接受
$ curl -s http://127.0.0.1:8877/api/config | head -c 120
{"ok":true,"config":{...,"send_hz":NaN,...}}                                      ← 非法 JSON 已进入运行态
$ # 新客户端握手时收到的 welcome 里同样带 NaN：
welcome 帧片段: ..."tile_cache_max_mb":64,"send_hz":NaN,"default_speed_scale":1.0,...
含 NaN 字面量: True   → 浏览器 JSON.parse 抛异常，welcome（含 config/设备快照）整帧丢失
```

- 期望：`/api/config` 局部更新必须做**类型 + 范围**校验（`tile_sources` 必须是对象、`send_hz` 必须是 1–20 的有限数、`tile_cache_max_mb` 必须是非负整数…），非法值回 `400 + 中文`，绝不允许写入运行态。
- 备注：`_merge()`（`config_store.py:92-101`）只在 value 是 dict 时做合并，字符串会**整体替换** `tile_sources`，所以 `tile_source()`（:147-152）随后 `"amap" not in "x"` → `"x"["amap"]` → TypeError。
- 交互影响：D5 说明这个接口可被任意网页调用，因此 D2 是可被外部触发的持久 DoS。

### D3（高）播放线程僵尸化 → 两个 worker 同时驱动设备

- 位置：`core/playback.py:144-152`（`stop_locked` 里 `self._thread = None` 后 `join(timeout=2.5)`，超时即放弃但不作废线程）、`core/playback.py:75-106`（`start()` 里 `self._stop_flag.clear()` 后新建线程）
- 复现：`/tmp/verify_playback_race.py`（用真实 `core/playback.py` + 模拟"真机下发耗时 3s"的 provider，runner 语义与 `core/server.py:30-64 AsyncRunner` 一致）

```
路线1 1403 点 2803 m（北京）；路线2 1464 点 2926 m（上海）
[0.30s] 播放线程 A = playback, active=True
[0.30s] 第二次 start（模拟用户再次点播放 / 前端重发 play.start）…
[2.81s] 新播放线程 = playback
[2.81s] 当前存活的 playback 线程：['playback', 'playback']        ← 两个线程同时存在

下发时存活的 playback 线程数 -> 下发记录：
  存活 1 个 playback 线程: 1 次 -> [(3.0, 39.908, 116.397)]
  存活 2 个 playback 线程: 4 次 -> [(5.81, 31.23, 121.47), (6.06, 39.923156, 116.412156),
                                    (8.87, 31.25, 121.49), (9.11, 39.928, 116.417)]
```

- 期望：`stop` 必须保证旧线程**真的结束**（或在旧线程存活期间拒绝新的 `start`）。
- 影响：真机上定位会在**北京 ↔ 上海**两条轨迹间来回跳（上表就是交替下发），且 `active` 属性、`sent_count`、进度全部错乱。触发条件现实存在：`stop` 时若有一次 `set_location` 在途且耗时 > 2.5s（RSD 抖动、设备卡顿、隧道半断），紧接着用户再点「播放」（前端命令超时后重试也会重发 `play.start`）。
- 最小修复：给 `_loop` 传一个"代次 token"（`start()` 时自增，线程每轮校验自己的代次），或在 `stop_locked` 里放大 join 超时并在 `start()` 检测 `thread.is_alive()` 时拒绝启动。

### D4（高）前端心跳只认 `pong` → 慢隧道下 `device.connect` 结果丢失

- 位置：`web/js/ws.js:163-167`（只有 `type === 'pong'` 才刷新 `lastPongAt`/清 `missedPings`）、`web/js/ws.js:246-262`（`missedPings >= 3` 就地 `sock.close()`）、`web/js/app.js:845`（`device.connect` 超时给到 90s）
- 复现：用 **node 直接跑真实的 `web/js/ws.js`**，连一个 `device.connect` 会阻塞 60s 的假服务端（模拟 iOS 17+ native 隧道建立；服务端每 2s 还持续推 `log` 帧，和真实的 `tunnel-reader` 线程行为一致）：

```
$ node /tmp/ws_client_stall_test.js          # 服务端 /tmp/stall_server.py，阻塞 60s 且持续推 log
[0.0s] open / welcome
[1.5s] -> 发送 device.connect（服务端将阻塞 60s）
[40.1s] error: 心跳超时，正在重建连接          ← 客户端单方面判定连接已死
[91.5s] device.connect 失败: code=timeout msg=命令超时（device.connect）   ← 服务端 61.5s 已经回了 ack
```

- 期望：只要**任何**入站消息到达就说明链路活着（`log`/`status`/`location` 都应刷新存活时间）；长命令执行期间不应被心跳掐断。
- 影响：README §4 自己写「iOS 17+ 会自动建隧道，**最长约 1 分钟**」。真机首次连接时服务端 WS 线程被 `tunnel.start()`（`core/tunnel.py:140-157`，最长 75s）+ DVT connect 阻塞，期间只有 tunnel reader 线程广播的 `log`；前端 40s 就强制断线重连，`device.connect` 的 ack 发给已断开的 socket，界面最终弹「命令超时」，而服务端其实已经连上（`provider_for` 已置 `simulating=True`）。用户需要**再点一次**「连接设备」才成功。
- 最小修复：`_dispatch` 里对任何消息都刷新 `lastPongAt`/`missedPings`（或至少统计"最近 N 秒内是否收到过任意帧"）；同时给 `device.connect` 这类长命令做"命令执行中暂停心跳判定"。

### D5（高）本机服务无 Origin/Host 校验、无鉴权 → 任意网页可控设备

- 位置：`core/server.py:614-769`（所有 REST，含 `POST /api/location`、`/api/config`、`/api/devices/connect`、`/api/shutdown`），无任何 Origin/Host/CSRF token 检查
- 复现（带恶意 Origin 的**简单请求** `text/plain`，浏览器跨站发这种请求**不触发预检**）：

```
$ curl -s -X POST -H "Origin: https://evil.example" -H "Content-Type: text/plain" \
       --data '{"send_hz":3}' http://127.0.0.1:8877/api/config -D - -o /dev/null | head -1
HTTP/1.1 200 OK
$ curl -s -X POST -H "Origin: https://evil.example" -H "Content-Type: text/plain" \
       --data '{"lat":39.9,"lng":116.4}' http://127.0.0.1:8877/api/location
{"ok":true}                                    ← 手机被单点钉住
$ curl -s -X POST -H "Origin: https://evil.example" -H "Content-Type: text/plain" \
       --data '{}' http://127.0.0.1:8877/api/shutdown
{"ok":true}    → 2 秒后 curl /api/health 返回 HTTP 000，进程退出
```

- 期望：拒绝带外部 `Origin` 的请求（仅允许同源/无 Origin），或给每个实例生成随机 token 并要求携带；`/api/shutdown` 至少要求 token。
- 影响：用户只要在浏览器里打开一个恶意/被挂马的页面，它就能在后台把用户手机的 GPS 钉到任意坐标（且是"虚拟定位"类工具，用户很难察觉），或直接关掉服务。风险等级与"本机 127.0.0.1 服务"并不相称。
- 附带：`Host` 头未校验，存在 DNS rebinding 面。

### D6（中）瓦片全部回源失败时返回 **HTTP 200**，与契约「404 + JSON 错误」不符，且前端失败告警路径变成死代码

- 位置：`core/server.py:779-784`（两个分支都写死 `200`）、`core/tilecache.py:167-176`（返回 `status=404` + 占位 PNG）、契约 `docs/PROTOCOL.md:26`
- 复现：

```
$ curl -s -o /dev/null -m 6 -w "直连 OSM: %{http_code}\n" https://tile.openstreetmap.org/1/0/0.png
直连 OSM: 000                                  ← 本机不可达
$ curl -s -o /tmp/osm_tile.bin -w "经服务端: HTTP %{http_code} type=%{content_type} size=%{size_download}\n" \
       http://127.0.0.1:8877/api/tiles/osm/1/0/0.png
经服务端: HTTP 200 type=image/png size=69        ← 契约要求 404
$ xxd /tmp/osm_tile.bin | head -1
00000000: 8950 4e47 0d0a 1a0a 0000 000d 4948 4452  .PNG........IHDR   ← 1x1 透明占位图
```

- 期望：失败时按契约返回 `404 + JSON`（占位图不必返回）。
- 影响：前端 `web/js/map.js:430-470` 依靠 `img.onerror` 做重试与「瓦片加载失败」提示；现在失败瓦片也走 `onload` 并标记 `state='ok'`，**重试与告警永远不会触发**，用户只看到地图空白且没有任何提示（README §6 却说"换底图/走缓存"）。

### D7（中）`play.stop` 之后再 `play.seek` 会重新下发模拟定位（设备被重新钉住，界面显示"未播放"）

- 位置：`core/playback.py:154-173`（`seek()` 不检查 `playing`，`_route` 在 `stop_locked` 后仍保留）、`core/server.py:342-344`
- 复现（`/tmp/verify_adv_ws3.py` C1，用服务端日志计数）：

```
停止后累计下发次数 N1 = 120  最后一条: [模拟模式] 下发定位 lat=39.908036, lon=116.397072（第 64 次，模拟）
停止后 seek(0.5) 之后 N2 = 121  最后一条: [模拟模式] 下发定位 lat=39.917949, lon=116.416899（第 65 次，模拟）
seek 是否重新下发定位: True
ack.status: {'playing': False, 'paused': False, 'progress': 0.5, 'distance_done': 2025.92}
```

- 期望：未播放（`playing=false`）时 `play.seek` 应回 `ack ok:false, error:"当前没有正在播放的轨迹"`（`seek` 里 `route is None` 才报错，而 route 一直保留）。
- 影响：真机上「停止 → 手机恢复真实定位」之后，任何客户端（包括误触、脚本）调 `play.seek` 都会把手机重新钉在轨迹中途，而 UI 仍然显示"未播放/未模拟"。前端把进度条置灰了（`app.js:1562`），所以 GUI 正常操作不会触发；但这是协议层的语义漏洞。

### D8（中）三处非中文/500 报错，违反契约「400 + 中文」

```
$ curl -s -o - -w " -> %{http_code}\n" "http://127.0.0.1:8877/api/logs?limit=abc"
{"ok":false,"error":"服务端错误：invalid literal for int() with base 10: 'abc'","code":"internal"} -> 500
$ curl -s -o - -w " -> %{http_code}\n" "http://127.0.0.1:8877/api/logs?limit=1e9"
{"ok":false,"error":"服务端错误：invalid literal for int() with base 10: '1e9'","code":"internal"} -> 500
$ curl -s -X POST --data '{"send_hz":"abc"}' -H "Content-Type: application/json" http://127.0.0.1:8877/api/config
{"ok":false,"error":"服务端错误：could not convert string to float: 'abc'","code":"internal"} -> 500
# WS 侧同源问题：
C2 log.list limit=abc  -> ok=False err=内部错误：ValueError: invalid literal for int() with base 10: 'abc'
```

- 位置：`core/server.py:670`（`int(limit)` 无 try）、`core/config_store.py:131-135`（`float(send_hz)` 无 try）
- 期望：非法参数 → `400 + code=bad_request/bad_limit` 的中文提示，且响应里不出现 Python 异常类名。
- 说明：这是**契约符合性**问题，不是安全漏洞（未泄漏栈，仅异常字符串）。

### D9（中）`--mock` 模式设备卡显示「隧道 空闲」，与 PROTOCOL §4（mock 设备 `tunnel="connected"`）矛盾；根因是共享对象被就地改写

- 位置：`web/js/app.js:400`（`welcome` → `applySessionInfo`）与 `:410`（hello 的 ack → 又一次 `applySessionInfo`）**同一条消息被处理两次**；`:461-465` 就地改 `S.device.tunnel`，`:466` 之后 `applyDevices()` 又把**同一个被改过的对象**装回去
- 复现（CDP 读真实 DOM + 钩 WebSocket 消息）：

```
WS 消息流水: [{"type":"log"},
 {"type":"ack","cmd":"hello","id":"c1-kcdo","tunnel":"idle","devTunnel":"connected"},
 {"type":"ack","cmd":"devices.refresh","id":"c2-kf5l","devTunnel":"connected"},
 {"type":"ack","cmd":"log.list","id":"c3-4zak"},
 {"type":"ack","cmd":"devices.refresh","id":"c4-2ql9","devTunnel":"connected"},     ← 第二次 hello 副作用
 {"type":"ack","cmd":"log.list","id":"c5-lbk2"}]
卡片隧道行: 隧道空闲            ← 服务端 /api/devices 明明返回 "tunnel":"connected"
```

- 期望：mock 模式下设备卡显示「隧道 已连接」（PROTOCOL.md:153），并且同一条消息只处理一次。
- 附带影响：每次页面加载会多发一次 `devices.refresh` + `log.list`（上表 c1/c2 与 c4/c5 成对出现）。
- 最小修复：`applySessionInfo` 加"同一条消息只处理一次"的幂等保护（例如按 `msg.id` 去重），`S.device.tunnel` 改成从服务端设备对象回读后再渲染。

### D10（中）`_kill_stale_tunnels()` 会杀掉**任何**`pymobiledevice3 … start-tunnel` 进程（含其它实例 / 用户手工建的），注释与实现不符

- 位置：`core/tunnel.py:172-196`（注释写"只清理本工具启动的同类进程"）、`core/tunnel.py:323-347`（按 `ps` 命令行匹配 `pymobiledevice3` + `remote start-tunnel`/`lockdown start-tunnel` 即 `SIGTERM`/`SIGKILL`）
- 复现（纯读代码即可判定；进程匹配逻辑不含 PID 文件、不含本工具标识）：

```
$ ./.venv/bin/python - <<'PY'
import sys; sys.path.insert(0,'.')
from core import tunnel
print(tunnel._find_stale_tunnel_pids())
PY
# 若此刻有任何人（其它实例、用户手动）跑着 pymobiledevice3 remote start-tunnel，PID 就会出现在这里 → 被 kill
```

- 期望：要么只清理自己记录在案的子进程（PID 文件/父子关系），要么在界面上明确提示"将清理所有第三方隧道进程"并二次确认。
- 影响：双开本工具、或用 `tools/probe_location.py --rsd` 手工建过隧道时，新实例会把别人的隧道掐断（iOS 17+ 只有一条 RSD 连接，表现为"隧道建好了定位立刻失效"，README §6 已把它列为已知现象，但没说是**本工具自己**干的）。

### D11（中）手写 WebSocket 的 3 处协议符合性问题

| 项 | 证据 | 期望（RFC 6455） |
| --- | --- | --- |
| 接受**未掩码**的客户端帧 | `B4 未掩码帧 -> {'type': 'ack', 'id': 'u1', 'cmd': 'ping', ...}` | §5.1 服务端必须 `1002` 关闭 |
| `Sec-WebSocket-Version: 8` 仍返回 101 | `B3 Sec-WebSocket-Version: 8 -> HTTP/1.1 101 Switching Protocols` | §4.4 回 `426 Upgrade Required` |
| 分片消息总量无上限 | 单帧限 8MB（实测 `B6 声明 9MB 的帧 -> {'__close__': True, 'code': 1009}` ✅），但 `B5c` 用 12 个 1MB 分片拼出 12MB 消息被完整接收处理 | 建议对累计分片长度也设上限 |

- 位置：`core/ws.py:130-162`（不校验 `masked`）、`core/ws.py:225-260`（`handshake` 不校验 Version）、`core/ws.py:185-200`（`_frag_data` 只受单帧限制）
- 影响：仅本机监听，属健壮性/合规问题；正常浏览器不受影响。

### D12（低）`/api/gpx/import` 不支持 multipart，契约却写了

```
$ # multipart/form-data 上传同一个 GPX
multipart GPX 导入 -> 400 {"ok":false,"error":"GPX 文件解析失败，请确认文件格式","code":"bad_gpx"}
text/plain        -> 200 {"ok":true,...,"count":2}
application/gpx+xml -> 200 {"ok":true,...,"count":2}
```

- 位置：`core/server.py:727-741`（直接 `raw.decode()` 后 `ET.fromstring`）、契约 `docs/PROTOCOL.md:29`
- 建议：要么按契约解析 multipart（提取 part body），要么把契约改成"仅 text/plain 与 application/gpx+xml"。

### D13（低）`DeviceManager.refresh()` 同步阻塞，且被 WS 命令线程与 3 秒监控线程调用

- 位置：`core/device.py:132-177` + `:307`（`asyncio.run(_collect())`，每个设备一次 `create_using_usbmux(..., autopair=True, pair_timeout=12.0)`）、`core/server.py:242-244`（**每次 hello 都 refresh**）、`core/device.py:123-129`（监控线程每 3s refresh）
- 实测（只读枚举，未建 lockdown 连接、未配对、未碰定位）：

```
第1次枚举: 139.3 ms, 设备数=1
第2次枚举: 16.3 ms, 设备数=1
第3次枚举: 17.5 ms, 设备数=1
   serial=00008030-001551840A02402E connection_type=USB is_usb=True
```

- 判断：健康设备下可接受（几十~几百毫秒）；但**未配对/被锁设备**上 `pair_timeout=12s` 会逐台阻塞，而 `hello` 的前端超时只有 8s（`app.js` 传入 `req(...)` 默认 8000ms），监控线程也会被拖慢。建议把设备发现移到后台线程/AsyncRunner，`hello` 只回缓存快照。

### D14（低）`sanitize_route_payload` 没有点数上限

```
B14 20 万点 play.start -> 3.07s ok=None
B15 path 100 万个点 -> ConnectionResetError（单帧 > 8MB，服务端按 1009 断开）
```

- 位置：`core/route_engine.py:511-544`（`parse_waypoints` 直接吃下整个数组，只在 `RouteEngine._densify` 里有 `MAX_PATH_POINTS=60000`）
- 建议：在 `sanitize_route_payload` 里对 `len(path)` 设上限（例如 20 万）并回中文错误，避免一次请求吃掉数百 MB。

### D15（低）`speeds` 里的 NaN 被静默接受

```
B15 speeds 含 NaN  -> ok=None（成功）
```

- 位置：`core/route_engine.py:533-540`：`max(0.05, min(400.0, float('nan')))` → Python 的 `min(400.0, nan)` 返回 `400.0`，NaN 被静默当成 400 m/s。
- 建议：`math.isfinite` 校验，非法则走 `compute_speeds`。

### D16（低）瓦片缓存 `_inflight` 只增不减

- 位置：`core/tilecache.py:57, 120-126`（每个瓦片 key 一个 `threading.Lock`，永不清理）。长时间浏览大量瓦片会缓慢增长内存。建议用 `WeakValueDictionary` 或定期清理。

### D17（低）多实例共享同一份 `config.json`（后写覆盖）

- 证据：lead 的 8787 实例与我 8877 实例读写同一个 `/Users/jbjf/Desktop/location/config.json`；我在 8877 上改配置后文件被改写（20:58、21:08 两次 mtime 变化）。
- 影响：团队/多开场景下配置互相覆盖；`--port` 并未隔离数据目录。建议按端口派生配置文件或加文件锁。

### D18（低）文档与实现不一致（逐条）

| # | 文档 | 实现/事实 |
| --- | --- | --- |
| a | `README.md:144` "离线自检：**80** 项单元 + 端到端" | 实际 **82** 项（`tests/selftest.py` 输出） |
| b | `docs/DEVICE_NOTES.md:736,758-762` 结论为"iOS 26.6.1 那台**开发者模式未开、无镜像**，真机端到端现在**无法跑通**"，并要求用户先做两步手工操作 | 交付结论与 task 描述是"真机链路已实测跑通（RSD native 隧道 + DVT + 轨迹播放）"；且现在 `usbmux list` 只有 1 台 USB 设备（`00008030-001551840A02402E`），文档里的第二台 Network 设备已不在列。**DEVICE_NOTES §8 已过时**，与最终结论冲突 |
| c | `docs/PROTOCOL.md:83-86` 示例瓦片源含 `tencent`、amap `style=8` | 实现 `core/config_store.py:13-46` 是 `amap(style=7)/amap_sat/arcgis_sat/osm`，**没有 tencent** |
| d | `web/tile_sources.json` 注释"仅作为前端离线兜底" | 全仓库无任何代码读取它（`grep -rn tile_sources.json` 仅命中自身） |
| e | `docs/PROTOCOL.md:21` `/api/devices/connect` 示例 tunnel 含 `"ios_major":26` | 实际返回 `{"state":"idle","message":...,"rsd":null,"mode":null,"udid":null}`，无 `ios_major`；且 **mock 模式下这里返回 `idle`**，与 §4"mock 设备 tunnel=connected"自相矛盾（前端因此显示"隧道 空闲"，见 D9） |
| f | `docs/PROTOCOL.md:20` `device.select` "回 `device` + `tunnel`" | WS ack 只有 `device` |
| g | `README.md:151` 截图命令 | 命令可用，但 `tools/screenshot.py` 在**成功**时也打印 `Chrome 超时被终止`（`--virtual-time-budget` + `--screenshot` 模式本就不会自己退出，被 `timeout=max(60, wait*6)` 杀掉后才拿到 PNG），易被误判为失败 |
| h | `README.md:132-154` 命令行工具清单 | 未提及 `tests/real_gui_acceptance.py`；`scripts/run.sh` 在 `--mock` 下仍会执行一段 usbmux 枚举（README 未说明） |
| i | `README.md`/task-3 提到 `resources/` | 目录已不存在（任务书把它列为交付范围，但无内容） |

---

## 3. 真机路径：与 pymobiledevice3 11.29.1 源码的逐条核对

全部在 `/Users/jbjf/Desktop/location/.venv/lib/python3.12/site-packages/pymobiledevice3/` 内核对（未改动设备）：

| `core/*.py` 主张 | 源码出处 | 结论 |
| --- | --- | --- |
| `RemoteServiceDiscoveryService((host, port))` | `remote/remote_service_discovery.py:103-110`（`address: tuple[str,int]` 位置参数） | ✅ 一致 |
| `rsd.product_version` / `rsd.udid` | `:179-185`（property，需 connect 后）、`:318`（connect 里赋值） | ✅ 一致（provider 也在 connect 之后才读，`location_provider.py:403-408`） |
| `DvtProvider(rsd)` / `DvtProvider(lockdown)` 自动选服务名 | `services/dvt/instruments/dvt_provider.py:33-35`、`dtx_service_provider.py:80-97, 99-111` | ✅ 一致 |
| `await dvt.connect()` / `await dvt.close()` 长连接 | `dtx_service_provider.py:149-171` | ✅ 一致 |
| `LocationSimulation(dvt)` + `await sim.connect()` | `services/dvt/instruments/location_simulation.py:17-47`、`dtx_service.py:140-151` | ✅ 一致 |
| `set(latitude, longitude)` / `clear()` | `location_simulation.py:30-47`（selector `simulateLocationWithLatitude:longitude:` / `stopLocationSimulation`） | ✅ 一致，**不是** `simulate_location()` |
| `DtSimulateLocation`（iOS<17 兜底） | `services/simulate_location.py:8-40`；`SERVICE_NAME` 在 :17；每次 set/clear 新开服务（:13-14 注释） | ✅ 一致；provider 确实在 connect 阶段真开一次服务校验（`location_provider.py:486-494`），避免"假成功" |
| `lockdown.start_lockdown_developer_service(name)` | `lockdown_service_provider.py:162` | ✅ 存在 |
| tunnel 输出解析 `HOST PORT` | `cli/remote.py:297-323`：`--script-mode` 时 `print(f"{address} {port}")`；非 script-mode 才打印 `--rsd addr port` | ✅ `core/tunnel.py:284-306` 两种格式都覆盖，与实现一致 |
| `_kill_stale_tunnels` 只清自己的进程 | —— | ❌ 见 D10 |
| 设备发现 `MuxDevice.serial` / `.connection_type`（"USB"/"Network"） | `usbmux.py:129-133`；`list_devices()` 为 `async`（`:456-463`） | ✅ `core/device.py:243-265` 用法正确（且字符串大小写按源码用 `"USB"`/`"Network"`） |

**异常路径的中文化**：`core/location_provider.py:155-193` 的 `_wrap()` 覆盖了 `StartServiceError`（其 `str()` 为空，需读 `service_name/message`）、`InvalidService`、开发者模式、未配对、连接被拒、超时；`tools/selftest_location.py` 36 项里明确断言"错误必须是 `LocationProviderError` 且含中文"。我另做了静态抽查：`set_location` 越界（`location_provider.py:520-521`）、未连接（:522-523）、RSD 端口不可达（`_connect_dvt_rsd` → `_wrap`）都会给出中文信息，**未发现会抛 `AttributeError` 的路径**。
唯一"非中文"来自上层：`App.dispatch`（`core/server.py:206-209`）把任意异常包成 `"内部错误：{类名}: {str}"`，于是 D8 的三处报错会带英文原文（有中文前缀，不算裸栈）。

---

## 4. 交付完备性核对

| 交付项 | 命令 | 结果 |
| --- | --- | --- |
| `scripts/run.sh` | `bash scripts/run.sh --mock --no-browser --port 8888` | ✅ 6 秒后 `/api/health` → HTTP 200（依赖检查、Python 检查、设备提示均正常输出） |
| `Location.app` | 读 `Info.plist` + `Contents/MacOS/launch` | ✅ 启动器逻辑自洽：从 `.app` 位置回退到 `~/Desktop/location`，日志写 `~/Library/Logs/ios-location-app.log`；`CFBundleExecutable=launch` 且可执行位正确（`-rwx--x--x`）。未实际双击（会拉起真实设备会话） |
| `tools/probe_device.py` | `--help` | ✅ 参数与 README:136-137 一致（`--probe/--rsd/--udid/--json`） |
| `tools/probe_location.py` | `--help` | ✅ 与 README:140-142 一致（`--lat/--lon/--clear/--rsd/--duration`）；`scripts/test-location.sh 39.9088 116.3975` 参数顺序正确 |
| `tools/screenshot.py` | `... http://127.0.0.1:8877/ /tmp/verify-shot.png --wait 8` | ✅ 退出码 0、产出 1920 KB PNG；⚠️ 见 D18-g |
| `tools/selftest_location.py` | 直接运行 | ✅ 36/36 |
| `tests/selftest.py`、`tests/ui_acceptance.py` | 见第 1 节 | ✅ 82/82、19/19 |
| `README.md` 其余命令 | 逐条比对参数 | ✅ 与脚本一致（`--mock/--port/--no-browser/--window`、`mounter list/auto-mount`、`remote start-tunnel --native --script-mode`） |
| 无外网依赖约束 | `grep` 全部 `web/` 资源引用 | ✅ 只有本地 `coord.js/ws.js/map.js/app.js/style.css` + `data:` SVG favicon，无 CDN/字体外链 |
| 纯 stdlib 约束（后端） | `grep '^import|^from' core/*.py main.py` | ✅ 除 `pymobiledevice3`（懒加载）外全为标准库 |

---

## 5. 验证过但**没有**问题的点（覆盖面说明）

- **坐标转换方向正确**（用 node 直接跑 `web/js/coord.js`）：
  `wgs84ToGcj02(116.3975, 39.9088) = [116.403744, 39.910204]`，与基准 116.4038/39.9101 相差 5.6e-5/1.0e-4 度；`gcj02ToWgs84` 回代误差 **0**（`[116.3975, 39.9088]`）。`outOfChina` 边界、`toDisplay/fromDisplay` 的 datum 分支均正确。
- **静态文件穿越防护**：7 种载荷（`/../../../../etc/passwd`、`%2e%2e%2f`、`..%2f`、`/web/../core/server.py`、`/....//....//etc/passwd` …）全部未泄漏文件，返回 200(index.html 回落)/404/400，`leaked=False`。
- **瓦片参数校验**：`z=30`、`x` 越界、非整数坐标 → `400 + 中文`；`POST` 体 >64MB → `413`。
- **HTTP 对抗输入**：非法 JSON/数组/缺字段/NaN/Infinity/负数倍速/非法 mode/1MB 级 waypoints/畸形 name/超大 trkpt(10 万点)/XXE 外部实体（`ET` 拒绝，未泄漏 `/etc/passwd`）/空 GPX → 全部 `400 + 中文`，**无 500、无栈泄漏**（除 D8 的三处）。
- **WS 帧层**：两段分片、分片中间夹控制帧、粘包（两个独立帧）、close 握手回显 1000、>8MB 帧回 `close 1009`、非法 JSON 回中文 `bad_json`、未知命令（有/无 id 分别回 `ack ok:false`/`error`）、`play.speed`/`location.set`/`log.list` 边界值校验，均符合契约。
- **并发广播不串包**：2 个客户端 + 30x 播放 4 秒，收帧 a=44/b=46，`严格 JSON 解析失败 = 0`，`status.seq` 单调递增。
- **播放引擎正常路径**：暂停后不再下发、继续恢复、倍速生效、seek 后位置过半、播放到终点、loop 回到起点、停止后推 `source:"cleared"`；单点轨迹（0 长度）不会崩（`loop=false` 时正常结束并下发 1 个点）。
- **前端 XSS 静态审查**：4 处 `innerHTML` 汇点（设备卡、日志行、途经点行、modal）中，用户可控数据（设备名、GPS/GPX 名称、坐标）要么经 `esc()` 转义（`app.js:749-753`），要么用 `textContent`/`.value`；`pushLog`/`toast` 用 `textContent`。**未发现可利用的注入点**。
- **瓦片 Image 释放**：`map.js:340-470` 有 LRU（`CACHE_LIMIT`）、淘汰时 `onload/onerror=null` + `img.src=''`、`_clearTiles()` 全量释放、连续失败会暂停加载并给出告警（告警路径因 D6 无法触发，但代码逻辑本身正确）。
- **设备发现字段用法**与 11.29.1 源码一致（见第 3 节表格）。
- **mock 契约字段**：`/api/devices` 返回 `MOCK-0001 / 模拟设备 (iPhone 17 Pro) / iOS 26.0 / state=connected / tunnel=connected`，与 PROTOCOL §4 一致（UI 显示问题见 D9）。

---

## 6. 未能验证的点与原因

1. **真机端到端（RSD 隧道 + DVT 下发 + 轨迹播放）**：任务要求"绝不设置定位或改变设备状态"，故**未运行** `tests/real_device_e2e.py --confirm`、`tests/real_gui_acceptance.py --confirm`、`tools/probe_location.py`，也未启动任何隧道。真机部分只做了（a）源码 API 逐条核对、（b）只读 `usbmux.list_devices()` 计时。**"真机已跑通"这一结论我无法独立背书**，只能确认代码与 11.29.1 源码的接口完全吻合。同理，`legacy`（iOS<17）路径只做了代码审查，未实测。
2. **iOS<17 经典通道**：本机两台设备里那台 iOS 16.5（Network）已不在 `usbmux list` 中，无法核对 `dvt-lockdown`/`legacy` 的真实行为。
3. **动态 XSS 注入**：只做了静态审查，未构造恶意 GPX 名称驱动真实浏览器验证 DOM 结果。
4. **长时间稳定性**：瓦片缓存淘汰（1GB 上限）、`_inflight` 增长、WS 连接长期挂机、设备监控线程 3s 轮询的累积开销均未做小时级压测。
5. **非 Chrome 浏览器**：前端只在 Chrome（headless 与 app 模式）验证；Safari/Firefox 未测（`ws.js` 里有针对子协议的兼容分支，但未实测）。
6. **管理员隧道**：`sudo ./scripts/run.sh` / osascript 提权路径需要密码且会建隧道，未实测（只核对了 `_build_command` 生成的命令字符串）。
7. **`/api/shutdown` 之后再启动**：我验证了关机接口可被跨站调用，但未验证重启后的状态一致性。

---

## 7. 给 lead 的修复优先级建议

1. **D2 + D5 一起修**（配置校验 + Origin/token 校验）——最小改动、直接消除"一次请求打挂地图/钉住手机"。
2. **D1**（`isfinite` 校验 + `allow_nan=False`）——三行以内，消除非法 JSON 推流。
3. **D3**（播放线程代次 token）——真机定位跳变的主因，改动集中在 `core/playback.py`。
4. **D4**（任何入站消息都算存活）——一行改动，解决慢隧道下"连接设备"假超时。
5. **D6**（瓦片失败按契约回 404）——注意会同时激活前端的失败重试/告警路径，改完建议重跑 `tests/ui_acceptance.py`。
6. **D7/D8/D9** 作为第二梯队；**D18** 的文档同步建议在最终提交前一并处理（尤其 DEVICE_NOTES §8 与最终结论的矛盾）。

> 复现脚本：`/tmp/verify_adv_http.py`、`/tmp/verify_adv_ws.py`、`/tmp/verify_adv_ws2.py`、`/tmp/verify_adv_ws3.py`、`/tmp/wsclient.py`、`/tmp/verify_playback_race.py`、`/tmp/stall_server.py`、`/tmp/ws_client_stall_test.js`、`/tmp/verify_coord.js`（均在 `/tmp`，不影响交付目录）。
> 本报告只写 `docs/VERIFY_REPORT.md` 一个文件；`config.json` 因对抗性测试被 API 改动过，已用同一接口恢复为默认值（`send_hz=5.0`、`tile_source=amap`、`tile_cache_max_mb=1024`）。

---

# 复验（第二轮）

- 触发：lead 针对 D1–D11 做了修复并 reopen task-4（revision 4→5），要求逐条复验 + 用对抗性思路找新引入的问题。
- 复验基线：`selftest 83/83`、`ui_acceptance 20/20`（本人独立复跑，见下）；真机部分仍只做代码级/只读核对。
- 复验方式：**全部用第一轮的同一批脚本 + 新增针对性脚本复跑**，不采信 lead 的验证结论。
- 自建实例：8877（mock）；`--host 0.0.0.0` 场景另起 8888；D4 假服务端 8897；验证结束后全部关闭。
- 副作用说明：CDP 底图切换测试把 `config.json` 的 `tile_source` 改成了 `osm`（前端会自动持久化），已用 `POST /api/config {"tile_source":"amap"}` 恢复默认值。

## 复验总览

| 编号 | 一轮结论 | 二轮复验 | 证据位置 |
| --- | --- | --- | --- |
| D1 NaN→非法 JSON | 高 | ✅ **已关闭** | §R2-1 |
| D2 config 无校验 | 高 | ✅ **已关闭** | §R2-1 |
| D3 播放线程僵尸化 | 高 | ✅ **已关闭** | §R2-2 |
| D4 心跳只认 pong | 高 | ✅ **已关闭** | §R2-3 |
| D5 无 Origin/Host 校验 | 高 | ✅ **已关闭** | §R2-4 |
| D6 瓦片失败回 200 | 中 | ✅ **已关闭**（前端告警路径实测被激活） | §R2-5 |
| D7 stop 后 seek 重下发 | 中 | ✅ **已关闭** | §R2-6 |
| D8 三处非中文/500 | 中 | ⚠️ **部分关闭**（REST 已修，WS `log.list` 仍泄漏英文异常） | §R3-1 |
| D9 mock 显示「隧道 空闲」 | 中 | ❌ **仍存在**（修的是前端根本不用的 REST 路径，真实原因链未动） | §R3-2 |
| D10 误杀他人隧道 | 中 | ✅ **已关闭**（动态构造伪造进程验证） | §R2-7 |
| D11 WS 协议细节 | 中 | ✅ **已关闭**（1002 / 426 / 8MB 分片上限 1009） | §R2-8 |
| D12 multipart GPX | 低 | ❌ 仍 400（未声称修复） | §R3-3 |
| D13 refresh 阻塞 | 低 | ✅ pair_timeout 12s→3s；`hello` 仍同步 refresh（可接受） | §R3-3 |
| D14 点数无上限 | 低 | ✅ 5000/20 万双上限，413 中文；20 万点 path 仍可用 | §R2-9 |
| D15 speeds NaN | 低 | ✅ 回退为基准速度 13.9 | §R2-9 |
| D16 `_inflight` 只增不减 | 低 | ❌ 仍在（未声称修复） | §R3-3 |
| D17 多实例共享 config | 低 | ❌ 仍在（未声称修复，属设计取舍） | §R3-3 |
| D18 文档不一致 | 低 | ✅ README 83 项、DEVICE_NOTES §8.2 已加"被后续进展取代"；❌ PROTOCOL 仍列 tencent、connect 的 tunnel 仍无 `ios_major` | §R3-3 |
| 新增 N1–N5 | — | ⚠️ 2 个需要判断（clear 顺序、配置合并校验），3 个纯提示 | §R4 |

**复验结论：D1–D7、D10、D11 确认关闭；D8 只关了一半；D9 用户可见症状仍在；另有 1 个"顺序未被显式保证"的加固项（N1）。仍不建议直接交付，但剩余问题已从"高危"降级为"1 个中危 + 2 个局部问题"。**

---

## R1. 独立复跑的基线测试

| 命令 | 结果 |
| --- | --- |
| `./.venv/bin/python tests/selftest.py` | **通过 83 项，失败 0 项，用时 9.8s** ✅（我独立复跑，不是采信） |
| `./.venv/bin/python tests/ui_acceptance.py --url http://127.0.0.1:8877/ --out /tmp/ui-verify2` | **通过 20 项，失败 0 项** ✅（含新增的「点击播放后进入播放态」） |
| `tools/selftest_location.py`（一轮已跑 36/36） | 未改动该模块，未复跑 |

---

## R2. 已关闭项的证据（逐条可复现）

### R2-1 D1 + D2

```
$ python /tmp/verify2_ws.py            # R4/R5/R9/R11 段
R4 seek(NaN) ack -> {'type':'ack','id':'nan1','cmd':'play.seek','ok':False,
                     'error':'进度不是有限数（不允许 NaN / Infinity）','code':'bad_number'}
R4 之后 3 秒：含 NaN 帧=0 严格解析失败=0
R5 progress=NaN/Infinity/-Infinity/"abc"/null/{}/[] -> 全部 ok=False 中文
R11 speeds=[NaN,1] -> ok=None route.speeds=[13.9, 1.0]        ← NaN 回退基准速度
```

D2（`config_store._validate` + `_normalize`，用**临时配置文件**测，不碰项目 `config.json`）：

```
=== 非法配置必须全部被拒 ===            非法用例被接受：0 / 25
  ✓ send_hz NaN         -> send_hz不是有限数（不允许 NaN / Infinity）
  ✓ tile_sources 字符串  -> tile_sources 必须是「源名称 → 配置」的对象
  ✓ cache 63            -> 瓦片缓存上限需在 64 ~ 102400 MB 之间
  ✓ tile_sources url 缺 {z} -> 瓦片源 bad 的 url 必须包含 {z}/{x}/{y} 占位符
  …（22 条同类）
=== 合法配置是否被误拒 ===              合法用例被误拒：1 / 25
  ✓ 默认 4 个瓦片源整体提交 / tile_source 取 4 个源 / send_hz 1~20 / cache 64~102400
  ✓ window 200~10000 / mode_defaults 四模式 / 自定义瓦片源 / 多键合法组合 …（24 条）
  ✗ 误拒：{"tile_sources":{"my2":…},"tile_source":"my2"} 同一请求 -> 未知的瓦片源：my2   ← 见 N2
=== 坏配置文件自愈 ===  4/4 全部回退默认值并把 tile_source 归位 amap
$ curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8877/api/tiles/amap/1/0/0.png
200                     ← 修复后瓦片接口不再 500
```

### R2-2 D3 代次机制（用真实 `core/playback.py`）

脚本 `/tmp/verify2_playback2.py`（慢 provider 每次下发 3s，模拟真机卡顿）：

```
S1' 慢 provider(3s) + 0.3s 后第二次 start
  第二次 start 时存活线程: ['playback-4']（应为 1）      ← 不再双线程
  9 秒后存活线程: []（应为 0）                          ← 旧线程自行退出
  下发序列: [('set',39.908,3.2), ('set',31.23,4.12), ('set',31.2445,7.18), ('set',31.25,10.23)]
  北京点(旧代次)= 1 次；上海点(新代次)= 3 次             ← 旧代次只完成"在途那一次"
  → ✅ 通过：不再出现北京/上海交替下发
S3 seek 与 stop 并发 ×200：clear 之后又 set 的次数 = 0    ← 未复现残留竞态
```

> 对照第一轮：同样是这个脚本，第一轮实测 `当前存活的 playback 线程：['playback','playback']` 且定位在北京/上海之间来回跳。

### R2-3 D4 心跳（node 直接跑真实 `web/js/ws.js`）

假服务端 `/tmp/verifier2_stall.py`（8897）：`device.connect` 阻塞 60s，期间每 2s 推一条 `log`（模拟 tunnel-reader 线程）。

```
[0.0s] open / welcome
[1.5s] -> 发送 device.connect（服务端将阻塞 60s，不回 pong）
[61.5s] device.connect 成功: {"type":"ack","id":"c2-jttw","cmd":"device.connect","device":{"udid":"FAKE"},...}
[100.0s] 结束观察（pending=0）
```

✅ 全程 **0 次"心跳超时"、0 次重连**，60s 的命令拿到了 ack。（第一轮同一测试在 40.0s 报「心跳超时，正在重建连接」，91.5s 报命令超时。）

### R2-4 D5 Origin/Host（含"会不会误伤正常用法"）

```
1 无 Origin（curl 正常用法）        -> HTTP 200
2 同源 Origin http://127.0.0.1:8877 -> HTTP 200
3 Origin http://localhost:8877      -> HTTP 200
4 Origin https://evil.example       -> HTTP 403 已拒绝来自 https://evil.example 的跨站请求…
5 Origin: null（沙箱/file://）      -> HTTP 403 已拒绝来自沙箱页面…
7 Host: evil.com（DNS rebinding）   -> HTTP 403 已拒绝 Host: evil.com 的请求…
9 Host: localhost:8877              -> HTTP 200
10 Host: [::1]:8877                 -> HTTP 200
POST /api/location|/api/shutdown|/api/config|/api/devices/connect (Origin: evil) -> 全部 403
POST /api/location (同源 Origin，正常用法) -> 200 {"ok":true}
WS 握手：同源 Origin -> 101；evil -> 403；Origin: null -> 403；无 Origin（原生客户端）-> 101
```

`--host 0.0.0.0` 场景（本人起 8888 实例）：`app.host` 由 `serve()` 赋值（server.py:876 `app.host = host`），因此
`Host: 192.168.1.50:8888` / `mymac.local:8888` / LAN 浏览器真实场景（Host+Origin 都是局域网 IP）**全部 200** ✅，
而同一实例上 `Host: 192.168.1.50 + Origin: https://evil.example` 仍 403 ✅。**没有误伤。**

### R2-5 D6 瓦片 404 + 前端告警路径

```
$ curl -s -o /dev/null -w "osm -> HTTP %{http_code} 用时 %{time_total}s\n" -m 60 http://127.0.0.1:8877/api/tiles/osm/3/6/3.png
osm -> HTTP 404 用时 12.009s        ← 契约要求 404，已符合
```

前端（真实 Chrome + CDP Network 域，切换到本机不可达的 OSM 源并驱动地图重绘）：

```
tile 请求(按源): {'osm': 12}
tile 响应(源,状态): {('osm', 404): 12}
告警 class: map-warn                 ← 不再 hidden
告警文本: 瓦片加载失败 2 张           ← onerror → 重试/告警路径确实被激活
```

### R2-6 D7 停止后 seek

```
R6 停止后 seek -> ok=False err=当前没有正在播放的轨迹（请先点播放）   下发次数 3 -> 4（隔离复测为 +0）
# 隔离复测（单独脚本，stop 后统计 /api/logs 里 "下发定位 lat=" 条数）：
  seek -> False 当前没有正在播放的轨迹（请先点播放）
  seek 之后累计下发 7 次（差值 0）
```

### R2-7 D10 隧道进程清理（动态构造伪造进程）

```
D10-A 没有 pid 文件时：调用 _kill_stale_tunnels() 后伪造的 "pymobiledevice3 remote start-tunnel" 进程仍存活 = True → ✅ 未误杀
D10-B pid 文件记录该 PID：进程被清理（存活=False），pid 文件被删除 → ✅
D10-C pid 文件记录的是普通 sleep 进程：仍存活 = True → ✅ 未误杀
D10-D pid 文件里是已消失的 PID：无异常，文件被清理 → ✅
```

（另核对：`tunnel.py:138` 在启动隧道时 `_write_pidfile([process.pid])`、189/294 清理，机制不是死代码。）

### R2-8 D11 WebSocket 协议

```
未掩码帧            -> {'__close__': True, 'code': 1002}     ✅ RFC §5.1
Sec-WebSocket-Version: 8 / 0 -> HTTP/1.1 426 Upgrade Required ✅（第一轮是 101）
分片累计 >8MB       -> 服务端 close 帧 op=8 code=1009 reason=b'fragmented message too large' ✅
单帧 9MB            -> close 1009 ✅（一轮已验）
并发广播 4s a=28/b=30 帧，严格 JSON 解析失败 0 ✅；close 回显 1000 ✅；粘包 ✅
```

### R2-9 低危附带修复

```
route/build 6000 waypoints -> HTTP 413 {"error":"坐标点过多（6000 个，上限 5000 个）：请减少途经点或改用 GPX 导入"}
route/build 100 waypoints  -> HTTP 200
play.start 20 万点 path    -> 2.55s 成功（GPX 大轨迹仍可播放）
```

---

## R3. 仍未关闭 / 未修的问题

### R3-1（中）D8 只关了一半：WS `log.list` 仍泄漏英文异常

- 位置：`core/server.py:355-356` `_cmd_log_list` 仍是 `int(message.get("limit") or 200)`（REST 的 `/api/logs` 已在 `:713-719` 加了 try/except，WS 命令路径漏了）。
- 复现与原始输出：

```
$ # 用自写 RFC6455 客户端发 {"type":"log.list","limit":"abc"}
  limit='abc'    -> [{'type':'log',...},
                     {'type':'error','error':"内部错误：ValueError: invalid literal for int() with base 10: 'abc'",'code':'internal'}]
  limit='1e9'    -> 同上（'1e9'）
  limit='abc123' -> 同上
$ curl -s -o - -w " -> %{http_code}\n" "http://127.0.0.1:8877/api/logs?limit=abc"
{"ok":false,"error":"limit 必须是整数","code":"bad_request"} -> HTTP 400     ← REST 已修好
```

- 期望：WS 侧同样回中文（`{"ok":false,"error":"limit 必须是整数","code":"bad_request"}`）。
- 影响：前端固定传数字，只有 API 客户端能触发；属契约/健壮性问题（低危），但属于"说好修了却没修全"，建议一并处理（3 行）。
- 顺带：`recent_logs()` 自身也仍然直接 `int(limit)`（`core/server.py:108`）。

### R3-2（中）D9 仍存在：mock 模式设备卡依然显示「隧道 空闲」

- 现状（CDP 读真实 DOM，页面刚加载、未点任何按钮）：

```
设备卡： ["型号=iPhone18,1","系统=iOS 26.0","连接=模拟","设备状态=已连接",
          "隧道=空闲","虚拟定位=未模拟","开发者模式=已开启"]
```

- 服务端侧确实修好了（`POST /api/devices/connect`、`POST /api/tunnel` 都返回 `state:"connected"` / "模拟模式：无需建立 RSD 隧道"），**但前端不调用 `/api/tunnel`**（`grep -rn "api/tunnel" web/` 无结果），而页面加载走的是 hello→welcome，这条链没改：
  - `core/server.py:133` `welcome_payload()` 仍返回 `self.tunnel.snapshot()` → mock 下就是 `{"state":"idle","message":"隧道未建立（iOS 17 及以上需要）",…}`（实测 hello ack：`tunnel = {"state":"idle",…}`，而同帧里 `devices[0].tunnel = connected`）；
  - `web/js/app.js:461-465` `applySessionInfo()` 仍就地把 `S.device.tunnel = msg.tunnel.state`（=idle），紧接着 `applyDevices()` 又把**同一个被改过的对象**装回 `S.device`；而且同一条 hello ack 被处理两次（`:400` 的 welcome 事件 + `:410` 的 ack 分支）。
- 期望：mock 下设备卡显示「已连接」（PROTOCOL.md:153），或至少不要用隧道快照覆盖设备对象的权威字段。
- 最小修复（任一即可，优先第 1 条）：
  1. 服务端 `welcome_payload()` 复用 mock-aware 的隧道语义（与 `_cmd_tunnel_ensure` 同一处实现），使 hello ack 的 `tunnel.state == "connected"`；
  2. 前端 `applySessionInfo` 不再用 `msg.tunnel.state` 覆盖 `S.device.tunnel`，并按 `msg.id` 去重避免同一消息处理两次。
- 附：`GET /api/tunnel` 在 mock 下仍返回 `idle`（与 `POST` 不一致）。前端未使用，属契约一致性小问题。

### R3-3（低）一轮中未声称修复、复验确认仍在的项

| 项 | 复验输出 | 说明 |
| --- | --- | --- |
| D12 multipart GPX | `multipart -> 400 {"error":"GPX 文件解析失败…"}`；`text/plain -> 200` | PROTOCOL.md:29 写了支持 multipart，仍未支持 |
| D16 `tilecache._inflight` | `grep _inflight` 仍只有 `get`/赋值，无清理 | 长跑内存缓慢增长 |
| D17 多实例共享 `config.json` | 本轮测试再次改写同一文件（前端自动持久化） | 设计取舍，未改 |
| D18-c PROTOCOL 瓦片源示例 | 仍含 `tencent`（实现里没有） | 文档示例与实现不一致 |
| D18-e `/api/devices/connect` 的 tunnel | `tunnel keys = ['message','mode','rsd','state','udid']`，无 `ios_major` | 与 PROTOCOL:21 示例不一致 |
| D13 `refresh` 同步阻塞 | `pair_timeout=3.0`（12→3 ✅）；`hello` 仍同步 refresh | 健康设备下几十 ms，可接受 |

---

## R4. 新发现的问题（修复过程引入 / 本轮新观察到）

### N1（中低，真机语义，建议加固）`play.stop` 的"恢复真实定位"与"在途下发"的相对顺序**没有被显式保证**

- 位置：`core/playback.py:139-175`（`stop()` 里 join 超时从 2.5s 降到 0.6s，然后**立刻**起 `playback-clear` 线程调用 `clear_location()`）。
- 实验（同一脚本、只改"设备何时生效"的模型，模拟真机 DVT 的两种情况）：

```
S2' stop 时的 set/clear 顺序（设备模型 effect_at=start，模拟真机：帧一发出设备就生效）
  stop() 返回耗时 0.61s
  事件序列: [('set', 39.908, 0.21), ('clear', None, 1.33)]
  → clear 之后的 set 次数 = 0   ✅ 设备最终已恢复

S2' stop 时的 set/clear 顺序（设备模型 effect_at=end，模拟 core/location_mock.py 的语义）
  stop() 返回耗时 0.60s
  事件序列: [('clear', None, 1.32), ('set', 39.908, 3.21)]
  → clear 之后的 set 次数 = 1   ⚠️ 设备最终停留在模拟位置
```

- 判断：真机走 DVT 时，`set` 的帧在 3 秒前就已经写出（`dtx/_sender.py:34-59` 的 `_send_lock` 串行化帧写入，`simulateLocationWithLatitude:longitude:` 发出后 await 回复），`clear` 的帧随后写入 → 设备按 set→clear 顺序处理 → **最终状态是已恢复**（`effect_at=start` 那一行）。但这依赖传输层时序，代码里没有任何"先等在途下发结束再 clear"的显式保证；而在 `--mock` 下（以及任何"回复到达才算生效"的实现下）可观测到 clear→set，最后停在模拟点。
- 影响：真机大概率没问题；风险在于①换传输/换 provider 时可能悄悄变成"停止后仍被钉住"；②mock 的日志会显示"已停止模拟，iPhone 恢复真实定位"，但内部最后状态仍是模拟点，容易误导后续验收。
- 最小修复：`_clear_provider_async` 里先 `old_thread.join(timeout=6)` 再 `clear_location()`（或把 clear 也塞进同一条串行下发队列），让顺序与传输无关。

### N2（低）D2 的校验顺序会把"新增瓦片源 + 同时切换过去"这种合法请求误拒

```
✗ 误拒：{"tile_sources":{"my2":{...}}, "tile_source":"my2"} -> 未知的瓦片源：my2
```

- 位置：`core/config_store.py:157`（`update()` 对 patch 里每个 key 用**当前** `self._data` 校验，此时新源还没合并）。
- 影响：前端只单独发 `{"tile_source": key}`（`app.js:663`），正常用法不受影响；属 API 边角。
- 建议：先合并 `tile_sources` 再校验 `tile_source`。

### N3（低）配置自动回退无任何提示

`ConfigStore._repaired` 被置位（`config_store.py:69/96/109/114/117`）但全仓库没有任何地方读取它 → 用户手改坏了 `config.json` 时会被静默重置。建议 `main.py` 启动时检测并 `app.log("warn", …)`。

### N4（低）`play.stop` 的 0.6s 是"必然耗满"的等待

`stop_locked()` 在**持有 `self._lock`** 的情况下 `thread.join(0.6s)`，而被 join 的播放线程此时通常正阻塞在 `stale()` 里 `with self._lock`（playback.py:233）→ 必然等满 0.6s 才继续。功能正确，若想让「停止」更快，可先释放锁再 join。

### N5（提示）一次「停止 + 退出」会重复 clear 三次

in-process 复验（`play.stop` 后立刻 `app.shutdown()`）：

```
provider 调用序列: [('set',39.908),('set',39.908),('set',39.9081),('set',39.9083),
                    'clear','clear','clear','close']
clear 次数: 3  close 次数: 1
→ ✅ 停止+退出后设备仍被恢复
```

三次 clear 分别来自：后台 clear 线程、`shutdown()` 里的 `playback.stop()`、`provider.close()` 内部的 clear。幂等无害，但日志会重复三遍。

---

## R5. 二轮"验证过但没问题"的补充覆盖

- 播放/停止/暂停/继续/seek 的完整 UI 流程：`ui_acceptance 20/20`，新增的「点击播放后进入播放态」也通过。
- `play.stop` 的 ack 延迟从 2.2s 降到 **0.60s**（实测 3 次），且 clear 结果（成功/失败）都会写日志：成功 `已停止模拟，iPhone 恢复真实定位`，失败 `恢复真实定位失败：<中文>`（用会抛异常的 provider 实测到 warn）。
- `--host 0.0.0.0` 局域网访问 + Origin/Host 双校验组合：正常访问 200、恶意 Origin 403（见 R2-4）。
- 分片累计上限生效后，正常分片消息（两段、中间夹控制帧）依然可用。
- 20 万点超长播放列表仍可用（2.55s），6000 途经点按新上限回 413 中文。
- 隧道 pid 文件机制（写/读/清理）与"只杀自己记录的进程"行为（R2-7）。

## R6. 仍未验证的点（与一轮相同）

1. **真机端到端**：受"不设定位/不改设备状态"约束，未运行 `real_device_e2e.py`/`real_gui_acceptance.py`，未建隧道。N1 的顺序结论来自源码级分析（DTX `_send_lock` + `await` 语义）+ 两种设备模型仿真，**没有真机实测**。若 lead 要闭环这一条，建议在真机上做一次：`play.start` → 0.3s 后 `play.stop` → 立刻看手机是否回到真实定位（这条由 lead 那边跑，我不占用唯一的 RSD 连接）。
2. `Wi-Fi/Network` 设备与 iOS<17 `legacy` 路径（本机当前只有 1 台 USB 设备）。
3. 小时级稳定性（瓦片 LRU 淘汰、`_inflight` 增长、WS 长挂）。
4. Safari/Firefox（只测了 Chrome）。
5. 管理员隧道（`sudo`/osascript 提权路径）。

---

## R7. 复验用到的脚本（均在 /tmp，可重跑）

| 脚本 | 用途 |
| --- | --- |
| `/tmp/verify2_ws.py` | WS 侧全套（NaN seek、协议、上限、并发、粘包） |
| `/tmp/verify2_playback2.py` | D3 代次 + clear 顺序 + clear 失败可见性（真实 `core/playback.py`） |
| `/tmp/verifier2_stall.py` + `/tmp/verifier2_stall_test.js` | D4 心跳（node 跑真实 `web/js/ws.js`，端口 8897） |
| `/tmp/wsclient.py` | 自写 RFC6455 客户端（未复用项目 `core/ws.py`） |
| 内联脚本 | D2 配置校验 25+25 例、D5 Origin/Host 矩阵、D10 伪造进程、CDP 底图 404 告警 |
