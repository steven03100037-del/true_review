// 真评 · popup：发起分析、实时显示收集状态。
(function () {
  "use strict";

  var ITEM_URL_RE = /^https:\/\/(detail\.tmall\.com\/item\.htm|item\.taobao\.com\/item\.htm)/;
  var btn = document.getElementById("go");
  var statusEl = document.getElementById("status");

  function setStatus(text, isErr) {
    statusEl.textContent = text || "";
    statusEl.className = isErr ? "err" : "";
  }

  btn.addEventListener("click", function () {
    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
      var tab = tabs && tabs[0];
      if (!tab || !ITEM_URL_RE.test(tab.url || "")) {
        setStatus("请先打开天猫/淘宝商品页，再点「开始分析」", true);
        return;
      }
      btn.disabled = true;
      setStatus("通知页面开始收集…");
      chrome.tabs.sendMessage(tab.id, { type: "START_ANALYSIS" }, function (resp) {
        if (chrome.runtime.lastError || !resp || !resp.ok) {
          btn.disabled = false;
          setStatus(
            (resp && resp.msg) || "页面未就绪，请刷新商品页后重试",
            true
          );
          return;
        }
        setStatus("收集中…");
      });
    });
  });

  // content.js 的进度推送（popup 开着才收得到）
  chrome.runtime.onMessage.addListener(function (msg) {
    if (msg && msg.type === "STATUS") {
      var isErr = /^失败|^未检测/.test(msg.text || "");
      setStatus(msg.text, isErr);
      if (/✓|失败/.test(msg.text || "")) btn.disabled = false;
    }
  });
})();
