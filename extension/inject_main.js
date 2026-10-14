// 真评 · MAIN world 注入脚本：hook XHR/fetch 旁听评价接口 + 自动滚动翻页 + 评论提取。
// 与 V0 backend/fetcher.py 同款纪律：接口匹配规则与字段路径全部集中在常量区，改版只改这里。
// TODO(V0.2)：接口旁听失败（零命中）时的 DOM 解析兜底方案（见 V0.1 改造笔 §6 边界清单）。
(function () {
  "use strict";

  // ==================== 常量区：页面改版时只改这里 ====================

  // 评价数据接口：JSON 递归查找的列表键
  var RATE_LIST_KEYS = ["rateList", "rateInfo", "rateDetailList", "feedbackList", "rateVO"];

  // 单条评论的字段（同义键依次尝试）
  var CONTENT_KEYS = ["content", "rateContent"];
  var DATE_KEYS = ["date", "rateDate", "created"];
  var IMAGES_KEYS = ["images", "pics", "photos"];       // 非空 → tags 加"带图"
  var APPEND_KEYS = ["append", "appendComment", "isAppend"]; // 真 → tags 加"追评"
  var STAR_KEYS = ["star", "rateStar", "score"];

  // 评价 tab 按钮文本（存在才点，用于触发评价模块加载）
  var RATE_TAB_TEXTS = ["用户评价", "全部评价", "累计评价"];

  // 终止条件（与 V0 修复后语义一致）：任一满足即收工
  var COLLECT_MAX_REVIEWS = 200;      // 达 200 条
  var COLLECT_MAX_SECONDS = 60;       // 60s 超时
  var NO_NEW_ROUNDS_GIVEUP = 3;       // 连续 3 轮无新评论

  var SCROLL_WAIT_MIN_MS = 1000;      // 轮间等待下限
  var SCROLL_WAIT_MAX_MS = 2000;      // 轮间等待上限
  var JSON_SCAN_MAX_CHARS = 3000000;  // 单个响应体解析上限
  var JSON_WALK_MAX_DEPTH = 12;
  var POST_SOURCE = "zhenping-inject";

  // ==================== 常量区结束 ====================

  if (window.__ZHENPING_HOOKED__) {
    startCollection(); // 二次触发：hook 已装好，只重开一轮收集
    return;
  }
  window.__ZHENPING_HOOKED__ = true;
  installHooks();
  startCollection();

  function post(type, extra) {
    var msg = { source: POST_SOURCE, type: type };
    if (extra) for (var k in extra) msg[k] = extra[k];
    window.postMessage(msg, "*");
  }

  // ---------------- hook XHR / fetch ----------------

  function installHooks() {
    var xhrProto = XMLHttpRequest.prototype;
    var origOpen = xhrProto.open;
    var origSend = xhrProto.send;
    xhrProto.open = function (method, url) {
      this.__zpUrl = String(url || "");
      return origOpen.apply(this, arguments);
    };
    xhrProto.send = function () {
      this.addEventListener("load", function () {
        try {
          if (this.status >= 200 && this.status < 300) {
            handleText(this.__zpUrl, this.responseText);
          }
        } catch (e) { /* 旁听绝不抛 */ }
      });
      return origSend.apply(this, arguments);
    };

    var origFetch = window.fetch;
    if (origFetch) {
      window.fetch = function () {
        var input = arguments[0];
        var url = String((input && input.url) || input || "");
        return origFetch.apply(this, arguments).then(function (resp) {
          try {
            resp.clone().text().then(function (t) { handleText(url, t); }).catch(function () {});
          } catch (e) { /* 旁听绝不抛 */ }
          return resp;
        });
      };
    }
  }

  // ---------------- 评论提取 ----------------

  function walkRateLists(node, depth, found) {
    if (depth > JSON_WALK_MAX_DEPTH || !node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (var i = 0; i < node.length; i++) walkRateLists(node[i], depth + 1, found);
      return;
    }
    var keys = Object.keys(node);
    for (var j = 0; j < keys.length; j++) {
      var k = keys[j], v = node[k];
      if (RATE_LIST_KEYS.indexOf(k) !== -1 && Array.isArray(v)) {
        for (var m = 0; m < v.length; m++) {
          if (v[m] && typeof v[m] === "object") found.push(v[m]);
        }
      } else {
        walkRateLists(v, depth + 1, found);
      }
    }
  }

  function firstKey(obj, keys) {
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      if (obj[k] !== undefined && obj[k] !== null) return obj[k];
    }
    return null;
  }

  function extractItem(item) {
    var text = firstKey(item, CONTENT_KEYS);
    if (!text || !String(text).trim()) return null;
    var tags = [];
    if (firstKey(item, IMAGES_KEYS)) tags.push("带图");
    if (firstKey(item, APPEND_KEYS)) tags.push("追评");
    var star = firstKey(item, STAR_KEYS);
    star = star === null ? null : Number(star);
    if (star === null || !isFinite(star)) star = null;
    return {
      text: String(text).trim(),
      date: firstKey(item, DATE_KEYS) || null,
      tags: tags,
      star: star,
    };
  }

  function handleText(url, text) {
    if (!text || text.length > JSON_SCAN_MAX_CHARS) return;
    var c = text.charAt(0);
    if (c !== "{" && c !== "[") return;
    var data;
    try { data = JSON.parse(text); } catch (e) { return; }
    var st = window.__ZHENPING_STATE__;
    if (!st || !st.running) return;
    var found = [];
    walkRateLists(data, 0, found);
    var fresh = [];
    for (var i = 0; i < found.length; i++) {
      var r = extractItem(found[i]);
      if (r && !st.seen.has(r.text)) { // 文本去重：同文本只收一条
        st.seen.add(r.text);
        st.reviews.push(r);
        fresh.push(r);
      }
    }
    if (fresh.length) post("DATA", { reviews: fresh, total: st.reviews.length });
  }

  // ---------------- 收集流程：点 tab + 滚动翻页 ----------------

  function clickRateTab() {
    var nodes = document.querySelectorAll('a,button,div,span,li,[role="tab"]');
    for (var i = 0; i < nodes.length; i++) {
      var t = (nodes[i].textContent || "").trim();
      if (RATE_TAB_TEXTS.indexOf(t) !== -1) {
        try { nodes[i].click(); } catch (e) { /* 忽略 */ }
        return true;
      }
    }
    return false;
  }

  function startCollection() {
    var st = {
      seen: new Set(),
      reviews: [],
      roundsNoNew: 0,
      startAt: Date.now(),
      running: true,
    };
    window.__ZHENPING_STATE__ = st;
    post("START", {});
    clickRateTab();

    var tick = function () {
      if (!st.running) return;
      var elapsedSec = (Date.now() - st.startAt) / 1000;
      if (elapsedSec >= COLLECT_MAX_SECONDS) return finish("timeout");
      if (st.reviews.length >= COLLECT_MAX_REVIEWS) return finish("cap");
      if (st.roundsNoNew >= NO_NEW_ROUNDS_GIVEUP) return finish("no-more");

      var prev = st.reviews.length;
      try { window.scrollTo(0, document.body.scrollHeight); } catch (e) { /* 忽略 */ }
      setTimeout(function () {
        if (!st.running) return;
        st.roundsNoNew = st.reviews.length > prev ? 0 : st.roundsNoNew + 1;
        var wait = SCROLL_WAIT_MIN_MS + Math.random() * (SCROLL_WAIT_MAX_MS - SCROLL_WAIT_MIN_MS);
        setTimeout(tick, wait);
      }, 600); // 给懒加载响应留出到达时间，再判定本轮是否有新
    };
    setTimeout(tick, 800);
  }

  function finish(reason) {
    var st = window.__ZHENPING_STATE__;
    if (st) st.running = false;
    post("END", { reason: reason, total: st ? st.reviews.length : 0 });
  }
})();
