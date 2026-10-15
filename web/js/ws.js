/*!
 * ws.js — WebSocket 客户端（协议见 docs/PROTOCOL.md §3）
 *
 * 能力：
 *   - 连接 ws://127.0.0.1:8787/ws，连接后立即发 hello
 *   - 自动重连：指数退避 400ms → 10s（带抖动），连接成功后重置
 *   - 心跳：每 10s 发 ping；连续 3 次没有 pong 主动断开重连
 *   - 命令请求-应答：request(cmd, fields, timeout) → Promise（ack ok:false 会 reject）
 *   - 事件订阅：on(type, fn) / on('*', fn) / onState(fn)
 *
 * 导出：window.WSClient
 */
(function (global) {
  'use strict';

  var CMD_TIMEOUT = 8000;
  var TICK_TIMEOUT = 6000;
  var HEARTBEAT_MS = 10000;
  var MAX_BACKOFF = 10000;

  function nowMs() {
    return (global.performance && global.performance.now) ? global.performance.now() : Date.now();
  }

  function WSClient(opts) {
    opts = opts || {};
    this.url = opts.url || defaultUrl();
    // 部分服务端实现会在 101 响应里回带未请求的 Sec-WebSocket-Protocol，
    // 浏览器按 RFC 6455 会直接判定握手失败。这里在首次连接失败后自动带上 v1 再试一次。
    this.subprotocols = opts.subprotocols || null;
    this.everOpened = false;
    this.protocolFallback = false;
    this.handlers = Object.create(null);   // type -> [fn]
    this.stateHandlers = [];
    this.state = 'closed';                 // closed | connecting | open
    this.sock = null;
    this.seq = 0;
    this.pending = Object.create(null);    // id -> {resolve, reject, timer, cmd}
    this.attempt = 0;
    this.closedByUser = false;
    this.reconnectTimer = null;
    this.heartbeatTimer = null;
    this.lastPongAt = 0;
    // 最近一次收到任何入站消息的时间（不只是 pong）：用于判断连接是否真的静默
    this.lastInboundAt = 0;
    this.missedPings = 0;
    this.manualClose = false;
  }

  function defaultUrl() {
    var loc = global.location;
    var host = (loc && loc.hostname) ? loc.hostname : '127.0.0.1';
    var port = (loc && loc.port) ? loc.port : '8787';
    var proto = (loc && loc.protocol === 'https:') ? 'wss:' : 'ws:';
    return proto + '//' + host + ':' + port + '/ws';
  }

  WSClient.prototype.on = function (type, fn) {
    (this.handlers[type] || (this.handlers[type] = [])).push(fn);
    return this;
  };

  WSClient.prototype.off = function (type, fn) {
    var list = this.handlers[type];
    if (!list) return this;
    var i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
    return this;
  };

  WSClient.prototype.onState = function (fn) {
    this.stateHandlers.push(fn);
    return this;
  };

  WSClient.prototype._setState = function (s, detail) {
    if (this.state === s && !detail) return;
    this.state = s;
    for (var i = 0; i < this.stateHandlers.length; i++) {
      try { this.stateHandlers[i](s, detail); } catch (e) { /* 忽略订阅者异常 */ }
    }
  };

  WSClient.prototype._emit = function (type, payload, raw) {
    var list = this.handlers[type];
    if (list) {
      for (var i = 0; i < list.length; i++) {
        try { list[i](payload, raw); } catch (e) {
          if (global.console) global.console.error('[ws] 事件处理异常 ' + type, e);
        }
      }
    }
    var all = this.handlers['*'];
    if (all) {
      for (var j = 0; j < all.length; j++) {
        try { all[j](payload, raw, type); } catch (e2) { /* 忽略 */ }
      }
    }
  };

  WSClient.prototype.connect = function () {
    var self = this;
    if (this.sock && (this.sock.readyState === 0 || this.sock.readyState === 1)) return this;
    this.closedByUser = false;
    this.manualClose = false;
    this._setState('connecting');
    var sock;
    try {
      sock = this.subprotocols ? new WebSocket(this.url, this.subprotocols) : new WebSocket(this.url);
    } catch (e) {
      this._scheduleReconnect('WebSocket 构造失败：' + e.message);
      return this;
    }
    this.sock = sock;

    sock.onopen = function () {
      self.attempt = 0;
      self.everOpened = true;
      self.lastPongAt = nowMs();
      self.lastInboundAt = nowMs();
      self.missedPings = 0;
      self._setState('open');
      self._emit('open', { url: self.url });
      // hello 用请求-应答发送：服务端把 welcome 数据并回在 hello 的 ack 里
      // （契约里 hello → welcome；两种形态都兼容：ack 命中后归一化成 welcome 事件）
      self.request('hello', { client: 'web' }, 8000).then(function (msg) {
        self._emit('welcome', msg);
      }).catch(function (err) {
        self._emit('hello_failed', { error: err && err.message });
      });
      self._startHeartbeat();
    };

    sock.onmessage = function (ev) {
      var msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        self._emit('error', { error: '服务端消息不是合法 JSON', raw: String(ev.data).slice(0, 200) });
        return;
      }
      if (!msg || typeof msg !== 'object') return;
      self._dispatch(msg);
    };

    sock.onerror = function () {
      self._emit('sockerror', { url: self.url });
    };

    sock.onclose = function (ev) {
      self._stopHeartbeat();
      self.sock = null;
      if (self.manualClose || self.closedByUser) {
        self._setState('closed', ev && ev.reason);
        self._emit('close', { code: ev && ev.code, reason: ev && ev.reason, manual: true });
        return;
      }
      self._setState('closed', ev && ev.reason);
      self._emit('close', { code: ev && ev.code, reason: ev && ev.reason, manual: false });
      self._scheduleReconnect(ev && ev.reason ? ('连接关闭：' + ev.reason) : '连接已关闭');
    };

    return this;
  };

  WSClient.prototype._dispatch = function (msg) {
    // 任何入站消息都证明服务端还活着。不能只认 pong：
    // 服务端执行 device.connect（iOS 17+ 建 RSD 隧道，最长约 1 分钟）时会持续推 log 帧，
    // 但由于该命令是同步阻塞的，ping 的 pong 可能迟迟回不来——只认 pong 会误判断线。
    this.lastInboundAt = nowMs();
    this.missedPings = 0;
    if (msg.type === 'pong') {
      this.lastPongAt = nowMs();
    }
    // 请求-应答：只要带 id 且命中 pending，就作为该请求的应答
    if (msg.id && this.pending[msg.id]) {
      var p = this.pending[msg.id];
      delete this.pending[msg.id];
      if (p.timer) clearTimeout(p.timer);
      if (msg.type === 'ack' && msg.ok === false) {
        var err = new Error(msg.error || '服务端拒绝了该命令');
        err.code = msg.code || 'ack_failed';
        err.cmd = msg.cmd || p.cmd;
        p.reject(err);
      } else {
        p.resolve(msg);
      }
      // 应答同时也是事件（例如 ack 之外带状态），继续广播
    }
    this._emit(msg.type || 'unknown', msg, msg);
  };

  /** 发送原始对象（无应答等待） */
  WSClient.prototype.send = function (obj) {
    if (!this.sock || this.sock.readyState !== 1) return false;
    try {
      this.sock.send(JSON.stringify(obj));
      return true;
    } catch (e) {
      this._emit('error', { error: '发送失败：' + e.message });
      return false;
    }
  };

  WSClient.prototype.nextId = function (prefix) {
    this.seq += 1;
    return (prefix || 'c') + this.seq + '-' + Math.floor(Math.random() * 1e6).toString(36);
  };

  /**
   * 命令请求-应答。
   * @param {string} cmd  命令 type，例如 'play.start'
   * @param {object} [fields] 附加字段
   * @param {number} [timeout] 毫秒，默认 8000
   * @returns {Promise<object>} 应答消息；ok:false 或超时 → reject(Error)
   */
  WSClient.prototype.request = function (cmd, fields, timeout) {
    var self = this;
    var id = this.nextId();
    var payload = Object.assign({ type: cmd, id: id }, fields || {});
    return new Promise(function (resolve, reject) {
      if (!self.sock || self.sock.readyState !== 1) {
        var e = new Error('尚未连接到本地服务，请确认服务已启动');
        e.code = 'ws_closed';
        reject(e);
        return;
      }
      var timer = setTimeout(function () {
        if (self.pending[id]) {
          delete self.pending[id];
          var te = new Error('命令超时（' + cmd + '）');
          te.code = 'timeout';
          te.cmd = cmd;
          reject(te);
        }
      }, timeout || CMD_TIMEOUT);
      self.pending[id] = { resolve: resolve, reject: reject, timer: timer, cmd: cmd };
      var ok = self.send(payload);
      if (!ok) {
        clearTimeout(timer);
        delete self.pending[id];
        var se = new Error('发送失败：连接不可用');
        se.code = 'ws_send_failed';
        reject(se);
      }
    });
  };

  WSClient.prototype.isOpen = function () {
    return !!this.sock && this.sock.readyState === 1;
  };

  WSClient.prototype._startHeartbeat = function () {
    var self = this;
    this._stopHeartbeat();
    this.heartbeatTimer = setInterval(function () {
      if (!self.isOpen()) return;
      var lastSeen = Math.max(self.lastInboundAt || 0, self.lastPongAt || 0);
      var idle = nowMs() - lastSeen;
      // 只要还在收到任何消息（log/status/location/ack）就说明连接健康，不累计丢包
      if (idle > HEARTBEAT_MS * 3) {
        self.missedPings += 1;
      } else {
        self.missedPings = 0;
      }
      if (self.missedPings >= 3) {
        self._emit('error', {
          error: '连接静默超过 ' + Math.round(HEARTBEAT_MS * 9 / 1000) + ' 秒，正在重建连接'
        });
        try { self.sock.close(); } catch (e) { /* 忽略 */ }
        return;
      }
      self.request('ping', {}, TICK_TIMEOUT).catch(function () { /* ping 超时由 missedPings 兜底 */ });
    }, HEARTBEAT_MS);
  };

  WSClient.prototype._stopHeartbeat = function () {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  };

  WSClient.prototype._scheduleReconnect = function (reason) {
    var self = this;
    if (this.closedByUser || this.reconnectTimer) return;
    if (!this.everOpened && !this.protocolFallback && !this.subprotocols) {
      this.protocolFallback = true;
      this.subprotocols = ['v1'];
      this.attempt = 0;
      this._emit('protocol_fallback', { reason: reason || '' });
    }
    var delay = Math.min(MAX_BACKOFF, 400 * Math.pow(2, this.attempt)) + Math.floor(Math.random() * 250);
    this.attempt += 1;
    this._emit('reconnecting', { delay: delay, attempt: this.attempt, reason: reason });
    this.reconnectTimer = setTimeout(function () {
      self.reconnectTimer = null;
      self.connect();
    }, delay);
  };

  WSClient.prototype.close = function () {
    this.closedByUser = true;
    this.manualClose = true;
    this._stopHeartbeat();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    var ids = Object.keys(this.pending);
    for (var i = 0; i < ids.length; i++) {
      var p = this.pending[ids[i]];
      if (p && p.timer) clearTimeout(p.timer);
      if (p) p.reject(new Error('连接已关闭'));
      delete this.pending[ids[i]];
    }
    if (this.sock) {
      try { this.sock.close(); } catch (e) { /* 忽略 */ }
    }
    this.sock = null;
    this._setState('closed', 'manual');
  };

  /** 立即重连（用户点「重试」时用） */
  WSClient.prototype.reconnectNow = function () {
    this.attempt = 0;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.sock) {
      try { this.sock.close(); } catch (e) { /* 忽略 */ }
      this.sock = null;
    }
    this.connect();
  };

  global.WSClient = WSClient;
})(typeof window !== 'undefined' ? window : globalThis);
