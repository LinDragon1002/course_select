/* 共用 UI：toast、頂部進度條、表單 loading、確認對話框、側欄。 */
(function () {
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- Toast ---------- */
  var toastRoot = document.getElementById("toast-root");

  function toastIcon(type) {
    var paths = {
      success: '<path d="M20 6 9 17l-5-5"/>',
      error: '<circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/>',
      warning: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4M12 17h.01"/>',
      info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>'
    };
    return '<svg class="toast-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (paths[type] || paths.info) + "</svg>";
  }

  function inferType(raw) {
    var t = String(raw || "info").toLowerCase();
    if (t.indexOf("error") !== -1 || t.indexOf("danger") !== -1) return "error";
    if (t.indexOf("success") !== -1) return "success";
    if (t.indexOf("warning") !== -1) return "warning";
    return "info";
  }

  function defaultDuration(type, message) {
    if (type === "error" && /衝堂/.test(message || "")) return 6000;
    if (type === "error") return 5000;
    return 4000;
  }

  function showToast(opts) {
    if (!toastRoot) return;
    opts = opts || {};
    var type = inferType(opts.type);
    var title = opts.title || ({ success: "完成", error: "無法完成", warning: "請注意", info: "提示" })[type];
    var message = opts.message || "";
    var duration = opts.duration != null ? opts.duration : defaultDuration(type, message);

    var el = document.createElement("div");
    el.className = "toast toast--" + type;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    el.innerHTML =
      toastIcon(type) +
      '<div class="toast-body"><p class="toast-title"></p><p class="toast-message"></p></div>' +
      '<button type="button" class="toast-close" aria-label="關閉通知">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>' +
      "</button>";
    el.querySelector(".toast-title").textContent = title;
    el.querySelector(".toast-message").textContent = message;

    var hideTimer;
    var remaining = duration;
    var startedAt = Date.now();

    function hide() {
      window.clearTimeout(hideTimer);
      el.classList.add("is-leaving");
      window.setTimeout(function () { el.remove(); }, reduced ? 0 : 200);
    }

    function startTimer() {
      startedAt = Date.now();
      hideTimer = window.setTimeout(hide, remaining);
    }

    el.querySelector(".toast-close").addEventListener("click", hide);
    el.addEventListener("mouseenter", function () {
      window.clearTimeout(hideTimer);
      remaining -= Date.now() - startedAt;
    });
    el.addEventListener("mouseleave", startTimer);

    toastRoot.appendChild(el);
    startTimer();
    return el;
  }

  window.showToast = showToast;

  var boot = window.__DJANGO_MESSAGES__ || [];
  boot.forEach(function (item) {
    showToast(item);
  });

  /* ---------- NProgress-like bar ---------- */
  var bar = document.getElementById("nprogress");
  var barInner = bar ? bar.querySelector(".nprogress-bar") : null;
  var progressTimer;
  var progressValue = 0;

  function setProgress(n) {
    if (!bar || !barInner) return;
    progressValue = Math.max(0, Math.min(n, 1));
    bar.hidden = false;
    barInner.style.width = (progressValue * 100) + "%";
  }

  function startProgress() {
    if (!bar) return;
    window.clearInterval(progressTimer);
    setProgress(0.12);
    progressTimer = window.setInterval(function () {
      if (progressValue >= 0.86) return;
      setProgress(progressValue + (0.86 - progressValue) * 0.08);
    }, 180);
  }

  function doneProgress() {
    window.clearInterval(progressTimer);
    setProgress(1);
    window.setTimeout(function () {
      if (bar) bar.hidden = true;
      if (barInner) barInner.style.width = "0%";
      progressValue = 0;
    }, reduced ? 0 : 250);
  }

  window.startPageProgress = startProgress;
  window.donePageProgress = doneProgress;

  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target && a.target !== "_self") return;
    var href = a.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
    try {
      var url = new URL(a.href, window.location.href);
      if (url.origin !== window.location.origin) return;
      if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) return;
    } catch (err) {
      return;
    }
    startProgress();
  });

  window.addEventListener("pageshow", function () { doneProgress(); });

  /* ---------- Form loading ---------- */
  function markButtonLoading(btn) {
    if (!btn || btn.disabled || btn.dataset.loading === "1") return;
    btn.dataset.loading = "1";
    btn.classList.add("is-loading");
    btn.disabled = true;
    if (!btn.querySelector(".spinner")) {
      var s = document.createElement("span");
      s.className = "spinner";
      s.setAttribute("aria-hidden", "true");
      btn.appendChild(s);
    }
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.skipLoading === "1") return;
    startProgress();
    var btn = form.querySelector("button[type='submit'], button:not([type]), input[type='submit']");
    if (form.__pendingSubmitter) btn = form.__pendingSubmitter;
    markButtonLoading(btn);
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("button, input[type='submit']");
    if (!btn || !btn.form) return;
    btn.form.__pendingSubmitter = btn;
  }, true);

  /* ---------- Confirm dialog (退選等危險動作) ---------- */
  var dialog = document.getElementById("confirm-dialog");
  var dialogTitle = dialog ? dialog.querySelector(".modal-title") : null;
  var dialogBody = dialog ? dialog.querySelector(".modal-body") : null;
  var dialogOk = dialog ? dialog.querySelector("[data-confirm-ok]") : null;
  var pendingForm = null;

  function openConfirm(opts) {
    if (!dialog) return Promise.resolve(window.confirm(opts.message));
    dialogTitle.textContent = opts.title || "請確認";
    dialogBody.textContent = opts.message || "";
    dialogOk.textContent = opts.okLabel || "確定";
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    return new Promise(function (resolve) {
      var settled = false;
      function finish(ok) {
        if (settled) return;
        settled = true;
        dialogOk.removeEventListener("click", onOk);
        dialog.removeEventListener("close", onCancel);
        dialog.removeEventListener("cancel", onCancel);
        if (dialog.open && typeof dialog.close === "function") dialog.close();
        resolve(ok);
      }
      function onOk() { finish(true); }
      function onCancel() { finish(false); }
      dialogOk.addEventListener("click", onOk);
      dialog.addEventListener("close", onCancel);
      dialog.addEventListener("cancel", onCancel);
    });
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    var msg = form.getAttribute("data-confirm");
    if (!msg || form.dataset.confirmed === "1") return;
    e.preventDefault();
    e.stopImmediatePropagation();
    pendingForm = form;
    openConfirm({
      title: form.getAttribute("data-confirm-title") || "確認退選",
      message: msg,
      okLabel: form.getAttribute("data-confirm-ok") || "確定退選"
    }).then(function (ok) {
      if (!ok || !pendingForm) return;
      pendingForm.dataset.confirmed = "1";
      pendingForm.dataset.skipLoading = "";
      if (typeof pendingForm.requestSubmit === "function") pendingForm.requestSubmit();
      else pendingForm.submit();
    });
  }, true);

  var cancelBtn = dialog ? dialog.querySelector("[data-confirm-cancel]") : null;
  if (cancelBtn) {
    cancelBtn.addEventListener("click", function () {
      if (dialog && typeof dialog.close === "function") dialog.close();
    });
  }

  /* ---------- Sidebar (mobile) ---------- */
  var sidebar = document.getElementById("app-sidebar");
  var backdrop = document.getElementById("sidebar-backdrop");
  var toggle = document.getElementById("sidebar-toggle");
  var collapseToggle = document.getElementById("sidebar-collapse-toggle");

  function setSidebar(open) {
    document.body.classList.toggle("sidebar-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (backdrop) backdrop.hidden = !open;
  }

  if (toggle) {
    toggle.addEventListener("click", function () {
      setSidebar(!document.body.classList.contains("sidebar-open"));
    });
  }
  if (collapseToggle) {
    collapseToggle.addEventListener("click", function () {
      var collapsed = document.body.classList.toggle("sidebar-collapsed");
      collapseToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      collapseToggle.setAttribute("aria-label", collapsed ? "展開側邊欄" : "收合側邊欄");
      collapseToggle.setAttribute("title", collapsed ? "展開側邊欄" : "收合側邊欄");
    });
  }
  if (sidebar) {
    sidebar.addEventListener("click", function (e) {
      if (e.target.closest("a")) setSidebar(false);
    });
  }
  if (backdrop) backdrop.addEventListener("click", function () { setSidebar(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") setSidebar(false);
  });
})();
