/* MyOtherApp document designs: long text fits, then the page says it is ready
   (app/core/doc_designs/fit.js; contract D4 d). Injected once into every page.

   After the fonts and images have loaded, each [data-fit] element is fitted:
     name   one line; the font shrinks until it fits (to data-fit-min, default
            60%), then an ellipsis. Names and roles. A name on two lines
            (first<br>last) is measured by its wider line.
     code   one line; shrinks until it fits and never truncates (ticket code,
            certificate no., badge id).
     title  a ticket's event name: one line down to 60%, then two lines, then
            smaller still (to 40%) until two lines fit. With data-fit-wrap
            (designs whose title block is narrow on purpose) two lines at
            the full size come first, as the designer laid them out.
     wrap   up to data-fit-lines lines (default 2); shrinks until it fits.
     line   other values: one line, and an ellipsis once it runs out of room.

   "Fits" is what the page shows, not a count of characters: the element has
   no text running out of itself (unless it ends in an ellipsis), it stays
   inside the nearest ancestor that clips and inside its box (the nearest
   [data-fit-box] ancestor's content box) if it has one, it pushes no other
   text in that box out of view (a single-line value: none in its clipping
   ancestor), and it overlaps no other text. A value that already fits is not touched,
   so the designer's layout is exactly as drawn for ordinary values. What is
   fitted gets data-fitted="<mode>". Titles go first, then wrapped text,
   names and codes, then single-line values widest first: the value most
   likely to be crowding the others gives way first.

   A layout v2 page (a .moa-doc inside the .moa-page; app/core/layout_v2)
   has its own modes, shrink | ellipsis | none: each text layer fits its own
   box and nothing else is looked at (fitLayer, below).

   Then window.__moaReady = true: the render service waits for exactly that.
   A font that failed to load throws, so the render fails instead of printing
   in a stand-in font. */
(function () {
  "use strict";
  var MIN = { name: 0.6, code: 0.3, title: 0.6, wrap: 0.5 };
  var TOL = 0.5;
  var page, leaves, pending;

  function num(v) { return parseFloat(v) || 0; }
  function css(el) { return getComputedStyle(el); }

  function contentRect(b) {
    var r = b.getBoundingClientRect(), s = css(b);
    return {
      left: r.left + num(s.borderLeftWidth) + num(s.paddingLeft),
      right: r.right - num(s.borderRightWidth) - num(s.paddingRight),
      top: r.top + num(s.borderTopWidth) + num(s.paddingTop),
      bottom: r.bottom - num(s.borderBottomWidth) - num(s.paddingBottom)
    };
  }

  function clips(a) { var s = css(a); return s.overflowX !== "visible" || s.overflowY !== "visible"; }

  /* Where an element's text may be: the nearest ancestor that clips, or the page. */
  function clipRect(el) {
    for (var a = el.parentElement; a && a !== page; a = a.parentElement) {
      if (clips(a)) { return a.getBoundingClientRect(); }
    }
    return page.getBoundingClientRect();
  }

  function clipOwner(el) {
    for (var a = el.parentElement; a && a !== page; a = a.parentElement) {
      if (clips(a)) { return a; }
    }
    return page;
  }

  function within(r, b) {
    return r.left >= b.left - TOL && r.right <= b.right + TOL && r.top >= b.top - TOL && r.bottom <= b.bottom + TOL;
  }

  function overlap(a, b) {
    return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
  }

  function ellipsis(el) { var s = css(el); return s.textOverflow === "ellipsis" && s.overflowX !== "visible"; }

  function shown(el) {
    for (var e = el; e && e !== page; e = e.parentElement) {
      if (e.getAttribute("aria-hidden") === "true") { return false; }
      var s = css(e);
      if (s.visibility === "hidden" || s.display === "none") { return false; }
    }
    return true;
  }

  /* The page's visible text, element by element (computed once). */
  function textLeaves() {
    var out = [], all = page.querySelectorAll("*");
    for (var i = 0; i < all.length; i++) {
      var e = all[i], own = false;
      if (e.closest("svg")) { continue; }
      for (var c = e.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 3 && c.textContent.trim()) { own = true; break; }
      }
      if (own && shown(e)) { out.push(e); }
    }
    return out;
  }

  /* An element's text as drawn: its text's box, or (cut by an ellipsis) its own. */
  function textRect(e) {
    if (ellipsis(e)) { return e.getBoundingClientRect(); }
    var range = document.createRange(), box = null;
    for (var c = e.firstChild; c; c = c.nextSibling) {
      if (c.nodeType !== 3 || !c.textContent.trim()) { continue; }
      range.selectNodeContents(c);
      var r = range.getBoundingClientRect();
      box = box ? { left: Math.min(box.left, r.left), right: Math.max(box.right, r.right),
                    top: Math.min(box.top, r.top), bottom: Math.max(box.bottom, r.bottom) } : r;
    }
    return box || e.getBoundingClientRect();
  }

  function vertical(el) { return css(el).writingMode.indexOf("vertical") === 0; }

  function fits(el) {
    var own = el.closest("[data-fit-box]");
    if (vertical(el)) {
      if (el.scrollHeight > el.clientHeight + 1) { return false; }
    } else if (!ellipsis(el) && el.scrollWidth > el.clientWidth + 1) {  /* integers, rounded */
      return false;
    }
    if (own === el) { return true; } /* its own box (a sash running off the card): only its text counts */
    var r = el.getBoundingClientRect();
    if (!within(r, clipRect(el)) || (own && !within(r, contentRect(own)))) { return false; }
    /* Pushing other text out of view is judged in a box the template names,
       or for a single-line value (fitted last, when the rest is settled). */
    var scope = own || (el.getAttribute("data-fit") === "line" ? clipOwner(el) : null);
    for (var i = 0; i < leaves.length; i++) {
      var o = leaves[i];
      if (o === el || el.contains(o) || o.contains(el)) { continue; }
      /* A value not fitted yet answers for itself in its own turn. */
      var owner = o.closest("[data-fit]");
      if (owner && pending.has(owner)) { continue; }
      /* What it may have pushed out of view: text in the same box. */
      if (scope && scope.contains(o) && !within(textRect(o), clipRect(o))) { return false; }
      if (overlap(r, o.getBoundingClientRect())) { return false; }
    }
    return true;
  }

  /* Lines of text as laid out (not the box's height: some boxes are fixed). */
  function lines(el) {
    var s = css(el);
    var lh = s.lineHeight === "normal" ? 1.2 * num(s.fontSize) : num(s.lineHeight);
    var range = document.createRange();
    range.selectNodeContents(el);
    var tops = Array.prototype.map.call(range.getClientRects(), function (r) { return r.height > 0 ? r.top : null; })
      .filter(function (t) { return t !== null; }).sort(function (a, b) { return a - b; });
    var count = 0, last = -Infinity;
    tops.forEach(function (t) { if (t - last > lh * 0.5) { count += 1; last = t; } });
    return count;
  }

  /* The largest value in [lo, hi] at which ok() holds, set through set();
     false (and lo set) if none does. */
  function search(set, ok, lo, hi) {
    set(hi);
    if (ok()) { return true; }
    set(lo);
    if (!ok()) { return false; }
    for (var i = 0; i < 14 && hi - lo > 0.25; i++) {
      var mid = (lo + hi) / 2;
      set(mid);
      if (ok()) { lo = mid; } else { hi = mid; }
    }
    set(lo);
    return true;
  }

  function sizeSetter(el) { return function (v) { el.style.fontSize = v + "px"; }; }

  /* Narrow the element (with an ellipsis) until it fits. */
  function squeeze(el) {
    el.style.overflow = "hidden";
    el.style.textOverflow = "ellipsis";
    if (css(el).display === "inline") { el.style.display = "inline-block"; }
    var width = el.getBoundingClientRect().width;
    search(function (v) { el.style.maxWidth = v + "px"; }, function () { return fits(el); }, 0, width);
  }

  function minFor(el, mode, size) {
    var m = el.getAttribute("data-fit-min");
    var v = m ? (m.slice(-1) === "%" ? size * num(m) / 100 : num(m)) : size * MIN[mode];
    /* An 18 px floor on 13 px text is no floor: then the usual share. */
    return v < size ? v : size * MIN[mode];
  }

  function mark(el, mode) { el.setAttribute("data-fitted", mode); }

  function fitTitle(el, size) {
    var min = minFor(el, "title", size);
    var ok = function () { return fits(el); };
    var two = function () { return fits(el) && lines(el) <= 2; };
    if (el.hasAttribute("data-fit-wrap")) {
      if (two()) { return; }
    }
    el.style.whiteSpace = "nowrap";
    if (ok()) { el.style.whiteSpace = ""; return; }
    mark(el, "title");
    if (search(sizeSetter(el), ok, min, size)) { return; }
    el.style.whiteSpace = "normal";
    if (!search(sizeSetter(el), two, size * 0.4, min)) { squeeze(el); }
  }

  function fit(el) {
    if (!shown(el)) { return; }
    var mode = el.getAttribute("data-fit");
    var size = num(css(el).fontSize);
    var ok = function () { return fits(el); };
    if (mode === "title") { fitTitle(el, size); return; }
    if (mode === "wrap") {
      var max = parseInt(el.getAttribute("data-fit-lines") || "2", 10);
      var okw = function () { return fits(el) && lines(el) <= max; };
      if (okw()) { return; }
      mark(el, mode);
      search(sizeSetter(el), okw, minFor(el, mode, size), size);
      return;
    }
    if (ok()) { return; }
    mark(el, mode);
    if (mode === "line") {
      squeeze(el);
    } else if (mode === "name") {
      if (!search(sizeSetter(el), ok, minFor(el, mode, size), size)) { squeeze(el); }
    } else if (mode === "code") {
      search(sizeSetter(el), ok, minFor(el, mode, size), size);
    }
  }

  /* Layout v2 (app/core/layout_v2/build.py): a text layer fits its own box,
     and nothing else is looked at (layers may overlap by design).
       shrink    1 px at a time, down to data-min, while the text has more
                 than data-lines lines or is taller than its box; then, if it
                 still does not fit, as ellipsis.
       ellipsis  clamped to data-lines lines (fewer if the box is shorter),
                 the last one ending in an ellipsis.
       none      as it is.
     Heights are the layer's own pixels (offsetHeight), so a page printed at
     another scale fits the same. */
  function v2LineHeight(el) {
    var s = css(el);
    return s.lineHeight === "normal" ? 1.2 * num(s.fontSize) : num(s.lineHeight);
  }

  function v2Lines(el) {
    var scale = el.offsetHeight ? el.getBoundingClientRect().height / el.offsetHeight : 1;
    var lh = v2LineHeight(el) * (scale || 1);
    var range = document.createRange();
    range.selectNodeContents(el);
    var tops = Array.prototype.map.call(range.getClientRects(), function (r) { return r.height > 0 ? r.top : null; })
      .filter(function (t) { return t !== null; }).sort(function (a, b) { return a - b; });
    var count = 0, last = -Infinity;
    tops.forEach(function (t) { if (t - last > lh * 0.5) { count += 1; last = t; } });
    return count;
  }

  function v2Room(el) {
    var box = el.parentElement, s = css(box);
    return box.clientHeight - num(s.paddingTop) - num(s.paddingBottom);
  }

  function v2Over(el, lines) {
    return v2Lines(el) > lines || el.offsetHeight > v2Room(el) + 1;
  }

  function v2Clamp(el, lines) {
    var fit = Math.max(1, Math.min(lines, Math.floor((v2Room(el) + 1) / v2LineHeight(el))));
    el.style.display = "-webkit-box";
    el.style.webkitBoxOrient = "vertical";
    el.style.webkitLineClamp = String(fit);
    el.style.overflow = "hidden";
  }

  function fitLayer(el) {
    var mode = el.getAttribute("data-fit");
    if (mode === "none") { return; }
    var lines = parseInt(el.getAttribute("data-lines") || "1", 10) || 1;
    if (!v2Over(el, lines)) { return; }
    mark(el, mode);
    if (mode === "shrink") {
      var size = num(css(el).fontSize), min = num(el.getAttribute("data-min")) || size;
      while (size - 1 >= min && v2Over(el, lines)) { size -= 1; el.style.fontSize = size + "px"; }
      if (v2Over(el, lines) && size > min) { size = min; el.style.fontSize = size + "px"; }
      if (!v2Over(el, lines)) { return; }
    }
    v2Clamp(el, lines);
  }

  var V2 = { shrink: 1, ellipsis: 1, none: 1 };

  function fitV2(p) {
    Array.prototype.forEach.call(p.querySelectorAll("[data-fit]"), function (el) {
      if (V2[el.getAttribute("data-fit")]) { fitLayer(el); }
    });
  }

  var ORDER = { title: 0, wrap: 1, name: 2, code: 3, line: 4 };

  function inOrder(p) {
    var els = Array.prototype.slice.call(p.querySelectorAll("[data-fit]"));
    var width = function (e) { return e.scrollWidth; };
    return els.map(function (e, i) { return { e: e, i: i, w: width(e), o: ORDER[e.getAttribute("data-fit")] }; })
      .sort(function (a, b) { return a.o - b.o || (a.o === 4 ? b.w - a.w : a.i - b.i); })
      .map(function (x) { return x.e; });
  }

  function done() {
    window.__moaReady = true;
    document.documentElement.setAttribute("data-moa-ready", "");
  }

  /* For tests and debugging: why an element does or does not fit. */
  window.__moaFit = {
    explain: function (el) {
      page = el.closest(".moa-page"); leaves = leaves || textLeaves(); pending = pending || new Set();
      var own = el.closest("[data-fit-box]"), r = el.getBoundingClientRect(), out = [];
      if (!ellipsis(el) && el.scrollWidth > el.clientWidth + 1) { out.push("overflows itself " + el.scrollWidth + ">" + el.clientWidth); }
      if (!within(r, clipRect(el)) || (own && !within(r, contentRect(own)))) { out.push("outside its box"); }
      var scope = own || (el.getAttribute("data-fit") === "line" ? clipOwner(el) : null);
      leaves.forEach(function (o) {
        if (o === el || el.contains(o) || o.contains(el)) { return; }
        if (scope && scope.contains(o) && !within(textRect(o), clipRect(o))) { out.push("pushed: " + o.textContent.trim().slice(0, 30)); }
        if (overlap(r, o.getBoundingClientRect())) { out.push("overlaps: " + o.textContent.trim().slice(0, 30)); }
      });
      return out;
    }
  };

  function fitPage(p) {
    if (p.querySelector(".moa-doc")) { fitV2(p); return; }
    page = p;
    leaves = textLeaves();
    var order = inOrder(p);
    pending = new Set(order);
    order.forEach(function (el) { pending.delete(el); fit(el); });
  }

  function run() {
    void document.documentElement.offsetWidth; /* lay out, so every font in use is asked for */
    var images = Array.prototype.map.call(document.images, function (img) {
      return img.decode ? img.decode().catch(function () {}) : null;
    });
    Promise.all(images).then(function () { return document.fonts.ready; }).then(function () {
      Array.prototype.forEach.call(document.querySelectorAll(".moa-page"), fitPage);
      return document.fonts.ready;
    }).then(function () {
      var failed = [];
      document.fonts.forEach(function (f) { if (f.status === "error") { failed.push(f.family + " " + f.weight); } });
      done();
      if (failed.length) { setTimeout(function () { throw new Error("moa: fonts failed to load: " + failed.join(", ")); }); }
    }, done);
  }

  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", run); } else { run(); }
})();
