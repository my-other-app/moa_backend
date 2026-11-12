"""The 35 designer designs as layout v2 starters (layout-v2 contract, V3).

    PLAYWRIGHT_BROWSERS_PATH=… <venv with playwright>/bin/python backend/scripts/convert_designs_v2.py
        [--only badge/06] [--parity DIR] [--cases]

Needs Playwright's Chromium besides the backend's own requirements: run it
from a throwaway venv that has `playwright` (and Pillow, numpy for
--parity); the backend's packages are taken from backend/.venv*/ when that
venv lacks them. It writes app/core/layout_v2/starters/: every design's
starter, the certificates' other wordings, card_cr80 and the blanks.

How a design becomes layers. Each template is filled with the designer's
sample values (the doc_designs fill, unchanged) except that every text
token comes out as <moa-tok data-t="token">value</moa-tok> (an inline
element: nothing moves) and the accent as a sentinel colour. Chromium lays
the page out; layout_v2_walk.js walks it in paint order and returns what
paints -- boxes (backgrounds, gradients, borders, shadows, outlines),
paragraphs (their words and tokens, styles, lines and box), inline <svg>
art, pictures and the QR. Here each becomes one or more layers at its exact
box: a box's fill and border a shape, a tiled or repeating background an
SVG pattern, a paragraph a text layer (styled runs as spans, optional
pieces as {% if %}), a guarded block's layers a showIf, the sentinel
`var:accent`, a transform a rotation about the layer's centre.

--parity DIR renders every starter through build_html in Chromium with the
same sample values and compares it with the designer's preview (tickets: the
render of the HTML preset), mean intensity match per design, the QR's box
left out of the designer comparisons (the designers' sample QR does not
encode our ticket); it also renders the long-text, one-word-name and
no-venue cases and reports any text that overflows its box.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
try:
    import pydantic  # noqa: F401
except ImportError:  # a Playwright venv: borrow the backend's packages
    for sp in sorted(BACKEND.glob(".venv*/lib/python3*/site-packages")):
        sys.path.append(str(sp))
for k, v in {
    "APP_ENV": "test", "APP_SECRET_KEY": "x" * 32, "APP_DATABASE_URL": "postgresql+asyncpg://localhost/x",
    "APP_DATABASE_URL_SYNC": "postgresql://localhost/x", "APP_CORS_ORIGINS": '["http://testserver"]',
    "APP_S3_BUCKET": "b", "APP_S3_ACCESS_KEY": "a", "APP_S3_SECRET_KEY": "s", "APP_S3_BASE_PATH": "https://b",
    "APP_SES_ACCESS_KEY": "a", "APP_SES_SECRET_KEY": "s", "APP_SES_DEFAULT_SENDER": "t@example.com",
    "APP_GOOGLE_WEB_CLIENT_ID": "g", "APP_GOOGLE_ANDROID_CLIENT_ID": "g", "APP_GOOGLE_IOS_CLIENT_ID": "g",
    "APP_RAZORPAY_KEY_ID": "r", "APP_RAZORPAY_KEY_SECRET": "r", "APP_RAZORPAY_WEBHOOK_SECRET": "r",
    "APP_ACCESS_TOKEN_EXPIRE_MINUTES": "15", "APP_REFRESH_TOKEN_EXPIRE_DAYS": "7",
}.items():
    os.environ.setdefault(k, v)

from app.core import doc_designs as dd  # noqa: E402
from app.core.doc_designs import designs as ddesigns  # noqa: E402
import importlib  # noqa: E402
dfill = importlib.import_module("app.core.doc_designs.fill")
from app.core.doc_designs import fonts as dfonts  # noqa: E402
from app.core.doc_designs import qr as dqr  # noqa: E402
from app.core.layout_v2 import schema as v2schema  # noqa: E402
from app.core.layout_v2 import validate as v2validate  # noqa: E402
from app.core.layout_v2 import vocabulary as voc  # noqa: E402
from app.core.layout_v2.sanitize import sanitize  # noqa: E402

TEMPLATES = BACKEND / "app" / "core" / "doc_designs" / "templates"
STARTERS = BACKEND / "app" / "core" / "layout_v2" / "starters"
WALK = (Path(__file__).resolve().parent / "layout_v2_walk.js").read_text()
#: The accent while converting: a colour no design uses, so whatever paints in it is the accent.
SENTINEL = "#FD03FC"
SENTINEL_RGB = (253, 3, 252)
IMAGE_TOKENS = {"qr_code_src", "qr_code_url", "logo_src", "photo_url", "sponsor_src"}
#: Image tokens a showIf may name: present when the context has the picture.
IMAGE_SHOW = {"logo_src", "photo_url", "sponsor_src", "signature_src", "second_signature_src"}
T0, T1, T2 = "", "", ""


def data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


LOGO = "data:image/svg+xml;base64," + base64.b64encode(
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='#888888'/>"
    b"<path d='M50 34 67 64H33z' fill='#fff'/></svg>").decode()
SPONSOR = "data:image/svg+xml;base64," + base64.b64encode(
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 300 84'><rect width='300' height='84' fill='#dddddd'/></svg>").decode()
PHOTO = data_uri(TEMPLATES / "badge/designer/sample-photo.svg", "image/svg+xml")


# ─── Sample values (as the doc-designs parity harness) ──────────────────────


def sample_values(kind: str) -> dict:
    if kind == "ticket":
        v = json.loads((TEMPLATES / "ticket/designer/sample-data.json").read_text())
        v = {k: x for k, x in v.items() if not k.startswith("_") and k not in ("design", "qr")}
        v["qr"] = v["ticket_id"]
        return v
    if kind == "certificate":
        v = json.loads((TEMPLATES / "certificate/designer/fields.json").read_text())["sample"]
        v = {k: x for k, x in v.items() if k not in ("qr_code_src", "logo_src")}
        v.update(qr=v["verify_url"], logo_src=LOGO)
        return v
    v = json.loads((TEMPLATES / "badge/designer/sample-data.json").read_text())
    v.update(photo_url=PHOTO, qr="A-0417")
    v.pop("qr_code_url", None)
    return v


def text_values(values: dict) -> dict:
    """What a v2 layout's text reads: the same values, without the pictures."""
    return {k: v for k, v in values.items() if k not in IMAGE_TOKENS and k != "qr"}


def context_for(kind: str, values: dict) -> dict:
    images = {}
    if values.get("logo_src"):
        images["org_logo"] = values["logo_src"]
    if values.get("photo_url"):
        images["guest_photo"] = values["photo_url"]
    if values.get("sponsor_src"):
        images["sponsor"] = values["sponsor_src"]
    return {"qr": values.get("qr", ""), "images": images}


# ─── The marker page ─────────────────────────────────────────────────────────


class _Marked(dfill._Context):
    def out(self, name, allow_empty):
        v = super().out(name, allow_empty)
        if not v or name in dfill.IMAGE_TOKENS or name == "accent_color":
            return v
        return f"{T0}{name}{T1}{v}{T2}"


_MARK = re.compile(f"{T0}(\\w+){T1}(.*?){T2}", re.S)


def _demark(body: str) -> str:
    """Tokens inside a tag or an <svg> become their values again; the rest
    <moa-tok data-t="name">value</moa-tok>."""
    out, in_tag, svg = [], False, 0
    i = 0
    size = len(body)
    while i < size:
        ch = body[i]
        if ch == T0:
            m = _MARK.match(body, i)
            if m:
                name, value = m.group(1), m.group(2)
                out.append(value if (in_tag or svg) else f'<moa-tok data-t="{name}">{value}</moa-tok>')
                i = m.end()
                continue
        if ch == "<":
            in_tag = True
            if body.startswith("<svg", i):
                svg += 1
            elif body.startswith("</svg", i):
                svg = max(0, svg - 1)
        elif ch == ">":
            in_tag = False
        out.append(ch)
        i += 1
    return "".join(out)


def marker_page(design_id: str, values: dict, options: dict | None) -> str:
    d = dd.get(design_id)
    ctx = _Marked(d, values, options, None, SENTINEL if d.accent else None)
    body = _demark(dfill._render(ddesigns.compiled(d.id).body, ctx))
    accent_var = ctx.accent if d.accent and d.accent.mode == "var" else None
    page = dfill._document(d, body, dfonts.for_render(d.fonts, body), accent_var)
    # Not the fit script: the samples fit as drawn (the doc-designs parity proof), and the
    # markers would read to it as text of their own. Ready once the fonts are.
    ready = ("<script>Promise.all(Array.from(document.images, i => i.decode().catch(() => 0)))"
             ".then(() => document.fonts.ready).then(() => { window.__moaReady = true; });</script>")
    return re.sub(r'<script data-moa="fit">.*?</script>', lambda _m: ready, page, flags=re.S)


# ─── CSS values ──────────────────────────────────────────────────────────────


def split_top(s: str, sep: str = ",") -> list[str]:
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


_RGB = re.compile(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)")


def rgba(s: str):
    s = (s or "").strip()
    if s in ("transparent", "none", ""):
        return (0, 0, 0, 0.0)
    m = _RGB.match(s)
    if not m:
        if s.startswith("#") and len(s) in (7, 9):
            r, g, b = int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)
            a = int(s[7:9], 16) / 255 if len(s) == 9 else 1.0
            return (r, g, b, a)
        raise ValueError(f"colour {s!r}")
    a = m.group(4)
    alpha = 1.0 if a is None else (float(a[:-1]) / 100 if a.endswith("%") else float(a))
    return (round(float(m.group(1))), round(float(m.group(2))), round(float(m.group(3))), alpha)


def color(s: str, accent: bool = True) -> str | None:
    r, g, b, a = rgba(s)
    if a <= 0.001:
        return None
    if accent and (r, g, b) == SENTINEL_RGB:
        if a < 0.999:
            raise ValueError("the accent with transparency cannot be var:accent")
        return "var:accent"
    if a >= 0.999:
        return f"#{r:02X}{g:02X}{b:02X}"
    return f"#{r:02X}{g:02X}{b:02X}{round(a * 255):02X}"


def px(s) -> float:
    try:
        return float(str(s).replace("px", "").strip() or 0)
    except ValueError:
        return 0.0


def shadows(s: str, text: bool = False) -> list[dict]:
    if not s or s == "none":
        return []
    out = []
    for part in split_top(s):
        inset = " inset" in f" {part}"
        part = part.replace("inset", "").strip()
        m = re.match(r"(rgba?\([^)]*\)|#[0-9a-fA-F]+)\s+(.*)$", part) or re.match(r"(.*?)\s+(rgba?\([^)]*\))$", part)
        if m and m.group(1).startswith(("rgb", "#")):
            col, rest = m.group(1), m.group(2)
        else:
            col, rest = m.group(2), m.group(1)
        nums = [px(x) for x in rest.split()]
        nums += [0.0] * (4 - len(nums))
        c = color(col)
        if c is None:
            continue
        sh = {"x": nums[0], "y": nums[1], "blur": nums[2], "color": c}
        if not text:
            sh["spread"] = nums[3]
        if inset:
            sh["inset"] = True
        out.append(sh)
    return out


def first_family(s: str) -> str:
    return split_top(s)[0].strip().strip("'\"")


# ─── Fonts: what the designer's browser picked ──────────────────────────────


def match_face(design, family: str, weight: int, style: str) -> tuple[int, str, bool]:
    """The design's own face CSS font matching picks for a request, and
    whether Chromium would synthesise bold or italic on top of it."""
    faces = [(s, w) for f, s, w in design.fonts if f == family]
    if not faces:
        faces = [(f["style"], f["weight"]) for f in dfonts.lock()["families"].get(family, {}).get("faces", [])]
    if not faces:
        return weight, style, False
    styled = [f for f in faces if f[0] == style] or faces
    ws = sorted({w for _s, w in styled})
    if weight in ws:
        w = weight
    elif 400 <= weight <= 500:
        up = [x for x in ws if weight < x <= 500]
        down = [x for x in ws if x < weight]
        w = up[0] if up else (down[-1] if down else min(x for x in ws if x > 500))
    elif weight < 400:
        down = [x for x in ws if x <= weight]
        w = down[-1] if down else ws[0]
    else:
        up = [x for x in ws if x >= weight]
        w = up[0] if up else ws[-1]
    got_style = style if any(f[0] == style for f in faces) else faces[0][0]
    synth = (weight >= 600 and w < 600 and (weight - w) >= 200) or (style == "italic" and got_style != "italic")
    return w, got_style, synth


# ─── Geometry ────────────────────────────────────────────────────────────────


def parse_matrix(m: str):
    nums = [float(x) for x in re.findall(r"-?[\d.]+(?:e-?\d+)?", m)]
    if m.startswith("matrix3d"):
        return nums[0], nums[1], nums[4], nums[5], nums[12], nums[13]
    return tuple(nums[:6])


def place(rect: dict, prim: dict, warn: list) -> tuple[dict, float, float]:
    """The layer box (x, y, w, h) and rotation for a primitive's rect after
    the transforms above it, and the uniform scale they apply."""
    cx, cy = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2
    rot, scale = 0.0, 1.0
    for t in prim.get("xf") or []:
        a, b, c, d, e, f = parse_matrix(t["matrix"])
        sx, sy = math.hypot(a, b), math.hypot(c, d)
        if abs(a * c + b * d) > 1e-3:
            warn.append(f"skew dropped ({t['matrix']})")
            c, d = -b, a  # keep the rotation, drop the skew
            sy = sx
        if abs(sx - sy) > 1e-3:
            warn.append(f"uneven scale dropped ({t['matrix']})")
        ox, oy = t["ox"], t["oy"]
        dx, dy = cx - ox, cy - oy
        cx, cy = a * dx + c * dy + ox + e, b * dx + d * dy + oy + f
        rot += math.degrees(math.atan2(b, a))
        scale *= sx
    w, h = rect["w"] * scale, rect["h"] * scale
    rot = ((rot + 180) % 360) - 180
    if abs(rot) < 1e-6:
        rot = 0.0
    return {"x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h}, rot, scale


def clamp_radii(r, w, h):
    from app.core.layout_v2.build import clamp_radii as clamp

    return clamp(r, w, h)


def n(v: float) -> str:
    """A number in markup, as the builder writes numbers."""
    from app.core.layout_v2.build import num

    return num(v)


def r3(v: float) -> float:
    return math.floor(v * 1000 + 0.5) / 1000


def ceil64(v: float) -> float:
    return math.ceil(v * 64 - 1e-6) / 64


# ─── Primitives → layers ─────────────────────────────────────────────────────


class Convert:
    def __init__(self, design, prims: list, page: dict):
        self.d = design
        self.prims = prims
        self.W, self.H = design.page.width_px, design.page.height_px
        self.layers: list[dict] = []
        self.warn: list[str] = []
        self.ids: dict[str, int] = {}
        self.canvas_bg = None
        self.signatures = 0

    # --- common -------------------------------------------------------------

    def uid(self, base: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-")[:32] or "layer"
        n = self.ids.get(base, 0) + 1
        self.ids[base] = n
        return base if n == 1 else f"{base}-{n}"

    def show_if(self, prim: dict) -> str | None:
        alls = list(dict.fromkeys(prim.get("all") or []))
        if alls:
            return " ".join(alls)
        anys = prim.get("any") or []
        if anys:
            return "|".join(anys[0])
        return None

    def common(self, kind: str, idbase: str, name: str, rect: dict, prim: dict, rot: float = 0.0) -> dict:
        layer = {"id": self.uid(idbase), "name": name[:60] or kind, "kind": kind,
                 "x": r3(rect["x"]), "y": r3(rect["y"]), "width": max(0.01, r3(rect["w"])), "height": max(0.01, r3(rect["h"]))}
        if rot:
            layer["rotation"] = r3(rot)
        op = prim.get("opacity", 1)
        if op < 0.9995:
            layer["opacity"] = r3(op)
        s = self.show_if(prim)
        if s:
            layer["showIf"] = s
        return layer

    # --- boxes --------------------------------------------------------------

    def corner_radii(self, rect: dict, own: list, prim: dict) -> list:
        """A box inside a rounded box that clips it, touching a corner, is
        rounded there too (overflow: hidden with border-radius)."""
        r = list(own)
        for clip in prim.get("clips") or []:
            c, cr = clip["rect"], clip["radii"]
            if not any(cr):
                continue
            tol = 0.75
            left, top = rect["x"] <= c["x"] + tol, rect["y"] <= c["y"] + tol
            right = rect["x"] + rect["w"] >= c["x"] + c["w"] - tol
            bottom = rect["y"] + rect["h"] >= c["y"] + c["h"] - tol
            for i, hit in enumerate((left and top, right and top, right and bottom, left and bottom)):
                if hit and cr[i] > r[i]:
                    r[i] = cr[i]
        return [r3(x) for x in r]

    def clip_rect(self, rect: dict, prim: dict) -> dict | None:
        """The part of a box its clipping boxes leave visible (None: nothing)."""
        x0, y0, x1, y1 = rect["x"], rect["y"], rect["x"] + rect["w"], rect["y"] + rect["h"]
        for clip in prim.get("clips") or []:
            c = clip["rect"]
            x0, y0 = max(x0, c["x"]), max(y0, c["y"])
            x1, y1 = min(x1, c["x"] + c["w"]), min(y1, c["y"] + c["h"])
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, self.W), min(y1, self.H)
        if x1 - x0 < 0.01 or y1 - y0 < 0.01:
            return None
        return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}

    @staticmethod
    def dash(width: float, length: float, closed: bool = False) -> list:
        """Chromium's dashes for a border: 3w dash, 2w gap under 3 px, else
        2w and w; the gap stretched so a side starts and ends with a dash."""
        d, g = (3 * width, 2 * width) if width < 3 else (2 * width, width)
        if closed:
            n = max(1, round(length / (d + g)))
            return [r3(d), r3(max(0.0, length / n - d))]
        n = max(2, math.floor((length + g) / (d + g)))
        return [r3(d), r3(max(0.0, (length - n * d) / (n - 1)))]

    @staticmethod
    def dots(width: float, length: float) -> list:
        """A thin dotted border: square dots a width apart, stretched to end on one."""
        n_ = max(2, math.floor((length + width) / (2 * width)))
        return [r3(width), r3(max(0.0, (length - n_ * width) / (n_ - 1)))]

    def box(self, p: dict):
        if p.get("slot") == "signature" and p["type"] == "slot":
            return
        rect0 = p["rect"]
        if rect0["w"] <= 0 or rect0["h"] <= 0:
            return
        rect, rot, _scale = place(rect0, p, self.warn)
        own_r = p["radii"]
        name = "Lanyard slot" if p.get("lanyard") and p["tag"] != "div" or p.get("lanyard") else "Shape"
        idbase = "lanyard" if p.get("lanyard") else "shape"
        # Drop shadows of a whole group (filter: drop-shadow): a shadow-only box behind it.
        drops = [f for f in re.findall(r"drop-shadow\(([^()]*(?:\([^()]*\)[^()]*)*)\)", p.get("filter") or "")]
        if drops:
            sh = []
            for f in drops:
                for one in shadows(f):  # a drop shadow's blur is a box shadow's half
                    one["blur"] = one["blur"] * 2
                    sh.append(one)
            corners = self.group_corners(p)
            layer = self.common("shape", "shadow", "Shadow", rect, p, rot)
            layer.update({"shape": "rect", "radius": corners, "shadow": sh})
            self.layers.append(layer)
        bg = color(p["bg"])
        sides = p["borders"]
        widths = [sides[k]["width"] if sides[k]["style"] not in ("none", "hidden") else 0 for k in ("top", "right", "bottom", "left")]
        uniform = len(set(widths)) == 1 and widths[0] > 0 and len({sides[k]["style"] for k in sides}) == 1 \
            and len({sides[k]["color"] for k in sides}) == 1 and sides["top"]["style"] in ("solid", "dashed")
        images = [i for i in split_top(p["bgImage"]) if i != "none"] if p["bgImage"] != "none" else []
        sh = shadows(p["boxShadow"])
        radius = self.corner_radii(rect, own_r, p)
        is_root = rect["w"] >= self.W - 0.5 and rect["h"] >= self.H - 0.5 and rect["x"] <= 0.5 and rect["y"] <= 0.5
        if is_root and bg and not images and not any(widths) and not sh and not self.layers and not rot:
            self.canvas_bg = {"type": "solid", "color": bg}
            bg = None
        ellipse = all(abs(x - min(rect["w"], rect["h"]) / 2) < 0.6 for x in own_r) and abs(rect["w"] - rect["h"]) < 0.6 \
            and min(rect["w"], rect["h"]) > 0
        base = None
        if bg or sh or (uniform and not images):
            base = self.common("shape", idbase, name, rect, p, rot)
            base["shape"] = "ellipse" if ellipse else "rect"
            if not ellipse and any(radius):
                base["radius"] = [r3(x) for x in clamp_radii(radius, rect["w"], rect["h"])]
            if bg:
                base["fill"] = {"type": "solid", "color": bg}
            if sh:
                base["shadow"] = sh
            if uniform and not images:
                base["stroke"] = self.stroke(sides["top"], rect, ellipse, radius)
            self.layers.append(base)
        sizes, positions = split_top(p["bgSize"]), split_top(p["bgPosition"])
        for i in reversed(range(len(images))):
            layer_p = {**p, "bgSize": sizes[i % len(sizes)], "bgPosition": positions[i % len(positions)]}
            self.background_image(images[i], layer_p, rect, rot, radius)
        if images and uniform:
            layer = self.common("shape", "border", "Border", rect, p, rot)
            layer.update({"shape": "ellipse" if ellipse else "rect", "stroke": self.stroke(sides["top"], rect, ellipse, radius)})
            if not ellipse and any(radius):
                layer["radius"] = radius
            self.layers.append(layer)
        if not uniform and any(widths):
            self.side_borders(p, rect, rot, widths)
        o = p.get("outline") or {}
        if o.get("width", 0) > 0 and o.get("style") not in ("none", None):
            grow = o["offset"] + o["width"]
            orect = {"x": rect["x"] - grow, "y": rect["y"] - grow, "w": rect["w"] + 2 * grow, "h": rect["h"] + 2 * grow}
            layer = self.common("shape", "outline", "Outline", orect, p, rot)
            c = color(o["color"])
            if c:
                layer.update({"shape": "rect", "stroke": {"color": c, "width": o["width"]}})
                if any(own_r):
                    layer["radius"] = [x + grow for x in own_r]
                self.layers.append(layer)
        if p.get("backdrop") not in (None, "none"):
            self.warn.append(f"backdrop-filter {p['backdrop']} dropped")

    def stroke(self, side: dict, rect: dict, ellipse: bool, radius: list) -> dict:
        c = color(side["color"]) or "#000000"
        s = {"color": c, "width": side["width"]}
        if side["style"] == "dashed":
            if ellipse:
                length = math.pi * (rect["w"] - side["width"])
            else:
                length = 2 * (rect["w"] + rect["h"] - 2 * side["width"])
            s["dash"] = self.dash(side["width"], length, closed=True)
        return s

    def side_borders(self, p: dict, rect: dict, rot: float, widths: list):
        x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
        t, r, b, l = widths
        boxes = {
            "top": {"x": x, "y": y, "w": w, "h": t},
            "right": {"x": x + w - r, "y": y, "w": r, "h": h},
            "bottom": {"x": x, "y": y + h - b, "w": w, "h": b},
            "left": {"x": x, "y": y, "w": l, "h": h},
        }
        for k, wd in zip(("top", "right", "bottom", "left"), widths):
            if wd <= 0:
                continue
            side = p["borders"][k]
            c = color(side["color"])
            if not c:
                continue
            bx = boxes[k]
            sub = dict(p)
            sub_rect, sub_rot, _ = place(self.unplace(bx, p), p, self.warn) if p.get("xf") else (bx, rot, 1)
            layer = self.common("shape", f"line-{k}", f"Line ({k})", sub_rect, sub, sub_rot)
            if side["style"] == "dotted" and wd >= 3:
                # Round dots, a diameter apart, one at each end (Chromium's thick dotted border).
                horizontal = k in ("top", "bottom")
                length = bx["w"] if horizontal else bx["h"]
                count = max(2, math.floor((length - wd) / (2 * wd)) + 1)
                period = (length - wd) / (count - 1)
                d = (f"M{n(wd / 2)} {n(wd / 2)}H{n(length - wd / 2)}" if horizontal
                     else f"M{n(wd / 2)} {n(wd / 2)}V{n(length - wd / 2)}")
                hexc = self.d.accent.default if c == "var:accent" else c[:7]
                markup = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n(bx["w"])} {n(bx["h"])}">'
                          f'<path d="{d}" fill="none" stroke="{hexc}" stroke-width="{n(wd)}" stroke-linecap="round" '
                          f'stroke-dasharray="0 {n(period)}"></path></svg>')
                layer["kind"] = "svg"
                layer.update({"markup": markup, "fit": "stretch"})
                if c == "var:accent":
                    layer["recolor"] = {hexc: "var:accent"}
                elif len(c) == 9:
                    layer["opacity"] = r3(layer.get("opacity", 1) * int(c[7:9], 16) / 255)
                self.layers.append(layer)
                continue
            if side["style"] in ("dashed", "dotted"):
                horizontal = k in ("top", "bottom")
                length = bx["w"] if horizontal else bx["h"]
                path = f"M0 {n(wd / 2)}H{n(bx['w'])}" if horizontal else f"M{n(wd / 2)} 0V{n(bx['h'])}"
                dash = self.dash(wd, length) if side["style"] == "dashed" else self.dots(wd, length)
                layer.update({"shape": "path", "path": path, "stroke": {"color": c, "width": wd, "dash": dash}})
            else:
                layer.update({"shape": "rect", "fill": {"type": "solid", "color": c}})
            self.layers.append(layer)

    def unplace(self, bx: dict, p: dict) -> dict:
        return bx  # boxes are measured untransformed; place() applies the chain

    def group_corners(self, p: dict) -> list:
        """A drop shadow's box takes the rounding of what fills its corners."""
        rect = p["rect"]
        out = [0.0, 0.0, 0.0, 0.0]
        for q in self.prims:
            if q.get("type") != "box" or q is p:
                continue
            r = q["rect"]
            tol = 1
            corners = (
                abs(r["x"] - rect["x"]) < tol and abs(r["y"] - rect["y"]) < tol,
                abs(r["x"] + r["w"] - rect["x"] - rect["w"]) < tol and abs(r["y"] - rect["y"]) < tol,
                abs(r["x"] + r["w"] - rect["x"] - rect["w"]) < tol and abs(r["y"] + r["h"] - rect["y"] - rect["h"]) < tol,
                abs(r["x"] - rect["x"]) < tol and abs(r["y"] + r["h"] - rect["y"] - rect["h"]) < tol,
            )
            for i, hit in enumerate(corners):
                if hit:
                    out[i] = max(out[i], q["radii"][i])
        return [r3(x) for x in out]

    # --- background images --------------------------------------------------

    def background_image(self, img: str, p: dict, rect: dict, rot: float, radius: list):
        size = p["bgSize"]
        tiled = img.startswith("repeating-") or (size not in ("auto", "auto auto") and "%" not in size)
        if not tiled:
            fill = self.css_fill(img, rect)
            if fill:
                layer = self.common("shape", "gradient", "Gradient", rect, p, rot)
                layer.update({"shape": "rect", "fill": fill})
                if any(radius):
                    layer["radius"] = radius
                self.layers.append(layer)
                return
        markup = self.pattern_svg(img, p, rect, radius)
        if markup:
            layer = self.common("svg", "pattern", "Pattern", rect, p, rot)
            layer.update({"markup": markup, "fit": "stretch"})
            self.layers.append(layer)

    @staticmethod
    def gradient(img: str):
        m = re.match(r"(repeating-)?(linear|radial)-gradient\((.*)\)$", img.strip(), re.S)
        if not m:
            return None
        rep, kind, body = bool(m.group(1)), m.group(2), m.group(3)
        parts = split_top(body)
        head = None
        if kind == "linear" and parts and (parts[0].endswith("deg") or parts[0].startswith("to ")):
            head = parts.pop(0)
        elif kind == "radial" and parts and not parts[0].startswith(("rgb", "#", "transparent")):
            head = parts.pop(0)
        stops = []
        for s in parts:
            m2 = re.match(r"(rgba?\([^)]*\)|#[0-9a-fA-F]+|transparent)\s*(.*)$", s.strip())
            if not m2:
                return None
            positions = m2.group(2).split()
            if not positions:
                stops.append((m2.group(1), None))
            for q in positions:
                stops.append((m2.group(1), q))
        angle = 180.0
        if kind == "linear" and head:
            if head.endswith("deg"):
                angle = float(head[:-3])
            else:
                angle = {"to top": 0, "to right": 90, "to bottom": 180, "to left": 270}.get(head, 180)
        return {"repeat": rep, "kind": kind, "angle": angle, "head": head, "stops": stops}

    def stop_offsets(self, stops, length: float) -> list[tuple[str, float]]:
        out: list = []
        for i, (c, q) in enumerate(stops):
            if q is None:
                v = None
            elif q.endswith("%"):
                v = float(q[:-1]) / 100
            else:
                v = px(q) / length if length else 0
            out.append([c, v])
        if out and out[0][1] is None:
            out[0][1] = 0.0
        if out and out[-1][1] is None:
            out[-1][1] = 1.0
        i = 0
        while i < len(out):
            if out[i][1] is None:
                j = i
                while out[j][1] is None:
                    j += 1
                a, b = out[i - 1][1], out[j][1]
                for k in range(i, j):
                    out[k][1] = a + (b - a) * (k - i + 1) / (j - i + 1)
                i = j
            i += 1
        last = 0.0
        for o in out:
            o[1] = max(last, o[1])
            last = o[1]
        return [(c, v) for c, v in out]

    def css_fill(self, img: str, rect: dict):
        g = self.gradient(img)
        if not g or g["repeat"] or len(g["stops"]) > 6:
            return None
        if g["kind"] == "linear":
            a = math.radians(g["angle"])
            length = abs(rect["w"] * math.sin(a)) + abs(rect["h"] * math.cos(a))
            stops = self.stop_offsets(g["stops"], length)
            return {"type": "linear", "angle": g["angle"],
                    "stops": [{"offset": r3(min(1, max(0, o))), "color": color(c) or "#00000000"} for c, o in stops]}
        if g["head"] and "circle" not in g["head"]:
            return None
        cx = cy = 0.5
        m = re.search(r"at ([\d.]+)% ([\d.]+)%", g["head"] or "")
        if m:
            cx, cy = float(m.group(1)) / 100, float(m.group(2)) / 100
        r = max(math.hypot(cx * rect["w"] - x, cy * rect["h"] - y) for x in (0, rect["w"]) for y in (0, rect["h"]))
        stops = self.stop_offsets(g["stops"], r)
        return {"type": "radial", "cx": cx, "cy": cy,
                "stops": [{"offset": r3(min(1, max(0, o))), "color": color(c) or "#00000000"} for c, o in stops]}

    def pattern_svg(self, img: str, p: dict, rect: dict, radius: list) -> str | None:
        g = self.gradient(img)
        if not g:
            self.warn.append(f"background {img[:60]} dropped")
            return None
        W, H = r3(rect["w"]), r3(rect["h"])
        size = p["bgSize"].split()
        pos = [px(v) for v in p["bgPosition"].split()] + [0, 0]
        if g["repeat"]:
            positions = [px(q) for _c, q in g["stops"] if q is not None and not q.endswith("%")]
            period = (max(positions) - min(positions)) if positions else 0
            if period <= 0:
                return None
            a = g["angle"] % 360
            if abs(a - 90) < 1e-6 or abs(a - 270) < 1e-6:
                tw, th = period, H
            elif abs(a) < 1e-6 or abs(a - 180) < 1e-6:
                tw, th = W, period
            else:
                self.warn.append("a repeating gradient at an angle other than 0/90/180/270 was not converted")
                return None
        else:
            tw = px(size[0]) if size and size[0] != "auto" else W
            th = px(size[1]) if len(size) > 1 and size[1] != "auto" else tw
        a = math.radians(g["angle"])
        if g["kind"] == "linear":
            length = abs(tw * math.sin(a)) + abs(th * math.cos(a)) if not g["repeat"] else period
            dx, dy = math.sin(a), -math.cos(a)
            cx, cy = tw / 2, th / 2
            if g["repeat"]:
                x1, y1 = (0, 0) if dx >= 0 and dy >= 0 else (tw, th)
                x1, y1 = (0, 0)
                x2, y2 = (period * dx, period * dy) if (dx > 0 or dy > 0) else (-period * dx, -period * dy)
                x2, y2 = abs(x2), abs(y2)
            else:
                x1, y1 = cx - dx * length / 2, cy - dy * length / 2
                x2, y2 = cx + dx * length / 2, cy + dy * length / 2
            stops = self.stop_offsets(g["stops"], length)
            grad = (f'<linearGradient id="g" gradientUnits="userSpaceOnUse" x1="{n(x1)}" y1="{n(y1)}" '
                    f'x2="{n(x2)}" y2="{n(y2)}">')
        else:
            rx = math.hypot(tw / 2, th / 2)
            stops = self.stop_offsets(g["stops"], rx)
            grad = f'<radialGradient id="g" gradientUnits="userSpaceOnUse" cx="{n(tw / 2)}" cy="{n(th / 2)}" r="{n(rx)}">'
        stop_xml = ""
        for c, o in stops:
            r_, g_, b_, a_ = rgba(c)
            if (r_, g_, b_) == SENTINEL_RGB:
                self.warn.append("an accent in a pattern is fixed at the default")
                r_, g_, b_ = rgba(self.d.accent.default)[:3]
            stop_xml += f'<stop offset="{n(min(1, max(0, o)))}" stop-color="#{r_:02X}{g_:02X}{b_:02X}"' + \
                (f' stop-opacity="{n(a_)}"' if a_ < 0.999 else "") + "></stop>"
        grad += stop_xml + ("</linearGradient>" if g["kind"] == "linear" else "</radialGradient>")
        shape = self.rounded_path(0, 0, W, H, radius) if any(radius) else None
        fill_el = (f'<path d="{shape}" fill="url(#p)"></path>' if shape else f'<rect width="{W}" height="{H}" fill="url(#p)"></rect>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"><defs>{grad}'
                f'<pattern id="p" patternUnits="userSpaceOnUse" x="{n(pos[0])}" y="{n(pos[1])}" width="{n(tw)}" height="{n(th)}">'
                f'<rect width="{n(tw)}" height="{n(th)}" fill="url(#g)"></rect></pattern></defs>{fill_el}</svg>')

    @staticmethod
    def rounded_path(x, y, w, h, r) -> str:
        tl, tr, br, bl = clamp_radii(list(r), w, h)
        return (f"M{n(x + tl)} {n(y)}H{n(x + w - tr)}" + (f"A{n(tr)} {n(tr)} 0 0 1 {n(x + w)} {n(y + tr)}" if tr else "")
                + f"V{n(y + h - br)}" + (f"A{n(br)} {n(br)} 0 0 1 {n(x + w - br)} {n(y + h)}" if br else "")
                + f"H{n(x + bl)}" + (f"A{n(bl)} {n(bl)} 0 0 1 {n(x)} {n(y + h - bl)}" if bl else "")
                + f"V{n(y + tl)}" + (f"A{n(tl)} {n(tl)} 0 0 1 {n(x + tl)} {n(y)}" if tl else "") + "Z")

    # --- text ---------------------------------------------------------------

    def run_style(self, st: dict) -> dict:
        family = first_family(st["family"])
        weight, style, synth = match_face(self.d, family, int(float(st["weight"])), st["style"])
        if synth:
            self.warn.append(f"{family} {st['weight']} {st['style']} is synthesised by the browser")
            weight, style = int(float(st["weight"])), st["style"]
        size = px(st["size"])
        ls = 0.0 if st["ls"] == "normal" else px(st["ls"]) / size if size else 0.0
        tt = {"uppercase": "upper", "lowercase": "lower"}.get(st["tt"], "none")
        if st["tt"] not in ("none", "uppercase", "lowercase"):
            self.warn.append(f"text-transform {st['tt']} dropped")
        return {"family": family, "weight": weight, "style": style, "size": size, "color": color(st["color"]) or "#00000000",
                "letterSpacing": round(ls, 4), "transform": tt}

    def template(self, segs: list, white_space: str, base: dict) -> list[tuple[dict, str]]:
        """The paragraph as (style, template text) runs."""
        runs: list[list] = []
        pre = white_space in ("pre", "pre-wrap", "break-spaces")

        stack: list[str] = []  # the open {% if %}s, each as written

        def add(st, text):
            if runs and runs[-1][0] == st:
                runs[-1][1] += text
            else:
                # A conditional never spans two runs: close it here, open it again there.
                if runs and stack:
                    runs[-1][1] += "{% endif %}" * sum(o.count("{% if") for o in stack)
                runs.append([st, "".join(stack) + text])

        cur = base
        for s in segs:
            if s["kind"] == "text":
                cur = self.run_style(s["st"])
                t = s["text"] if pre else re.sub(r"[ \t\n\r\f]+", " ", s["text"])
                add(cur, t)
            elif s["kind"] == "tok":
                cur = self.run_style(s["st"])
                add(cur, "{{" + s["name"] + "}}")
            elif s["kind"] == "br":
                add(cur, "\n")
            elif s["kind"] == "if":
                names = [t for t in s["tokens"] if t not in ("accent_color",)]
                if s["mode"] == "all":
                    opening = "".join("{% if " + t + " %}" for t in names)
                else:
                    opening = "{% if " + names[0] + " %}"
                    if len(names) > 1:
                        self.warn.append(f"an inline any-of {names} reads as {names[0]} only")
                add(cur, opening)
                stack.append(opening)
            elif s["kind"] == "endif":
                opening = stack.pop() if stack else ""
                add(cur, "{% endif %}" * opening.count("{% if"))
        # Collapse spaces as CSS does: at most one between words, none at a line's ends.
        if not pre:
            flat = []
            prev_space = True
            for st, text in runs:
                out = []
                i = 0
                while i < len(text):
                    if text.startswith("{%", i):
                        j = text.index("%}", i) + 2
                        out.append(text[i:j])
                        i = j
                        continue
                    ch = text[i]
                    if ch == " ":
                        if not prev_space:
                            out.append(" ")
                            prev_space = True
                    elif ch == "\n":
                        while out and out[-1] == " ":
                            out.pop()
                        out.append("\n")
                        prev_space = True
                    else:
                        out.append(ch)
                        prev_space = False
                    i += 1
                flat.append([st, "".join(out)])
            # no trailing space at the end
            for k in range(len(flat) - 1, -1, -1):
                t = flat[k][1]
                stripped = re.sub(r" +((?:\{%[^%]*%\})*)$", r"\1", t)
                flat[k][1] = stripped
                if re.sub(r"\{%[^%]*%\}", "", stripped):
                    break
            runs = [r for r in flat if r[1]]
        return [(st, t) for st, t in runs]

    def text(self, p: dict):
        segs = p["segs"]
        rect0 = p["rect"]
        if rect0["w"] <= 0 or rect0["h"] <= 0:
            return
        base = self.run_style(p["base"])
        runs = self.template(segs, p["whiteSpace"], base)
        if not runs or not "".join(t for _s, t in runs).strip():
            return
        has_tokens = any(s["kind"] == "tok" for s in segs)
        vertical = p["writingMode"].startswith("vertical")
        rect = dict(rect0)
        rot_extra = 0.0
        if vertical:
            cx, cy = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2
            rect = {"x": cx - rect["h"] / 2, "y": cy - rect["w"] / 2, "w": rect["h"], "h": rect["w"]}
            rot_extra = 90.0 if p["writingMode"] == "vertical-rl" else -90.0
        lines = p["lines"]
        fit = p.get("fit") or {}
        mode = fit.get("mode")
        size = base["size"]

        def floor_of(default_share):
            m = fit.get("min")
            if m:
                v = size * float(m[:-1]) / 100 if m.endswith("%") else float(m)
                if v < size:
                    return v
            return size * default_share

        if mode == "name":
            fit_v2, max_lines, minimum = "shrink", max(1, lines), floor_of(0.6)
        elif mode == "code":
            fit_v2, max_lines, minimum = "shrink", 1, floor_of(0.3)
        elif mode == "title":
            fit_v2, max_lines, minimum = "shrink", max(1, lines), size * 0.4
        elif mode == "wrap":
            fit_v2, max_lines, minimum = "shrink", int(fit.get("lines") or 2), floor_of(0.5)
        elif mode == "line":
            fit_v2, max_lines, minimum = "ellipsis", 1, size
        else:
            fit_v2, max_lines, minimum = "shrink", max(1, lines), size * (0.7 if has_tokens else 0.8)
        max_lines = max(max_lines, lines)
        align = {"start": "left", "end": "right", "center": "center", "-webkit-center": "center", "left": "left",
                 "right": "right", "justify": "left"}.get(p["align"], "left")
        # One line with a value: the box takes the room beside it.
        hard_lines = 1 + sum(1 for sg in segs if sg["kind"] == "br")
        if not vertical and (lines == 1 or lines == hard_lines) and has_tokens and not p.get("xf"):
            rect, align = self.widen(rect, p, align)
        # A hair wider than measured, so the words break where they did; the
        # extra goes on the side the text is not aligned to.
        grown = ceil64(rect["w"]) + 0.02 - rect["w"]
        if align == "right":
            rect["x"] -= grown
        elif align == "center":
            rect["x"] -= grown / 2
        rect["w"] += grown
        rect["h"] = rect["h"] + 0.02
        placed, rot, scale = place(rect, p, self.warn)
        rot = ((rot + rot_extra + 180) % 360) - 180
        lh = p["lineHeight"]
        if lh == "normal":
            line_height = (rect0["w"] if vertical else rect0["h"]) / max(1, lines) / size if size else 1.2
        else:
            line_height = px(lh) / size if size else 1.2
        token = next((s["name"] for s in segs if s["kind"] == "tok"), None)
        label = voc_label(token) if token else " ".join(t for _s, t in runs).strip()
        idbase = f"t-{token}" if token else "t-" + re.sub(r"[^a-z0-9]+", "-", label.lower())[:24]
        if p.get("slot"):
            idbase = f"slot-{p['slot']}"
        layer = self.common("text", idbase, label, placed, p, rot)
        layer.update({
            "font": {"family": base["family"], "weight": base["weight"], "style": base["style"]},
            "size": r3(size * scale), "color": base["color"], "letterSpacing": base["letterSpacing"],
            "lineHeight": r3(line_height), "align": align, "transform": base["transform"],
            "maxLines": min(20, max_lines), "fit": fit_v2,
        })
        if fit_v2 == "shrink":
            layer["minSize"] = r3(min(minimum, size) * scale)
        tw = p.get("textWrap") or ""
        if "balance" in tw:
            layer["textWrap"] = "balance"
        elif "pretty" in tw:
            layer["textWrap"] = "pretty"
        tsh = shadows(p.get("textShadow") or "none", text=True)
        if tsh:
            layer["shadow"] = tsh
        keys = ("family", "weight", "style", "size", "color", "letterSpacing", "transform")
        if len(runs) == 1 and all(runs[0][0][k] == base[k] for k in keys):
            layer["text"] = runs[0][1]
        elif all(st == runs[0][0] for st, _t in runs):
            st0 = runs[0][0]
            if all(st0[k] == base[k] for k in keys):
                layer["text"] = "".join(t for _s, t in runs)
            else:
                base = st0
                layer["font"] = {"family": st0["family"], "weight": st0["weight"], "style": st0["style"]}
                layer.update(size=r3(st0["size"] * scale), color=st0["color"], letterSpacing=st0["letterSpacing"],
                             transform=st0["transform"])
                if fit_v2 == "shrink":
                    layer["minSize"] = r3(min(minimum / size * st0["size"], st0["size"]) * scale)
                layer["text"] = "".join(t for _s, t in runs)
        else:
            spans = []
            for st, t in runs:
                sp = {"text": t}
                for k, field in (("family", "family"), ("weight", "weight"), ("style", "style"), ("color", "color"),
                                 ("letterSpacing", "letterSpacing"), ("transform", "transform")):
                    if st[k] != base[k]:
                        sp[field] = st[k]
                if st["size"] != base["size"]:
                    sp["size"] = r3(st["size"] * scale)
                spans.append(sp)
            layer["spans"] = spans
        self.layers.append(layer)

    def widen(self, rect: dict, p: dict, align: str) -> tuple[dict, str]:
        """A one-line value sits in a box as wide as its text. Give it the room
        it has on the side it is not anchored to: up to the edge of its
        box, never over anything else drawn on its line."""
        room = p.get("room") or {}
        bound = room.get("bound")
        if not bound:
            return rect, align
        # A value on a pill (its own background or border, hugging it) stays
        # inside the pill: the pill does not grow, so the value shrinks in it.
        me = p.get("border") or rect
        for q in self.prims:
            if q.get("type") != "box":
                continue
            r = q["rect"]
            hugs = (r["x"] <= me["x"] + 0.5 and r["y"] <= me["y"] + 0.5 and r["x"] + r["w"] >= me["x"] + me["w"] - 0.5
                    and r["y"] + r["h"] >= me["y"] + me["h"] - 0.5 and r["w"] <= me["w"] + 80 and r["h"] <= me["h"] + 40)
            if hugs:
                return rect, align
        x0, x1 = rect["x"], rect["x"] + rect["w"]
        lo, hi = bound["x"], bound["x"] + bound["w"]
        if hi - lo <= rect["w"] + 1:
            return rect, align
        y0, y1 = rect["y"], rect["y"] + rect["h"]
        gap = 8.0
        for q in self.prims:
            if q is p or q.get("type") not in ("text", "img", "svg", "box"):
                continue
            r = q["rect"] if q["type"] != "text" else (q.get("glyphs") or q["rect"])
            if q["type"] == "box" and (r["w"] > bound["w"] * 0.9 or r["h"] > (y1 - y0) * 3):
                continue  # a background, not a neighbour
            if min(y1, r["y"] + r["h"]) - max(y0, r["y"]) <= 1:
                continue
            # A value next to a value: they share the room between them.
            shared = q["type"] == "text" and q.get("lines") == 1 and any(sg["kind"] == "tok" for sg in q["segs"])
            if r["x"] + r["w"] <= x0 + 0.5:
                edge = r["x"] + r["w"]
                lo = max(lo, (edge + x0) / 2 + gap / 2 if shared else edge + gap)
            elif r["x"] >= x1 - 0.5:
                edge = r["x"]
                hi = min(hi, (x1 + edge) / 2 - gap / 2 if shared else edge - gap)
        lo, hi = min(lo, x0), max(hi, x1)
        left_gap, right_gap = x0 - lo, hi - x1
        tol = 1.0
        anchored_left = abs(x0 - bound["x"]) < tol or (align == "left" and right_gap > tol)
        anchored_right = abs(x1 - (bound["x"] + bound["w"])) < tol or align == "right"
        if align == "center" or (abs(left_gap - right_gap) < 2 and not anchored_left and not anchored_right):
            g = min(left_gap, right_gap)
            return {**rect, "x": x0 - g, "w": rect["w"] + 2 * g}, "center"
        if anchored_right and not anchored_left:
            return {**rect, "x": lo, "w": x1 - lo}, "right"
        return {**rect, "w": hi - x0}, "left"

    # --- art, pictures, QR --------------------------------------------------

    def svg(self, p: dict):
        rect, rot, _ = place(p["rect"], p, self.warn)
        markup = p["markup"]
        accent = self.d.accent.default if self.d.accent else None
        recolor = None
        if SENTINEL.lower() in markup.lower() or "var(--accent)" in markup:
            stand = accent.upper()
            while stand.lower() in markup.lower().replace(SENTINEL.lower(), ""):
                stand = "#%06X" % ((int(stand[1:], 16) + 1) % 0xFFFFFF)
            markup = re.sub(re.escape(SENTINEL), stand, markup, flags=re.I).replace("var(--accent)", stand)
            recolor = {stand: "var:accent"}
        if "{{" in markup or T0 in markup:
            self.warn.append("an svg holds a token: kept as its sample")
        markup = self.clip_svg(markup, p)
        layer = self.common("svg", "art", "Artwork", rect, p, rot)
        layer.update({"markup": markup, "fit": "stretch" if (p.get("par") or "").startswith("none") else "contain"})
        if recolor:
            layer["recolor"] = recolor
        self.layers.append(layer)

    def clip_svg(self, markup: str, p: dict) -> str:
        """Art that its clipping boxes cut (a rounded card's corner, a box it
        runs out of) keeps that cut, as a clipPath in its own units."""
        rect = p["rect"]
        clips = p.get("clips") or []
        need = None
        for clip in clips:
            c = clip["rect"]
            outside = rect["x"] < c["x"] - 0.5 or rect["y"] < c["y"] - 0.5 or \
                rect["x"] + rect["w"] > c["x"] + c["w"] + 0.5 or rect["y"] + rect["h"] > c["y"] + c["h"] + 0.5
            rounded = any(clip["radii"])
            if outside or rounded:
                need = clip
                break
        if not need:
            return markup
        vb = re.search(r'viewBox="([^"]+)"', markup)
        if not vb:
            return markup
        vx, vy, vw, vh = [float(x) for x in vb.group(1).replace(",", " ").split()]
        k = min(rect["w"] / vw, rect["h"] / vh)
        ox = rect["x"] + (rect["w"] - vw * k) / 2
        oy = rect["y"] + (rect["h"] - vh * k) / 2
        c = need["rect"]
        x, y = (c["x"] - ox) / k + vx, (c["y"] - oy) / k + vy
        w, h = c["w"] / k, c["h"] / k
        path = self.rounded_path(x, y, w, h, [r / k for r in need["radii"]])
        m = re.match(r"(<svg[^>]*>)(.*)(</svg>)\s*$", markup, re.S)
        if not m:
            return markup
        return (f'{m.group(1)}<defs><clipPath id="moa-clip"><path d="{path}"></path></clipPath></defs>'
                f'<g clip-path="url(#moa-clip)">{m.group(2)}</g>{m.group(3)}')

    def img(self, p: dict):
        role = p.get("role")
        rect, rot, _ = place(p["rect"], p, self.warn)
        if role == "qr":
            layer = self.common("qr", "qr", "QR code", rect, {**p, "all": [], "any": []}, rot)
            layer.pop("showIf", None)
            self.layers.append(layer)
            return
        source = {"logo": "org_logo", "photo": "guest_photo", "sponsor": "sponsor"}.get(role or "")
        if not source:
            self.warn.append("an <img> that is not a known picture was left out")
            return
        if role == "sponsor" and p.get("parent") and p.get("parentOnlyChild"):
            rect, rot, _ = place(p["parent"], p, self.warn)
        names = {"org_logo": "Club logo", "guest_photo": "Guest photo", "sponsor": "Sponsor logos"}
        prim = dict(p)
        prim["all"] = [t for t in (p.get("all") or []) if t not in IMAGE_SHOW]
        prim["any"] = [g for g in (p.get("any") or []) if not set(g) & IMAGE_SHOW]
        layer = self.common("image", {"org_logo": "logo", "guest_photo": "photo", "sponsor": "sponsor"}[source],
                            names[source], rect, prim, rot)
        fit = "cover" if p.get("fit") == "cover" else "contain"
        layer.update({"source": source, "fit": fit})
        pos = (p.get("position") or "50% 50%").split()
        if pos and pos[0] in ("0%", "0px", "left"):
            layer["align"] = "left"
        elif pos and pos[0] in ("100%", "right"):
            layer["align"] = "right"
        radius = self.corner_radii(p["rect"], p.get("radii") or [0, 0, 0, 0], p)
        if any(radius):
            layer["radius"] = radius
        if source == "guest_photo":
            layer["placeholder"] = "initials"
            st = self.d.raw.get("initials") or {}
            ps = {"color": (color(st["color"]) if st.get("color") and not st["color"].startswith("var(")
                            else ("var:accent" if st.get("color") == "var(--accent)" else "#3C4257"))}
            bgc = st.get("background")
            if bgc and bgc not in ("transparent",):
                c = "var:accent" if bgc == "var(--accent)" else color(bgc)
                if c:
                    ps["background"] = {"type": "solid", "color": c}
            if st.get("font"):
                ps["family"] = first_family(st["font"])
            ps["weight"] = int(st.get("weight", 700))
            layer["placeholderStyle"] = ps
        self.layers.append(layer)

    def slot(self, p: dict):
        if p.get("slot") != "signature":
            return
        self.signatures += 1
        rect, rot, _ = place(p["rect"], p, self.warn)
        source = "signature_1" if self.signatures == 1 else "signature_2"
        prim = dict(p)
        prim["all"] = [t for t in (p.get("all") or []) if t not in IMAGE_SHOW]
        layer = self.common("image", "signature", "Signature" if self.signatures == 1 else "Second signature", rect, prim, rot)
        layer.update({"source": source, "fit": "contain", "valign": "bottom"})
        self.layers.append(layer)

    # --- all ----------------------------------------------------------------

    def run(self) -> list[dict]:
        for p in self.prims:
            t = p["type"]
            try:
                if t == "box":
                    self.box(p)
                elif t == "text":
                    self.text(p)
                elif t == "svg":
                    self.svg(p)
                elif t == "img":
                    self.img(p)
                elif t == "slot":
                    self.slot(p)
            except Exception as exc:  # noqa: BLE001 - report and go on
                self.warn.append(f"{t}: {type(exc).__name__}: {exc}")
                raise
        return self.layers


_LABELS = {t["key"]: t["label"] for k in ("ticket", "certificate", "badge") for t in voc.tokens(k)}


def voc_label(token: str) -> str:
    if token in _LABELS:
        return _LABELS[token].split(" (")[0]
    from app.core.layout.tokens import TOKEN_BY_KEY

    t = TOKEN_BY_KEY.get(token)
    return t["label"] if t else token.replace("_", " ").capitalize()


# ─── One design ──────────────────────────────────────────────────────────────


def images_map(kind: str, values: dict) -> dict:
    out = {}
    payload = values.get("qr")
    d_qr = {"ticket": "qr_code_src", "certificate": "qr_code_src", "badge": "qr_code_url"}[kind]
    if payload:
        out[dqr.data_uri(payload, "M", 0)] = "qr"
    if values.get(d_qr):
        out[values[d_qr]] = "qr"
    if values.get("logo_src"):
        out[values["logo_src"]] = "logo"
    if values.get("photo_url"):
        out[values["photo_url"]] = "photo"
    if values.get("sponsor_src"):
        out[values["sponsor_src"]] = "sponsor"
    return out


def convert_design(browser, design_id: str, wording: str | None = None) -> tuple[dict, list[str]]:
    d = dd.get(design_id)
    values = sample_values(d.kind)
    if d.kind == "badge":
        values["sponsor_src"] = SPONSOR
    options = {"wording": wording} if wording else None
    html = marker_page(design_id, values, options)
    page = browser.new_page(viewport={"width": d.page.width_px, "height": d.page.height_px})
    try:
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__moaReady === true", timeout=20000)
        result = page.evaluate(WALK, {"accent": SENTINEL, "images": images_map(d.kind, values)})
    finally:
        page.close()
    conv = Convert(d, result["primitives"], result["page"])
    layers = conv.run()
    layout = {
        "version": 2,
        "page": {"widthMm": d.page.width_mm, "heightMm": d.page.height_mm},
        "canvas": {"width": d.page.width_px, "height": d.page.height_px},
        "vars": {"accent": d.accent.default} if d.accent else {},
        "layers": layers,
        "starter": d.id,
    }
    if conv.canvas_bg:
        layout["canvas"]["background"] = conv.canvas_bg
    warnings = list(dict.fromkeys(result["warnings"] + conv.warn))
    for layer in layout["layers"]:
        if layer["kind"] == "svg":  # stored canonical: what the builders draw as it is
            layer["markup"] = sanitize(layer["markup"])
    parsed = v2schema.parse(layout)
    probs = [p for p in v2validate.problems(parsed, d.kind) if p["level"] == "error"]
    if probs:
        warnings += [f"PROBLEM {p['code']} {p['field']}: {p['message']}" for p in probs]
    return compact(v2schema.dump(parsed)), warnings


#: Fields left out of a starter's JSON when they hold their default.
_DEFAULTS = {
    "rotation": 0, "opacity": 1, "hidden": False, "locked": False, "preprinted": False, "letterSpacing": 0,
    "lineHeight": 1.2, "align": "left", "valign": "top", "transform": "none", "textWrap": "normal", "maxLines": 1,
    "fit": None, "padding": [0, 0, 0, 0], "radius": [0, 0, 0, 0], "quietZone": 0, "dark": "#000000",
    "light": "#FFFFFF", "placeholder": "none",
}


def _ints(v):
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, list):
        return [_ints(x) for x in v]
    if isinstance(v, dict):
        return {k: _ints(x) for k, x in v.items()}
    return v


def compact(layout: dict) -> dict:
    layout = _ints(layout)
    out = dict(layout)
    layers = []
    for layer in layout["layers"]:
        lay = {}
        for k, v in layer.items():
            if k in _DEFAULTS and _DEFAULTS[k] is not None and v == _DEFAULTS[k]:
                continue
            if k == "text" and layer.get("spans"):
                continue
            if k == "valign" and layer["kind"] == "image" and v == "middle":
                continue
            if k == "align" and layer["kind"] == "image" and v == "center":
                continue
            lay[k] = v
        layers.append(lay)
    out["layers"] = layers
    return out


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


# ─── The others: card_cr80 and the blanks (from v1) ─────────────────────────


def v1_starters() -> None:
    from app.core.layout import starters as v1
    from app.core.layout.model import dump as v1dump
    from app.core.layout_v2.convert import from_v1

    def v1layout(d: dict) -> dict:
        return {"canvas": v1dump(d["canvas"]), "layers": [v1dump(x) for x in d["layers"]], "page": v1dump(d["page"])}

    card = from_v1(v1layout(v1.badge_starter("card_cr80")))
    card["starter"] = "badge/card_cr80"
    write_json(STARTERS / "badge" / "card_cr80.json", compact(card))
    for kind in ("ticket", "certificate"):
        blank = from_v1(v1layout(v1.blank_layout(kind)))
        blank["starter"] = f"{kind}/blank"
        write_json(STARTERS / kind / "blank.json", compact(blank))
    write_json(STARTERS / "badge" / "blank.json", compact(badge_blank()))


def badge_blank() -> dict:
    """A 4 x 6 in badge with the guest's first name, last name and the QR."""
    return v2schema.dump(v2schema.parse({
        "version": 2, "page": {"widthMm": 101.6, "heightMm": 152.4},
        "canvas": {"width": 384, "height": 576, "background": {"type": "solid", "color": "#FFFFFF"}},
        "layers": [
            {"id": "t-first_name", "name": "First name", "kind": "text", "x": 28, "y": 120, "width": 328, "height": 64,
             "text": "{{first_name}}", "font": {"family": "Inter", "weight": 700}, "size": 56, "lineHeight": 1.1,
             "align": "center", "fit": "shrink", "minSize": 24, "color": "#111111"},
            {"id": "t-last_name", "name": "Last name", "kind": "text", "x": 28, "y": 190, "width": 328, "height": 34,
             "text": "{{last_name}}", "font": {"family": "Inter", "weight": 500}, "size": 26, "lineHeight": 1.2,
             "align": "center", "fit": "shrink", "minSize": 14, "color": "#3C413D", "showIf": "last_name"},
            {"id": "t-role", "name": "Role", "kind": "text", "x": 28, "y": 250, "width": 328, "height": 24,
             "text": "{{role}}", "font": {"family": "Inter", "weight": 600}, "size": 16, "letterSpacing": 0.12,
             "align": "center", "fit": "shrink", "minSize": 10, "color": "#6B716C"},
            {"id": "qr", "name": "QR code", "kind": "qr", "x": 132, "y": 380, "width": 120, "height": 120},
            {"id": "t-badge_id", "name": "Badge number", "kind": "text", "x": 28, "y": 512, "width": 328, "height": 20,
             "text": "{{badge_id}}", "font": {"family": "Roboto Mono", "weight": 400}, "size": 14, "align": "center",
             "fit": "shrink", "minSize": 8, "color": "#5A5F5B"},
        ],
        "starter": "badge/blank",
    }))


# ─── Parity ──────────────────────────────────────────────────────────────────


def render(browser, html: str, w: int, h: int, scale: int):
    from io import BytesIO

    from PIL import Image

    ctx = browser.new_context(device_scale_factor=scale, viewport={"width": w, "height": h})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.set_content(html, wait_until="load")
        page.wait_for_function("window.__moaReady === true", timeout=20000)
        over = page.evaluate("""() => Array.from(document.querySelectorAll('[data-layer] [data-fit]')).filter(el => {
              const box = el.parentElement, s = getComputedStyle(box);
              const room = box.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom);
              return el.offsetHeight > room + 1.5;
            }).map(el => el.closest('[data-layer]').getAttribute('data-layer') + ': ' + el.textContent.slice(0, 40))""")
        fitted = page.evaluate("""() => Array.from(document.querySelectorAll('[data-layer] [data-fitted]')).map(el =>
              el.closest('[data-layer]').getAttribute('data-layer') + ' -> ' + getComputedStyle(el).fontSize)""")
        png = page.screenshot(clip={"x": 0, "y": 0, "width": w, "height": h})
    finally:
        ctx.close()
    return Image.open(BytesIO(png)).convert("RGB"), over, fitted, errors


def score(a, b, mask=None):
    import numpy as np

    x = np.asarray(a.convert("RGB"), dtype=np.int16)
    y = np.asarray(b.convert("RGB"), dtype=np.int16)
    d = np.abs(x - y).max(axis=2)
    keep = np.ones(d.shape, dtype=bool) if mask is None else ~mask
    return 100.0 * (1 - d[keep].mean() / 255.0), 100.0 * (d[keep] > 48).mean(), d


def qr_mask(layout: dict, shape, scale: int):
    import numpy as np

    m = np.zeros(shape[:2], dtype=bool)
    for layer in layout["layers"]:
        if layer["kind"] == "qr":
            x0, y0 = int(layer["x"] * scale) - 2, int(layer["y"] * scale) - 2
            x1, y1 = int((layer["x"] + layer["width"]) * scale) + 2, int((layer["y"] + layer["height"]) * scale) + 2
            m[max(0, y0):y1, max(0, x0):x1] = True
    return m


def side_by_side(ref, ours, d, path: Path, title: str, labels=("designer preview", "layout v2 starter")):
    import numpy as np
    from PIL import Image, ImageDraw

    w, h = ref.size
    heat = Image.fromarray(np.clip(d * 4, 0, 255).astype(np.uint8), "L").convert("RGB")
    canvas = Image.new("RGB", (w * 3 + 40, h + 40), "white")
    canvas.paste(ref, (0, 40))
    canvas.paste(ours, (w + 20, 40))
    canvas.paste(heat, (2 * w + 40, 40))
    dr = ImageDraw.Draw(canvas)
    dr.text((10, 12), labels[0], fill="black")
    dr.text((w + 30, 12), f"{labels[1]}  {title}", fill="black")
    dr.text((2 * w + 50, 12), "difference x4", fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, optimize=True)


def cases(kind: str) -> dict:
    base = sample_values(kind)
    long_name = "Annapoorna Venkataraman Pillai"
    long_event = "The Grand Midsummer Music Fest"
    if kind == "ticket":
        return {
            "long": {**base, "event_name": long_event, "attendee_name": long_name,
                     "tagline": "An evening of live music under the stars and lanterns",
                     "venue": "Sree Chithira Thirunal Hall, Trivandrum", "app_name": "Trivandrum Makers Collective",
                     "ticket_type": "PREMIUM PASS", "ticket_id": "TK-482139057"},
            "oneword": {**base, "attendee_name": "Annapoorna"},
            "novenue": {k: v for k, v in base.items() if k not in ("venue", "gate", "row", "seat", "doors_time")},
        }
    if kind == "certificate":
        return {
            "long": {**base, "recipient_name": long_name,
                     "course_title": "Advanced Robotics, Embedded Systems and Applied Machine Vision",
                     "event_title": "Advanced Robotics, Embedded Systems and Applied Machine Vision",
                     "institute_name": "Government Engineering College Thiruvananthapuram",
                     "signatory_name": "Prof. Lakshmikanth Raghunathan", "signatory_designation": "Head of Department, Electronics",
                     "certificate_id": "MOA-CERT-2026-0001234"},
            "oneword": {**base, "recipient_name": "Annapoorna"},
            "novenue": {k: v for k, v in base.items() if k not in ("issue_city", "grade", "rank_or_grade", "duration_hours")},
        }
    return {
        "long": {**base, "first_name": "Annapoorna", "last_name": "Venkataraman Pillai", "full_name": long_name,
                 "role": "PHOTOGRAPHER", "job_title": "Principal Product Designer", "organization": "Kerala Startup Mission",
                 "event_name": "Kerala Developer Festival 2026", "org_name": "Trivandrum Makers Collective",
                 "access_level": "All-access + Backstage", "badge_id": "MOA-004172", "member_id": "MOA-004172",
                 "event_venue": "Technopark, Trivandrum", "initials": "AV"},
        "oneword": {**base, "first_name": "Annapoorna", "last_name": "", "full_name": "Annapoorna", "initials": ""},
        "novenue": {**{k: v for k, v in base.items() if k not in ("event_venue", "zone", "organization", "job_title")},
                    "photo_url": ""},
    }


def parity(browser, out: Path, design_ids: list[str]) -> dict:
    from PIL import Image

    from app.core.layout_v2.build import build_html

    report = {}
    for design_id in design_ids:
        d = dd.get(design_id)
        layout = json.loads((STARTERS / d.kind / f"{d.key}.json").read_text())
        values = sample_values(d.kind)
        scale = 2 if d.kind == "badge" else 1
        w, h = d.page.width_px, d.page.height_px
        html = build_html(layout, text_values(values), context=context_for(d.kind, values), kind=d.kind)
        ours, over, fitted, errors = render(browser, html, w, h, scale)
        entry = {"overflow": over, "fitted": fitted, "errors": errors}
        own_wording = None
        if d.kind == "certificate":
            own_wording = next(k for k, v in d.raw["wordings"].items() if v is None)
            wl = STARTERS / "certificate" / "wordings" / f"{d.key}.{own_wording}.json"
            if wl.exists():
                layout_w = json.loads(wl.read_text())
                html_w = build_html(layout_w, text_values(values), context=context_for(d.kind, values), kind=d.kind)
                ours_w, over_w, _fitted_w, _errors_w = render(browser, html_w, w, h, scale)
                entry["overflow_own_wording"] = over_w
            else:
                layout_w, ours_w = layout, ours
        else:
            layout_w, ours_w = layout, ours
        prev = TEMPLATES / d.kind / "previews" / f"{d.key}.png"
        ref = Image.open(prev).convert("RGB")
        mask = qr_mask(layout_w, (ref.size[1], ref.size[0]), scale) if d.kind != "ticket" else None
        s, changed, diff = score(ref, ours_w, mask)
        s_full, _c, _d = score(ref, ours_w)
        entry.update(score=round(s, 3), score_unmasked=round(s_full, 3), changed=round(changed, 3))
        side_by_side(ref, ours_w, diff, out / f"{d.kind}-{d.key}.png",
                     f"match {s:.2f}% (QR box left out: {d.kind != 'ticket'}), {changed:.2f}% of pixels differ",
                     ("designer preview" if d.kind != "ticket" else "HTML preset render", "layout v2 starter"))
        # The same design through the preset path, same data, our QR: conversion alone.
        preset_html = dd.fill(d.id, values, {"wording": own_wording} if own_wording else None)
        preset_png, _o, _f, _e = render(browser, preset_html, w, h, scale)
        sp, _c2, dp = score(preset_png, ours_w)
        entry["score_vs_preset"] = round(sp, 3)
        side_by_side(preset_png, ours_w, dp, out / "vs-preset" / f"{d.kind}-{d.key}.png", f"match {sp:.2f}%",
                     ("HTML preset (our QR)", "layout v2 starter"))
        for case, cvals in cases(d.kind).items():
            chtml = build_html(layout, text_values(cvals), context=context_for(d.kind, cvals), kind=d.kind)
            cpng, cover, cfit, cerr = render(browser, chtml, w, h, scale)
            entry[f"case_{case}"] = {"overflow": cover, "fitted": cfit, "errors": cerr}
            (out / "cases").mkdir(parents=True, exist_ok=True)
            cpng.save(out / "cases" / f"{d.kind}-{d.key}.{case}.png", optimize=True)
        report[design_id] = entry
        flag = "" if s >= 99.3 else "  BELOW 99.3"
        print(f"{design_id:40} match {s:7.3f}% (unmasked {s_full:7.3f}%)  vs preset {sp:7.3f}%  "
              f"overflow {len(over)}  fitted {len(fitted)}{flag}", flush=True)
    return report


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="", help="convert only designs whose id contains this")
    ap.add_argument("--parity", type=Path, help="render, compare and save side-by-sides here")
    ap.add_argument("--no-convert", action="store_true", help="only the parity proof, of the starters already written")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    ids = [d.id for kind in dd.KINDS for d in dd.catalogue(kind) if args.only in d.id]
    log = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            if not args.no_convert:
                for design_id in ids:
                    d = dd.get(design_id)
                    wording = d.raw.get("default_wording") if d.kind == "certificate" else None
                    layout, warnings = convert_design(browser, design_id, wording)
                    write_json(STARTERS / d.kind / f"{d.key}.json", layout)
                    log[design_id] = warnings
                    print(f"{design_id:40} {len(layout['layers']):3} layers  {len(warnings)} notes", flush=True)
                    for wn in warnings:
                        print(f"    {wn}")
                    if d.kind == "certificate":
                        for other in d.raw["wordings"]:
                            if other == wording:
                                continue
                            lw, ww = convert_design(browser, design_id, other)
                            write_json(STARTERS / "certificate" / "wordings" / f"{d.key}.{other}.json", lw)
                            for wn in ww:
                                if wn not in warnings:
                                    print(f"    [{other}] {wn}")
                if not args.only:
                    v1_starters()
            if args.parity:
                args.parity.mkdir(parents=True, exist_ok=True)
                report = parity(browser, args.parity, ids)
                (args.parity / "report.json").write_text(json.dumps(report, indent=1))
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
