/*!
 * map.js — 自绘滑动地图引擎（Canvas 2D，无任何外部库）
 *
 * 特性：
 *   - Web Mercator 投影，256px 瓦片，z3..z19（受瓦片源 max_zoom 限制），devicePixelRatio 适配
 *   - 拖拽平移（带惯性）、滚轮以鼠标为锚点缩放、双击放大、缩放控件、比例尺、经纬度/层级读数
 *   - 瓦片走本地服务端代理 GET /api/tiles/{source}/{z}/{x}/{y}.png
 *     失败占位 + 最多 3 次退避重试 + 连续失败自动暂停加载（防止重试风暴）+ 一键重试
 *   - 瓦片 LRU 内存缓存，淘汰时释放 Image
 *   - 图层：路径折线（path）、轨迹折线、已播放段高亮、途经点（可拖拽/选中）、起终点特殊图标、当前定位点（脉冲）
 *
 * 坐标：对外全部 WGS84（与 docs/PROTOCOL.md 一致）。瓦片源为 GCJ-02 时，
 *       仅在「投影/绘制」这一步用 Coord.toDisplay 偏移，鼠标取点用 Coord.fromDisplay 还原。
 *
 * 导出：window.SlippyMap
 */
(function (global) {
  'use strict';

  var TILE = 256;
  var MIN_ZOOM = 3;
  var HARD_MAX_ZOOM = 19;
  var MAX_PARALLEL_LOADS = 6;
  var MAX_TILE_TRIES = 3;
  var CACHE_LIMIT = 360;          // 内存中最多保留的瓦片张数
  var FAIL_STREAK_LIMIT = 6;      // 连续失败达到该值 → 暂停 6s
  var LOAD_PAUSE_MS = 6000;

  var SCALE_STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000,
    20000, 50000, 100000, 200000, 500000, 1000000, 2000000, 5000000];

  var COLORS = {
    bg: '#0b0f14',
    tileEmpty: '#141a22',
    tileBorder: '#1e2632',
    tileFail: '#2a1a20',
    tileFailMark: '#f85149',
    pathOutline: '#08111f',
    path: '#2f81f7',
    playedOutline: '#08110a',
    played: '#3fb950',
    track: '#d29922',
    wpStart: '#2ea043',
    wpEnd: '#f85149',
    wpMid: '#1f6feb',
    wpSelected: '#ffd33d',
    current: '#58a6ff'
  };

  function now() {
    return (global.performance && global.performance.now) ? global.performance.now() : Date.now();
  }

  /* ------------------------------------------------------------------ */
  /* 构造                                                                */
  /* ------------------------------------------------------------------ */

  function SlippyMap(container, opts) {
    opts = opts || {};
    var self = this;
    this.el = typeof container === 'string' ? document.querySelector(container) : container;
    if (!this.el) throw new Error('地图容器不存在');

    this.el.classList.add('slippy-map');
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'map-canvas';
    this.el.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d');

    // 视图状态
    this.center = { lng: 116.397428, lat: 39.90923 };
    this.zoom = opts.zoom || 12;
    this.minZoom = opts.minZoom || MIN_ZOOM;
    this.maxZoom = HARD_MAX_ZOOM;

    // 瓦片源
    this.source = {
      key: '', name: '未选择瓦片源', url: '', max_zoom: 18,
      attribution: '', datum: 'wgs84'
    };

    // 图层数据（全部 WGS84）
    this.layers = {
      path: [],
      pathColor: COLORS.path,
      track: [],
      waypoints: [],
      selected: -1,
      current: null,
      playedIndex: -1,
      showTrack: true
    };

    this.waypointsDraggable = true;
    this.doubleClickZoom = true;
    this.drawMode = 'select';

    // 交互状态
    this.pointers = new Map();
    this.dragging = false;
    this.dragStart = null;
    this.dragWp = null;
    this.pinch = null;
    this.velocity = { x: 0, y: 0 };
    this.inertiaOn = false;
    this.mouse = null;

    // 瓦片状态
    this.tiles = new Map();          // key -> entry
    this.queue = [];
    this.activeLoads = 0;
    this.failStreak = 0;
    this.failTotal = 0;
    this.loadPausedUntil = 0;

    // 渲染状态
    this.w = 1; this.h = 1; this.dpr = 1;
    this._dirty = true;
    this._rafId = null;
    this._animOn = false;
    this.handlers = Object.create(null);

    this._buildControls();
    this._bindEvents();
    this.resize();

    if (global.ResizeObserver) {
      this._ro = new ResizeObserver(function () { self.resize(); });
      this._ro.observe(this.el);
    } else {
      this._onWinResize = function () { self.resize(); };
      global.addEventListener('resize', this._onWinResize);
    }
  }

  /* ------------------------------------------------------------------ */
  /* 事件订阅                                                            */
  /* ------------------------------------------------------------------ */

  SlippyMap.prototype.on = function (type, fn) {
    (this.handlers[type] || (this.handlers[type] = [])).push(fn);
    return this;
  };

  SlippyMap.prototype._emit = function (type, data) {
    var list = this.handlers[type];
    if (!list) return;
    for (var i = 0; i < list.length; i++) {
      try { list[i](data); } catch (e) {
        if (global.console) global.console.error('[map] 事件异常 ' + type, e);
      }
    }
  };

  /* ------------------------------------------------------------------ */
  /* 投影（Web Mercator）                                                */
  /* ------------------------------------------------------------------ */

  SlippyMap.prototype._projectUnit = function (lng, lat) {
    var la = Math.max(-85.05112878, Math.min(85.05112878, +lat));
    var x = (lng + 180) / 360;
    var s = Math.sin(la * Math.PI / 180);
    var y = 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
    return { x: x, y: y };
  };

  SlippyMap.prototype._unprojectUnit = function (x, y) {
    var lng = x * 360 - 180;
    var n = Math.PI - 2 * Math.PI * y;
    var lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
    return { lng: lng, lat: lat };
  };

  /** WGS84 → 显示基准 unit（GCJ-02 源会做一次偏移；按坐标缓存，避免每帧重复转换上万次） */
  SlippyMap.prototype._displayUnit = function (lng, lat) {
    var datum = this.source.datum;
    if (this._duDatum !== datum) {
      this._duDatum = datum;
      this._duCache = new Map();
    }
    var key = lng + ',' + lat;
    var hit = this._duCache.get(key);
    if (hit) return hit;
    var d = Coord.toDisplay(lng, lat, datum);
    var u = this._projectUnit(d[0], d[1]);
    if (this._duCache.size > 40000) this._duCache.clear();
    this._duCache.set(key, u);
    return u;
  };

  /** 显示基准 unit → WGS84 */
  SlippyMap.prototype._unitToWgs = function (u) {
    var ll = this._unprojectUnit(u.x, u.y);
    var w = Coord.fromDisplay(ll.lng, ll.lat, this.source.datum);
    return { lng: w[0], lat: w[1] };
  };

  SlippyMap.prototype._worldPx = function (zoom) {
    if (zoom == null || zoom === this.zoom) {
      if (this._wpZoom !== this.zoom) {
        this._wpZoom = this.zoom;
        this._wp = TILE * Math.pow(2, this.zoom);
      }
      return this._wp;
    }
    return TILE * Math.pow(2, zoom);
  };

  SlippyMap.prototype._centerUnit = function () {
    var key = this.center.lng + ',' + this.center.lat + ',' + this.source.datum;
    if (this._cuKey === key && this._cu) return this._cu;
    this._cu = this._displayUnit(this.center.lng, this.center.lat);
    this._cuKey = key;
    return this._cu;
  };

  /** WGS84 → 屏幕 CSS 像素坐标 */
  SlippyMap.prototype.lngLatToPoint = function (lng, lat) {
    var c = this._centerUnit();
    var u = this._displayUnit(lng, lat);
    var wp = this._worldPx();
    return { x: (u.x - c.x) * wp + this.w / 2, y: (u.y - c.y) * wp + this.h / 2 };
  };

  /** 屏幕 CSS 像素坐标 → WGS84 */
  SlippyMap.prototype.pointToLngLat = function (x, y) {
    var c = this._centerUnit();
    var wp = this._worldPx();
    var u = { x: c.x + (x - this.w / 2) / wp, y: c.y + (y - this.h / 2) / wp };
    return this._unitToWgs(u);
  };

  SlippyMap.prototype.getCenter = function () {
    return { lng: this.center.lng, lat: this.center.lat };
  };

  SlippyMap.prototype.getZoom = function () { return this.zoom; };

  SlippyMap.prototype.setView = function (lng, lat, zoom, silent) {
    if (isFinite(lng)) this.center.lng = +lng;
    if (isFinite(lat)) this.center.lat = Math.max(-85, Math.min(85, +lat));
    if (isFinite(zoom)) this.zoom = this._clampZoom(+zoom);
    this.invalidate();
    if (!silent) this._emit('viewchange', { center: this.getCenter(), zoom: this.zoom });
    return this;
  };

  SlippyMap.prototype.setZoom = function (z, anchor) {
    var target = Math.max(this.minZoom, Math.min(this.maxZoom, +z));
    if (target === this.zoom) return this;
    var ax = anchor ? anchor.x : this.w / 2;
    var ay = anchor ? anchor.y : this.h / 2;
    var before = this._unitAtPoint(ax, ay);
    this.zoom = target;
    this._applyAnchor(before, ax, ay);
    this.invalidate();
    this._emit('zoomend', { zoom: this.zoom, center: this.getCenter() });
    return this;
  };

  SlippyMap.prototype._clampZoom = function (z) {
    return Math.max(this.minZoom, Math.min(this.maxZoom, z));
  };

  SlippyMap.prototype._unitAtPoint = function (x, y) {
    var c = this._centerUnit();
    var wp = this._worldPx();
    return { x: c.x + (x - this.w / 2) / wp, y: c.y + (y - this.h / 2) / wp };
  };

  /** 缩放后让点 (x,y) 处的地理位置保持不变 */
  SlippyMap.prototype._applyAnchor = function (unit, x, y) {
    var wp = this._worldPx();
    var cx = unit.x - (x - this.w / 2) / wp;
    var cy = unit.y - (y - this.h / 2) / wp;
    var ll = this._unitToWgs({ x: cx, y: cy });
    this.center.lng = ll.lng;
    this.center.lat = ll.lat;
  };

  /** 让一组 WGS84 点全部可见 */
  SlippyMap.prototype.fitBounds = function (points, opts) {
    opts = opts || {};
    if (!points || !points.length) return this;
    var pad = opts.padding == null ? 48 : opts.padding;
    var minU = { x: Infinity, y: Infinity }, maxU = { x: -Infinity, y: -Infinity };
    var i, u;
    for (i = 0; i < points.length; i++) {
      var p = points[i];
      if (!p || !isFinite(p[0]) || !isFinite(p[1])) continue;
      u = this._displayUnit(p[0], p[1]);
      if (u.x < minU.x) minU.x = u.x;
      if (u.y < minU.y) minU.y = u.y;
      if (u.x > maxU.x) maxU.x = u.x;
      if (u.y > maxU.y) maxU.y = u.y;
    }
    if (!isFinite(minU.x) || !isFinite(maxU.x)) return this;
    var dw = Math.max(maxU.x - minU.x, 1e-9);
    var dh = Math.max(maxU.y - minU.y, 1e-9);
    var availW = Math.max(40, this.w - pad * 2);
    var availH = Math.max(40, this.h - pad * 2);
    var z = this.maxZoom;
    for (; z > this.minZoom; z--) {
      var wp = this._worldPx(z);
      if (dw * wp <= availW && dh * wp <= availH) break;
    }
    if (opts.maxZoom != null) z = Math.min(z, opts.maxZoom);
    z = this._clampZoom(z);
    var mid = { x: (minU.x + maxU.x) / 2, y: (minU.y + maxU.y) / 2 };
    var ll = this._unitToWgs(mid);
    this.zoom = z;
    this.center.lng = ll.lng;
    this.center.lat = ll.lat;
    this.invalidate();
    this._emit('zoomend', { zoom: this.zoom, center: this.getCenter() });
    return this;
  };

  /* ------------------------------------------------------------------ */
  /* 瓦片源                                                              */
  /* ------------------------------------------------------------------ */

  SlippyMap.prototype.setSource = function (src) {
    if (!src || !src.key) return this;
    var same = this.source.key === src.key;
    this.source = {
      key: src.key,
      name: src.name || src.key,
      url: src.url || '',
      max_zoom: Math.min(HARD_MAX_ZOOM, src.max_zoom ? +src.max_zoom : 18),
      attribution: src.attribution || '',
      datum: src.datum || Coord.datumOfSource(src.key, src)
    };
    this.maxZoom = Math.max(this.minZoom + 1, this.source.max_zoom);
    if (this.zoom > this.maxZoom) this.zoom = this.maxZoom;
    this._updateAttrib();
    this._duCache = new Map();
    this._duDatum = this.source.datum;
    this._cuKey = null;
    if (!same) {
      this._clearTiles();
      this.failTotal = 0;
      this.failStreak = 0;
      this.loadPausedUntil = 0;
    }
    this._updateWarn();
    this.invalidate();
    return this;
  };

  SlippyMap.prototype.getSource = function () { return this.source; };

  SlippyMap.prototype._tileUrl = function (z, x, y) {
    return '/api/tiles/' + encodeURIComponent(this.source.key) + '/' + z + '/' + x + '/' + y + '.png';
  };

  SlippyMap.prototype._clearTiles = function () {
    this.tiles.forEach(function (e) { SlippyMap._release(e); });
    this.tiles.clear();
    this.queue.length = 0;
    this.activeLoads = 0;
  };

  SlippyMap._release = function (entry) {
    if (!entry) return;
    if (entry.retryTimer) { clearTimeout(entry.retryTimer); entry.retryTimer = null; }
    if (entry.img) {
      entry.img.onload = null;
      entry.img.onerror = null;
      try { entry.img.src = ''; } catch (e) { /* 忽略 */ }
      entry.img = null;
    }
    entry.state = 'released';
  };

  SlippyMap.prototype._getTile = function (z, x, y) {
    var key = this.source.key + '/' + z + '/' + x + '/' + y;
    var e = this.tiles.get(key);
    if (e) {
      e.lastUsed = now();
      // Map 保持插入序：重新插入实现 LRU 排序
      this.tiles.delete(key);
      this.tiles.set(key, e);
      return e;
    }
    e = { key: key, z: z, x: x, y: y, img: null, state: 'new', tries: 0, lastUsed: now() };
    this.tiles.set(key, e);
    this._evictIfNeeded();
    return e;
  };

  SlippyMap.prototype._evictIfNeeded = function () {
    while (this.tiles.size > CACHE_LIMIT) {
      var oldestKey = this.tiles.keys().next().value;
      var e = this.tiles.get(oldestKey);
      this.tiles.delete(oldestKey);
      SlippyMap._release(e);
    }
  };

  SlippyMap.prototype._queueLoad = function (entry) {
    if (!entry || entry.state === 'loading' || entry.state === 'ok' || entry.state === 'queued') return;
    entry.state = 'queued';
    this.queue.push(entry);
  };

  SlippyMap.prototype._pump = function () {
    var self = this;
    var t = now();
    if (t < this.loadPausedUntil) {
      // 连续失败暂停加载：定时唤醒，不占用渲染循环
      if (this._wakeTimer == null) {
        this._wakeTimer = setTimeout(function () {
          self._wakeTimer = null;
          self._pump();
          self.invalidate();
        }, Math.max(200, this.loadPausedUntil - t + 50));
      }
      return;
    }
    while (this.queue.length && this.activeLoads < MAX_PARALLEL_LOADS) {
      var entry = this.queue.shift();
      if (!entry || !this.tiles.has(entry.key)) continue;
      this._startLoad(entry);
    }
  };

  SlippyMap.prototype._startLoad = function (entry) {
    var self = this;
    var img = new Image();
    entry.img = img;
    entry.state = 'loading';
    this.activeLoads += 1;
    img.decoding = 'async';
    img.onload = function () {
      self.activeLoads = Math.max(0, self.activeLoads - 1);
      if (!self.tiles.has(entry.key)) { SlippyMap._release(entry); self._pump(); return; }
      entry.state = 'ok';
      entry.tries = 0;
      entry.lastUsed = now();
      self.failStreak = 0;
      self.invalidate();
      self._emit('tileload', { key: entry.key });
      self._pump();
    };
    img.onerror = function () {
      self.activeLoads = Math.max(0, self.activeLoads - 1);
      entry.state = 'error';
      entry.tries += 1;
      self.failStreak += 1;
      self.failTotal += 1;
      if (self.failStreak >= FAIL_STREAK_LIMIT) {
        self.loadPausedUntil = now() + LOAD_PAUSE_MS;
        self._emit('tileerror', { paused: true, failTotal: self.failTotal });
      } else {
        self._emit('tileerror', { paused: false, failTotal: self.failTotal });
      }
      self._updateWarn();
      if (entry.tries < MAX_TILE_TRIES && self.tiles.has(entry.key)) {
        entry.retryTimer = setTimeout(function () {
          entry.retryTimer = null;
          if (!self.tiles.has(entry.key)) return;
          entry.state = 'new';
          self.queue.push(entry);
          self._pump();
          self.invalidate();
        }, 500 * entry.tries);
      }
      self._pump();
      self.invalidate();
    };
    img.src = this._tileUrl(entry.z, entry.x, entry.y);
    this.invalidate();
  };

  /** 重试所有失败瓦片（界面上「重试」按钮 / 网络恢复时调用） */
  SlippyMap.prototype.retryFailedTiles = function () {
    var self = this;
    this.loadPausedUntil = 0;
    this.failStreak = 0;
    this.failTotal = 0;
    this.tiles.forEach(function (e) {
      if (e.state === 'error') {
        e.state = 'new';
        e.tries = 0;
        self.queue.push(e);
      }
    });
    this._updateWarn();
    this._pump();
    this.invalidate();
    return this;
  };

  /* ------------------------------------------------------------------ */
  /* 图层数据                                                            */
  /* ------------------------------------------------------------------ */

  /** 路径折线（Route.path，WGS84 [lng,lat][]） */
  SlippyMap.prototype.setPath = function (path, color) {
    this.layers.path = Array.isArray(path) ? path : [];
    if (color) this.layers.pathColor = color;
    this.layers.playedIndex = -1;
    this.invalidate();
    return this;
  };

  /** 轨迹折线（实际走过的点） */
  SlippyMap.prototype.setTrack = function (points) {
    this.layers.track = Array.isArray(points) ? points : [];
    this.invalidate();
    return this;
  };

  SlippyMap.prototype.pushTrack = function (lng, lat) {
    var t = this.layers.track;
    var last = t[t.length - 1];
    if (last && Math.abs(last[0] - lng) < 1e-7 && Math.abs(last[1] - lat) < 1e-7) return this;
    t.push([+lng, +lat]);
    if (t.length > 4000) t.splice(0, t.length - 4000);
    this.invalidate();
    return this;
  };

  SlippyMap.prototype.clearTrack = function () {
    this.layers.track = [];
    this.invalidate();
    return this;
  };

  /**
   * 途经点：[lng,lat][]（WGS84）
   * @param {Array} list
   * @param {object} [opts] {draggable:boolean}
   */
  SlippyMap.prototype.setWaypoints = function (list, opts) {
    this.layers.waypoints = (list || []).map(function (p) {
      return [ +p[0], +p[1] ];
    });
    if (opts && opts.draggable != null) this.waypointsDraggable = !!opts.draggable;
    if (this.layers.selected >= this.layers.waypoints.length) this.layers.selected = -1;
    this.invalidate();
    return this;
  };

  SlippyMap.prototype.setSelected = function (index) {
    this.layers.selected = (index == null) ? -1 : index;
    this.invalidate();
    return this;
  };

  /** 已播放段高亮：path 中 0..index 用高亮色 */
  SlippyMap.prototype.setPlayedIndex = function (index) {
    this.layers.playedIndex = (index == null) ? -1 : index;
    this.invalidate();
    return this;
  };

  /** 当前定位点（脉冲动画）；null 表示隐藏 */
  SlippyMap.prototype.setCurrent = function (lng, lat) {
    if (lng == null || lat == null || !isFinite(lng) || !isFinite(lat)) {
      this.layers.current = null;
    } else {
      this.layers.current = { lng: +lng, lat: +lat };
    }
    this.invalidate();
    return this;
  };

  SlippyMap.prototype.getCurrent = function () {
    return this.layers.current ? { lng: this.layers.current.lng, lat: this.layers.current.lat } : null;
  };

  SlippyMap.prototype.setDrawMode = function (mode) {
    this.drawMode = mode || 'select';
    this.el.classList.toggle('is-crosshair', this.drawMode === 'add' || this.drawMode === 'polyline');
    this.waypointsDraggable = (this.drawMode === 'select');
    this.invalidate();
    return this;
  };

  SlippyMap.prototype.setDoubleClickZoom = function (on) {
    this.doubleClickZoom = !!on;
    return this;
  };

  /* ------------------------------------------------------------------ */
  /* 交互                                                                */
  /* ------------------------------------------------------------------ */

  SlippyMap.prototype._buildControls = function () {
    var self = this;

    this.ctrlZoom = document.createElement('div');
    this.ctrlZoom.className = 'map-zoom';
    this.ctrlZoom.innerHTML =
      '<button type="button" class="map-btn" data-act="in" title="放大">+</button>' +
      '<button type="button" class="map-btn" data-act="out" title="缩小">−</button>';
    this.ctrlZoom.addEventListener('click', function (ev) {
      var b = ev.target.closest('button');
      if (!b) return;
      self.setZoom(self.zoom + (b.dataset.act === 'in' ? 1 : -1));
    });
    this.el.appendChild(this.ctrlZoom);

    this.ctrlWarn = document.createElement('div');
    this.ctrlWarn.className = 'map-warn hidden';
    this.ctrlWarn.innerHTML = '<span class="map-warn-text"></span>' +
      '<button type="button" class="map-warn-btn">重试</button>';
    this.ctrlWarn.querySelector('.map-warn-btn').addEventListener('click', function () {
      self.retryFailedTiles();
    });
    this.el.appendChild(this.ctrlWarn);

    this.ctrlReadout = document.createElement('div');
    this.ctrlReadout.className = 'map-readout';
    this.ctrlReadout.innerHTML =
      '<span class="mr-ll">--</span><span class="mr-z">z--</span>';
    this.el.appendChild(this.ctrlReadout);

    this.ctrlScale = document.createElement('div');
    this.ctrlScale.className = 'map-scalebar';
    this.ctrlScale.innerHTML = '<div class="ms-bar"></div><span class="ms-label"></span>';
    this.el.appendChild(this.ctrlScale);

    this.ctrlAttrib = document.createElement('div');
    this.ctrlAttrib.className = 'map-attrib';
    this.el.appendChild(this.ctrlAttrib);
  };

  SlippyMap.prototype._updateAttrib = function () {
    var a = this.source.attribution || '';
    this.ctrlAttrib.textContent = (a ? a + ' · ' : '') + this.source.name;
  };

  SlippyMap.prototype._updateWarn = function () {
    if (this.failTotal > 0) {
      this.ctrlWarn.classList.remove('hidden');
      var extra = now() < this.loadPausedUntil ? '（已暂停加载，稍后自动重试）' : '';
      this.ctrlWarn.querySelector('.map-warn-text').textContent =
        '瓦片加载失败 ' + this.failTotal + ' 张' + extra;
    } else {
      this.ctrlWarn.classList.add('hidden');
    }
  };

  SlippyMap.prototype._bindEvents = function () {
    var self = this;
    var cv = this.canvas;

    cv.addEventListener('pointerdown', function (e) { self._onDown(e); });
    cv.addEventListener('pointermove', function (e) { self._onMove(e); });
    cv.addEventListener('pointerup', function (e) { self._onUp(e); });
    cv.addEventListener('pointercancel', function (e) { self._onUp(e); });
    cv.addEventListener('pointerleave', function () {
      self.mouse = null;
      self._updateReadout();
    });
    cv.addEventListener('wheel', function (e) { self._onWheel(e); }, { passive: false });
    cv.addEventListener('dblclick', function (e) {
      var p = self._localPoint(e);
      var ll = self.pointToLngLat(p.x, p.y);
      self._emit('dblclick', { x: p.x, y: p.y, lng: ll.lng, lat: ll.lat, original: e });
      if (self.doubleClickZoom) {
        e.preventDefault();
        self.setZoom(Math.round(self.zoom) + 1, p);
      }
    });
    cv.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    cv.addEventListener('click', function (e) {
      if (self._suppressClick) { self._suppressClick = false; return; }
      var p = self._localPoint(e);
      var ll = self.pointToLngLat(p.x, p.y);
      self._emit('click', { x: p.x, y: p.y, lng: ll.lng, lat: ll.lat, original: e });
    });
  };

  SlippyMap.prototype._localPoint = function (e) {
    var r = this.canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  SlippyMap.prototype._hitWaypoint = function (x, y) {
    var wps = this.layers.waypoints;
    for (var i = wps.length - 1; i >= 0; i--) {
      var p = this.lngLatToPoint(wps[i][0], wps[i][1]);
      var dx = p.x - x, dy = p.y - y;
      if (dx * dx + dy * dy <= 14 * 14) return i;
    }
    return -1;
  };

  SlippyMap.prototype._onDown = function (e) {
    if (e.button !== undefined && e.button !== 0 && e.pointerType === 'mouse') return;
    var p = this._localPoint(e);
    this.mouse = p;
    try { this.canvas.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    this.pointers.set(e.pointerId, p);
    this._stopInertia();

    if (this.pointers.size === 2) {
      var pts = Array.from(this.pointers.values());
      this.dragging = false;
      this.dragWp = null;
      this.pinch = {
        dist: Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y),
        zoom: this.zoom,
        mid: { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 }
      };
      return;
    }

    // 优先命中途经点（选择模式下可拖拽）
    if (this.waypointsDraggable && this.drawMode === 'select') {
      var hit = this._hitWaypoint(p.x, p.y);
      if (hit >= 0) {
        this.dragWp = { index: hit, moved: false, start: p };
        this.layers.selected = hit;
        this._emit('waypointselect', { index: hit });
        this.invalidate();
        return;
      }
    }

    this.dragging = true;
    this.dragStart = { p: p, centerUnit: this._centerUnit(), last: p, lastT: now() };
    this.velocity = { x: 0, y: 0 };
    this.el.classList.add('is-dragging');
  };

  SlippyMap.prototype._onMove = function (e) {
    var p = this._localPoint(e);
    this.mouse = p;
    if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, p);

    if (this.pinch && this.pointers.size >= 2) {
      var pts = Array.from(this.pointers.values());
      var dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
      var mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      if (this.pinch.dist > 0) {
        var z = this.pinch.zoom + Math.log2(Math.max(0.01, dist / this.pinch.dist));
        var before = this._unitAtPoint(mid.x, mid.y);
        this.zoom = this._clampZoom(z);
        this._applyAnchor(before, mid.x, mid.y);
        this.pinch.mid = mid;
        this.invalidate();
      }
      this._updateReadout();
      return;
    }

    if (this.dragWp) {
      var moved = Math.abs(p.x - this.dragWp.start.x) > 2 || Math.abs(p.y - this.dragWp.start.y) > 2;
      if (moved) this.dragWp.moved = true;
      var ll = this.pointToLngLat(p.x, p.y);
      var i = this.dragWp.index;
      if (this.layers.waypoints[i]) {
        this.layers.waypoints[i][0] = ll.lng;
        this.layers.waypoints[i][1] = ll.lat;
      }
      this._emit('waypointdrag', { index: i, lng: ll.lng, lat: ll.lat });
      this.invalidate();
      this._updateReadout();
      return;
    }

    if (this.dragging && this.dragStart) {
      var ds = this.dragStart;
      var wp = this._worldPx();
      var dx = p.x - ds.p.x;
      var dy = p.y - ds.p.y;
      var u = { x: ds.centerUnit.x - dx / wp, y: ds.centerUnit.y - dy / wp };
      var c = this._unitToWgs(u);
      this.center.lng = c.lng;
      this.center.lat = c.lat;
      // 甩动速度：只在间隔 ≥8ms 时采样（避免 pointermove 亚毫秒间隔造成速度尖峰），并限幅
      var t = now();
      var dt = t - ds.lastT;
      if (dt >= 8) {
        this.velocity = {
          x: Math.max(-80, Math.min(80, (p.x - ds.last.x) / dt * 16)),
          y: Math.max(-80, Math.min(80, (p.y - ds.last.y) / dt * 16))
        };
        ds.last = p;
        ds.lastT = t;
      }
      this._suppressClick = true;
      this.invalidate();
      this._emit('move', { center: this.getCenter(), zoom: this.zoom });
      this._updateReadout();
      return;
    }

    this._updateReadout();
  };

  SlippyMap.prototype._onUp = function (e) {
    var had = this.pointers.has(e.pointerId);
    var p = this.mouse || this._localPoint(e);
    try { this.canvas.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    this.pointers.delete(e.pointerId);

    if (this.pointers.size < 2) this.pinch = null;

    if (this.dragWp) {
      var moved = this.dragWp.moved;
      var idx = this.dragWp.index;
      this.dragWp = null;
      if (moved) {
        this._suppressClick = true;
        this._emit('waypointdragend', {
          index: idx,
          lng: this.layers.waypoints[idx] ? this.layers.waypoints[idx][0] : null,
          lat: this.layers.waypoints[idx] ? this.layers.waypoints[idx][1] : null,
          moved: true
        });
      } else {
        this._emit('waypointclick', { index: idx });
      }
      this.invalidate();
      return;
    }

    if (this.dragging) {
      this.dragging = false;
      this.el.classList.remove('is-dragging');
      var v = Math.hypot(this.velocity.x, this.velocity.y);
      if (had && v > 1.2) {
        this.inertiaOn = true;
        this._scheduleRender();
      } else {
        this.velocity = { x: 0, y: 0 };
      }
      this._emit('dragend', { center: this.getCenter(), zoom: this.zoom });
      if (p) this._updateReadout();
    }
  };

  SlippyMap.prototype._stopInertia = function () {
    this.inertiaOn = false;
    this.velocity = { x: 0, y: 0 };
  };

  SlippyMap.prototype._stepInertia = function () {
    if (!this.inertiaOn) return false;
    var wp = this._worldPx();
    var c = this._centerUnit();
    var u = { x: c.x - this.velocity.x / wp, y: c.y - this.velocity.y / wp };
    var ll = this._unitToWgs(u);
    // 到达世界边界或速度衰减到阈值就停下
    if (ll.lng < -180 || ll.lng > 180) { this._stopInertia(); return false; }
    this.center.lng = ll.lng;
    this.center.lat = Math.max(-85, Math.min(85, ll.lat));
    this.velocity.x *= 0.93;
    this.velocity.y *= 0.93;
    if (Math.hypot(this.velocity.x, this.velocity.y) < 0.08) this._stopInertia();
    this._emit('move', { center: this.getCenter(), zoom: this.zoom });
    return true;
  };

  SlippyMap.prototype._onWheel = function (e) {
    e.preventDefault();
    var p = this._localPoint(e);
    var dy = e.deltaY;
    if (e.deltaMode === 1) dy *= 18;
    else if (e.deltaMode === 2) dy *= 100;
    var step = -dy * 0.0022;
    if (Math.abs(step) > 1.5) step = step > 0 ? 1.5 : -1.5;
    var before = this._unitAtPoint(p.x, p.y);
    this.zoom = this._clampZoom(this.zoom + step);
    this._applyAnchor(before, p.x, p.y);
    this._stopInertia();
    this.mouse = p;
    this.invalidate();
    this._emit('zoomend', { zoom: this.zoom, center: this.getCenter() });
    this._updateReadout();
  };

  /* ------------------------------------------------------------------ */
  /* 尺寸 / 读数                                                         */
  /* ------------------------------------------------------------------ */

  SlippyMap.prototype.resize = function () {
    var rect = this.el.getBoundingClientRect();
    var w = Math.max(1, Math.floor(rect.width));
    var h = Math.max(1, Math.floor(rect.height));
    var dpr = Math.min(3, Math.max(1, global.devicePixelRatio || 1));
    if (w === this.w && h === this.h && dpr === this.dpr) return;
    this.w = w; this.h = h; this.dpr = dpr;
    this.canvas.width = Math.floor(w * dpr);
    this.canvas.height = Math.floor(h * dpr);
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
    this.invalidate();
  };

  SlippyMap.prototype._updateReadout = function () {
    var ll;
    if (this.mouse) {
      ll = this.pointToLngLat(this.mouse.x, this.mouse.y);
    } else {
      ll = this.center;
    }
    var el = this.ctrlReadout.querySelector('.mr-ll');
    if (el) el.textContent = Geo.fmtLngLat(ll.lng, ll.lat, 6);
    var zl = this.ctrlReadout.querySelector('.mr-z');
    if (zl) zl.textContent = 'z' + (Math.round(this.zoom * 10) / 10) + ' · ' + this.source.name;
  };

  SlippyMap.prototype._updateScale = function () {
    var mpp = 156543.03392 * Math.cos(this.center.lat * Math.PI / 180) / Math.pow(2, this.zoom);
    var target = mpp * 110;
    var meters = SCALE_STEPS[0];
    for (var i = SCALE_STEPS.length - 1; i >= 0; i--) {
      if (SCALE_STEPS[i] <= target) { meters = SCALE_STEPS[i]; break; }
    }
    var px = Math.max(24, Math.round(meters / mpp));
    var bar = this.ctrlScale.querySelector('.ms-bar');
    var lab = this.ctrlScale.querySelector('.ms-label');
    if (bar) bar.style.width = px + 'px';
    if (lab) lab.textContent = meters >= 1000 ? (meters / 1000) + ' 公里' : meters + ' 米';
  };

  /* ------------------------------------------------------------------ */
  /* 渲染                                                                */
  /* ------------------------------------------------------------------ */

  SlippyMap.prototype.invalidate = function () {
    this._dirty = true;
    this._scheduleRender();
  };

  SlippyMap.prototype._scheduleRender = function () {
    var self = this;
    if (this._rafId != null) return;
    this._rafId = global.requestAnimationFrame(function () {
      self._rafId = null;
      self._frame();
    });
  };

  SlippyMap.prototype._frame = function () {
    var anim = false;
    if (this._stepInertia()) anim = true;
    this._render();
    var paused = now() < this.loadPausedUntil;
    var loading = !!(this.activeLoads || (this.queue.length && !paused));
    if (loading) this._pump();
    if (this.layers.current) anim = true;
    if (loading || anim) this._scheduleRender();
  };

  /**
   * 当前视口需要绘制的瓦片列表（每帧只画可视范围）。
   * @returns {Array<{z:number,x:number,y:number,sx:number,sy:number,size:number}>}
   */
  SlippyMap.prototype._visibleTiles = function () {
    var c = this._centerUnit();
    var zoom = this.zoom;
    var wp = this._worldPx();
    var tz = Math.max(this.minZoom, Math.min(this.source.max_zoom, Math.round(zoom)));
    var size = TILE * Math.pow(2, zoom - tz);
    var n = Math.pow(2, tz);
    var unitsPerPx = 1 / wp;
    var x0u = c.x - (this.w / 2) * unitsPerPx;
    var x1u = c.x + (this.w / 2) * unitsPerPx;
    var y0u = c.y - (this.h / 2) * unitsPerPx;
    var y1u = c.y + (this.h / 2) * unitsPerPx;
    var tx0 = Math.floor(x0u * n) - 1;
    var tx1 = Math.floor(x1u * n) + 1;
    var ty0 = Math.max(0, Math.floor(y0u * n) - 1);
    var ty1 = Math.min(n - 1, Math.floor(y1u * n) + 1);
    var out = [];
    for (var ty = ty0; ty <= ty1; ty++) {
      for (var tx = tx0; tx <= tx1; tx++) {
        out.push({
          z: tz,
          x: ((tx % n) + n) % n,
          y: ty,
          sx: (tx / n - c.x) * wp + this.w / 2,
          sy: (ty / n - c.y) * wp + this.h / 2,
          size: size
        });
      }
    }
    return out;
  };

  SlippyMap.prototype._render = function () {
    var ctx = this.ctx, w = this.w, h = this.h;
    this._dirty = false;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = COLORS.bg;
    ctx.fillRect(0, 0, w, h);

    var t = now();
    var tiles = this._visibleTiles();
    for (var i = 0; i < tiles.length; i++) {
      var tl = tiles[i];
      var entry = this._getTile(tl.z, tl.x, tl.y);
      this._drawTile(ctx, entry, tl.sx, tl.sy, tl.size, t);
    }

    this._drawPaths(ctx);
    this._drawWaypoints(ctx);
    this._drawCurrent(ctx, t);

    this._updateScale();
    this._updateReadout();
    this._updateWarn();
  };

  SlippyMap.prototype._drawTile = function (ctx, entry, sx, sy, size, t) {
    var img = entry.img;
    if (entry.state === 'ok' && img && img.complete && img.naturalWidth > 0) {
      ctx.drawImage(img, sx, sy, size + 0.7, size + 0.7);
      return;
    }
    if (entry.state === 'new' || entry.state === 'queued') {
      this._queueLoad(entry);
      this._pump();
    }
    // 占位
    ctx.fillStyle = (entry.state === 'error' && entry.tries >= MAX_TILE_TRIES) ? COLORS.tileFail : COLORS.tileEmpty;
    ctx.fillRect(sx, sy, size, size);
    ctx.strokeStyle = COLORS.tileBorder;
    ctx.lineWidth = 1;
    ctx.strokeRect(sx + 0.5, sy + 0.5, size - 1, size - 1);
    if (entry.state === 'error' && entry.tries >= MAX_TILE_TRIES) {
      ctx.strokeStyle = COLORS.tileFailMark;
      ctx.lineWidth = 1.4;
      var cx = sx + size / 2, cy = sy + size / 2, r = Math.min(10, size / 6);
      ctx.beginPath();
      ctx.moveTo(cx - r, cy - r); ctx.lineTo(cx + r, cy + r);
      ctx.moveTo(cx + r, cy - r); ctx.lineTo(cx - r, cy + r);
      ctx.stroke();
    } else if (entry.state === 'loading' || entry.state === 'queued') {
      // 加载中的呼吸圆点
      var phase = (t / 600) % 1;
      var alpha = 0.15 + 0.5 * Math.abs(Math.sin(phase * Math.PI));
      ctx.fillStyle = 'rgba(88,166,255,' + alpha.toFixed(3) + ')';
      var cx2 = sx + size / 2, cy2 = sy + size / 2;
      var r2 = Math.min(12, Math.max(3, size / 10));
      ctx.beginPath();
      ctx.arc(cx2, cy2, r2, 0, Math.PI * 2);
      ctx.fill();
    }
  };

  /**
   * 画折线（可选区间 [from, to]）。
   * 屏幕距离小于 1.4px 的中间点会被抽稀：OSRM 路线动辄上万点，
   * 全量描边会拖垮渲染（实测 8000 点在 GCJ-02 底图上每帧数百毫秒）。
   */
  SlippyMap.prototype._strokeLine = function (ctx, points, color, width, outline, from, to) {
    if (!points || points.length < 2) return;
    var start = Math.max(0, from || 0);
    var end = Math.min(points.length - 1, to == null ? points.length - 1 : to);
    if (end <= start) return;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    for (var pass = 0; pass < (outline ? 2 : 1); pass++) {
      ctx.beginPath();
      var begun = false, lastX = 0, lastY = 0, i, p, sp;
      for (i = start; i <= end; i++) {
        p = points[i];
        if (!p || !isFinite(p[0]) || !isFinite(p[1])) continue;
        sp = this.lngLatToPoint(p[0], p[1]);
        if (!begun) {
          ctx.moveTo(sp.x, sp.y);
          begun = true;
          lastX = sp.x; lastY = sp.y;
          continue;
        }
        if (i !== end && Math.abs(sp.x - lastX) < 1.4 && Math.abs(sp.y - lastY) < 1.4) continue;
        ctx.lineTo(sp.x, sp.y);
        lastX = sp.x; lastY = sp.y;
      }
      if (!begun) return;
      ctx.strokeStyle = pass === 0 ? (outline || '#08111f') : color;
      ctx.lineWidth = pass === 0 ? width + 3 : width;
      ctx.stroke();
    }
  };

  SlippyMap.prototype._drawPaths = function (ctx) {
    var path = this.layers.path;
    var i = this.layers.playedIndex;
    if (path && path.length >= 2) {
      // 未播放部分
      if (i > 0 && i < path.length - 1) {
        this._strokeLine(ctx, path, 'rgba(47,129,247,0.55)', 3, 'rgba(8,17,31,0.7)', i, path.length - 1);
      } else {
        this._strokeLine(ctx, path, this.layers.pathColor, 3.5, COLORS.pathOutline);
      }
      // 已播放部分高亮
      if (i > 0) {
        this._strokeLine(ctx, path, COLORS.played, 4, COLORS.playedOutline, 0, Math.min(path.length - 1, i));
      }
    }
    if (this.layers.showTrack && this.layers.track && this.layers.track.length >= 2) {
      this._strokeLine(ctx, this.layers.track, COLORS.track, 2.5, 'rgba(10,12,16,0.8)');
    }
  };

  SlippyMap.prototype._drawWaypoints = function (ctx) {
    var wps = this.layers.waypoints;
    if (!wps || !wps.length) return;
    var n = wps.length;
    for (var i = 0; i < n; i++) {
      var p = this.lngLatToPoint(wps[i][0], wps[i][1]);
      if (p.x < -40 || p.y < -40 || p.x > this.w + 40 || p.y > this.h + 40) continue;
      var isStart = i === 0;
      var isEnd = i === n - 1 && n > 1;
      var color = isStart ? COLORS.wpStart : (isEnd ? COLORS.wpEnd : COLORS.wpMid);
      var r = isStart || isEnd ? 12 : 10;
      var selected = (this.layers.selected === i);

      ctx.beginPath();
      ctx.arc(p.x, p.y + 1.5, r, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(0,0,0,0.45)';
      ctx.fill();

      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.lineWidth = selected ? 3 : 2;
      ctx.strokeStyle = selected ? COLORS.wpSelected : 'rgba(255,255,255,0.85)';
      ctx.stroke();

      var label = (isStart || isEnd) ? (isStart ? '起' : '终') : String(i + 1);
      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold ' + (isStart || isEnd ? 11 : 10) + 'px -apple-system, "PingFang SC", sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, p.x, p.y + 0.5);
      ctx.textBaseline = 'alphabetic';
      ctx.textAlign = 'left';
    }
  };

  SlippyMap.prototype._drawCurrent = function (ctx, t) {
    var cur = this.layers.current;
    if (!cur) return;
    var p = this.lngLatToPoint(cur.lng, cur.lat);
    if (p.x < -60 || p.y < -60 || p.x > this.w + 60 || p.y > this.h + 60) return;
    var phase = (t / 1600) % 1;
    for (var k = 0; k < 2; k++) {
      var ph = (phase + k * 0.5) % 1;
      var r = 8 + 22 * ph;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(88,166,255,' + (0.55 * (1 - ph)).toFixed(3) + ')';
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(p.x, p.y, 8, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(88,166,255,0.28)';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(p.x, p.y, 5, 0, Math.PI * 2);
    ctx.fillStyle = COLORS.current;
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#ffffff';
    ctx.stroke();
  };

  /* ------------------------------------------------------------------ */

  SlippyMap.prototype.destroy = function () {
    if (this._ro) this._ro.disconnect();
    if (this._onWinResize) global.removeEventListener('resize', this._onWinResize);
    if (this._rafId != null) global.cancelAnimationFrame(this._rafId);
    if (this._wakeTimer) { clearTimeout(this._wakeTimer); this._wakeTimer = null; }
    this._clearTiles();
    this.handlers = Object.create(null);
  };

  SlippyMap.COLORS = COLORS;
  SlippyMap.TILE_SIZE = TILE;
  global.SlippyMap = SlippyMap;
})(typeof window !== 'undefined' ? window : globalThis);
