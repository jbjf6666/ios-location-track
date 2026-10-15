/*!
 * app.js — 状态机与全部交互（严格按 docs/PROTOCOL.md）
 *
 * 内容：
 *   - 设备卡片 / 连接 / 断开 / 隧道状态 / 开发者模式提示
 *   - 模式、倍速、循环、路线概览、途经点列表（拖拽排序 / 双击改坐标 / 定位 / 删除）
 *   - 绘制工具条（选择 / 添加途经点 / 折线绘制 / 撤销 / 删除 / 全览）
 *   - 播放控制（播放 / 暂停 / 继续 / 停止 / seek / 倍速）与实时状态
 *   - GPX 导入导出、路线保存到 localStorage
 *   - 日志面板（最多 300 行）
 *
 * 所有坐标对外（后端 / localStorage / 日志）一律 WGS84。
 */
(function () {
  'use strict';

  var WS_URL = (function () {
    var loc = window.location;
    var proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
    var host = loc.hostname || '127.0.0.1';
    var port = loc.port || '8787';
    return proto + '//' + host + ':' + port + '/ws';
  })();

  var LOG_MAX = 300;
  var UNDO_MAX = 50;
  var PREF_KEY = 'iosloc.prefs.v1';
  var ROUTES_KEY = 'iosloc.routes.v1';

  var LEVEL_CN = { info: '信息', warn: '警告', error: '错误', success: '成功' };
  var MODE_CN = { driving: '驾车', cycling: '骑行', walking: '步行', flight: '飞行' };
  var CONN_CN = { usb: 'USB', network: '网络', wifi: 'Wi-Fi', mock: '模拟', lan: '局域网' };
  var STATE_CN = { connected: '已连接', unpaired: '未配对', unavailable: '不可用' };
  var TUNNEL_CN = {
    idle: '空闲', starting: '建立中', connected: '已连接',
    error: '失败', unsupported: '不需要'
  };

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  /* ================================================================== */
  /* 全局状态                                                            */
  /* ================================================================== */

  var S = {
    ws: null,
    map: null,
    health: null,
    mock: false,
    version: '--',
    config: null,
    modeDefaults: { driving: 13.9, cycling: 5.5, walking: 1.4, flight: 60.0 },

    devices: [],
    selected: null,
    device: null,
    tunnel: null,

    mode: 'driving',
    speedScale: 1,
    loop: false,
    routeName: '',

    waypoints: [],      // WGS84 [lng,lat][]
    drawnPath: [],      // 手绘折线（WGS84）
    route: null,        // 服务端返回的 Route 对象
    polyline: [],       // 正在绘制的折线
    drawing: false,
    selectedWp: -1,
    drawMode: 'select',

    playing: false,
    paused: false,
    status: null,
    scrubbing: false,
    progress: 0,

    logs: [],
    undo: [],
    tileSources: [],
    tileSource: '',
    logCollapsed: true,

    lastLocation: null,

    // 真实感模拟（校园跑等打卡测试）：GPS 漂移 + 速度浮动
    driftEnabled: true,
    driftM: 4,
    jitterEnabled: true,
    jitterPercent: 8,
    timingJitter: true,
    deriveSpeed: true,
    grossError: false,
    grossErrorPercent: 2,
    config: null
  };

  /* ================================================================== */
  /* 小工具                                                              */
  /* ================================================================== */

  function toast(msg, kind, ms) {
    var wrap = $('#toastWrap');
    var t = document.createElement('div');
    t.className = 'toast' + (kind ? ' is-' + kind : '');
    t.textContent = msg;
    wrap.appendChild(t);
    setTimeout(function () {
      t.classList.add('is-out');
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 320);
    }, ms || 3400);
  }

  var modalOnOk = null;

  function openModal(opt) {
    $('#modalTitle').textContent = opt.title || '提示';
    $('#modalBody').innerHTML = opt.html || '';
    $('#modalOk').textContent = opt.okText || '确定';
    $('#modalCancel').textContent = opt.cancelText || '取消';
    $('#modalOk').className = 'btn ' + (opt.danger ? 'btn-danger' : 'btn-primary');
    modalOnOk = opt.onOk || null;
    $('#modalMask').classList.remove('hidden');
    var first = $('#modalBody').querySelector('input, select, textarea');
    if (first) setTimeout(function () { first.focus(); first.select && first.select(); }, 30);
  }

  function closeModal() {
    $('#modalMask').classList.add('hidden');
    modalOnOk = null;
  }

  $('#modalOk').addEventListener('click', function () {
    var fn = modalOnOk;
    if (fn) {
      var r = fn();
      if (r === false) return;
    }
    closeModal();
  });
  $('#modalCancel').addEventListener('click', closeModal);
  $('#modalMask').addEventListener('click', function (e) {
    if (e.target === $('#modalMask')) closeModal();
  });

  function confirmModal(title, text, onOk, okText) {
    openModal({
      title: title,
      html: '<div>' + text + '</div>',
      okText: okText || '确定',
      danger: true,
      onOk: onOk
    });
  }

  function setBusy(btn, busy, busyText) {
    if (!btn) return;
    if (busy) {
      if (!btn.dataset.origText) btn.dataset.origText = btn.textContent;
      btn.disabled = true;
      if (busyText) btn.textContent = busyText;
    } else {
      btn.disabled = false;
      if (btn.dataset.origText) { btn.textContent = btn.dataset.origText; delete btn.dataset.origText; }
    }
  }

  /** JSON 接口；服务端 ok:false 一律抛出中文 Error */
  function apiJson(path, opts) {
    return fetch(path, opts).then(function (res) {
      var ct = res.headers.get('content-type') || '';
      if (ct.indexOf('application/json') >= 0) {
        return res.json().then(function (data) {
          if (data && data.ok === false) {
            var e = new Error(data.error || ('请求失败：' + path));
            e.code = data.code || 'api_error';
            throw e;
          }
          return data;
        });
      }
      if (!res.ok) {
        var err = new Error('请求失败 HTTP ' + res.status + '（' + path + '）');
        err.code = 'http_' + res.status;
        throw err;
      }
      return res.text();
    });
  }

  function postJson(path, body) {
    return apiJson(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body || {})
    });
  }

  /** 下载文本文件 */
  function downloadText(text, filename, mime) {
    var blob = new Blob([text], { type: mime || 'text/plain;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 1200);
  }

  function debounce(fn, ms) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  /* ================================================================== */
  /* 日志                                                                */
  /* ================================================================== */

  var lastLog = null;

  /** 把数字（坐标、序号、时间）换成 # 作为“同一类消息”的指纹 */
  function logSignature(msg) {
    return String(msg).replace(/-?\d+(?:\.\d+)?/g, '#');
  }

  function pushLog(level, msg, ts) {
    var l = { ts: ts == null ? Date.now() / 1000 : ts, level: LEVEL_CN[level] ? level : 'info', msg: String(msg) };
    var sig = logSignature(l.msg);
    // 短时间内同类型消息（例如 5Hz 的定位下发日志）合并成一行，避免刷屏
    if (lastLog && lastLog.sig === sig && lastLog.level === l.level && Math.abs(l.ts - lastLog.ts) < 3) {
      lastLog.count += 1;
      lastLog.ts = l.ts;
      var merged = lastLog.msg + '  ×' + lastLog.count;
      var mEl = lastLog.row.querySelector('.log-msg');
      if (mEl) mEl.textContent = merged;
      if (S.logs.length) S.logs[S.logs.length - 1] = { ts: l.ts, level: l.level, msg: merged };
      return;
    }
    S.logs.push(l);
    var body = $('#logBody');
    var atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 24;
    while (S.logs.length > LOG_MAX) {
      S.logs.shift();
      if (body.firstChild) body.removeChild(body.firstChild);
    }
    var row = document.createElement('div');
    row.className = 'log-line level-' + l.level;
    row.innerHTML = '<span class="log-ts"></span><span class="log-lvl"></span><span class="log-msg"></span>';
    row.querySelector('.log-ts').textContent = Geo.fmtClock(l.ts);
    row.querySelector('.log-lvl').textContent = LEVEL_CN[l.level];
    row.querySelector('.log-msg').textContent = l.msg;
    body.appendChild(row);
    if (atBottom) body.scrollTop = body.scrollHeight;
    lastLog = { sig: sig, count: 1, row: row, msg: l.msg, level: l.level, ts: l.ts };
    $('#logCount').textContent = S.logs.length + ' / ' + LOG_MAX;
    var label = $('#logCollapsedLabel');
    if (label) label.textContent = '运行日志 (' + S.logs.length + ')';
    // 出错时自动展开日志面板，避免错误被折叠面板藏住
    if (l.level === 'error' && S.logCollapsed) setLogCollapsed(false, true);
  }

  function setLogs(lines) {
    S.logs = [];
    lastLog = null;
    $('#logBody').innerHTML = '';
    (lines || []).forEach(function (l) { pushLog(l.level, l.msg, l.ts); });
    if (!lines || !lines.length) pushLog('info', '服务端暂无历史日志');
  }

  /* ================================================================== */
  /* 首选项 / 本地路线                                                    */
  /* ================================================================== */

  function loadPrefs() {
    try {
      var p = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
      if (p.mode && MODE_CN[p.mode]) S.mode = p.mode;
      if (isFinite(p.speedScale)) S.speedScale = Geo.clamp(+p.speedScale, Geo.SPEED_MIN, Geo.SPEED_MAX);
      if (typeof p.loop === 'boolean') S.loop = p.loop;
      if (p.tileSource) S.tileSource = p.tileSource;
      if (p.routeName) S.routeName = p.routeName;
      if (typeof p.logCollapsed === 'boolean') S.logCollapsed = p.logCollapsed;
      if (p.view && isFinite(p.view.lng)) S.savedView = p.view;
      return p;
    } catch (e) {
      return {};
    }
  }

  var savePrefs = debounce(function () {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify({
        mode: S.mode,
        speedScale: S.speedScale,
        loop: S.loop,
        tileSource: S.tileSource,
        routeName: S.routeName,
        logCollapsed: S.logCollapsed,
        view: S.map ? { lng: S.map.getCenter().lng, lat: S.map.getCenter().lat, zoom: S.map.getZoom() } : null
      }));
    } catch (e) { /* localStorage 不可用时忽略 */ }
  }, 350);

  function readSavedRoutes() {
    try {
      var arr = JSON.parse(localStorage.getItem(ROUTES_KEY) || '[]');
      return Array.isArray(arr) ? arr : [];
    } catch (e) { return []; }
  }

  function writeSavedRoutes(arr) {
    try {
      localStorage.setItem(ROUTES_KEY, JSON.stringify(arr));
      return true;
    } catch (e) {
      toast('保存失败：浏览器本地存储不可用', 'err');
      return false;
    }
  }

  /* ================================================================== */
  /* 初始化                                                              */
  /* ================================================================== */

  function init() {
    loadPrefs();

    S.map = new SlippyMap('#map', { zoom: 12, minZoom: 3 });
    S.map.setView(116.397428, 39.90923, 12, true);
    bindMapEvents();
    bindUI();

    var v = S.savedView;
    if (v && isFinite(v.lng)) S.map.setView(v.lng, v.lat, v.zoom || 12, true);
    S.map.setSource({ key: 'amap', name: '高德矢量', max_zoom: 18, attribution: '© 高德地图', datum: 'gcj02' });

    applyPrefsToUI();
    renderDevice();
    renderWaypoints();
    updateStats();
    renderSavedRoutes();
    updateTransport();

    setLogCollapsed(S.logCollapsed, true);
    pushLog('info', '前端已启动，正在连接本地服务 ' + WS_URL);
    connectWs();
    loadHealth();
    loadConfig();
    loadTileSources();

    window.addEventListener('beforeunload', function () { savePrefs(); });
  }

  function applyPrefsToUI() {
    $$('#modeGroup .seg-btn').forEach(function (b) {
      b.classList.toggle('is-active', b.dataset.mode === S.mode);
    });
    $('#speedRange').value = Math.round(Geo.speedToSlider(S.speedScale) * 1000);
    $('#speedValue').textContent = Geo.fmtSpeedScale(S.speedScale);
    $('#liveScale').textContent = Geo.fmtSpeedScale(S.speedScale);
    $('#loopToggle').checked = S.loop;
    $('#routeName').value = S.routeName || '';
  }

  /* ================================================================== */
  /* WebSocket                                                           */
  /* ================================================================== */

  function connectWs() {
    S.ws = new WSClient({ url: WS_URL });
    S.ws.onState(function (st) {
      var pill = $('#connPill');
      if (st === 'open') { pill.textContent = '服务已连接'; pill.className = 'pill pill-ok'; }
      else if (st === 'connecting') { pill.textContent = '连接中…'; pill.className = 'pill pill-warn'; }
      else { pill.textContent = '未连接'; pill.className = 'pill pill-err'; }
      $('#appSubtitle').textContent = st === 'open'
        ? '本地服务 ' + WS_URL.replace('ws://', '').replace('/ws', '') + ' · 已连接'
        : '本地服务未连接，正在自动重连…';
      updateBanner();
      updateButtons();
    });

    S.ws.on('open', function () {
      pushLog('success', '已连接到本地服务');
    });

    S.ws.on('close', function (info) {
      if (!info.manual) pushLog('warn', '与本地服务的连接已断开，正在自动重连…');
      S.playing = false; S.paused = false;
      updateTransport();
    });

    S.ws.on('protocol_fallback', function (info) {
      pushLog('warn', '服务端握手返回了未请求的 WebSocket 子协议，已切换兼容模式（v1）重连' +
        (info && info.reason ? '：' + info.reason : ''));
    });

    S.ws.on('reconnecting', function (info) {
      $('#appSubtitle').textContent = '连接断开，' + Math.round(info.delay / 100) / 10 + ' 秒后重试…';
    });

    S.ws.on('welcome', applySessionInfo);

    S.ws.on('pong', function () { /* 心跳正常，无需处理 */ });

    S.ws.on('ack', function (msg) {
      if (msg.ok === false) {
        pushLog('error', '命令失败：' + (msg.error || msg.cmd || '未知错误'));
        return;
      }
      // 服务端把 welcome 数据并在 hello 的 ack 里返回（welcome 字段不在独立消息里）
      if (msg.cmd === 'hello') applySessionInfo(msg);
    });

    S.ws.on('error', function (msg) {
      var text = msg && msg.error ? msg.error : '服务端返回未知错误';
      pushLog('error', text);
      toast(text, 'err');
    });

    S.ws.on('devices', function (msg) {
      applyDevices(msg.devices, msg.selected);
    });

    S.ws.on('device', function (msg) {
      S.device = msg.device || null;
      if (S.device) S.selected = S.device.udid;
      renderDevice();
      updateBanner();
    });

    S.ws.on('tunnel', function (msg) {
      S.tunnel = msg;
      if (S.device) S.device.tunnel = msg.state || S.device.tunnel;
      pushLog(msg.state === 'error' ? 'error' : 'info',
        '隧道状态：' + (TUNNEL_CN[msg.state] || msg.state) + (msg.message ? ' — ' + msg.message : ''));
      renderDevice();
      updateBanner();
      // tunnel 事件可能是瞬时中间态（starting）：设备对象才是权威状态，直接回读对齐
      // （tunnel 事件很稀少，不需要节流；节流会导致卡片停在「建立中」）
      lastDeviceSync = 0;
      syncDeviceFromServer(true);
      scheduleTunnelWatchdog(msg.state);
    });

    S.ws.on('status', onStatus);

    // 开发者镜像挂载结果
    S.ws.on('mounted', function (m) {
      var btn = $('#btnMountImage');
      if (btn) btn.disabled = false;
      if (m && m.mounted) {
        pushLog('success', '开发者镜像挂载完成：' + (m.message || ''));
        toast('开发者镜像已挂载，现在可以进行虚拟定位', 'ok', 4200);
      } else {
        pushLog('error', '开发者镜像挂载失败：' + ((m && m.message) || '未知原因'));
        toast('挂载失败，详情见运行日志', 'err', 5000);
      }
    });
    S.ws.on('location', onLocation);

    S.ws.on('log', function (msg) { pushLog(msg.level, msg.msg, msg.ts); });
    S.ws.on('logs', function (msg) {
      if (msg.lines) setLogs(msg.lines);
    });

    S.ws.connect();
  }

  /** 应用会话握手信息（兼容 hello→ack 与 hello→welcome 两种服务端实现） */
  function applySessionInfo(msg) {
    if (!msg) return;
    if (msg.version) S.version = msg.version;
    if (msg.mock != null) S.mock = !!msg.mock;
    if (msg.config) applyConfig(msg.config);
    if (msg.tunnel) {
      S.tunnel = msg.tunnel;
      if (S.device) S.device.tunnel = msg.tunnel.state || S.device.tunnel;
    }
    if (Array.isArray(msg.devices)) applyDevices(msg.devices, msg.selected);
    $('#serverInfo').textContent = '服务端 v' + S.version + (S.mock ? '·模拟' : '');
    if (!S.sessionLogged) {
      S.sessionLogged = true;
      pushLog('success', '服务端就绪：版本 ' + S.version + (S.mock ? '（模拟模式）' : '') +
        (msg.backend ? '，后端 ' + msg.backend : ''));
    }
    renderDevice();
    updateBanner();
    updateButtons();
    loadTileSources();
    req('devices.refresh', {}, 6000).catch(function () { /* 忽略，等待推送 */ });
    req('log.list', { limit: 200 }, 6000).catch(function () { /* 忽略 */ });
  }

  var lastDeviceSync = 0;
  var tunnelWatch = null;

  /** 以服务端设备对象为准回读设备状态（tunnel/state 字段） */
  function syncDeviceFromServer(force) {
    var t = Date.now();
    if (!force && t - lastDeviceSync < 1200) return;
    lastDeviceSync = t;
    if (S.ws && S.ws.isOpen()) {
      req('devices.refresh', {}, 8000).then(function (msg) {
        if (msg.devices) applyDevices(msg.devices, msg.selected);
      }).catch(function () { /* 忽略 */ });
    } else {
      apiJson('/api/devices').then(function (d) {
        applyDevices(d.devices, d.selected);
      }).catch(function () { /* 忽略 */ });
    }
  }

  /**
   * 隧道停在「建立中」时的兜底：服务端不一定会为中间态推后续事件，
   * 这里每 2 秒回读一次权威设备状态，最长 20 秒；仍无结果就给出可操作提示。
   */
  function scheduleTunnelWatchdog(state) {
    if (tunnelWatch) { clearInterval(tunnelWatch); tunnelWatch = null; }
    if (state !== 'starting') { S.tunnelStuck = false; return; }
    var tries = 0;
    tunnelWatch = setInterval(function () {
      tries += 1;
      syncDeviceFromServer(true);
      if (S.device && S.device.tunnel !== 'starting') {
        clearInterval(tunnelWatch);
        tunnelWatch = null;
        S.tunnelStuck = false;
        renderDevice();
        return;
      }
      if (tries >= 10) {
        clearInterval(tunnelWatch);
        tunnelWatch = null;
        S.tunnelStuck = true;
        pushLog('warn', '隧道建立超过 20 秒仍未完成。iOS 17+ 通常需要管理员权限（sudo 运行服务）；' +
          '也可用「手动 RSD」直接填写地址端口。');
        renderDevice();
        updateBanner();
      }
    }, 2000);
  }

  function req(cmd, fields, timeout) {
    if (!S.ws || !S.ws.isOpen()) {
      var e = new Error('尚未连接到本地服务，请稍后重试');
      e.code = 'ws_closed';
      return Promise.reject(e);
    }
    return S.ws.request(cmd, fields, timeout);
  }

  /* ================================================================== */
  /* 配置 / 健康检查 / 瓦片源                                             */
  /* ================================================================== */

  function loadHealth() {
    apiJson('/api/health').then(function (h) {
      S.health = h;
      S.version = h.version || S.version;
      S.mock = !!h.mock;
      $('#serverInfo').textContent = '服务端 v' + S.version + (S.mock ? '·模拟' : '');
      updateBanner();
    }).catch(function () {
      $('#serverInfo').textContent = '服务端不可达';
    });
  }

  function applyConfig(cfg) {
    if (!cfg) return;
    S.config = cfg;
    if (cfg.mode_defaults) {
      Object.keys(cfg.mode_defaults).forEach(function (k) {
        if (isFinite(cfg.mode_defaults[k])) S.modeDefaults[k] = +cfg.mode_defaults[k];
      });
    }
    if (isFinite(cfg.default_speed_scale) && S.speedScale === 1) {
      S.speedScale = Geo.clamp(+cfg.default_speed_scale, Geo.SPEED_MIN, Geo.SPEED_MAX);
      applyPrefsToUI();
    }
    if (!S.tileSource && cfg.tile_source) S.tileSource = cfg.tile_source;
    // 真实感模拟参数（服务端为准，例如上次退出前调好的值）
    if (typeof cfg.drift_enabled === 'boolean') S.driftEnabled = cfg.drift_enabled;
    if (isFinite(cfg.drift_m)) S.driftM = Math.round(+cfg.drift_m);
    if (typeof cfg.speed_jitter_enabled === 'boolean') S.jitterEnabled = cfg.speed_jitter_enabled;
    if (isFinite(cfg.speed_jitter)) S.jitterPercent = Math.round(+cfg.speed_jitter * 100);
    if (typeof cfg.timing_jitter_enabled === 'boolean') S.timingJitter = cfg.timing_jitter_enabled;
    if (typeof cfg.derive_speed === 'boolean') S.deriveSpeed = cfg.derive_speed;
    if (typeof cfg.gross_error_enabled === 'boolean') S.grossError = cfg.gross_error_enabled;
    if (isFinite(cfg.gross_error_probability)) S.grossErrorPercent = Math.round(+cfg.gross_error_probability * 100);
    if (typeof applyTuningUi === 'function') applyTuningUi();
  }

  function loadConfig() {
    apiJson('/api/config').then(function (data) {
      applyConfig(data.config);
      if (!S.tileSources.length) buildTileSourcesFromConfig();
    }).catch(function (e) {
      pushLog('warn', '读取配置失败：' + e.message);
    });
  }

  /** 兼容多种 /api/settings/tiles 返回结构 */
  function normalizeTileSettings(data) {
    var payload = (data && (data.settings || data)) || {};
    var raw = payload.sources || payload.tile_sources || payload.tiles || payload.list || null;
    var current = payload.current || payload.tile_source || payload.selected || null;
    var list = [];
    if (Array.isArray(raw)) {
      raw.forEach(function (it) {
        if (!it) return;
        var key = it.key || it.id || it.name;
        if (key) list.push(toTileSource(key, it));
      });
    } else if (raw && typeof raw === 'object') {
      Object.keys(raw).forEach(function (k) { list.push(toTileSource(k, raw[k] || {})); });
    }
    return { list: list, current: current };
  }

  function toTileSource(key, o) {
    o = o || {};
    return {
      key: String(key),
      name: o.name || String(key),
      max_zoom: +(o.max_zoom || o.maxZoom || 18),
      attribution: o.attribution || '',
      datum: Coord.datumOfSource(key, o)
    };
  }

  function buildTileSourcesFromConfig() {
    var cfg = S.config || {};
    var list = [];
    if (cfg.tile_sources) {
      Object.keys(cfg.tile_sources).forEach(function (k) { list.push(toTileSource(k, cfg.tile_sources[k])); });
    }
    if (!list.length) {
      list = [{ key: 'amap', name: '高德矢量', max_zoom: 18, attribution: '© 高德地图', datum: 'gcj02' }];
    }
    S.tileSources = list;
    if (!S.tileSource) S.tileSource = cfg.tile_source || list[0].key;
    renderTileSelect();
    applyTileSource(S.tileSource, true);
  }

  function loadTileSources() {
    apiJson('/api/settings/tiles').then(function (data) {
      var n = normalizeTileSettings(data);
      if (!n.list.length) { buildTileSourcesFromConfig(); return; }
      S.tileSources = n.list;
      S.tileSource = S.tileSource || n.current || n.list[0].key;
      if (!S.tileSources.some(function (s) { return s.key === S.tileSource; })) {
        S.tileSource = n.current && n.list.some(function (s) { return s.key === n.current; })
          ? n.current : n.list[0].key;
      }
      renderTileSelect();
      applyTileSource(S.tileSource, true);
    }).catch(function (e) {
      pushLog('warn', '读取瓦片源列表失败：' + e.message + '，使用配置中的瓦片源');
      buildTileSourcesFromConfig();
    });
  }

  function renderTileSelect() {
    var sel = $('#tileSourceSelect');
    sel.innerHTML = '';
    S.tileSources.forEach(function (s) {
      var o = document.createElement('option');
      o.value = s.key;
      o.textContent = s.name + (s.datum === 'gcj02' ? '（GCJ-02）' : '（WGS84）');
      sel.appendChild(o);
    });
    sel.value = S.tileSource;
  }

  /** 切换底图；persist=true 时同步到服务端配置 */
  function applyTileSource(key, initial) {
    var src = S.tileSources.filter(function (s) { return s.key === key; })[0];
    if (!src) return;
    var changed = S.map.getSource().key !== key;
    S.tileSource = key;
    S.map.setSource(src);
    S.map.invalidate();
    $('#tileSourceSelect').value = key;
    if (!initial) {
      pushLog('info', '底图已切换为「' + src.name + '」，坐标基准 ' + (src.datum === 'gcj02' ? 'GCJ-02' : 'WGS84'));
      postJson('/api/config', { tile_source: key }).catch(function (e) {
        pushLog('warn', '底图设置未能保存到服务端：' + e.message);
      });
    }
    if (changed) savePrefs();
  }

  /* ================================================================== */
  /* 设备                                                                */
  /* ================================================================== */

  function applyDevices(list, selected) {
    S.devices = Array.isArray(list) ? list : [];
    if (selected) S.selected = selected;
    if (!S.selected || !S.devices.some(function (d) { return d.udid === S.selected; })) {
      S.selected = S.devices.length ? S.devices[0].udid : null;
    }
    S.device = S.devices.filter(function (d) { return d.udid === S.selected; })[0] || null;
    renderDevice();
    updateBanner();
  }

  function renderDevice() {
    var card = $('#deviceCard');
    var d = S.device;

    if (!d) {
      card.innerHTML = '<div class="device-empty">未检测到 iOS 设备 — 请用数据线连接 iPhone，' +
        '解锁屏幕后点击右上角「刷新设备」</div>';
    } else {
      var stateCls = d.state === 'connected' ? 'ok' : (d.state === 'unpaired' ? 'warn' : 'err');
      var tunCls = d.tunnel === 'connected' ? 'ok' : (d.tunnel === 'error' ? 'err' :
        (d.tunnel === 'starting' ? 'warn' : ''));
      var html = '';
      html += '<div class="dev-name"><b>' + esc(d.name || d.udid) + '</b><span>' + esc(d.udid || '') + '</span></div>';
      html += '<div class="dev-grid">';
      html += devRow('型号', d.product_type || '--');
      html += devRow('系统', 'iOS ' + (d.ios_version || '--'));
      html += devRow('连接', CONN_CN[d.connection] || d.connection || '--');
      html += devRow('设备状态', STATE_CN[d.state] || d.state || '--', stateCls);
      html += devRow('隧道', TUNNEL_CN[d.tunnel] || d.tunnel || '--', tunCls);
      html += devRow('虚拟定位', d.simulating ? '模拟中' : '未模拟', d.simulating ? 'ok' : '');
      html += devRow('开发者模式', d.developer_mode ? '已开启' : '未开启', d.developer_mode ? 'ok' : 'warn');
      html += '</div>';
      if (d.state === 'unpaired') {
        html += '<div class="dev-hint">设备未配对：请在 iPhone 上点「信任此电脑」后重新连接</div>';
      }
      if (d.developer_mode === false) {
        html += '<div class="dev-hint">未开启开发者模式：设置 → 隐私与安全性 → 开发者模式</div>';
      }
      if (d.tunnel === 'starting' && S.tunnelStuck) {
        html += '<div class="dev-hint">隧道建立已超过 20 秒，请检查是否以管理员权限运行，或改用「手动 RSD」</div>';
      }
      if (d.tunnel === 'error' && d.tunnel_hint) {
        html += '<div class="dev-hint">' + esc(d.tunnel_hint) + '</div>';
      }
      if (d.last_error) {
        html += '<div class="dev-hint">' + esc(d.last_error) + '</div>';
      }
      card.innerHTML = html;
    }

    if (S.devices.length > 1) {
      var sel = document.createElement('select');
      sel.className = 'input select';
      sel.style.maxWidth = '190px';
      S.devices.forEach(function (dd) {
        var o = document.createElement('option');
        o.value = dd.udid;
        o.textContent = (dd.name || dd.udid) + ' · iOS ' + (dd.ios_version || '?');
        if (dd.udid === S.selected) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('change', function () { selectDevice(sel.value); });
      card.appendChild(sel);
    }

    updateButtons();
    updateBanner();
  }

  function devRow(k, v, cls) {
    return '<div class="dev-row"><span class="dev-k">' + esc(k) + '</span>' +
      '<span class="dev-v ' + (cls || '') + '">' + esc(v) + '</span></div>';
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function updateButtons() {
    var open = S.ws && S.ws.isOpen();
    var d = S.device;
    $('#btnDevicesRefresh').disabled = !open;
    $('#btnDeviceConnect').disabled = !open || !d || d.state !== 'connected';
    $('#btnDeviceDisconnect').disabled = !open || !d;
    $('#btnFetchLogs').disabled = !open;
    updateTransport();
  }

  function updateBanner() {
    var banner = $('#alertBanner');
    var text = '', kind = '', action = null;

    if (!S.ws || !S.ws.isOpen()) {
      text = '本地服务未连接（' + WS_URL.replace('ws://', '') + '）。请确认已启动服务后点击「重试」。';
      kind = 'is-error';
      action = { label: '重试', fn: function () { S.ws.reconnectNow(); } };
    } else if (S.device && S.device.state === 'unpaired') {
      text = '「' + (S.device.name || S.device.udid) + '」尚未与此电脑配对，请在 iPhone 上点击「信任」后重新连接。';
    } else if (S.device && S.device.developer_mode === false) {
      text = '请在 iPhone 上开启「开发者模式」：设置 → 隐私与安全性 → 开发者模式，开启后需重启手机，否则无法模拟定位。';
    } else if (S.device && S.device.tunnel === 'error') {
      text = S.device.tunnel_hint || '隧道建立失败。iOS 17 及以上需要管理员权限建立隧道，请以 sudo 运行服务。';
      kind = 'is-error';
      action = { label: '建立隧道', fn: function () { $('#btnTunnelEnsure').click(); } };
    } else if (!S.device && S.devices.length === 0) {
      text = '未检测到 iOS 设备。请用数据线连接 iPhone 并解锁屏幕；首次连接需要在手机上信任此电脑。';
      kind = 'is-info';
    } else if (S.mock) {
      text = '当前为模拟模式（--mock）：不会真正下发给手机，可用于完整验证轨迹与播放流程。';
      kind = 'is-info';
    }

    if (!text) { banner.classList.add('hidden'); return; }
    banner.className = 'banner ' + kind;
    $('#bannerText').textContent = text;
    var btn = $('#bannerAction');
    if (action) {
      btn.classList.remove('hidden');
      btn.textContent = action.label;
      btn.onclick = action.fn;
    } else {
      btn.classList.add('hidden');
      btn.onclick = null;
    }
  }

  function refreshDevices() {
    var btn = $('#btnDevicesRefresh');
    setBusy(btn, true, '扫描中…');
    var done = function () { setBusy(btn, false); };
    if (S.ws && S.ws.isOpen()) {
      req('devices.refresh', {}, 12000).then(function (msg) {
        if (msg.devices) applyDevices(msg.devices, msg.selected);
        pushLog('info', '设备列表已刷新，共 ' + S.devices.length + ' 台');
      }).catch(function (e) {
        pushLog('error', '刷新设备失败：' + e.message);
        toast(e.message, 'err');
      }).then(done);
    } else {
      apiJson('/api/devices').then(function (data) {
        applyDevices(data.devices, data.selected);
      }).catch(function (e) {
        toast(e.message, 'err');
      }).then(done);
    }
  }

  function selectDevice(udid) {
    if (!udid) return;
    if (S.ws && S.ws.isOpen()) {
      req('device.select', { udid: udid }, 10000).then(function (msg) {
        if (msg.device) { S.device = msg.device; renderDevice(); }
        pushLog('info', '已选择设备：' + ((msg.device && msg.device.name) || udid));
      }).catch(function (e) { toast(e.message, 'err'); });
    } else {
      postJson('/api/devices/select', { udid: udid }).then(function (data) {
        S.device = data.device;
        renderDevice();
      }).catch(function (e) { toast(e.message, 'err'); });
    }
  }

  function deviceConnect() {
    var btn = $('#btnDeviceConnect');
    setBusy(btn, true, '连接中…');
    pushLog('info', '正在建立隧道并连接 DVT 定位服务…');
    var finish = function () { setBusy(btn, false); };
    if (S.ws && S.ws.isOpen()) {
      req('device.connect', {}, 90000).then(function (msg) {
        if (msg.device) { S.device = msg.device; renderDevice(); }
        if (msg.tunnel) { S.tunnel = msg.tunnel; }
        toast('设备已连接', 'ok');
        syncDeviceFromServer(true);
        pushLog('success', '设备连接成功' + (msg.tunnel && msg.tunnel.rsd ? '（RSD ' + msg.tunnel.rsd + '）' : ''));
      }).catch(function (e) {
        pushLog('error', '连接设备失败：' + e.message);
        toast(e.message, 'err', 5200);
      }).then(finish);
    } else {
      postJson('/api/devices/connect', {}).then(function (data) {
        S.device = data.device;
        renderDevice();
        toast('设备已连接', 'ok');
      }).catch(function (e) {
        toast(e.message, 'err', 5200);
      }).then(finish);
    }
  }

  function deviceDisconnect() {
    confirmModal('断开设备', '断开后将释放 DVT 会话并让手机恢复真实定位，确定继续？', function () {
      var btn = $('#btnDeviceDisconnect');
      setBusy(btn, true, '断开中…');
      var finish = function () { setBusy(btn, false); };
      var after = function () {
        stopLocalPlaybackState();
        toast('已断开连接，手机已恢复真实定位', 'ok');
      };
      if (S.ws && S.ws.isOpen()) {
        req('device.disconnect', {}, 20000).then(function (msg) {
          if (msg.device) { S.device = msg.device; renderDevice(); }
          after();
        }).catch(function (e) { toast(e.message, 'err'); }).then(finish);
      } else {
        postJson('/api/devices/disconnect', {}).then(after)
          .catch(function (e) { toast(e.message, 'err'); }).then(finish);
      }
    }, '断开');
  }

  /* ================================================================== */
  /* 地图事件                                                            */
  /* ================================================================== */

  function bindMapEvents() {
    var m = S.map;

    m.on('click', function (e) {
      if (S.drawMode === 'add') {
        addWaypoint(e.lng, e.lat);
      } else if (S.drawMode === 'polyline') {
        if (!S.drawing) startPolyline();
        S.polyline.push([Coord.round6(e.lng), Coord.round6(e.lat)]);
        m.setPath(S.polyline, '#d29922');
        $('#toolHint').textContent = '折线绘制中：已加 ' + S.polyline.length + ' 点，双击或 Esc 结束';
        pushLog('info', '折线加点 ' + S.polyline.length + '：' + Geo.fmtLngLat(e.lng, e.lat, 6));
      } else {
        selectWaypoint(-1);
      }
    });

    m.on('dblclick', function () {
      if (S.drawMode === 'polyline' && S.drawing) finishPolyline();
    });

    m.on('waypointclick', function (e) { selectWaypoint(e.index); });

    m.on('waypointdrag', function (e) {
      if (S.waypoints[e.index]) {
        S.waypoints[e.index][0] = Coord.round6(e.lng);
        S.waypoints[e.index][1] = Coord.round6(e.lat);
      }
      var row = $('#wpList').children[e.index];
      if (row) row.querySelector('.wp-coord').textContent = Geo.fmtLngLat(e.lng, e.lat, 6);
      updateStats();
    });

    m.on('waypointdragend', function (e) {
      if (!e.moved) return;
      pushUndo();
      invalidateRoute('途经点已移动');
      renderWaypoints();
      updateStats();
    });

    m.on('move', function () { savePrefs(); });
    m.on('zoomend', function () { savePrefs(); });
  }

  function setDrawMode(mode) {
    if (mode !== 'polyline' && S.drawing) finishPolyline();
    S.drawMode = mode;
    $$('#drawToolbar .tool-btn[data-tool]').forEach(function (b) {
      b.classList.toggle('is-active', b.dataset.tool === mode);
    });
    S.map.setDrawMode(mode);
    S.map.setDoubleClickZoom(mode !== 'polyline');
    S.map.setWaypoints(S.waypoints, { draggable: mode === 'select' });
    var hints = {
      select: '拖拽平移 · 滚轮缩放 · 可拖动途经点',
      add: '点击地图添加途经点',
      polyline: '连续点击加点，双击或 Esc 结束'
    };
    $('#toolHint').textContent = hints[mode] || '';
  }

  /* ================================================================== */
  /* 途经点 / 折线                                                       */
  /* ================================================================== */

  function snapshot() {
    return JSON.stringify({ waypoints: S.waypoints, drawnPath: S.drawnPath, route: S.route });
  }

  function pushUndo() {
    S.undo.push(snapshot());
    if (S.undo.length > UNDO_MAX) S.undo.shift();
  }

  function undo() {
    if (!S.undo.length) { toast('没有可撤销的操作', 'warn'); return; }
    var st = JSON.parse(S.undo.pop());
    S.waypoints = st.waypoints || [];
    S.drawnPath = st.drawnPath || [];
    S.route = st.route || null;
    syncMapLayers();
    renderWaypoints();
    updateStats();
    toast('已撤销上一步', 'info', 1600);
  }

  function invalidateRoute(reason) {
    if (S.route) {
      S.route = null;
      pushLog('warn', (reason || '路线已变更') + '，需要重新点击「加载 OSRM 路线」');
    }
    syncMapLayers();
  }

  function addWaypoint(lng, lat) {
    pushUndo();
    S.waypoints.push([Coord.round6(lng), Coord.round6(lat)]);
    S.route = null;
    S.drawnPath = [];
    syncMapLayers();
    renderWaypoints();
    updateStats();
    var i = S.waypoints.length - 1;
    selectWaypoint(i);
    pushLog('info', '已添加途经点 ' + (i + 1) + '：' + Geo.fmtLngLat(lng, lat, 6));
  }

  function removeWaypoint(index) {
    if (index < 0 || index >= S.waypoints.length) return;
    pushUndo();
    S.waypoints.splice(index, 1);
    S.route = null;
    if (S.selectedWp === index) S.selectedWp = -1;
    else if (S.selectedWp > index) S.selectedWp -= 1;
    syncMapLayers();
    renderWaypoints();
    updateStats();
  }

  function setWaypointCoords(index, lng, lat) {
    if (index < 0 || index >= S.waypoints.length) return;
    pushUndo();
    S.waypoints[index] = [Coord.round6(lng), Coord.round6(lat)];
    S.route = null;
    syncMapLayers();
    renderWaypoints();
    updateStats();
  }

  function reorderWaypoints(from, to) {
    if (from === to || from < 0 || to < 0 || from >= S.waypoints.length || to >= S.waypoints.length) return;
    pushUndo();
    var item = S.waypoints.splice(from, 1)[0];
    S.waypoints.splice(to, 0, item);
    S.route = null;
    syncMapLayers();
    renderWaypoints();
    updateStats();
  }

  function selectWaypoint(index, center) {
    S.selectedWp = index;
    S.map.setSelected(index);
    $$('#wpList .wp-item').forEach(function (row, i) {
      row.classList.toggle('is-selected', i === index);
    });
    if (index >= 0 && center && S.waypoints[index]) {
      S.map.setView(S.waypoints[index][0], S.waypoints[index][1], Math.max(S.map.getZoom(), 15));
    }
  }

  function syncMapLayers() {
    S.map.setWaypoints(S.waypoints, { draggable: S.drawMode === 'select' });
    if (S.drawing) {
      S.map.setPath(S.polyline, '#d29922');
    } else if (S.route && S.route.path) {
      S.map.setPath(S.route.path);
    } else if (S.drawnPath.length >= 2) {
      S.map.setPath(S.drawnPath, '#d29922');
    } else if (S.waypoints.length >= 2) {
      S.map.setPath(S.waypoints.slice(), '#8b98a8');
    } else {
      S.map.setPath([]);
    }
    S.map.setSelected(S.selectedWp);
  }

  function startPolyline() {
    S.drawing = true;
    S.polyline = [];
    pushLog('info', '开始折线绘制：连续点击地图加点，双击或 Esc 结束');
    $('#toolHint').textContent = '折线绘制中：点击加点，双击或 Esc 结束';
  }

  function finishPolyline() {
    S.drawing = false;
    var pts = S.polyline.slice();
    S.polyline = [];
    if (pts.length < 2) {
      S.map.setPath([]);
      syncMapLayers();
      toast('折线至少需要 2 个点', 'warn');
      return;
    }
    pushUndo();
    S.drawnPath = pts;
    S.route = null;
    syncMapLayers();
    updateStats();
    S.map.fitBounds(pts, { padding: 60 });
    pushLog('success', '折线绘制完成，共 ' + pts.length + ' 点，长度 ' + Geo.fmtDistance(Geo.pathLength(pts)));
    toast('折线绘制完成，可直接播放或导出 GPX', 'ok');
    setDrawMode('select');
  }

  function renderWaypoints() {
    var list = $('#wpList');
    list.innerHTML = '';
    $('#wpEmpty').classList.toggle('hidden', S.waypoints.length > 0);
    var n = S.waypoints.length;

    S.waypoints.forEach(function (wp, i) {
      var row = document.createElement('div');
      row.className = 'wp-item' + (i === 0 ? ' is-start' : (i === n - 1 ? ' is-end' : '')) +
        (i === S.selectedWp ? ' is-selected' : '');
      row.draggable = true;
      row.dataset.index = String(i);
      row.innerHTML =
        '<span class="wp-drag" title="拖拽排序">⋮⋮</span>' +
        '<span class="wp-idx">' + (i === 0 ? '起' : (i === n - 1 ? '终' : (i + 1))) + '</span>' +
        '<span class="wp-main">' +
        '<span class="wp-coord" title="双击可修改坐标">' + Geo.fmtLngLat(wp[0], wp[1], 6) + '</span>' +
        '<span class="wp-actions">' +
        '<button type="button" class="wp-btn" data-act="center" title="地图定位到该点">定位到此处</button>' +
        '<button type="button" class="wp-btn" data-act="single" title="让设备模拟定位到该点">模拟到此</button>' +
        '<button type="button" class="wp-btn danger" data-act="del" title="删除该点">删除</button>' +
        '</span></span>';

      row.addEventListener('click', function (ev) {
        var btn = ev.target.closest('button');
        if (btn) {
          ev.stopPropagation();
          if (btn.dataset.act === 'del') removeWaypoint(i);
          else if (btn.dataset.act === 'center') {
            selectWaypoint(i);
            S.map.setView(wp[0], wp[1], Math.max(S.map.getZoom(), 15));
          } else if (btn.dataset.act === 'single') setSingleLocation(wp[0], wp[1], i);
          return;
        }
        selectWaypoint(i);
        S.map.setView(wp[0], wp[1], Math.max(S.map.getZoom(), 14));
      });

      row.addEventListener('dblclick', function () { editWaypointCoords(i); });

      row.addEventListener('dragstart', function (ev) {
        ev.dataTransfer.effectAllowed = 'move';
        try { ev.dataTransfer.setData('text/plain', String(i)); } catch (err) { /* 忽略 */ }
        row.classList.add('is-dragging');
      });
      row.addEventListener('dragend', function () { row.classList.remove('is-dragging'); });
      row.addEventListener('dragover', function (ev) {
        ev.preventDefault();
        row.classList.add('drag-over');
      });
      row.addEventListener('dragleave', function () { row.classList.remove('drag-over'); });
      row.addEventListener('drop', function (ev) {
        ev.preventDefault();
        row.classList.remove('drag-over');
        var from = parseInt(ev.dataTransfer.getData('text/plain'), 10);
        if (isFinite(from)) reorderWaypoints(from, i);
      });

      list.appendChild(row);
    });

    $('#statWaypoints').textContent = String(n);
    $('#routeMeta').textContent = (S.routeName || '未命名轨迹');
    updateButtons();
  }

  function editWaypointCoords(index) {
    var wp = S.waypoints[index];
    if (!wp) return;
    openModal({
      title: '修改途经点坐标（第 ' + (index + 1) + ' 点）',
      okText: '保存',
      html:
        '<div class="field"><label>经度</label><input class="input" id="m_lng" value="' + wp[0].toFixed(6) + '"></div>' +
        '<div class="field"><label>纬度</label><input class="input" id="m_lat" value="' + wp[1].toFixed(6) + '"></div>' +
        '<div class="hint">坐标为 WGS84，单位度（例如 116.397428 / 39.909230）。高德底图显示时会自动转换为 GCJ-02。</div>',
      onOk: function () {
        var lng = parseFloat($('#m_lng').value);
        var lat = parseFloat($('#m_lat').value);
        if (!isFinite(lng) || !isFinite(lat) || Math.abs(lng) > 180 || Math.abs(lat) > 90) {
          toast('坐标格式不正确', 'err');
          return false;
        }
        setWaypointCoords(index, lng, lat);
        return true;
      }
    });
  }

  /* ================================================================== */
  /* 路线构建 / 统计                                                     */
  /* ================================================================== */

  function buildLocalRoute(points, engine) {
    var speed = S.modeDefaults[S.mode] || 13.9;
    var path = points.map(function (p) { return [Coord.round6(p[0]), Coord.round6(p[1])]; });
    var distance = Geo.pathLength(path);
    var duration = speed > 0 ? distance / speed : 0;
    return {
      id: 'r-' + Date.now(),
      name: S.routeName || '未命名轨迹',
      mode: S.mode,
      waypoints: (S.waypoints.length >= 2 ? S.waypoints : path).map(function (p) { return [p[0], p[1]]; }),
      path: path,
      speeds: path.map(function () { return speed; }),
      distance: distance,
      duration: duration,
      engine: engine || 'straight',
      loop: S.loop,
      speed_scale: S.speedScale
    };
  }

  /** 归一化服务端 Route，补齐可能缺失的字段 */
  function normalizeRoute(r, engine) {
    if (!r || !Array.isArray(r.path) || r.path.length < 2) {
      throw new Error('服务端返回的路线数据不完整');
    }
    var path = r.path.map(function (p) { return [Coord.round6(p[0]), Coord.round6(p[1])]; });
    var speed = S.modeDefaults[S.mode] || 13.9;
    var speeds = Array.isArray(r.speeds) && r.speeds.length === path.length
      ? r.speeds.map(Number) : path.map(function () { return speed; });
    var distance = isFinite(r.distance) ? +r.distance : Geo.pathLength(path);
    var duration = isFinite(r.duration) ? +r.duration : (speed > 0 ? distance / speed : 0);
    return {
      id: r.id || ('r-' + Date.now()),
      name: r.name || S.routeName || '未命名轨迹',
      mode: r.mode || S.mode,
      waypoints: Array.isArray(r.waypoints) && r.waypoints.length
        ? r.waypoints.map(function (p) { return [Coord.round6(p[0]), Coord.round6(p[1])]; })
        : S.waypoints.map(function (p) { return [p[0], p[1]]; }),
      path: path,
      speeds: speeds,
      distance: distance,
      duration: duration,
      engine: r.engine || engine || 'straight',
      loop: !!S.loop,
      speed_scale: S.speedScale
    };
  }

  function buildRoute() {
    if (S.waypoints.length < 2) {
      toast('至少需要 2 个途经点才能规划路线', 'warn');
      return;
    }
    var btn = $('#btnBuildRoute');
    setBusy(btn, true, '规划中…');
    pushLog('info', '正在规划路线（' + (MODE_CN[S.mode] || S.mode) + '，' + S.waypoints.length + ' 个途经点）…');

    postJson('/api/route/build', { waypoints: S.waypoints, mode: S.mode }).then(function (data) {
      S.route = normalizeRoute(data.route, data.engine);
      S.drawnPath = [];
      syncMapLayers();
      S.map.fitBounds(S.route.path, { padding: 60 });
      updateStats();
      if (S.route.engine === 'osrm') {
        pushLog('success', 'OSRM 路线已生成：' + Geo.fmtDistance(S.route.distance) + '，' + Geo.fmtDuration(S.route.duration));
        toast('路线已生成（OSRM 路网）', 'ok');
      } else {
        pushLog('warn', '路线规划服务不可用，已按直线生成；播放与导出仍然可用');
        toast('OSRM 不可用，已退化为直线路线', 'warn', 4600);
      }
    }).catch(function (e) {
      pushLog('error', '路线规划失败：' + e.message + '，改用本地直线路线');
      toast('路线服务不可用，已本地生成直线路线', 'warn', 4600);
      if (S.waypoints.length >= 2) {
        S.route = buildLocalRoute(S.waypoints, 'straight');
        S.drawnPath = [];
        syncMapLayers();
        S.map.fitBounds(S.route.path, { padding: 60 });
        updateStats();
      }
    }).then(function () { setBusy(btn, false); });
  }

  /** 播放/导出用的路线：优先服务端路线 → 手绘折线 → 途经点直线 */
  function ensureRoute() {
    if (S.route && S.route.path && S.route.path.length >= 2) return S.route;
    if (S.drawnPath.length >= 2) return buildLocalRoute(S.drawnPath, 'drawn');
    if (S.waypoints.length >= 2) return buildLocalRoute(S.waypoints, 'straight');
    return null;
  }

  function currentPath() {
    if (S.route && S.route.path) return S.route.path;
    if (S.drawnPath.length >= 2) return S.drawnPath;
    return S.waypoints;
  }

  function updateStats() {
    var path = currentPath();
    var route = S.route;
    var distance = route && isFinite(route.distance) ? route.distance : Geo.pathLength(path);
    var speed = S.modeDefaults[S.mode] || 13.9;
    var duration = route && isFinite(route.duration) ? route.duration : (speed > 0 ? distance / speed : 0);

    $('#statDistance').textContent = distance > 0 ? Geo.fmtDistance(distance) : '--';
    $('#statDuration').textContent = duration > 0 ? Geo.fmtDurationCn(duration) : '--';
    $('#statPlayDuration').textContent = duration > 0
      ? Geo.fmtDurationCn(duration / Math.max(0.01, S.speedScale)) : '--';
    $('#statPoints').textContent = String(path ? path.length : 0);
    $('#statWaypoints').textContent = String(S.waypoints.length);

    var engine = '--';
    if (S.drawing) engine = '绘制中';
    else if (route) engine = route.engine === 'osrm' ? 'OSRM 路网' : (route.engine === 'drawn' ? '手绘折线' : '直线');
    else if (S.drawnPath.length >= 2) engine = '手绘折线';
    else if (S.waypoints.length >= 2) engine = '待规划';
    $('#statEngine').textContent = engine;

    $('#chipTrack').textContent = '轨迹点 ' + (S.map ? S.map.layers.track.length : 0);
    $('#routeMeta').textContent = (S.routeName || '未命名轨迹') + (route ? '' : '（未规划）');
    updateTransport();
  }

  /* ================================================================== */
  /* 播放控制                                                            */
  /* ================================================================== */

  function routePayload(route) {
    return {
      id: route.id,
      name: route.name || S.routeName || '未命名轨迹',
      mode: route.mode || S.mode,
      waypoints: (route.waypoints || S.waypoints).map(function (p) { return [Coord.round6(p[0]), Coord.round6(p[1])]; }),
      path: route.path.map(function (p) { return [Coord.round6(p[0]), Coord.round6(p[1])]; }),
      speeds: route.speeds,
      distance: route.distance,
      duration: route.duration,
      engine: route.engine,
      loop: !!S.loop,
      speed_scale: S.speedScale
    };
  }

  function startPlay() {
    var route = ensureRoute();
    if (!route) {
      toast('请先添加至少 2 个途经点，或绘制折线、加载路线', 'warn', 4200);
      return;
    }
    route.loop = !!S.loop;
    route.speed_scale = S.speedScale;
    S.map.clearTrack();
    S.map.setPlayedIndex(-1);
    updateStats();
    pushLog('info', '开始播放：' + Geo.fmtDistance(route.distance) + '，倍速 ' + Geo.fmtSpeedScale(S.speedScale) + (S.loop ? '，循环' : ''));
    req('play.start', {
      route: routePayload(route),
      speed_scale: S.speedScale,
      loop: !!S.loop,
      start_index: 0,
      tuning: {
        drift_enabled: !!S.driftEnabled,
        drift_m: Number(S.driftM),
        speed_jitter_enabled: !!S.jitterEnabled,
        speed_jitter: Number(S.jitterPercent),
        timing_jitter_enabled: !!S.timingJitter,
        derive_speed: !!S.deriveSpeed,
        gross_error_enabled: !!S.grossError,
        gross_error_probability: Number(S.grossErrorPercent) / 100
      }
    }, 15000).then(function () {
      S.playing = true;
      S.paused = false;
      S.stoppedAt = 0;
      S.statusSeq = null;
      updateTransport();
      pushLog('success', '播放已开始');
    }).catch(function (e) {
      pushLog('error', '开始播放失败：' + e.message);
      toast(e.message, 'err', 4600);
    });
  }

  function pausePlay() {
    req('play.pause', {}, 8000).then(function () {
      S.paused = true;
      updateTransport();
      pushLog('info', '播放已暂停');
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  function resumePlay() {
    req('play.resume', {}, 8000).then(function () {
      S.paused = false;
      S.playing = true;
      updateTransport();
      pushLog('info', '继续播放');
    }).catch(function (e) { toast(e.message, 'err'); });
  }

  function stopPlay(silent) {
    req('play.stop', {}, 10000).then(function () {
      stopLocalPlaybackState();
      pushLog('info', '播放已停止，设备已恢复真实定位');
      if (!silent) toast('已停止播放，设备恢复真实定位', 'ok');
    }).catch(function (e) {
      stopLocalPlaybackState();
      if (!silent) toast(e.message, 'err');
    });
  }

  function stopLocalPlaybackState() {
    S.playing = false;
    S.paused = false;
    S.status = null;
    S.statusSeq = null;
    S.stoppedAt = Date.now();
    S.progress = 0;
    S.distanceDone = 0;
    $('#progressRange').value = 0;
    $('#liveProgress').textContent = '0.0%';
    $('#timeElapsed').textContent = '00:00';
    $('#timeRemaining').textContent = '00:00';
    $('#liveIndex').textContent = '0 / 0';
    $('#liveSpeed').textContent = '--';
    $('#chipSpeed').textContent = '速度 --';
    updateProgressChip();
    S.map.setPlayedIndex(-1);
    updateTransport();
  }

  function seek(progress) {
    progress = Geo.clamp(progress, 0, 1);
    req('play.seek', { progress: progress }, 8000).then(function () {
      S.progress = progress;
    }).catch(function (e) {
      toast(e.message, 'err', 3800);
    });
  }

  function setSpeedScale(v, opts) {
    S.speedScale = Geo.clamp(+v, Geo.SPEED_MIN, Geo.SPEED_MAX);
    $('#speedValue').textContent = Geo.fmtSpeedScale(S.speedScale);
    $('#liveScale').textContent = Geo.fmtSpeedScale(S.speedScale);
    $('#speedRange').value = Math.round(Geo.speedToSlider(S.speedScale) * 1000);
    if (S.route) S.route.speed_scale = S.speedScale;
    updateStats();
    savePrefs();
    if (S.playing && !(opts && opts.silent)) {
      req('play.speed', { speed_scale: S.speedScale }, 8000).catch(function (e) {
        pushLog('warn', '设置播放倍速失败：' + e.message);
      });
    }
  }

  function toggleLoop() {
    S.loop = $('#loopToggle').checked;
    savePrefs();
    if (S.playing || S.paused) {
      // 契约没有单独的 loop 命令：带上当前进度重新开始播放以应用循环设置
      var route = ensureRoute();
      if (!route) return;
      var idx = S.status && isFinite(S.status.index) ? S.status.index : 0;
      route.loop = S.loop;
      pushLog('info', '循环设置已变更，正在按当前位置重新开始播放…');
      req('play.start', {
        route: routePayload(route),
        speed_scale: S.speedScale,
        loop: S.loop,
        start_index: idx
      }, 15000).then(function () {
        S.playing = true;
        S.paused = false;
        updateTransport();
      }).catch(function (e) {
        toast('应用循环设置失败：' + e.message, 'err');
      });
    } else {
      pushLog('info', S.loop ? '已开启循环播放' : '已关闭循环播放');
    }
  }

  function updateProgressChip() {
    var pct = (Geo.clamp(S.progress, 0, 1) * 100).toFixed(1) + '%';
    var d = (S.status && isFinite(S.status.distance_done)) ? S.status.distance_done : S.distanceDone;
    $('#chipProgress').textContent = '进度 ' + pct + (isFinite(d) && d > 0 ? ' · 已走 ' + Geo.fmtDistance(d) : '');
  }

  function onStatus(st) {
    // play.stop 之后服务端可能还会推一条最终状态（progress=1）。用户已点「停止」，
    // 这里在 4 秒窗口内忽略非播放态的状态推送，避免进度条/定位点被“弹回终点”。
    if (S.stoppedAt) {
      if (st.playing) {
        S.stoppedAt = 0;
      } else if (Date.now() - S.stoppedAt < 4000) {
        updateTransport();
        return;
      } else {
        S.stoppedAt = 0;
      }
    }
    // seq 自增：丢弃乱序/重复的旧状态
    if (isFinite(st.seq)) {
      if (S.statusSeq != null && st.seq <= S.statusSeq) return;
      S.statusSeq = st.seq;
    }
    S.status = st;
    var wasPlaying = S.playing;
    S.playing = !!st.playing;
    S.paused = !!st.paused;
    // 服务端回报的真实感参数（可能有别的客户端改过），状态变化时同步到开关
    if (st.tuning) {
      var t = st.tuning;
      var changed = (typeof t.drift_enabled === 'boolean' && t.drift_enabled !== S.driftEnabled) ||
                    (isFinite(t.drift_m) && Math.round(t.drift_m) !== S.driftM) ||
                    (typeof t.speed_jitter_enabled === 'boolean' && t.speed_jitter_enabled !== S.jitterEnabled) ||
                    (isFinite(t.speed_jitter_percent) && Math.round(t.speed_jitter_percent) !== S.jitterPercent) ||
                    (typeof t.timing_jitter_enabled === 'boolean' && t.timing_jitter_enabled !== S.timingJitter) ||
                    (typeof t.derive_speed === 'boolean' && t.derive_speed !== S.deriveSpeed) ||
                    (typeof t.gross_error_enabled === 'boolean' && t.gross_error_enabled !== S.grossError);
      if (changed) {
        if (typeof t.drift_enabled === 'boolean') S.driftEnabled = t.drift_enabled;
        if (isFinite(t.drift_m)) S.driftM = Math.round(t.drift_m);
        if (typeof t.speed_jitter_enabled === 'boolean') S.jitterEnabled = t.speed_jitter_enabled;
        if (isFinite(t.speed_jitter_percent)) S.jitterPercent = Math.round(t.speed_jitter_percent);
        if (typeof t.timing_jitter_enabled === 'boolean') S.timingJitter = t.timing_jitter_enabled;
        if (typeof t.derive_speed === 'boolean') S.deriveSpeed = t.derive_speed;
        if (typeof t.gross_error_enabled === 'boolean') S.grossError = t.gross_error_enabled;
        if (typeof applyTuningUi === 'function') applyTuningUi();
      }
    }
    if (isFinite(st.speed_scale) && Math.abs(st.speed_scale - S.speedScale) > 1e-6 && !S.speedDragging) {
      // 服务端为准（例如其他客户端改过倍速）
      S.speedScale = Geo.clamp(st.speed_scale, Geo.SPEED_MIN, Geo.SPEED_MAX);
      applyPrefsToUI();
      updateStats();
    }
    if (isFinite(st.progress) && !S.scrubbing) {
      S.progress = st.progress;
      $('#progressRange').value = Math.round(st.progress * 1000);
      $('#liveProgress').textContent = (st.progress * 100).toFixed(1) + '%';
      updateProgressChip();
    }
    if (isFinite(st.elapsed)) $('#timeElapsed').textContent = Geo.fmtDuration(st.elapsed);
    if (isFinite(st.remaining)) $('#timeRemaining').textContent = Geo.fmtDuration(st.remaining);
    if (isFinite(st.index)) {
      var total = isFinite(st.total) ? st.total : (currentPath() || []).length;
      $('#liveIndex').textContent = st.index + ' / ' + total;
      S.map.setPlayedIndex(st.index);
    }
    if (isFinite(st.lng) && isFinite(st.lat)) {
      S.map.setCurrent(st.lng, st.lat);
      $('#liveCoord').textContent = Geo.fmtLngLat(st.lng, st.lat, 6);
      $('#chipCoord').textContent = Geo.fmtLngLat(st.lng, st.lat, 6);
    }
    if (isFinite(st.distance_done)) S.distanceDone = st.distance_done;
    updateProgressChip();
    if (st.loop != null) {
      S.loop = !!st.loop;
      $('#loopToggle').checked = S.loop;
    }
    if (!wasPlaying && S.playing) pushLog('info', '轨迹播放中…');
    if (wasPlaying && !S.playing && st.progress >= 1) {
      pushLog('success', '轨迹播放完成' + (S.loop ? '（循环继续）' : ''));
    }
    updateTransport();
  }

  function onLocation(loc) {
    S.lastLocation = loc;
    // play.stop / location.clear 后服务端会推 source:"cleared" 且 lng/lat 为 null
    if (loc.source === 'cleared' || loc.lng == null || loc.lat == null) {
      if (loc.source === 'cleared') {
        S.map.setCurrent(null);
        $('#liveCoord').textContent = '--';
        $('#chipCoord').textContent = '--';
        $('#liveSpeed').textContent = '--';
        $('#chipSpeed').textContent = '速度 --';
        pushLog('info', '设备已恢复真实定位');
      }
      return;
    }
    if (isFinite(loc.lng) && isFinite(loc.lat)) {
      S.map.setCurrent(loc.lng, loc.lat);
      $('#liveCoord').textContent = Geo.fmtLngLat(loc.lng, loc.lat, 6);
      $('#chipCoord').textContent = Geo.fmtLngLat(loc.lng, loc.lat, 6);
      if (loc.source === 'play') {
        S.map.pushTrack(loc.lng, loc.lat);
        $('#chipTrack').textContent = '轨迹点 ' + S.map.layers.track.length;
      }
    }
    if (isFinite(loc.speed)) {
      $('#liveSpeed').textContent = Geo.fmtSpeed(loc.speed);
      $('#chipSpeed').textContent = '速度 ' + Geo.fmtSpeed(loc.speed);
    }
    if (isFinite(loc.progress) && !S.scrubbing && !S.stoppedAt) {
      S.progress = loc.progress;
      $('#progressRange').value = Math.round(loc.progress * 1000);
      $('#liveProgress').textContent = (loc.progress * 100).toFixed(1) + '%';
      updateProgressChip();
    }
    if (loc.source === 'manual') {
      pushLog('success', '单点定位已下发：' + Geo.fmtLngLat(loc.lng, loc.lat, 6));
    }
  }

  function updateTransport() {
    var hasRoute = !!(S.route || S.drawnPath.length >= 2 || S.waypoints.length >= 2);
    var playing = S.playing && !S.paused;
    $('#btnPlay').disabled = playing || !hasRoute;
    $('#btnPause').disabled = !playing;
    $('#btnResume').disabled = !(S.playing && S.paused);
    $('#btnStop').disabled = !(S.playing || S.paused);
    $('#progressRange').disabled = !(S.playing || S.paused);

    var stateText = '未播放';
    var cls = '';
    if (S.playing && !S.paused) { stateText = '播放中'; cls = 'is-playing'; }
    else if (S.playing && S.paused) { stateText = '已暂停'; cls = 'is-paused'; }
    else if (S.progress >= 1 && hasRoute) { stateText = '已结束'; }
    $('#playStateText').textContent = stateText;
    $('#playStateText').className = 'play-state ' + cls;
    $('#chipPlayState').textContent = stateText;
    $('#chipPlayState').className = 'chip ' + cls;
  }

  function setSingleLocation(lng, lat, index) {
    req('location.set', { lng: Coord.round6(lng), lat: Coord.round6(lat) }, 10000).then(function () {
      S.map.setCurrent(lng, lat);
      pushLog('success', '已让设备定位到第 ' + (index + 1) + ' 个途经点：' + Geo.fmtLngLat(lng, lat, 6));
      toast('已下发单点定位', 'ok');
    }).catch(function (e) {
      pushLog('error', '单点定位失败：' + e.message);
      toast(e.message, 'err', 4600);
    });
  }

  function clearLocation() {
    confirmModal('恢复真实定位', '将停止模拟并让手机恢复 GPS 真实定位，确定继续？', function () {
      if (S.playing || S.paused) stopPlay(true);
      req('location.clear', {}, 10000).then(function () {
        pushLog('success', '已发送「恢复真实定位」');
        toast('设备已恢复真实定位', 'ok');
      }).catch(function (e) {
        pushLog('warn', '恢复真实定位失败：' + e.message);
        toast(e.message, 'err', 4600);
      });
    }, '恢复');
  }

  /* ================================================================== */
  /* GPX 导入导出                                                        */
  /* ================================================================== */

  function exportGpx() {
    var route = ensureRoute();
    if (!route) {
      toast('没有可导出的路线，请先添加途经点或绘制折线', 'warn', 4200);
      return;
    }
    var name = S.routeName || route.name || '未命名轨迹';
    var btn = $('#btnExportGpx');
    setBusy(btn, true, '导出中…');
    pushLog('info', '正在导出 GPX：' + name + '（' + route.path.length + ' 点）');
    fetch('/api/gpx/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({ route: routePayload(route), name: name })
    }).then(function (res) {
      var ct = res.headers.get('content-type') || '';
      if (ct.indexOf('json') >= 0) {
        return res.json().then(function (j) {
          throw new Error(j.error || '导出失败');
        });
      }
      if (!res.ok) throw new Error('导出失败 HTTP ' + res.status);
      return res.text().then(function (text) {
        var filename = (name.replace(/[\\/:*?"<>|]/g, '_') || 'route') + '.gpx';
        downloadText(text, filename, 'application/gpx+xml;charset=utf-8');
        pushLog('success', 'GPX 已开始下载：' + filename + '（' + text.length + ' 字节）');
        toast('GPX 已开始下载', 'ok');
      });
    }).catch(function (e) {
      pushLog('error', '导出 GPX 失败：' + e.message);
      toast(e.message, 'err', 4600);
    }).then(function () { setBusy(btn, false); });
  }

  function importGpx() {
    $('#gpxFile').value = '';
    $('#gpxFile').click();
  }

  function onGpxFile(ev) {
    var file = ev.target.files && ev.target.files[0];
    if (!file) return;
    pushLog('info', '正在导入 GPX：' + file.name + '（' + file.size + ' 字节）');
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result || '');
      fetch('/api/gpx/import', {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain; charset=utf-8' },
        body: text
      }).then(function (res) {
        return res.json();
      }).then(function (data) {
        if (!data || data.ok === false) throw new Error((data && data.error) || '导入失败');
        var wps = (data.waypoints || []).map(function (p) {
          return [Coord.round6(p[0]), Coord.round6(p[1])];
        });
        var full = (data.path || data.track || data.points || []).map(function (p) {
          return [Coord.round6(p[0]), Coord.round6(p[1])];
        });
        if (!wps.length && !full.length) throw new Error('GPX 中没有可用的坐标点');
        pushUndo();
        S.route = null;
        var geom = full.length >= wps.length ? full : wps;
        var MAX_WP = 200;
        if (geom.length > MAX_WP) {
          // 大轨迹（例如把导出的整条路线再导回来）：完整几何用于显示/播放/导出，
          // 途经点只保留起终点，否则列表会渲染上万行
          S.waypoints = [geom[0], geom[geom.length - 1]];
          S.drawnPath = geom;
        } else {
          S.waypoints = wps.length >= 2 ? wps : geom;
          // 服务端只回起点/终点时，用完整轨迹点保证导入后的形状可播放、可导出
          S.drawnPath = geom.length > S.waypoints.length ? geom : [];
        }
        S.selectedWp = -1;
        if (data.name) {
          S.routeName = data.name;
          $('#routeName').value = data.name;
          savePrefs();
        }
        syncMapLayers();
        renderWaypoints();
        updateStats();
        S.map.fitBounds(wps, { padding: 60 });
        var note = S.drawnPath.length ? '（完整几何 ' + S.drawnPath.length + ' 点，途经点保留 ' + S.waypoints.length + ' 个）' : '';
        pushLog('success', 'GPX 导入成功：' + (data.count || wps.length) + ' 个点' + note +
          (data.name ? '，名称「' + data.name + '」' : ''));
        toast('已导入 ' + (data.count || wps.length) + ' 个点' + (note ? '，轨迹已按完整几何载入' : ''), 'ok', 4200);
        if (wps.length >= 2) buildRoute();
      }).catch(function (e) {
        pushLog('error', 'GPX 导入失败：' + e.message);
        toast(e.message, 'err', 4600);
      });
    };
    reader.onerror = function () {
      toast('读取文件失败', 'err');
    };
    reader.readAsText(file);
  }

  /* ================================================================== */
  /* 本地保存的路线                                                      */
  /* ================================================================== */

  function renderSavedRoutes() {
    var list = readSavedRoutes();
    var sel = $('#savedRoutes');
    sel.innerHTML = '';
    if (!list.length) {
      var o = document.createElement('option');
      o.value = '';
      o.textContent = '（暂无保存的路线）';
      sel.appendChild(o);
      sel.disabled = true;
    } else {
      sel.disabled = false;
      list.forEach(function (it, i) {
        var op = document.createElement('option');
        op.value = String(i);
        op.textContent = (it.name || ('路线 ' + (i + 1))) + ' · ' +
          new Date(it.ts || Date.now()).toLocaleString('zh-CN', { hour12: false });
        sel.appendChild(op);
      });
    }
    $('#btnLoadRoute').disabled = !list.length;
    $('#btnDeleteSaved').disabled = !list.length;
  }

  function saveRouteLocal() {
    var route = ensureRoute();
    if (!route && !S.waypoints.length) {
      toast('没有可保存的内容', 'warn');
      return;
    }
    var name = ($('#routeName').value || '').trim() || ('轨迹 ' + new Date().toLocaleString('zh-CN', { hour12: false }));
    S.routeName = name;
    var list = readSavedRoutes();
    var payload = {
      name: name,
      ts: Date.now(),
      mode: S.mode,
      speed_scale: S.speedScale,
      loop: S.loop,
      waypoints: S.waypoints,
      drawnPath: S.drawnPath,
      route: route
    };
    var idx = -1;
    list.forEach(function (it, i) { if (it.name === name) idx = i; });
    if (idx >= 0) list[idx] = payload; else list.push(payload);
    if (writeSavedRoutes(list)) {
      renderSavedRoutes();
      savePrefs();
      pushLog('success', '路线已保存到浏览器本地：' + name);
      toast('已保存到本地：' + name, 'ok');
    }
  }

  function loadRouteLocal() {
    var list = readSavedRoutes();
    var i = parseInt($('#savedRoutes').value, 10);
    if (!isFinite(i) || !list[i]) { toast('请先选择要载入的路线', 'warn'); return; }
    var it = list[i];
    pushUndo();
    S.waypoints = (it.waypoints || []).map(function (p) { return [Coord.round6(p[0]), Coord.round6(p[1])]; });
    S.drawnPath = (it.drawnPath || []).map(function (p) { return [Coord.round6(p[0]), Coord.round6(p[1])]; });
    S.route = it.route || null;
    S.routeName = it.name || '';
    $('#routeName').value = S.routeName;
    if (it.mode && MODE_CN[it.mode]) {
      S.mode = it.mode;
      $$('#modeGroup .seg-btn').forEach(function (b) { b.classList.toggle('is-active', b.dataset.mode === S.mode); });
    }
    if (isFinite(it.speed_scale)) setSpeedScale(it.speed_scale, { silent: true });
    if (typeof it.loop === 'boolean') { S.loop = it.loop; $('#loopToggle').checked = S.loop; }
    syncMapLayers();
    renderWaypoints();
    updateStats();
    var pts = currentPath();
    if (pts && pts.length >= 2) S.map.fitBounds(pts, { padding: 60 });
    pushLog('success', '已载入本地路线：' + S.routeName);
    toast('已载入：' + S.routeName, 'ok');
  }

  function deleteSavedRoute() {
    var list = readSavedRoutes();
    var i = parseInt($('#savedRoutes').value, 10);
    if (!isFinite(i) || !list[i]) { toast('请先选择要删除的路线', 'warn'); return; }
    var name = list[i].name;
    confirmModal('删除本地路线', '确定删除保存的路线「' + esc(name) + '」？此操作不可恢复。', function () {
      list.splice(i, 1);
      writeSavedRoutes(list);
      renderSavedRoutes();
      pushLog('warn', '已删除本地路线：' + name);
    }, '删除');
  }

  /* ================================================================== */
  /* UI 绑定                                                             */
  /* ================================================================== */

  function bindUI() {
    // 顶部
    $('#btnDevicesRefresh').addEventListener('click', refreshDevices);
    $('#btnDeviceConnect').addEventListener('click', deviceConnect);
    $('#btnDeviceDisconnect').addEventListener('click', deviceDisconnect);

    // 模式
    $$('#modeGroup .seg-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        S.mode = b.dataset.mode;
        $$('#modeGroup .seg-btn').forEach(function (x) { x.classList.toggle('is-active', x === b); });
        savePrefs();
        updateStats();
        pushLog('info', '出行模式：' + (MODE_CN[S.mode] || S.mode));
        if (S.route && S.route.engine === 'osrm') {
          toast('已切换为' + MODE_CN[S.mode] + '，如需按新模式的路线请重新加载', 'info', 3600);
        }
      });
    });

    // 倍速
    var speedRange = $('#speedRange');
    speedRange.addEventListener('input', function () {
      S.speedDragging = true;
      var v = Geo.sliderToSpeed(speedRange.value / 1000);
      S.speedScale = v;
      $('#speedValue').textContent = Geo.fmtSpeedScale(v);
      $('#liveScale').textContent = Geo.fmtSpeedScale(v);
      updateStats();
    });
    speedRange.addEventListener('change', function () {
      S.speedDragging = false;
      setSpeedScale(Geo.sliderToSpeed(speedRange.value / 1000));
    });

    $('#loopToggle').addEventListener('change', toggleLoop);

    // ---- 真实感模拟：定位漂移 / 速度浮动 ----
    var driftToggle = $('#driftToggle');
    var driftRange = $('#driftRange');
    var jitterToggle = $('#jitterToggle');
    var jitterRange = $('#jitterRange');

    function applyTuningUi() {
      driftToggle.checked = !!S.driftEnabled;
      driftRange.value = S.driftM;
      driftRange.disabled = !S.driftEnabled;
      $('#driftValue').textContent = S.driftM + ' 米';
      jitterToggle.checked = !!S.jitterEnabled;
      jitterRange.value = S.jitterPercent;
      jitterRange.disabled = !S.jitterEnabled;
      $('#jitterValue').textContent = '±' + S.jitterPercent + '%';
      var tj = $('#timingJitterToggle');
      if (tj) tj.checked = !!S.timingJitter;
      var ds = $('#deriveSpeedToggle');
      if (ds) ds.checked = !!S.deriveSpeed;
      var ge = $('#grossErrorToggle');
      if (ge) ge.checked = !!S.grossError;
      var gr = $('#grossErrorRange');
      if (gr) { gr.value = S.grossErrorPercent; gr.disabled = !S.grossError; }
      var gv = $('#grossErrorValue');
      if (gv) gv.textContent = S.grossErrorPercent + '%';
    }

    function pushTuning(withLog) {
      applyTuningUi();
      savePrefs();
      if (!S.ws || !S.ws.isOpen()) return;
      req('play.tuning', {
        drift_enabled: !!S.driftEnabled,
        drift_m: Number(S.driftM),
        speed_jitter_enabled: !!S.jitterEnabled,
        speed_jitter: Number(S.jitterPercent),
        timing_jitter_enabled: !!S.timingJitter,
        derive_speed: !!S.deriveSpeed,
        gross_error_enabled: !!S.grossError,
        gross_error_probability: Number(S.grossErrorPercent) / 100,
        log: !!withLog
      }, 8000).catch(function (e) {
        pushLog('error', '真实感设置失败：' + e.message);
      });
    }

    driftToggle.addEventListener('change', function () {
      S.driftEnabled = driftToggle.checked;
      pushTuning(true);
      toast(S.driftEnabled ? ('已开启定位漂移（' + S.driftM + ' 米）') : '已关闭定位漂移', 'info', 2600);
    });
    driftRange.addEventListener('input', function () {
      S.driftM = Number(driftRange.value);
      $('#driftValue').textContent = S.driftM + ' 米';
    });
    driftRange.addEventListener('change', function () { pushTuning(true); });

    jitterToggle.addEventListener('change', function () {
      S.jitterEnabled = jitterToggle.checked;
      pushTuning(true);
      toast(S.jitterEnabled ? ('已开启速度浮动（±' + S.jitterPercent + '%）') : '已关闭速度浮动', 'info', 2600);
    });
    jitterRange.addEventListener('input', function () {
      S.jitterPercent = Number(jitterRange.value);
      $('#jitterValue').textContent = '±' + S.jitterPercent + '%';
    });
    jitterRange.addEventListener('change', function () { pushTuning(true); });

    var timingToggle = $('#timingJitterToggle');
    if (timingToggle) {
      timingToggle.addEventListener('change', function () {
        S.timingJitter = timingToggle.checked;
        pushTuning(true);
        toast(S.timingJitter ? '已开启发送节奏抖动（贴近真实 GPS）' : '已关闭发送节奏抖动（精确等间隔）', 'info', 2600);
      });
    }
    var deriveToggle = $('#deriveSpeedToggle');
    if (deriveToggle) {
      deriveToggle.addEventListener('change', function () {
        S.deriveSpeed = deriveToggle.checked;
        pushTuning(true);
        toast(S.deriveSpeed ? '上报速度由位移推算（与 iOS 一致）' : '上报速度用基准速度×浮动系数', 'info', 3000);
      });
    }
    var grossToggle = $('#grossErrorToggle');
    var grossRange = $('#grossErrorRange');
    if (grossToggle) {
      grossToggle.addEventListener('change', function () {
        S.grossError = grossToggle.checked;
        pushTuning(true);
        toast(S.grossError ? ('已开启粗差注入（' + S.grossErrorPercent + '% 概率）') : '已关闭粗差注入', 'info', 3000);
      });
    }
    if (grossRange) {
      grossRange.addEventListener('input', function () {
        S.grossErrorPercent = Number(grossRange.value);
        $('#grossErrorValue').textContent = S.grossErrorPercent + '%';
      });
      grossRange.addEventListener('change', function () { pushTuning(true); });
    }

    // ---- 校园跑 / 长跑预设：一键把参数调到适合运动类 App 的组合 ----
    function applyPreset(name) {
      if (name === 'campus') {
        S.mode = 'walking';
        S.driftEnabled = true; S.driftM = 4;
        S.jitterEnabled = true; S.jitterPercent = 8;
      } else {
        S.mode = 'cycling';
        S.driftEnabled = true; S.driftM = 6;
        S.jitterEnabled = true; S.jitterPercent = 10;
      }
      S.loop = false;
      $$('#modeGroup .seg-btn').forEach(function (x) {
        x.classList.toggle('is-active', x.dataset.mode === S.mode);
      });
      $('#loopToggle').checked = false;
      applyTuningUi();
      // 关键：用 1Hz 下发，与真实手机 GPS 给 App 的回调频率一致。
      // 发太快（5~10Hz）时每帧位移只有 0.14~0.28 米，运动类 App 会当成 GPS 抖动丢掉，
      // 结果就是"地图上点在动，但公里数一直是 0"。1Hz 下步行每帧位移 1.4 米，
      // 落在 App 里程过滤器的正常范围内。
      req('config.update', {
        config: {
          send_hz: 1,
          drift_enabled: true, drift_m: S.driftM,
          speed_jitter_enabled: true, speed_jitter: S.jitterPercent / 100
        }
      }, 8000).then(function (res) {
        if (res && res.config) applyConfig(res.config);
        var label = name === 'campus' ? '校园跑预设' : '长跑预设';
        toast(label + '已启用：' + MODE_CN[S.mode] + '，下发 1Hz，漂移 ' + S.driftM +
          ' 米，速度浮动 ±' + S.jitterPercent + '%', 'ok', 4400);
        pushLog('info', '已应用' + label + '（' + MODE_CN[S.mode] + '，1Hz，漂移 ' +
          S.driftM + 'm，浮动 ±' + S.jitterPercent + '%）');
      }).catch(function (e) {
        pushLog('error', '预设设置失败：' + e.message);
      });
      // 正在播放时让"真实感"参数立即生效
      if (S.playing || S.paused) pushTuning(false);
      savePrefs();
      updateStats();
    }

    $('#btnCampusPreset').addEventListener('click', function () { applyPreset('campus'); });
    $('#btnMarathonPreset').addEventListener('click', function () { applyPreset('run'); });

    // ---- 快速定位到坐标（校园跑：先把地图移到学校范围）----
    function parseCoord(text) {
      if (!text) return null;
      var parts = String(text).replace(/[，、]/g, ',').split(/[\s,]+/).filter(function (x) { return x !== ''; });
      if (parts.length < 2) return null;
      var lat = parseFloat(parts[0]), lng = parseFloat(parts[1]);
      if (!isFinite(lat) || !isFinite(lng)) return null;
      if (lat < -85 || lat > 85 || lng < -180 || lng > 180) return null;
      return { lat: lat, lng: lng };
    }

    var PLACES_KEY = 'iosloc.places.v1';

    function loadPlaces() {
      try { return JSON.parse(localStorage.getItem(PLACES_KEY) || '[]') || []; } catch (e) { return []; }
    }

    function renderPlaces() {
      var sel = $('#placeSelect');
      if (!sel) return;
      var list = loadPlaces();
      sel.innerHTML = '';
      if (!list.length) {
        var opt = document.createElement('option');
        opt.value = ''; opt.textContent = '（暂无记住的地点）';
        sel.appendChild(opt);
        return;
      }
      list.forEach(function (p, i) {
        var item = document.createElement('option');
        item.value = String(i);
        item.textContent = p.name + '（' + p.lat.toFixed(5) + ', ' + p.lng.toFixed(5) + '）';
        sel.appendChild(item);
      });
    }

    $('#btnJumpCoord').addEventListener('click', function () {
      var c = parseCoord($('#jumpCoord').value);
      if (!c) { toast('坐标格式不对，请填「纬度, 经度」，例如 30.2741, 120.1551', 'err', 4200); return; }
      S.map.setView(c.lng, c.lat, Math.max(S.map.getZoom(), 16), true);
      toast('已跳转到 ' + c.lat.toFixed(5) + ', ' + c.lng.toFixed(5), 'ok', 2600);
      pushLog('info', '地图已跳转到 ' + c.lat.toFixed(6) + ', ' + c.lng.toFixed(6));
    });

    $('#btnSavePlace').addEventListener('click', function () {
      var c = parseCoord($('#jumpCoord').value);
      if (!c) { toast('请先填一个有效坐标', 'err', 3200); return; }
      var name = prompt('给这个地点起个名字', '学校');
      if (name === null) return;
      var list = loadPlaces().filter(function (p) { return p.name !== name; });
      list.push({ name: name || '未命名', lat: c.lat, lng: c.lng });
      try { localStorage.setItem(PLACES_KEY, JSON.stringify(list)); } catch (e) { /* 忽略 */ }
      renderPlaces();
      toast('已记住「' + (name || '未命名') + '」', 'ok', 2400);
    });

    $('#btnGoPlace').addEventListener('click', function () {
      var idx = $('#placeSelect').value;
      var list = loadPlaces();
      if (idx === '' || !list[Number(idx)]) { toast('还没有记住的地点', 'warn', 3000); return; }
      var p = list[Number(idx)];
      S.map.setView(p.lng, p.lat, Math.max(S.map.getZoom(), 16), true);
      toast('已跳转到「' + p.name + '」', 'ok', 2600);
    });

    renderPlaces();

    // 轨迹名称
    $('#routeName').addEventListener('input', function () {
      S.routeName = $('#routeName').value;
      $('#routeMeta').textContent = S.routeName || '未命名轨迹';
      savePrefs();
    });

    // 路线按钮
    $('#btnBuildRoute').addEventListener('click', buildRoute);
    $('#btnClear').addEventListener('click', function () {
      confirmModal('清空轨迹', '将清空途经点、路线、绘制内容与轨迹点，确定继续？', function () {
        pushUndo();
        S.waypoints = [];
        S.route = null;
        S.drawnPath = [];
        S.polyline = [];
        S.drawing = false;
        S.selectedWp = -1;
        S.map.clearTrack();
        S.map.setCurrent(null);
        syncMapLayers();
        renderWaypoints();
        updateStats();
        setDrawMode('select');
        pushLog('warn', '已清空当前轨迹');
      }, '清空');
    });
    $('#btnExportGpx').addEventListener('click', exportGpx);
    $('#btnImportGpx').addEventListener('click', importGpx);
    $('#gpxFile').addEventListener('change', onGpxFile);
    $('#btnClearLocation').addEventListener('click', clearLocation);

    // 本地路线
    $('#btnSaveRoute').addEventListener('click', saveRouteLocal);
    $('#btnLoadRoute').addEventListener('click', loadRouteLocal);
    $('#btnDeleteSaved').addEventListener('click', deleteSavedRoute);
    $('#savedRoutes').addEventListener('dblclick', loadRouteLocal);

    // 底图
    $('#tileSourceSelect').addEventListener('change', function () {
      applyTileSource($('#tileSourceSelect').value, false);
    });

    // 绘制工具条
    $$('#drawToolbar .tool-btn[data-tool]').forEach(function (b) {
      b.addEventListener('click', function () {
        setDrawMode(b.dataset.tool);
        if (b.dataset.tool === 'polyline') startPolyline();
      });
    });
    $('#toolUndo').addEventListener('click', undo);
    $('#toolDelete').addEventListener('click', function () {
      if (S.selectedWp < 0) { toast('请先在地图或列表中选择一个途经点', 'warn'); return; }
      removeWaypoint(S.selectedWp);
    });
    $('#toolFit').addEventListener('click', function () {
      var pts = currentPath();
      if (pts && pts.length >= 2) S.map.fitBounds(pts, { padding: 60 });
      else toast('当前没有可全览的轨迹', 'warn');
    });

    // 播放条
    $('#btnPlay').addEventListener('click', startPlay);
    $('#btnPause').addEventListener('click', pausePlay);
    $('#btnResume').addEventListener('click', resumePlay);
    $('#btnStop').addEventListener('click', function () { stopPlay(false); });

    var pr = $('#progressRange');
    pr.addEventListener('input', function () {
      S.scrubbing = true;
      var p = pr.value / 1000;
      $('#liveProgress').textContent = (p * 100).toFixed(1) + '%';
      if (S.status && isFinite(S.status.elapsed) && isFinite(S.status.remaining)) {
        var total = S.status.elapsed + S.status.remaining;
        $('#timeElapsed').textContent = Geo.fmtDuration(total * p);
        $('#timeRemaining').textContent = Geo.fmtDuration(total * (1 - p));
      }
    });
    var commitSeek = function () {
      if (!S.scrubbing) return;
      S.scrubbing = false;
      seek(pr.value / 1000);
    };
    pr.addEventListener('change', commitSeek);
    pr.addEventListener('pointerup', commitSeek);

    // 设备与缓存（契约外的补充命令）
    $('#btnTunnelEnsure').addEventListener('click', tunnelEnsure);
    $('#btnTunnelManual').addEventListener('click', tunnelManual);
    // iOS 17 以下：挂载开发者镜像（后台执行，过程日志实时推送）
    $('#btnMountImage').addEventListener('click', function () {
      var btn = this;
      var dev = S.device;
      if (!dev) { toast('请先连接设备', 'err', 3000); return; }
      if ((dev.ios_major || 0) < 17) {
        toast('本工具只支持 iOS 17 及以上；你的设备是 iOS ' + dev.ios_version +
              '，其镜像需由 Apple 按构建号签发，请用 Xcode 连接一次自动准备', 'err', 6000);
        return;
      }
      btn.disabled = true;
      pushLog('info', '开始挂载开发者镜像：' + (dev.name || '') + '（iOS ' + (dev.ios_version || '?') + '）');
      toast('正在挂载开发者镜像，过程见右侧日志…', 'info', 3200);
      req('device.mount_image', {}, 20000).then(function () {
        // 结果通过 mounted 事件 / 日志推送，这里只做超时兜底恢复
        setTimeout(function () { btn.disabled = false; }, 3000);
      }).catch(function (e) {
        btn.disabled = false;
        pushLog('error', '挂载请求失败：' + e.message);
        toast(e.message, 'err', 4200);
      });
    });

    $('#btnTilesClear').addEventListener('click', tilesClear);

    // 日志面板
    $('#btnClearLog').addEventListener('click', function () {
      S.logs = [];
      lastLog = null;
      $('#logBody').innerHTML = '';
      $('#logCount').textContent = '0 / ' + LOG_MAX;
    });
    $('#btnFetchLogs').addEventListener('click', function () {
      req('log.list', { limit: 200 }, 8000).then(function (msg) {
        if (msg.lines) setLogs(msg.lines);
      }).catch(function (e) {
        toast('拉取日志失败：' + e.message, 'err');
      });
    });
    $('#btnToggleLog').addEventListener('click', toggleLogPanel);
    $('#logPanel').addEventListener('click', function (ev) {
      if (S.logCollapsed && !ev.target.closest('button')) toggleLogPanel();
    });

    // 键盘
    document.addEventListener('keydown', onKeyDown);
  }

  function tunnelEnsure() {
    var btn = $('#btnTunnelEnsure');
    setBusy(btn, true, '建立中…');
    pushLog('info', '正在请求建立隧道（iOS 17+ 需要管理员权限）…');
    req('tunnel.ensure', {}, 120000).then(function (msg) {
      if (msg.device) { S.device = msg.device; }
      if (msg.tunnel) {
        S.tunnel = msg.tunnel;
        if (S.device) S.device.tunnel = msg.tunnel.state || S.device.tunnel;
      }
      // need_tunnel === false：mock 模式或 iOS<17 经典通道，无需等待隧道
      if (msg.need_tunnel === false) {
        if (tunnelWatch) { clearInterval(tunnelWatch); tunnelWatch = null; }
        S.tunnelStuck = false;
        pushLog('info', '服务端无需建立 RSD 隧道：' + ((msg.tunnel && msg.tunnel.message) || '当前不需要隧道'));
      }
      renderDevice();
      updateBanner();
      syncDeviceFromServer(true);
      pushLog('success', '隧道指令已完成：' + ((msg.tunnel && (msg.tunnel.message || msg.tunnel.state)) || '已连接'));
      toast((msg.tunnel && msg.tunnel.message) || '隧道已建立', 'ok', 4200);
    }).catch(function (e) {
      pushLog('error', '建立隧道失败：' + e.message);
      toast(e.message, 'err', 5200);
    }).then(function () { setBusy(btn, false); });
  }

  function tunnelManual() {
    openModal({
      title: '手动填写 RSD 地址',
      okText: '连接',
      html:
        '<div class="field"><label>主机</label><input class="input" id="m_rsd_host" placeholder="例如 fd00::1 或 127.0.0.1"></div>' +
        '<div class="field"><label>端口</label><input class="input" id="m_rsd_port" placeholder="例如 51234"></div>' +
        '<div class="hint">iOS 17 及以上需要 RSD 隧道。可先用管理员权限执行 ' +
        '<code>pymobiledevice3 lockdown start-tunnel</code>，把输出的地址与端口填到这里。</div>',
      onOk: function () {
        var host = ($('#m_rsd_host').value || '').trim();
        var port = parseInt($('#m_rsd_port').value, 10);
        if (!host || !isFinite(port) || port <= 0 || port > 65535) {
          toast('请填写正确的主机与端口', 'err');
          return false;
        }
        var btn = $('#btnTunnelManual');
        setBusy(btn, true, '连接中…');
        req('tunnel.manual', { host: host, port: port }, 60000).then(function (msg) {
          if (msg.device) S.device = msg.device;
          if (msg.tunnel) {
            S.tunnel = msg.tunnel;
            if (S.device) S.device.tunnel = msg.tunnel.state || S.device.tunnel;
          }
          renderDevice();
          updateBanner();
          pushLog('success', '已按手动 RSD 地址连接：' + host + ':' + port);
          toast('已连接手动 RSD 地址', 'ok');
        }).catch(function (e) {
          pushLog('error', '手动 RSD 连接失败：' + e.message);
          toast(e.message, 'err', 5200);
        }).then(function () { setBusy(btn, false); });
        return true;
      }
    });
  }

  function tilesClear() {
    var btn = $('#btnTilesClear');
    setBusy(btn, true, '清理中…');
    req('tiles.clear', {}, 30000).then(function () {
      S.map.retryFailedTiles();
      pushLog('success', '服务端瓦片缓存已清理，正在重新拉取底图');
      toast('瓦片缓存已清理', 'ok');
    }).catch(function (e) {
      pushLog('warn', '清理瓦片缓存失败：' + e.message);
      toast(e.message, 'err', 4200);
    }).then(function () { setBusy(btn, false); });
  }

  function toggleLogPanel() {
    setLogCollapsed(!S.logCollapsed);
  }

  function setLogCollapsed(collapsed, silent) {
    S.logCollapsed = !!collapsed;
    $('#workspace').classList.toggle('log-collapsed', S.logCollapsed);
    var btn = $('#btnToggleLog');
    btn.textContent = S.logCollapsed ? '‹' : '折叠';
    btn.title = S.logCollapsed ? '展开日志面板' : '折叠日志面板';
    if (!S.logCollapsed) setTimeout(function () { $('#logBody').scrollTop = $('#logBody').scrollHeight; }, 30);
    if (S.map) setTimeout(function () { S.map.resize(); }, 60);
    if (!silent) savePrefs();
  }

  function onKeyDown(e) {
    var tag = (e.target && e.target.tagName) || '';
    var typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
    if (e.key === 'Escape') {
      if (!$('#modalMask').classList.contains('hidden')) { closeModal(); return; }
      if (S.drawing) { finishPolyline(); return; }
      if (typing) { e.target.blur(); return; }
      setDrawMode('select');
      return;
    }
    if (typing) return;
    if (e.key === ' ' || e.code === 'Space') {
      e.preventDefault();
      if (S.playing && !S.paused) pausePlay();
      else if (S.playing && S.paused) resumePlay();
      else startPlay();
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      if (S.selectedWp >= 0) { e.preventDefault(); removeWaypoint(S.selectedWp); }
    } else if (e.key === '+' || e.key === '=') {
      S.map.setZoom(S.map.getZoom() + 1);
    } else if (e.key === '-' || e.key === '_') {
      S.map.setZoom(S.map.getZoom() - 1);
    }
  }

  /* ================================================================== */

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // 便于在控制台排查
  window.__IOSLOC = { S: S, toast: toast, pushLog: pushLog };
})();
