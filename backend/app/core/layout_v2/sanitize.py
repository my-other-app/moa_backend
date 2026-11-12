"""SVG art for svg layers, made safe to draw (layout-v2 contract, svg).

`sanitize(markup)` keeps one <svg> element and, inside it, only drawing
elements and presentation attributes from an allow-list. Refused, and left
out with what they hold: <script>, <foreignObject>, <style>, <iframe>,
anything not on the list; every on* attribute; every href that is not a
reference inside the same drawing ("#id") or a picture already in the page
(data:image/...;base64 on <image>); any url(...) that is not url(#id);
javascript: anywhere. The renderer never fetches (doc-designs D1).

The output is canonical, so sanitising twice changes nothing and the
TypeScript builder can draw the stored markup as it is:

- the root is `<svg xmlns="http://www.w3.org/2000/svg" viewBox="...">` with
  its other kept attributes after, and no width, height or
  preserveAspectRatio (the builder sets those per the layer's fit); a
  missing viewBox is made from the width and height it had;
- elements and attributes keep their SVG spelling (linearGradient,
  viewBox), attributes in source order, values in double quotes;
- text is escaped (& < >), attribute values (& < > ");
- every element is written with an end tag (<path ...></path>).

`problems_of(markup)` says what was removed, for the editor.
"""
from __future__ import annotations

import html
import re
from functools import lru_cache
from html.parser import HTMLParser

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

_ELEMENTS = (
    "svg g defs symbol use title desc path rect circle ellipse line polyline polygon text tspan "
    "linearGradient radialGradient stop pattern clipPath mask marker image filter "
    "feGaussianBlur feOffset feBlend feColorMatrix feComposite feFlood feMerge feMergeNode "
    "feDropShadow feMorphology feTurbulence feDisplacementMap feComponentTransfer feFuncR feFuncG "
    "feFuncB feFuncA"
).split()
ELEMENTS = {e.lower(): e for e in _ELEMENTS}

_ATTRS = (
    "id class style transform fill fill-opacity fill-rule stroke stroke-width stroke-opacity "
    "stroke-linecap stroke-linejoin stroke-dasharray stroke-dashoffset stroke-miterlimit opacity "
    "clip-path clip-rule mask filter color display visibility paint-order vector-effect "
    "shape-rendering text-rendering image-rendering "
    "d x y x1 y1 x2 y2 cx cy r rx ry fx fy fr width height points pathLength "
    "viewBox preserveAspectRatio xmlns xmlns:xlink href xlink:href "
    "offset stop-color stop-opacity gradientUnits gradientTransform spreadMethod "
    "patternUnits patternContentUnits patternTransform clipPathUnits maskUnits maskContentUnits "
    "markerWidth markerHeight markerUnits refX refY orient marker-start marker-mid marker-end "
    "filterUnits primitiveUnits in in2 result stdDeviation dx dy mode operator k1 k2 k3 k4 values type "
    "flood-color flood-opacity radius baseFrequency numOctaves seed stitchTiles scale "
    "xChannelSelector yChannelSelector tableValues slope intercept amplitude exponent "
    "font-family font-size font-weight font-style letter-spacing text-anchor dominant-baseline "
    "alignment-baseline baseline-shift textLength lengthAdjust rotate xml:space"
).split()
ATTRS = {a.lower(): a for a in _ATTRS}
#: Not on the root: the builder sets them per the layer's fit.
ROOT_DROPPED = {"width", "height", "preserveaspectratio", "x", "y", "style", "class"}

_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
_LOCAL_URL = re.compile(r"^#[A-Za-z_][\w.:-]*$")
_DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$")
_BAD = re.compile(r"javascript:|vbscript:|expression\s*\(|@import|behavior\s*:|-moz-binding", re.I)
_NUM = re.compile(r"-?\d*\.?\d+(?:e-?\d+)?", re.I)
MAX_ELEMENTS = 5000
MAX_DEPTH = 64


class UnsafeSvg(ValueError):
    """The markup is not one <svg> drawing we can keep."""


def _esc_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s: str) -> str:
    return _esc_text(s).replace('"', "&quot;")


def _num(v: str) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else repr(round(f, 4))


def _clean_style(value: str, removed: list) -> str:
    out = []
    for decl in value.split(";"):
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop, val = prop.strip().lower(), " ".join(val.split())
        if not prop or not val:
            continue
        if _BAD.search(val) or prop.startswith("--"):
            removed.append(f"style {prop}")
            continue
        bad_url = any(not _LOCAL_URL.match(m.group(2).strip()) for m in _URL.finditer(val))
        if bad_url:
            removed.append(f"style {prop}: an outside url()")
            continue
        out.append(f"{prop}: {val}")
    return "; ".join(out)


class _Sanitiser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str | None] = []  # kept element names; None = skipped subtree marker
        self.skip = 0  # depth inside a refused element
        self.removed: list[str] = []
        self.roots = 0
        self.count = 0
        self.done = False

    # --- tags -----------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag: str, attrs, closed: bool):
        t = tag.lower()
        if self.done:
            self.removed.append(f"<{tag}> after the drawing")
            if not closed:
                self.stack.append(None)
                self.skip += 1
            return
        if self.skip:
            if not closed:
                self.stack.append(None)
                self.skip += 1
            return
        name = ELEMENTS.get(t)
        if name is None or (not self.stack and name != "svg"):
            self.removed.append(f"<{tag}>")
            if not closed:
                self.stack.append(None)
                self.skip = 1
            return
        if len(self.stack) >= MAX_DEPTH:
            raise UnsafeSvg("the drawing is nested too deeply")
        self.count += 1
        if self.count > MAX_ELEMENTS:
            raise UnsafeSvg(f"more than {MAX_ELEMENTS} elements")
        root = not self.stack
        if root:
            self.roots += 1
        kept = self._attrs(name, attrs, root)
        self.out.append(f"<{name}{kept}>")
        if closed:
            self.out.append(f"</{name}>")
            if root:
                self.done = True
        else:
            self.stack.append(name)

    def _attrs(self, element: str, attrs, root: bool) -> str:
        seen: dict[str, str] = {}
        for k, v in attrs:
            key = k.lower()
            v = "" if v is None else v
            if key.startswith("on"):
                self.removed.append(f"{k} on <{element}>")
                continue
            name = ATTRS.get(key)
            if name is None:
                if not key.startswith("data-") and not key.startswith("aria-"):
                    self.removed.append(f"{k} on <{element}>")
                continue
            if root and key in ROOT_DROPPED:
                if key in ("width", "height"):
                    seen.setdefault("__" + key, v)
                continue
            if key in ("xmlns", "xmlns:xlink"):
                continue
            if _BAD.search(v):
                self.removed.append(f"{k} on <{element}>")
                continue
            if key in ("href", "xlink:href"):
                ok = _LOCAL_URL.match(v.strip()) or (element == "image" and _DATA_IMAGE.match(v.strip()))
                if not ok:
                    self.removed.append(f"{k}={v[:40]!r} on <{element}>")
                    continue
                name, v = "href", v.strip()
            elif key == "style":
                v = _clean_style(v, self.removed)
                if not v:
                    continue
            elif "url(" in v.lower():
                if any(not _LOCAL_URL.match(m.group(2).strip()) for m in _URL.finditer(v)):
                    self.removed.append(f"{k}: an outside url() on <{element}>")
                    continue
            seen[name] = v
        if root:
            if "viewBox" not in seen:
                w, h = seen.get("__width"), seen.get("__height")
                try:
                    seen = {"viewBox": f"0 0 {_num(w)} {_num(h)}", **seen}
                except (TypeError, ValueError):
                    raise UnsafeSvg("the drawing has neither a viewBox nor a width and height") from None
            seen.pop("__width", None)
            seen.pop("__height", None)
            ordered = {"xmlns": SVG_NS, "viewBox": seen.pop("viewBox")}
            ordered.update(seen)
            seen = ordered
        else:
            seen.pop("__width", None)
            seen.pop("__height", None)
        return "".join(f' {k}="{_esc_attr(v)}"' for k, v in seen.items())

    def handle_endtag(self, tag):
        if not self.stack:
            return
        name = self.stack.pop()
        if name is None:
            self.skip = max(0, self.skip - 1)
            return
        # Close what HTML would have closed implicitly in between.
        self.out.append(f"</{name}>")
        if not self.stack:
            self.done = True

    def handle_data(self, data):
        if self.skip or not self.stack or self.done:
            return
        if self.stack[-1] in ("text", "tspan", "title", "desc"):
            self.out.append(_esc_text(data))
        elif data.strip():
            self.out.append(_esc_text(data))

    def handle_comment(self, data):
        pass

    def handle_pi(self, data):
        self.removed.append("a processing instruction")

    def unknown_decl(self, data):
        self.removed.append("a declaration")

    def handle_decl(self, decl):
        self.removed.append("a declaration")


def _run(markup: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(markup, str) or "<svg" not in markup.lower():
        raise UnsafeSvg("an svg layer holds one <svg> element")
    p = _Sanitiser()
    p.feed(markup)
    p.close()
    while p.stack:  # unclosed elements
        name = p.stack.pop()
        if name is not None:
            p.out.append(f"</{name}>")
    if p.roots != 1:
        raise UnsafeSvg("an svg layer holds exactly one <svg> element")
    return "".join(p.out), tuple(p.removed)


@lru_cache(maxsize=1024)
def _cached(markup: str) -> tuple[str, tuple[str, ...]]:
    return _run(markup)


def sanitize(markup: str) -> str:
    """The markup made safe and canonical (raises UnsafeSvg if there is no
    single <svg> drawing to keep)."""
    return _cached(markup)[0]


def problems_of(markup: str) -> list[str]:
    """What sanitising removed (empty when the markup was already safe)."""
    return list(_cached(markup)[1])


def view_box(markup: str) -> tuple[float, float, float, float] | None:
    m = re.search(r'<svg[^>]*\sviewBox="([^"]+)"', markup)
    if not m:
        return None
    nums = [float(x) for x in _NUM.findall(m.group(1))]
    return tuple(nums) if len(nums) == 4 else None  # type: ignore[return-value]


def unescape(s: str) -> str:
    return html.unescape(s)
