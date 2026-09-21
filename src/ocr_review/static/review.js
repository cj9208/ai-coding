/* 校对台前端：页图 + SVG 块叠加 + 稀疏 review 覆盖层。
 * 坐标系全程用契约像素（svg viewBox = 页宽高），缩放交给 CSS。 */
(function () {
  "use strict";

  var root = document.getElementById("review-root");
  var WS = root.getAttribute("data-ws");
  var KINDS = ["title", "paragraph", "table", "formula", "figure", "caption",
    "list_item", "header", "footer", "page_number", "footnote", "other"];
  var FORMATS = ["text", "markdown", "html", "latex"];

  var S = { doc: null, review: null, base: "", page: 0, sel: null,
    dirty: false, draw: false, nextId: 1 };

  function $(id) { return document.getElementById(id); }
  var overlay = $("overlay"), wrap = $("canvas-wrap"), img = $("page-img");

  /* ------------------------------------------------------------ review helpers */

  function reviewPage(i, create) {
    var pages = S.review.pages;
    for (var k = 0; k < pages.length; k++) if (pages[k].page_index === i) return pages[k];
    if (!create) return null;
    var p = { page_index: i, status: "pending", entries: {} };
    pages.push(p);
    return p;
  }

  function getEntry(pageIdx, id) {
    var p = reviewPage(pageIdx, false);
    return p ? p.entries[id] : undefined;
  }

  function ensureEntry(pageIdx, id) {
    var e = getEntry(pageIdx, id);
    if (!e) {
      e = { state: "corrected", updated_by: null, updated_at: null };
      reviewPage(pageIdx, true).entries[id] = e;
    }
    e.updated_by = null; /* 本次改动由服务端重新署名 */
    return e;
  }

  function pageBlocks(i) {
    var out = [];
    var blocks = docPage(i).blocks;
    var seen = {};
    for (var k = 0; k < blocks.length; k++) {
      var b = blocks[k];
      seen[b.id] = true;
      var e = getEntry(i, b.id);
      var merged = Object.assign({}, b);
      if (e) {
        merged.state = e.state;
        merged.entry = e;
        ["content", "kind", "content_format", "bbox", "order"].forEach(function (f) {
          if (e[f] !== null && e[f] !== undefined) merged[f] = e[f];
        });
      }
      out.push(merged);
    }
    var p = reviewPage(i, false);
    if (p) {
      Object.keys(p.entries).forEach(function (id) {
        var e = p.entries[id];
        if (e.state !== "added" || seen[id]) return;
        out.push({ id: Number(id), raw_label: "human", score: null, added: true,
          state: "added", entry: e, kind: e.kind, content: e.content || "",
          content_format: e.content_format, bbox: e.bbox, order: e.order });
      });
    }
    out.sort(function (a, b) {
      var ua = a.order === null || a.order === undefined;
      var ub = b.order === null || b.order === undefined;
      if (ua !== ub) return ua ? 1 : -1;
      return (ua ? a.id : a.order) - (ub ? b.id : b.order) || a.id - b.id;
    });
    return out;
  }

  function docPage(i) { return S.doc.pages[i]; }

  function currentBlock() {
    if (S.sel === null) return null;
    var list = pageBlocks(S.page);
    for (var k = 0; k < list.length; k++) if (list[k].id === S.sel) return list[k];
    return null;
  }

  /* ---------------------------------------------------------------- rendering */

  function renderPage() {
    var page = docPage(S.page);
    wrap.style.width = $("zoom").value + "%";
    img.src = "/w/" + WS + "/pages/page-" + pad(S.page) + ".png";
    overlay.setAttribute("viewBox", "0 0 " + page.width + " " + page.height);
    overlay.style.height = "100%";
    $("page-select").value = String(S.page);
    $("btn-done").checked =
      (reviewPage(S.page, false) || {}).status === "done";
    renderOverlay();
    renderList();
    renderEditor();
  }

  function pad(i) { return ("000" + i).slice(-3); }

  function renderOverlay() {
    var parts = pageBlocks(S.page).map(function (b) {
      var cls = "bx" + (b.state ? " st-" + b.state : "") + (b.id === S.sel ? " sel" : "");
      var x = b.bbox[0], y = b.bbox[1];
      var w = Math.max(1, b.bbox[2] - b.bbox[0]), h = Math.max(1, b.bbox[3] - b.bbox[1]);
      return '<rect class="' + cls + '" data-id="' + b.id + '" x="' + x + '" y="' + y +
        '" width="' + w + '" height="' + h + '"><title>#' + b.id + " " + b.kind +
        "</title></rect>";
    });
    overlay.innerHTML = parts.join("");
  }

  function stateChip(st) {
    if (st === "kept") return '<span class="tag ok">已接受</span>';
    if (st === "corrected") return '<span class="tag part">已修正</span>';
    if (st === "rejected") return '<span class="tag warn">已删除</span>';
    if (st === "added") return '<span class="tag add">人工新增</span>';
    return "";
  }

  function renderList() {
    var html = pageBlocks(S.page).map(function (b) {
      var text = (b.content || "").replace(/\s+/g, " ").slice(0, 48);
      return '<li data-id="' + b.id + '" class="' + (b.id === S.sel ? "sel" : "") +
        (b.state === "rejected" ? " rejected" : "") + '"><span class="bid">#' + b.id +
        '</span><span class="bkind">' + b.kind + "</span>" + stateChip(b.state) +
        '<div class="btext">' + esc(text) + "</div></li>";
    });
    $("blocklist").innerHTML = html.join("");
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function renderEditor() {
    var b = currentBlock();
    var ed = $("editor");
    ed.hidden = !b;
    if (!b) return;
    $("ed-title").textContent = "块 #" + b.id + (b.added ? "（人工新增）" : "");
    $("ed-state-chip").outerHTML = '<span id="ed-state-chip">' + stateChip(b.state) + "</span>";
    fillSelect($("ed-kind"), KINDS, b.kind);
    fillSelect($("ed-format"), FORMATS, b.content_format);
    $("ed-raw").textContent = b.raw_label;
    $("ed-score").textContent = b.score === null || b.score === undefined ? "—" :
      Number(b.score).toFixed(3);
    $("ed-order").value = b.order === null || b.order === undefined ? "" : b.order;
    $("ed-note").value = (b.entry && b.entry.note) || "";
    $("ed-content").value = b.content;
    var machine = b.added ? "" : docPage(S.page).blocks.filter(function (m) {
      return m.id === b.id;
    })[0].content;
    $("ed-orig").textContent = machine;
    $("ed-orig-box").open = !b.added && machine !== b.content;
    $("ed-render").hidden = true;
  }

  function fillSelect(sel, opts, val) {
    if (sel.options.length !== opts.length) {
      sel.innerHTML = opts.map(function (o) {
        return '<option value="' + o + '">' + o + "</option>";
      }).join("");
    }
    sel.value = opts.indexOf(val) >= 0 ? val : "other";
  }

  function status(text, cls) {
    var el = $("save-status");
    el.textContent = text;
    el.className = "status" + (cls ? " " + cls : "");
  }

  function touch() { S.dirty = true; status("● 未保存", "dirty"); }

  /* ----------------------------------------------------------------- actions */

  function select(id) {
    S.sel = id;
    renderOverlay();
    renderList();
    renderEditor();
  }

  function editField(fn) {
    var b = currentBlock();
    if (!b) return;
    var e = ensureEntry(S.page, b.id);
    if (e.state === "kept") e.state = "corrected";
    fn(e);
    touch();
    renderOverlay();
    renderList();
    $("ed-state-chip").outerHTML = '<span id="ed-state-chip">' + stateChip(e.state) + "</span>";
  }

  function setState(st) {
    var b = currentBlock();
    if (!b || b.added) return;
    var p = reviewPage(S.page, true);
    if (st === null) delete p.entries[b.id];
    else {
      var e = p.entries[b.id] || (p.entries[b.id] = {});
      e.state = st;
      e.updated_by = null;
      if (st === "kept") {
        ["content", "kind", "content_format", "bbox", "order"].forEach(function (f) {
          delete e[f];
        });
      }
    }
    touch();
    renderOverlay();
    renderList();
    renderEditor();
  }

  function save() {
    fetch("/api/w/" + WS + "/review", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review: S.review, base_updated_at: S.base }),
    }).then(function (r) {
      if (r.status === 409) { status("他人已保存，点击“保存”旁重新加载", "conflict"); return; }
      if (!r.ok) return r.json().then(function (d) { throw new Error(JSON.stringify(d)); });
      return r.json();
    }).then(function (d) {
      if (!d) return;
      S.dirty = false;
      status("已保存 " + d.updated_at.slice(11), "ok");
      return load(false);
    }).catch(function (e) { status("保存失败: " + e.message, "conflict"); });
  }

  function load(preserveSelection) {
    fetch("/api/w/" + WS + "/document").then(function (r) {
      return r.json();
    }).then(function (d) {
      S.doc = d.document;
      S.review = d.review;
      S.base = d.review.updated_at;
      if (!preserveSelection) S.sel = null;
      var max = 0;
      S.doc.pages.forEach(function (p) {
        p.blocks.forEach(function (b) { max = Math.max(max, b.id); });
      });
      S.review.pages.forEach(function (p) {
        Object.keys(p.entries).forEach(function (id) { max = Math.max(max, Number(id)); });
      });
      S.nextId = max + 1;
      var sel = $("page-select");
      sel.innerHTML = S.doc.pages.map(function (p, i) {
        return '<option value="' + i + '">第 ' + (i + 1) + " / " + S.doc.pages.length + " 页</option>";
      }).join("");
      renderPage();
      if (!d.source_ok) status("缺少源 PDF：无法显示页图底图", "conflict");
    });
  }

  /* ------------------------------------------------------------- box drawing */

  function svgPoint(evt) {
    // 不用 createSVGPoint/matrixTransform：Chrome 要求 SVGMatrix 参数，
    // 传 DOMMatrix 会抛。viewBox 就是当前页的像素网格，线性换算即可。
    var r = overlay.getBoundingClientRect();
    var page = docPage(S.page);
    return {
      x: (evt.clientX - r.left) * page.width / r.width,
      y: (evt.clientY - r.top) * page.height / r.height
    };
  }

  var drag = null;
  overlay.addEventListener("pointerdown", function (evt) {
    if (!S.draw) {
      var id = evt.target.getAttribute && evt.target.getAttribute("data-id");
      if (id !== null && id !== undefined) select(Number(id));
      return;
    }
    evt.preventDefault();
    overlay.setPointerCapture(evt.pointerId);
    drag = { p0: svgPoint(evt) };
  });
  overlay.addEventListener("pointermove", function (evt) {
    if (!drag) return;
    setTempRect(drag.p0, svgPoint(evt));
  });
  overlay.addEventListener("pointerup", function (evt) {
    if (!drag) return;
    var p0 = drag.p0, p = svgPoint(evt);
    var x1 = Math.min(p0.x, p.x), y1 = Math.min(p0.y, p.y);
    var x2 = Math.max(p0.x, p.x), y2 = Math.max(p0.y, p.y);
    removeTemp();
    if (x2 - x1 > 8 && y2 - y1 > 8) {
      var page = docPage(S.page);
      x1 = clamp(x1, 0, page.width); y1 = clamp(y1, 0, page.height);
      x2 = clamp(x2, 0, page.width); y2 = clamp(y2, 0, page.height);
      reviewPage(S.page, true).entries[String(S.nextId)] = {
        state: "added", kind: "paragraph", content_format: "text",
        content: "", bbox: [x1, y1, x2, y2], order: null,
        note: null, updated_by: null, updated_at: null };
      select(S.nextId);
      S.nextId += 1;
      touch();
    }
    setDraw(false);
    drag = null;
  });

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function setTempRect(a, b) {
    var r = document.getElementById("temp-rect");
    if (!r) {
      r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      r.id = "temp-rect";
      r.setAttribute("class", "temp");
      overlay.appendChild(r);
    }
    r.setAttribute("x", Math.min(a.x, b.x));
    r.setAttribute("y", Math.min(a.y, b.y));
    r.setAttribute("width", Math.abs(b.x - a.x));
    r.setAttribute("height", Math.abs(b.y - a.y));
    return r;
  }

  function removeTemp() {
    var r = document.getElementById("temp-rect");
    if (r) r.remove();
  }

  function setDraw(on) {
    S.draw = on;
    $("btn-add").classList.toggle("active", on);
    overlay.classList.toggle("drawing", on);
  }

  /* -------------------------------------------------------------- event wire */

  function bind() {
    $("page-select").addEventListener("change", function () {
      S.page = Number(this.value);
      renderPage();
    });
    $("btn-prev").addEventListener("click", function () { go(-1); });
    $("btn-next").addEventListener("click", function () { go(1); });
    $("zoom").addEventListener("input", function () {
      wrap.style.width = this.value + "%";
    });
    $("btn-done").addEventListener("change", function () {
      reviewPage(S.page, true).status = this.checked ? "done" : "pending";
      touch();
    });
    $("btn-add").addEventListener("click", function () { setDraw(!S.draw); });
    $("btn-save").addEventListener("click", save);
    $("btn-export").addEventListener("click", function () {
      save();
      setTimeout(function () {
        fetch("/api/w/" + WS + "/export", { method: "POST" })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d.paths) status("已导出: " + d.paths[0], "ok");
            else status(d.detail || "导出失败", "conflict");
          });
      }, 300);
    });
    $("blocklist").addEventListener("click", function (evt) {
      var li = evt.target.closest("li");
      if (li) select(Number(li.getAttribute("data-id")));
    });
    $("ed-content").addEventListener("input", function () {
      var v = this.value;
      editField(function (e) { e.content = v; });
      renderList();
    });
    $("ed-kind").addEventListener("change", function () {
      var v = this.value;
      editField(function (e) { e.kind = v; });
    });
    $("ed-format").addEventListener("change", function () {
      var v = this.value;
      editField(function (e) { e.content_format = v; });
    });
    $("ed-order").addEventListener("change", function () {
      var v = this.value === "" ? null : Number(this.value);
      editField(function (e) { e.order = v; });
    });
    $("ed-note").addEventListener("input", function () {
      var v = this.value;
      editField(function (e) { e.note = v || null; });
    });
    $("ed-keep").addEventListener("click", function () { setState("kept"); });
    $("ed-reject").addEventListener("click", function () { setState("rejected"); });
    $("ed-reset").addEventListener("click", function () { setState(null); });
    $("ed-preview").addEventListener("click", function () {
      var b = currentBlock();
      var fr = $("ed-render");
      fr.hidden = !fr.hidden;
      if (!fr.hidden) {
        var body = b.content_format === "html" ? b.content
          : "<pre>" + esc(b.content) + "</pre>";
        fr.srcdoc = "<!doctype html><meta charset=utf-8><style>body{font:13px/1.5 " +
          "system-ui;margin:8px}table{border-collapse:collapse}td,th{border:1px solid " +
          "#999;padding:2px 6px}</style>" + body;
      }
    });
    document.addEventListener("keydown", function (evt) {
      var t = evt.target.tagName;
      if (t === "INPUT" || t === "TEXTAREA" || t === "SELECT") return;
      var list = pageBlocks(S.page);
      var idx = list.findIndex(function (b) { return b.id === S.sel; });
      if (evt.key === "j") { idx = Math.min(list.length - 1, idx + 1); select(list[Math.max(0, idx)].id); }
      else if (evt.key === "k") { idx = Math.max(0, idx - 1); select(idx >= 0 ? list[idx].id : list[0].id); }
      else if (evt.key === "a" && S.sel !== null) setState("kept");
      else if (evt.key === "x" && S.sel !== null) setState("rejected");
      else if (evt.key === "Escape") { setDraw(false); select(null); }
    });
    window.addEventListener("beforeunload", function (evt) {
      if (S.dirty) { evt.preventDefault(); evt.returnValue = ""; }
    });
    img.onerror = function () {
      status("页图缺失（源 PDF 不在收件箱旁？）", "conflict");
    };
  }

  function go(delta) {
    S.page = clamp(S.page + delta, 0, S.doc.pages.length - 1);
    renderPage();
  }

  bind();
  load(false);
})();
