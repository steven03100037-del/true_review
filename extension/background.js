// 真评 · service worker：代 content.js 注入 MAIN world 脚本；上报数据并打开报告页。
(function () {
  "use strict";

  var API_BASE = "http://127.0.0.1:8000";

  chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    // content script 无 chrome.scripting 权限，注入由这里代为执行
    if (msg && msg.type === "INJECT_MAIN" && sender.tab) {
      chrome.scripting.executeScript({
        target: { tabId: sender.tab.id },
        world: "MAIN",
        files: ["inject_main.js"],
      }).then(function () {
        sendResponse({ ok: true });
      }).catch(function (e) {
        sendResponse({ ok: false, error: String(e) });
      });
      return true; // 异步 sendResponse
    }

    if (msg && msg.type === "INGEST") {
      fetch(API_BASE + "/api/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(msg.payload || {}),
      })
        .then(function (r) {
          if (!r.ok) throw new Error("本地服务返回 HTTP " + r.status);
          return r.json();
        })
        .then(function (d) {
          sendResponse({ ok: true, report_id: d.report_id });
          return chrome.tabs.create({
            url: API_BASE + "/report.html?id=" + d.report_id,
          });
        })
        .catch(function (e) {
          sendResponse({ ok: false, error: String(e) });
        });
      return true; // 异步 sendResponse
    }
  });
})();
