/* The DOM walk of scripts/convert_designs_v2.py: a filled design page in, the
   primitives it paints out, in paint order, in the page's own pixels.

   The page is a doc_designs design filled with the designer's sample values,
   each text token wrapped in <moa-tok data-t="name"> (an inline element, so
   nothing moves) and the accent replaced by a sentinel colour. Then:

   1. Text that CSS would wrap in an anonymous box (a text node in a flex or
      grid container, inline content beside blocks) gets a <span> of its own,
      which lays out the same; every element is then a box or inline content.
   2. Transforms are recorded and set to none, so every box is measured where
      layout put it; each primitive carries the chain of transforms above it.
   3. The page is painted in CSS order (backgrounds of a stacking context,
      blocks, then inline content and atomic boxes, then positioned boxes,
      z-index), and every box that paints, every paragraph, every <svg> and
      <img> becomes one primitive.

   Called as page.evaluate(WALK, {accent, images}). */
(args) => {
  "use strict";
  const page = document.querySelector(".moa-page");
  const P = page.getBoundingClientRect();
  const warnings = [];
  const cs = (e, p) => getComputedStyle(e, p || null);
  const num = (v) => parseFloat(v) || 0;

  /* ── 1. anonymous text gets a span ─────────────────────────────────── */
  const outOfFlow = (e) => { const s = cs(e); return s.position === "absolute" || s.position === "fixed" || s.float !== "none"; };
  const inlineLevel = (n) => {
    if (n.nodeType === 3) { return true; }
    if (n.nodeType !== 1) { return false; }
    const d = cs(n).display;
    return d === "inline" || d === "contents";
  };
  const hasText = (n) => (n.textContent || "").replace(/\s+/g, "") !== "";
  Array.from(page.querySelectorAll("*")).forEach((el) => {
    if (el.closest("svg") || el.localName === "moa-tok") { return; }
    const d = cs(el).display;
    if (d === "none" || d === "inline" || d === "contents") { return; }
    const kids = Array.from(el.childNodes);
    if (/flex|grid/.test(d)) {
      /* A run of text is one anonymous flex item; a token was text before it
         was marked, so a run of text and <moa-tok>s is one item too. */
      let run = [];
      const flush = () => {
        if (run.some((n) => n.nodeType === 1 || n.textContent.trim())) {
          const s = document.createElement("span");
          s.setAttribute("data-moa-anon", "");
          run[0].before(s);
          run.forEach((n) => s.appendChild(n));
        }
        run = [];
      };
      kids.forEach((k) => {
        if (k.nodeType === 3 || (k.nodeType === 1 && k.localName === "moa-tok")) { run.push(k); } else if (k.nodeType === 1) { flush(); }
      });
      flush();
      return;
    }
    const blocky = kids.some((k) => k.nodeType === 1 && !inlineLevel(k) && !outOfFlow(k));
    if (!blocky) { return; }
    let run = [];
    const flush = () => {
      if (run.some(hasText)) {
        const s = document.createElement("span");
        s.setAttribute("data-moa-anon", "");
        s.style.display = "block";
        run[0].before(s);
        run.forEach((n) => s.appendChild(n));
      }
      run = [];
    };
    kids.forEach((k) => { if (inlineLevel(k)) { run.push(k); } else { flush(); } });
    flush();
  });

  /* ── 2. transforms ─────────────────────────────────────────────────── */
  const xf = new Map();
  Array.from(page.querySelectorAll("*")).forEach((el) => {
    if (el.closest("svg") && el.localName !== "svg") { return; }
    const s = cs(el);
    if (s.transform && s.transform !== "none") {
      xf.set(el, { matrix: s.transform, origin: s.transformOrigin });
    }
  });
  xf.forEach((_v, el) => el.style.setProperty("transform", "none", "important"));
  void page.offsetWidth;

  const rect = (e) => {
    const r = e.getBoundingClientRect();
    return { x: r.left - P.left, y: r.top - P.top, w: r.width, h: r.height };
  };
  const box = (e) => {
    const r = rect(e), s = cs(e);
    const bl = num(s.borderLeftWidth), br = num(s.borderRightWidth), bt = num(s.borderTopWidth), bb = num(s.borderBottomWidth);
    const pl = num(s.paddingLeft), pr = num(s.paddingRight), pt = num(s.paddingTop), pb = num(s.paddingBottom);
    return {
      border: r,
      padding: { x: r.x + bl, y: r.y + bt, w: r.w - bl - br, h: r.h - bt - bb },
      content: { x: r.x + bl + pl, y: r.y + bt + pt, w: r.w - bl - br - pl - pr, h: r.h - bt - bb - pt - pb },
    };
  };
  const radii = (e) => {
    const s = cs(e), r = rect(e);
    const one = (v, side) => {
      const first = String(v).split(" ")[0];
      if (first.endsWith("%")) { return num(first) / 100 * side; }
      return num(first);
    };
    return [one(s.borderTopLeftRadius, r.w), one(s.borderTopRightRadius, r.w),
      one(s.borderBottomRightRadius, r.w), one(s.borderBottomLeftRadius, r.w)];
  };

  /* What sits above a primitive: transforms (inner first), opacity, the
     optional blocks that guard it, the boxes that clip it. */
  const chain = (el, own) => {
    const out = { xf: [], opacity: 1, all: [], any: [], clips: [], lanyard: false };
    for (let e = el; e && e !== page; e = e.parentElement) {
      if (xf.has(e)) {
        const r = rect(e), o = xf.get(e).origin.split(" ").map(num);
        out.xf.push({ matrix: xf.get(e).matrix, ox: r.x + o[0], oy: r.y + o[1] });
      }
      out.opacity *= num(cs(e).opacity) || (cs(e).opacity === "0" ? 0 : 1);
      ["data-optional", "data-optional-hide"].forEach((a) => {
        const v = e.getAttribute(a);
        if (v) { v.split(/\s+/).filter(Boolean).forEach((t) => out.all.push(t)); }
      });
      const any = e.getAttribute("data-optional-any");
      if (any) { out.any.push(any.split(/\s+/).filter(Boolean)); }
      if (e.hasAttribute("data-lanyard")) { out.lanyard = true; }
      if (e !== el || !own) {
        const s = cs(e);
        if (e !== el && (s.overflowX !== "visible" || s.overflowY !== "visible")) {
          const b = box(e), rr = radii(e);
          const bw = [num(s.borderTopWidth), num(s.borderRightWidth), num(s.borderBottomWidth), num(s.borderLeftWidth)];
          out.clips.push({ rect: b.padding, radii: [Math.max(0, rr[0] - Math.max(bw[3], bw[0])), Math.max(0, rr[1] - Math.max(bw[0], bw[1])),
            Math.max(0, rr[2] - Math.max(bw[1], bw[2])), Math.max(0, rr[3] - Math.max(bw[2], bw[3]))] });
        }
      }
    }
    return out;
  };

  /* ── 3. paint ──────────────────────────────────────────────────────── */
  const out = [];
  const positioned = (e) => cs(e).position !== "static";
  const zOf = (e) => { const z = cs(e).zIndex; return z === "auto" ? null : parseInt(z, 10); };
  const flexChild = (e) => { const p = e.parentElement; return !!p && /flex|grid/.test(cs(p).display); };
  const stacking = (e) => {
    const s = cs(e);
    return (positioned(e) && zOf(e) !== null) || num(s.opacity) < 1 || xf.has(e) || s.filter !== "none" ||
      s.isolation === "isolate" || s.mixBlendMode !== "normal" || (flexChild(e) && zOf(e) !== null);
  };
  const atomic = (e) => cs(e).display.startsWith("inline-") || flexChild(e);
  const byZ = (a, b) => (zOf(a) || 0) - (zOf(b) || 0);

  const styleOf = (e) => {
    const s = cs(e);
    return { family: s.fontFamily, weight: s.fontWeight, style: s.fontStyle, size: s.fontSize, color: s.color,
      ls: s.letterSpacing, tt: s.textTransform };
  };

  function segments(el) {
    const segs = [];
    const walk = (node) => {
      for (const k of node.childNodes) {
        if (k.nodeType === 3) { segs.push({ kind: "text", text: k.textContent, st: styleOf(k.parentElement) }); continue; }
        if (k.nodeType !== 1) { continue; }
        if (k.localName === "br") { segs.push({ kind: "br" }); continue; }
        if (k.localName === "moa-tok") { segs.push({ kind: "tok", name: k.getAttribute("data-t"), st: styleOf(k) }); continue; }
        const s = cs(k);
        if (s.display === "none" || (s.display !== "inline" && s.display !== "contents") || positioned(k)) { continue; }
        if (k.localName === "svg" || k.localName === "img") { continue; }
        const conds = [];
        const opt = k.getAttribute("data-optional") || k.getAttribute("data-optional-hide");
        if (opt) { conds.push({ tokens: opt.split(/\s+/).filter(Boolean), mode: "all" }); }
        const any = k.getAttribute("data-optional-any");
        if (any) { conds.push({ tokens: any.split(/\s+/).filter(Boolean), mode: "any" }); }
        conds.forEach((c) => segs.push({ kind: "if", tokens: c.tokens, mode: c.mode }));
        walk(k);
        conds.forEach(() => segs.push({ kind: "endif" }));
      }
    };
    walk(el);
    return segs;
  }

  function lines(el) {
    const s = cs(el);
    const lh = s.lineHeight === "normal" ? 1.2 * num(s.fontSize) : num(s.lineHeight);
    const range = document.createRange();
    range.selectNodeContents(el);
    const tops = Array.from(range.getClientRects()).filter((r) => r.height > 0 && r.width > 0).map((r) => r.top).sort((a, b) => a - b);
    let count = 0, last = -Infinity;
    tops.forEach((t) => { if (t - last > lh * 0.5) { count += 1; last = t; } });
    return Math.max(1, count);
  }

  function textBox(el) {
    const range = document.createRange();
    range.selectNodeContents(el);
    const rs = Array.from(range.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
    if (!rs.length) { return null; }
    const l = Math.min(...rs.map((r) => r.left)), r = Math.max(...rs.map((r) => r.right));
    const t = Math.min(...rs.map((r) => r.top)), b = Math.max(...rs.map((r) => r.bottom));
    return { x: l - P.left, y: t - P.top, w: r - l, h: b - t };
  }

  function fitOf(el) {
    for (let e = el, i = 0; e && e !== page && i < 2; e = e.parentElement, i++) {
      if (e.hasAttribute("data-fit")) {
        return { mode: e.getAttribute("data-fit"), min: e.getAttribute("data-fit-min"), lines: e.getAttribute("data-fit-lines"),
          wrap: e.hasAttribute("data-fit-wrap") };
      }
      if (!e.hasAttribute("data-moa-anon")) { break; }
    }
    return null;
  }

  /* The room a one-line text has beside it: the containing box (the
     nearest data-fit-box, else the parent's content box), less what else
     is drawn on the same line. */
  function room(el) {
    let boundEl = el.parentElement;
    for (let e = el.parentElement; e && e !== page; e = e.parentElement) {
      if (e.hasAttribute("data-fit-box")) { boundEl = e; break; }
    }
    const b = box(boundEl).content, me = box(el).border;
    return { bound: b, me: me };
  }

  function hasParagraph(el) {
    const d = cs(el).display;
    if (d === "inline" || d === "contents" || el.localName === "svg" || el.localName === "img") { return false; }
    return segments(el).some((s) => (s.kind === "text" && s.text.trim()) || s.kind === "tok");
  }

  function emitText(el) {
    const s = cs(el), c = chain(el, false), b = box(el);
    out.push({
      type: "text", tag: el.localName, rect: b.content, border: b.border, segs: segments(el), base: styleOf(el),
      lineHeight: s.lineHeight, whiteSpace: s.whiteSpace, align: s.textAlign, writingMode: s.writingMode,
      textWrap: s.textWrapStyle || s.textWrap || "", textShadow: s.textShadow, lines: lines(el), glyphs: textBox(el),
      fit: fitOf(el), room: room(el), slot: el.closest("[data-option]") ? el.closest("[data-option]").getAttribute("data-option") : null,
      ...c,
    });
  }

  function hasDecor(e) {
    const s = cs(e);
    if (s.visibility === "hidden") { return false; }
    const bg = s.backgroundColor !== "rgba(0, 0, 0, 0)" || s.backgroundImage !== "none";
    const border = ["Top", "Right", "Bottom", "Left"].some((k) => num(s["border" + k + "Width"]) > 0 && s["border" + k + "Style"] !== "none" && s["border" + k + "Style"] !== "hidden");
    return bg || border || s.boxShadow !== "none" || (s.outlineStyle !== "none" && num(s.outlineWidth) > 0) || s.filter !== "none";
  }

  function emitDecor(e) {
    const slot = e.getAttribute("data-slot");
    if (hasDecor(e)) {
      const s = cs(e);
      const sides = {};
      ["Top", "Right", "Bottom", "Left"].forEach((k) => {
        sides[k.toLowerCase()] = { width: num(s["border" + k + "Width"]), style: s["border" + k + "Style"], color: s["border" + k + "Color"] };
      });
      const b = box(e);
      out.push({
        type: "box", tag: e.localName, rect: b.border, padding: b.padding, radii: radii(e),
        bg: s.backgroundColor, bgImage: s.backgroundImage, bgSize: s.backgroundSize, bgPosition: s.backgroundPosition,
        bgRepeat: s.backgroundRepeat, bgOrigin: s.backgroundOrigin, bgClip: s.backgroundClip, borders: sides,
        boxShadow: s.boxShadow, outline: { width: num(s.outlineWidth), style: s.outlineStyle, color: s.outlineColor, offset: num(s.outlineOffset) },
        filter: s.filter, backdrop: s.backdropFilter, slot: slot, ...chain(e, true),
      });
    }
    if (slot === "signature") {
      out.push({ type: "slot", slot: "signature", rect: box(e).content, ...chain(e, true) });
    }
  }

  function emitSvg(e) {
    const c = chain(e, true);
    out.push({ type: "svg", rect: rect(e), markup: e.outerHTML, par: e.getAttribute("preserveAspectRatio"),
      viewBox: e.getAttribute("viewBox"), ...c });
  }

  function emitImg(e) {
    const s = cs(e), src = e.getAttribute("src") || "";
    const parent = e.parentElement;
    out.push({ type: "img", rect: box(e).content, role: args.images[src] || null, slot: e.getAttribute("data-slot"),
      fit: s.objectFit, position: s.objectPosition, radii: radii(e), parent: parent ? box(parent).content : null,
      parentOnlyChild: !!parent && parent.children.length === 1, rendering: s.imageRendering,
      border: { width: num(s.borderTopWidth), color: s.borderTopColor, style: s.borderTopStyle }, ...chain(e, true) });
  }

  function collect(el, L) {
    if (hasParagraph(el)) { L.inl.push({ text: el }); }
    for (const c of el.children) {
      const s = cs(c);
      if (s.display === "none" || c.localName === "moa-tok" || c.localName === "br" || c.localName === "style" || c.localName === "script") { continue; }
      if (positioned(c) || stacking(c)) {
        const z = zOf(c) || 0;
        (z < 0 ? L.neg : z > 0 ? L.pos : L.pos0).push(c);
        continue;
      }
      if (c.localName === "svg" || c.localName === "img" || atomic(c)) { L.inl.push({ atomic: c }); continue; }
      if (s.display === "inline") {
        if (hasDecor(c)) { L.inl.push({ decor: c }); }
        collect(c, L);
        continue;
      }
      L.blocks.push(c);
      collect(c, L);
    }
  }

  function paint(el) {
    if (el.localName === "svg") { emitSvg(el); return; }
    if (el.localName === "img") { emitImg(el); return; }
    emitDecor(el);
    const L = { neg: [], blocks: [], inl: [], pos0: [], pos: [] };
    collect(el, L);
    L.neg.sort(byZ).forEach(paint);
    L.blocks.forEach((b) => emitDecor(b));
    L.inl.forEach((i) => {
      if (i.text) { emitText(i.text); } else if (i.decor) { emitDecor(i.decor); } else { paint(i.atomic); }
    });
    L.pos0.forEach(paint);
    L.pos.sort(byZ).forEach(paint);
  }

  Array.from(page.querySelectorAll("*")).forEach((e) => {
    if (e.closest("svg")) { return; }
    ["::before", "::after"].forEach((p) => {
      const content = cs(e, p).content;
      if (content && content !== "none" && content !== "normal") { warnings.push(`pseudo ${p} on <${e.localName}>: ${content}`); }
    });
  });

  for (const child of page.children) { paint(child); }
  return { primitives: out, warnings: warnings, page: { w: P.width, h: P.height } };
}
