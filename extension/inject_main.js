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

  // DOM 扫描：评价卡片选择器。CSS modules 的哈希后缀会随版本变，用 -- 前缀匹配；
  // 实测卡片 class "comments--xxx"，父容器 "Comments--xxx"（大写 C，不会被误匹配）。
  var CARD_SELECTOR = '[class*="comments--"]';

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

    // ---- 探针 A：评价内容特征词检测（响应体任何位置）----
    try {
      var feats = [];
      if (text.indexOf("好评率") !== -1) feats.push("好评率");
      if (text.indexOf("外观好看") !== -1) feats.push("外观好看");
      if (text.indexOf("回头客") !== -1) feats.push("回头客");
      if (text.indexOf("使用体验") !== -1) feats.push("使用体验");
      if (feats.length) {
        console.log("[ZHENPING] ★特征命中 [" + feats.join("/") + "]",
                    String(url || "").slice(0, 110), "| 响应长度:", text.length);
      }
    } catch (e) { /* 忽略 */ }

    // ---- 探针 B：mtop 响应的 data 键树（两层），定位评价模块的容器路径 ----
    try {
      if (data && data.data && typeof data.data === "object") {
        var dk = Object.keys(data.data);
        console.log("[ZHENPING] data子键:", dk.slice(0, 14).join(","),
                    "| url:", String(url || "").split("?")[0].slice(0, 100));
        for (var di = 0; di < dk.length && di < 14; di++) {
          var dv = data.data[dk[di]];
          if (dv && typeof dv === "object" && !Array.isArray(dv)) {
            console.log("    data." + dk[di] + " →", Object.keys(dv).slice(0, 10).join(","));
          }
        }
      }
    } catch (e) { /* 忽略 */ }

    var st = window.__ZHENPING_STATE__;
    if (!st || !st.running) return;
    var found = [];
    walkRateLists(data, 0, found);
    if (found.length) {
      try {
        console.log("[ZHENPING] HIT len=" + found.length, String(url || "").slice(0, 110),
                    "| itemKeys:", found[0] ? Object.keys(found[0]).slice(0, 14).join(",") : "");
      } catch (e) { /* 忽略 */ }
    }
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

  function clickTabByText(texts) {
    var nodes = document.querySelectorAll('a,button,div,span,li,[role="tab"]');
    for (var i = 0; i < nodes.length; i++) {
      var t = (nodes[i].textContent || "").trim();
      if (texts.indexOf(t) !== -1) {
        try { nodes[i].click(); } catch (e) { /* 忽略 */ }
        return true;
      }
    }
    return false;
  }

  function clickRateTab() {
    return clickTabByText(RATE_TAB_TEXTS);
  }

  // 打开"查看全部评论"完整面板（文本包含匹配，按钮文案可能带数量）
  function clickSeeAllComments() {
    var nodes = document.querySelectorAll('a,button,div,span,li,[role="button"]');
    for (var i = 0; i < nodes.length; i++) {
      var t = (nodes[i].textContent || "").trim();
      if (t.indexOf("查看全部评论") !== -1 && t.length < 24) {
        try { nodes[i].click(); } catch (e) { /* 忽略 */ }
        return true;
      }
    }
    return false;
  }

  // ---------------- DOM 扫描：直接读已渲染的评价卡片（SSR/接口来源通吃） ----------------

  var DATE_RE = /(\d{4}-\d{2}-\d{2})|(\d{4}年\d{1,2}月\d{1,2}日)/; // 兼容 2026-09-23 与 2026年9月23日

  function scanCommentsDOM(st) {
    // 启发式找卡片：含日期文本的最内层块（不绑 class，抗改版）；
    // 面板里每条含：昵称/日期/已购SKU/正文/商家回复——正文为主，"商家回复"之后截断
    var nodes = document.querySelectorAll("div,li");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var t = el.textContent || "";
      if (!DATE_RE.test(t) || t.length > 900 || t.length < 30) continue;
      // 最内层：子 div/li 里也存在日期匹配 → 本元素是外层，跳过
      var subs = el.querySelectorAll("div,li");
      var hasInner = false;
      for (var j = 0; j < subs.length; j++) {
        if (DATE_RE.test(subs[j].textContent || "")) { hasInner = true; break; }
      }
      if (hasInner) continue;

      var clean = t.replace(/\s+/g, " ").trim();
      clean = clean.split("商家回复")[0].trim();
      if (clean.length < 15 || st.seen.has(clean)) continue;
      var tags = [];
      try { if (el.querySelector("img")) tags.push("带图"); } catch (e) { /* 忽略 */ }
      if (t.indexOf("追评") !== -1) tags.push("追评");
      st.seen.add(clean);
      st.reviews.push({ text: clean, date: null, tags: tags, star: null });
    }
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
    // 强制重拉评价数据：先点走当前 tab 再点回评价（当前已在评价 tab 时重复点击不会重新请求）
    var switchedAway = clickTabByText(["图文详情", "商品详情", "参数信息"]);
    if (switchedAway) {
      setTimeout(function () { clickRateTab(); }, 600);
    } else {
      clickRateTab();
    }

    var tick = function () {
      if (!st.running) return;
      var elapsedSec = (Date.now() - st.startAt) / 1000;
      if (elapsedSec >= COLLECT_MAX_SECONDS) return finish("timeout");
      if (st.reviews.length >= COLLECT_MAX_REVIEWS) return finish("cap");
      if (st.roundsNoNew >= NO_NEW_ROUNDS_GIVEUP) return finish("no-more");

      scanCommentsDOM(st); // 每轮先扫 DOM（含初始 SSR 直出的评价卡片）

      var prev = st.reviews.length;
      try { window.scrollTo(0, document.body.scrollHeight); } catch (e) { /* 忽略 */ }
      // 评价列表是"容器内滚动条"形态（beautify-scroll-bar）：把每个评价容器内部也滚到底，触发懒加载
      try {
        var cs = document.querySelectorAll(CARD_SELECTOR);
        for (var ci = 0; ci < cs.length; ci++) {
          cs[ci].scrollTop = cs[ci].scrollHeight;
        }
      } catch (e) { /* 忽略 */ }
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
    post("END", { reason: reason, total: st ? st.reviews.length : 0,
                  reviews: st ? st.reviews : [] });
  }
})();
