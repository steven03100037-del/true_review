// 真评 · content script（isolated world）：流程控制与 MAIN world 桥接。
// 职责：响应 popup 指令 → 触发 inject_main.js 注入 → 汇总收集结果 → 交 background 上报。
(function () {
  "use strict";

  var POST_SOURCE = "zhenping-inject";
  var running = false;
  var collected = new Map(); // text -> review（与 inject 侧文本去重双保险）

  chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (msg && msg.type === "START_ANALYSIS") {
      if (running) {
        sendResponse({ ok: false, msg: "收集已在进行中，请等它结束" });
        return;
      }
      start();
      sendResponse({ ok: true });
    }
  });

  function start() {
    running = true;
    collected.clear();
    status("开始收集，页面会自动滚动，请停留在本页…");
    // 注：MV3 的 chrome.scripting 仅暴露给扩展页面，content script 无权调用，
    // 因此由 background.js 代为执行 executeScript({world:"MAIN"})。
    chrome.runtime.sendMessage({ type: "INJECT_MAIN" }, function (resp) {
      if (chrome.runtime.lastError) {
        running = false;
        status("注入失败：" + chrome.runtime.lastError.message);
        return;
      }
      if (!resp || !resp.ok) {
        running = false;
        status("注入失败：" + ((resp && resp.error) || "未知错误"));
      }
    });
  }

  window.addEventListener("message", function (ev) {
    if (ev.source !== window) return;
    var m = ev.data;
    if (!m || m.source !== POST_SOURCE) return;

    if (m.type === "DATA") {
      var reviews = m.reviews || [];
      for (var i = 0; i < reviews.length; i++) {
        if (reviews[i] && reviews[i].text && !collected.has(reviews[i].text)) {
          collected.set(reviews[i].text, reviews[i]);
        }
      }
      status("已收集 " + collected.size + " 条…");
    } else if (m.type === "END") {
      running = false;
      // DOM 扫描通道（m.reviews）为主，hook 通道（collected）数据并入去重
      var merged = {};
      var order = [];
      var addOne = function (r) {
        if (r && r.text && !merged[r.text]) { merged[r.text] = r; order.push(r); }
      };
      (m.reviews || []).forEach(addOne);
      collected.forEach(addOne);
      var total = order.length;
      if (total === 0) {
        status("未检测到评价数据，正在生成空报告…");
      } else {
        var head =
          m.reason === "timeout" ? "240 秒超时，返回已收集部分" :
          m.reason === "cap" ? "已达 300 条上限" :
          m.reason === "no-more" ? "没有更多评价了" : "收集结束";
        status(head + "（共 " + total + " 条），正在生成报告…");
      }
      chrome.runtime.sendMessage({
        type: "INGEST",
        payload: {
          product: grabProduct(),
          page_url: location.href,
          reviews: order,
        },
      }, function (resp) {
        if (chrome.runtime.lastError) {
          status("上报失败：" + chrome.runtime.lastError.message);
          return;
        }
        if (resp && resp.ok) {
          status("✓ 报告已生成，已在新标签页打开");
        } else {
          status("上报失败：" + ((resp && resp.error) || "本地服务未启动？请先运行 start.bat"));
        }
      });
    }
  });

  function grabProduct() {
    var metaContent = function (sel) {
      var el = document.querySelector(sel);
      return el ? el.content : null;
    };
    return {
      title: metaContent('meta[property="og:title"]') || document.title || null,
      image_url: metaContent('meta[property="og:image"]') || null,
    };
  }

  function status(text) {
    try { chrome.runtime.sendMessage({ type: "STATUS", text: text }); } catch (e) { /* popup 可能已关闭 */ }
  }

  // 页面打开即常驻装好 hook（不等用户点击）：评价数据若在初始加载时就拉取，也能被旁听到。
  // inject_main 二次注入会走 __ZHENPING_HOOKED__ 分支，只重开一轮收集，hook 不重复安装。
  function armHookEarly() {
    chrome.runtime.sendMessage({ type: "INJECT_MAIN" }, function (resp) {
      if (chrome.runtime.lastError || !resp || !resp.ok) {
        setTimeout(armHookEarly, 2000); // 页面尚未就绪时退避重试
      }
    });
  }
  if (document.readyState === "complete") {
    setTimeout(armHookEarly, 1000);
  } else {
    window.addEventListener("load", function () { setTimeout(armHookEarly, 1000); });
  }
})();
