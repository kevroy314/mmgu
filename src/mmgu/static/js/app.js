/* Hallkeeper client helpers: toasts, local times and countdowns, search shortcut, theme, uploads. */
(function () {
  "use strict";

  // ---------- toasts ----------
  function toast(message, kind) {
    var box = document.getElementById("toasts");
    if (!box || !message) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "ok");
    el.setAttribute("role", "status");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 5200);
  }
  window.hallToast = toast;
  document.body.addEventListener("toast", function (e) { toast(e.detail.message, e.detail.kind); });
  function flash() {
    var raw = document.body.dataset.flash;
    if (!raw) return;
    try { var f = JSON.parse(raw); toast(f.message, f.kind); } catch (e) {}
    delete document.body.dataset.flash;
  }

  // ---------- times ----------
  var rtf = typeof Intl !== "undefined" && Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }) : null;
  function rel(date) {
    var s = (date - Date.now()) / 1000, a = Math.abs(s);
    if (!rtf) return date.toLocaleString();
    if (a < 45) return rtf.format(Math.round(s), "second");
    if (a < 2700) return rtf.format(Math.round(s / 60), "minute");
    if (a < 72000) return rtf.format(Math.round(s / 3600), "hour");
    if (a < 86400 * 26) return rtf.format(Math.round(s / 86400), "day");
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }
  function dur(ms) {
    var neg = ms < 0; ms = Math.abs(ms);
    var s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    var out = d ? d + "d " + h + "h" : h ? h + "h " + String(m).padStart(2, "0") + "m" : m + "m " + String(sec).padStart(2, "0") + "s";
    return (neg ? "-" : "") + out;
  }
  function renderTimes(root) {
    (root || document).querySelectorAll("time[data-local]").forEach(function (t) {
      var d = new Date(t.getAttribute("datetime"));
      if (!isNaN(d)) t.textContent = d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    });
    (root || document).querySelectorAll("time[data-rel]").forEach(function (t) {
      var d = new Date(t.getAttribute("datetime"));
      if (!isNaN(d)) { t.textContent = rel(d); t.title = d.toLocaleString(); }
    });
  }
  function tickCountdowns() {
    document.querySelectorAll("time[data-countdown]").forEach(function (t) {
      var d = new Date(t.getAttribute("datetime"));
      if (isNaN(d)) return;
      var ms = d - Date.now();
      var open = t.dataset.openLabel || "now";
      t.textContent = ms <= 0 ? open : dur(ms);
      t.classList.toggle("countdown-open", ms <= 0);
      t.classList.toggle("countdown-soon", ms > 0 && ms < 15 * 60 * 1000);
    });
  }
  setInterval(tickCountdowns, 1000);
  setInterval(function () { renderTimes(); }, 60000);

  // ---------- navigation ----------
  function markRoom() {
    var path = location.pathname;
    document.querySelectorAll(".room").forEach(function (a) {
      var href = a.getAttribute("href");
      a.classList.toggle("is-here", href === path || (href !== "/" && path.indexOf(href) === 0));
    });
    document.body.classList.remove("nav-open");
    var main = document.getElementById("main");
    if (main && main.dataset.title) document.title = main.dataset.title;
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-nav-toggle]")) { document.body.classList.toggle("nav-open"); return; }
    if (document.body.classList.contains("nav-open") && !e.target.closest(".rail")) document.body.classList.remove("nav-open");
    var tt = e.target.closest("[data-theme-toggle]");
    if (tt) {
      var cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
      var next = cur === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("mmgu-theme", next); } catch (err) {}
    }
    var copy = e.target.closest("[data-copy]");
    if (copy) {
      var text = copy.getAttribute("data-copy");
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { toast("Copied"); }, function () { toast(text); });
    }
    var modalClose = e.target.closest("[data-close-modal]");
    if (modalClose) { var m = document.getElementById("modal"); if (m && m.open) m.close(); }
  });

  // "/" focuses search; Escape closes quick results
  document.addEventListener("keydown", function (e) {
    var tag = (e.target.tagName || "").toLowerCase();
    if (e.key === "/" && tag !== "input" && tag !== "textarea" && tag !== "select") {
      var s = document.getElementById("global-search");
      if (s) { e.preventDefault(); s.focus(); }
    }
    if (e.key === "Escape") { var q = document.getElementById("quick-results"); if (q) q.hidden = true; }
  });
  document.addEventListener("focusout", function (e) {
    if (e.target.id === "global-search") setTimeout(function () {
      var q = document.getElementById("quick-results");
      if (q && !q.contains(document.activeElement)) q.hidden = true;
    }, 150);
  });

  // ---------- image upload: drag/drop, paste, preview, crop ----------
  function setupDropzone(dz) {
    if (dz.dataset.ready) return;
    dz.dataset.ready = "1";
    var input = dz.querySelector("input[type=file]");
    var preview = dz.parentElement.querySelector("[data-preview]");
    var cropField = dz.parentElement.querySelector("input[name=crop]");
    function show(file) {
      if (!file || !preview) return;
      var url = URL.createObjectURL(file);
      preview.hidden = false;
      preview.innerHTML = "";
      var wrap = document.createElement("div");
      wrap.className = "cropper";
      var img = document.createElement("img");
      img.src = url; img.alt = "Screenshot preview";
      var box = document.createElement("div"); box.className = "box"; box.hidden = true;
      wrap.appendChild(img); wrap.appendChild(box); preview.appendChild(wrap);
      var hint = document.createElement("p"); hint.className = "hint tiny muted";
      hint.textContent = "Optional: drag across the image to keep only the item window. Nothing outside the box is saved.";
      preview.appendChild(hint);
      if (cropField) enableCrop(wrap, img, box, cropField);
    }
    dz.addEventListener("dragover", function (e) { e.preventDefault(); dz.classList.add("is-over"); });
    dz.addEventListener("dragleave", function () { dz.classList.remove("is-over"); });
    dz.addEventListener("drop", function (e) {
      e.preventDefault(); dz.classList.remove("is-over");
      if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(input.files[0]); }
    });
    input.addEventListener("change", function () { if (input.files.length) show(input.files[0]); });
    document.addEventListener("paste", function (e) {
      if (!document.body.contains(dz)) return;
      var items = (e.clipboardData || {}).items || [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].type.indexOf("image") === 0) {
          var f = items[i].getAsFile();
          var dt = new DataTransfer(); dt.items.add(new File([f], "pasted.png", { type: f.type }));
          input.files = dt.files; show(input.files[0]); toast("Screenshot pasted"); break;
        }
      }
    });
  }
  function enableCrop(wrap, img, box, field) {
    var start = null;
    function pt(e) { var r = img.getBoundingClientRect(); return { x: Math.max(0, Math.min(r.width, e.clientX - r.left)), y: Math.max(0, Math.min(r.height, e.clientY - r.top)) }; }
    wrap.addEventListener("pointerdown", function (e) { start = pt(e); wrap.setPointerCapture(e.pointerId); box.hidden = false; });
    wrap.addEventListener("pointermove", function (e) {
      if (!start) return; var p = pt(e);
      var x = Math.min(start.x, p.x), y = Math.min(start.y, p.y), w = Math.abs(p.x - start.x), h = Math.abs(p.y - start.y);
      box.style.left = x + "px"; box.style.top = y + "px"; box.style.width = w + "px"; box.style.height = h + "px";
      var sx = img.naturalWidth / img.clientWidth, sy = img.naturalHeight / img.clientHeight;
      field.value = w > 10 && h > 10 ? [Math.round(x * sx), Math.round(y * sy), Math.round(w * sx), Math.round(h * sy)].join(",") : "";
    });
    wrap.addEventListener("pointerup", function () { start = null; if (!field.value) box.hidden = true; });
  }

  function init(root) {
    renderTimes(root);
    tickCountdowns();
    (root || document).querySelectorAll(".dropzone").forEach(setupDropzone);
    var q = document.getElementById("quick-results");
    if (q && q.children.length) q.hidden = false;
  }

  document.addEventListener("DOMContentLoaded", function () { init(document); flash(); markRoom(); });
  document.addEventListener("htmx:afterSettle", function (e) { init(e.target); markRoom(); });
  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.target.id === "modal-body") { var m = document.getElementById("modal"); if (m && !m.open) m.showModal(); }
  });
  document.addEventListener("htmx:responseError", function (e) {
    if (e.detail.xhr.status >= 500) toast("The hall hit a snag (" + e.detail.xhr.status + "). Try again, or tell an officer.", "err");
  });
  document.addEventListener("htmx:sendError", function () { toast("Can't reach the hall. Check your connection.", "err"); });
})();
