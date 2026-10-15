/*!
 * coord.js — 坐标系统转换与地理计算（纯 vanilla ES2020，无外部依赖）
 *
 * 约定（见 docs/PROTOCOL.md）：
 *   - 内部存储、发往后端的一切经纬度 **一律 WGS84**；
 *   - 高德 / 腾讯瓦片为 GCJ-02（火星坐标），ArcGIS / OSM 为 WGS84；
 *   - 显示用点 = WGS84 → 瓦片基准（GCJ-02 源时做一次 wgs84ToGcj02），
 *     取点（鼠标 → 坐标）= 瓦片基准 → WGS84（gcj02ToWgs84）。
 *
 * 导出：window.Coord（转换）、window.Geo（距离/格式化工具）
 */
(function (global) {
  'use strict';

  var PI = Math.PI;
  var A = 6378245.0;                    // 克拉索夫斯基椭球长半轴
  var EE = 0.00669342162296594323;      // 偏心率平方

  /** 中国大陆粗略范围之外（含港澳台、境外）不做偏移 */
  function outOfChina(lng, lat) {
    if (!isFinite(lng) || !isFinite(lat)) return true;
    if (lng < 72.004 || lng > 137.8347) return true;
    if (lat < 0.8293 || lat > 55.8271) return true;
    return false;
  }

  function transformLat(x, y) {
    var ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(y * PI) + 40.0 * Math.sin(y / 3.0 * PI)) * 2.0 / 3.0;
    ret += (160.0 * Math.sin(y / 12.0 * PI) + 320.0 * Math.sin(y * PI / 30.0)) * 2.0 / 3.0;
    return ret;
  }

  function transformLng(x, y) {
    var ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(x * PI) + 40.0 * Math.sin(x / 3.0 * PI)) * 2.0 / 3.0;
    ret += (150.0 * Math.sin(x / 12.0 * PI) + 300.0 * Math.sin(x / 30.0 * PI)) * 2.0 / 3.0;
    return ret;
  }

  /**
   * WGS84 → GCJ-02
   * @returns {[number, number]} [lng, lat]
   */
  function wgs84ToGcj02(lng, lat) {
    lng = +lng; lat = +lat;
    if (outOfChina(lng, lat)) return [lng, lat];
    var dLat = transformLat(lng - 105.0, lat - 35.0);
    var dLng = transformLng(lng - 105.0, lat - 35.0);
    var radLat = lat / 180.0 * PI;
    var magic = Math.sin(radLat);
    magic = 1 - EE * magic * magic;
    var sqrtMagic = Math.sqrt(magic);
    dLat = (dLat * 180.0) / ((A * (1 - EE)) / (magic * sqrtMagic) * PI);
    dLng = (dLng * 180.0) / (A / sqrtMagic * Math.cos(radLat) * PI);
    return [lng + dLng, lat + dLat];
  }

  /**
   * GCJ-02 → WGS84（迭代逼近，精度 1e-9 度 ≈ 0.1mm，足够）
   * @returns {[number, number]} [lng, lat]
   */
  function gcj02ToWgs84(lng, lat) {
    lng = +lng; lat = +lat;
    if (outOfChina(lng, lat)) return [lng, lat];
    var wLng = lng, wLat = lat;
    for (var i = 0; i < 12; i++) {
      var g = wgs84ToGcj02(wLng, wLat);
      var dLng = lng - g[0];
      var dLat = lat - g[1];
      wLng += dLng;
      wLat += dLat;
      if (Math.abs(dLng) < 1e-10 && Math.abs(dLat) < 1e-10) break;
    }
    return [wLng, wLat];
  }

  /** 判断瓦片源使用的坐标基准 */
  function datumOfSource(key, src) {
    if (src) {
      var explicit = src.datum || src.coordsys || src.crs || src.coordinate_system;
      if (typeof explicit === 'string') {
        var e = explicit.toLowerCase();
        if (e.indexOf('gcj') >= 0 || e.indexOf('mars') >= 0 || e.indexOf('amap') >= 0) return 'gcj02';
        if (e.indexOf('wgs') >= 0 || e.indexOf('4326') >= 0 || e.indexOf('epsg') >= 0) return 'wgs84';
      }
    }
    var probe = ((key || '') + ' ' + ((src && (src.url || src.name)) || '')).toLowerCase();
    if (/amap|autonavi|gaode|tencent|gtimg|map\.qq|qqmap/.test(probe)) return 'gcj02';
    return 'wgs84';
  }

  /** WGS84 → 显示基准（GCJ-02 源需要偏移，其余原样返回） */
  function toDisplay(lng, lat, datum) {
    if (datum === 'gcj02') return wgs84ToGcj02(lng, lat);
    return [+lng, +lat];
  }

  /** 显示基准 → WGS84 */
  function fromDisplay(lng, lat, datum) {
    if (datum === 'gcj02') return gcj02ToWgs84(lng, lat);
    return [+lng, +lat];
  }

  /** 保留 6 位小数（契约要求下发坐标 6 位小数） */
  function round6(v) { return Math.round((+v) * 1e6) / 1e6; }

  global.Coord = {
    outOfChina: outOfChina,
    wgs84ToGcj02: wgs84ToGcj02,
    gcj02ToWgs84: gcj02ToWgs84,
    datumOfSource: datumOfSource,
    toDisplay: toDisplay,
    fromDisplay: fromDisplay,
    round6: round6,
    A: A,
    EE: EE
  };

  /* ------------------------------------------------------------------ */
  /* 地理计算 / 格式化小工具                                             */
  /* ------------------------------------------------------------------ */

  var EARTH_R = 6371008.8; // 平均地球半径（米）

  function rad(d) { return d * PI / 180; }

  /** 两点球面距离（米），入参 [lng,lat] 或 (lng,lat) */
  function haversine(a, b) {
    var lng1, lat1, lng2, lat2;
    if (Array.isArray(a)) { lng1 = a[0]; lat1 = a[1]; } else { lng1 = a; lat1 = arguments[1]; b = arguments[2]; lng2 = b[0]; lat2 = b[1]; }
    if (Array.isArray(b)) { lng2 = b[0]; lat2 = b[1]; }
    var dLat = rad(lat2 - lat1);
    var dLng = rad(lng2 - lng1);
    var s = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return 2 * EARTH_R * Math.asin(Math.min(1, Math.sqrt(s)));
  }

  /** 折线总长度（米） */
  function pathLength(path) {
    var d = 0;
    if (!path || path.length < 2) return 0;
    for (var i = 1; i < path.length; i++) d += haversine(path[i - 1], path[i]);
    return d;
  }

  /** 距离格式化：<1000 → 米，否则 公里 */
  function fmtDistance(m) {
    if (!isFinite(m)) return '--';
    if (m < 1000) return Math.round(m) + ' 米';
    if (m < 10000) return (m / 1000).toFixed(2) + ' 公里';
    return (m / 1000).toFixed(1) + ' 公里';
  }

  /** 秒 → 中文时长 */
  function fmtDuration(sec) {
    if (!isFinite(sec) || sec < 0) return '--';
    sec = Math.round(sec);
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    if (h > 0) return h + ':' + pad(m) + ':' + pad(s);
    return pad(m) + ':' + pad(s);
  }

  /** 秒 → 「1 小时 2 分」式估算文案 */
  function fmtDurationCn(sec) {
    if (!isFinite(sec) || sec < 0) return '--';
    sec = Math.round(sec);
    if (sec < 60) return sec + ' 秒';
    var m = Math.floor(sec / 60);
    if (m < 60) return m + ' 分 ' + (sec % 60) + ' 秒';
    var h = Math.floor(m / 60);
    return h + ' 小时 ' + (m % 60) + ' 分';
  }

  function fmtSpeed(ms) {
    if (!isFinite(ms)) return '--';
    return (ms * 3.6).toFixed(1) + ' km/h';
  }

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  function fmtLngLat(lng, lat, digits) {
    var d = digits == null ? 6 : digits;
    if (!isFinite(lng) || !isFinite(lat)) return '--';
    return (+lng).toFixed(d) + ', ' + (+lat).toFixed(d);
  }

  /** 倍速滑块（0..1000）↔ 0.1x..50x 指数映射，低速段更细腻 */
  var SPEED_MIN = 0.1, SPEED_MAX = 50;
  function sliderToSpeed(t) {
    var k = SPEED_MAX / SPEED_MIN;
    return SPEED_MIN * Math.pow(k, clamp(+t, 0, 1));
  }
  function speedToSlider(v) {
    var k = SPEED_MAX / SPEED_MIN;
    return clamp(Math.log(clamp(+v, SPEED_MIN, SPEED_MAX) / SPEED_MIN) / Math.log(k), 0, 1);
  }
  function fmtSpeedScale(v) {
    var x = +v;
    if (x >= 10) return x.toFixed(0) + 'x';
    if (x >= 1) return x.toFixed(1) + 'x';
    return x.toFixed(2) + 'x';
  }

  /** 时间戳 → HH:MM:SS */
  function fmtClock(ts) {
    var d = ts == null ? new Date() : new Date(ts * 1000);
    if (isNaN(d.getTime())) d = new Date();
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  global.Geo = {
    haversine: haversine,
    pathLength: pathLength,
    fmtDistance: fmtDistance,
    fmtDuration: fmtDuration,
    fmtDurationCn: fmtDurationCn,
    fmtSpeed: fmtSpeed,
    fmtLngLat: fmtLngLat,
    fmtClock: fmtClock,
    clamp: clamp,
    sliderToSpeed: sliderToSpeed,
    speedToSlider: speedToSlider,
    fmtSpeedScale: fmtSpeedScale,
    SPEED_MIN: SPEED_MIN,
    SPEED_MAX: SPEED_MAX
  };
})(typeof window !== 'undefined' ? window : globalThis);
