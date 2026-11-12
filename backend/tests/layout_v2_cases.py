"""The layout v2 conformance cases: small layouts that between them use every
layer kind and option, with their values and context. The Python builder's
output for each, normalised, is written to tests/fixtures/layout_v2/<name>.json
(`LAYOUT_V2_WRITE_FIXTURES=1 pytest tests/test_layout_v2.py -k fixtures`);
the TypeScript builder (moa_web packages/layout/src/v2) must produce the same.
See tests/fixtures/layout_v2/README.md for the normaliser.
"""
from __future__ import annotations

import base64
import html as _html
import re
from html.parser import HTMLParser

PNG = "data:image/png;base64," + base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                  "1f15c4890000000d49444154789c6360f8cf00000301010018dd8db00000000049454e44ae426082")).decode()
SVG_IMG = "data:image/svg+xml;base64," + base64.b64encode(
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'><rect width='10' height='10' fill='#888'/></svg>").decode()


def page(w=384, h=576, wmm=101.6, hmm=152.4, background=None, accent=None, layers=()):
    out = {"version": 2, "page": {"widthMm": wmm, "heightMm": hmm}, "canvas": {"width": w, "height": h},
           "layers": list(layers)}
    if background is not None:
        out["canvas"]["background"] = background
    if accent:
        out["vars"] = {"accent": accent}
    return out


def text(id, t="{{first_name}}", **more):
    return {"id": id, "name": id, "kind": "text", "x": 10, "y": 10, "width": 200, "height": 30, "text": t, **more}


def shape(id, **more):
    return {"id": id, "name": id, "kind": "shape", "x": 5, "y": 6, "width": 100, "height": 50, **more}


SOLID = {"type": "solid", "color": "#112233"}
LINEAR = {"type": "linear", "angle": 135, "stops": [{"offset": 0, "color": "var:accent"}, {"offset": 0.5, "color": "#FFFFFF80"},
                                                     {"offset": 1, "color": "brand.primary"}]}
RADIAL = {"type": "radial", "cx": 0.25, "cy": 0.75, "stops": [{"offset": 0, "color": "#FF0000"}, {"offset": 1, "color": "#0000FF"}]}
CTX = {"qr": "TK-48213", "colors": {"primary": "#1F3BD8", "secondary": "#FF8A3D", "ticketType": "#0B6B3A"}}
VALUES = {"first_name": "Meera", "last_name": "Pillai", "full_name": "Meera Pillai", "role": "SPEAKER",
          "event_name": "DevFest <Kerala> & \"Friends\" 'tis", "ticket_type": "VIP", "organization": "",
          "attendee_name": "Meera Pillai", "field.t_shirt_size": "M", "badge_id": "A-0417"}

CASES: dict[str, dict] = {
    "text-basic": {
        "about": "One text layer with its defaults; the value HTML-escaped.",
        "kind": "badge",
        "layout": page(layers=[text("name", "{{event_name}}")]),
        "values": VALUES, "context": CTX,
    },
    "text-style": {
        "about": "Font, weight, italic, size, colour, letter spacing, line height, align, transform, wrap, lines, fit, minSize.",
        "kind": "badge",
        "layout": page(layers=[
            text("a", "{{first_name}}", font={"family": "Space Grotesk", "weight": 700}, size=74, color="#0A0A0A",
                 letterSpacing=-0.04, lineHeight=0.9, transform="upper", fit="shrink", minSize=18),
            text("b", "Plain words\non two lines", font={"family": "Fraunces", "weight": 500, "style": "italic"}, size=19.5,
                 align="center", valign="middle", maxLines=2, fit="ellipsis", textWrap="balance", y=60, height=60),
            text("c", "{{last_name}}", align="right", valign="bottom", transform="lower", fit="none", textWrap="pretty",
                 y=130, maxLines=3),
        ]),
        "values": VALUES, "context": CTX,
    },
    "text-spans": {
        "about": "Styled runs: each span writes only what it sets; size as em of the layer's.",
        "kind": "certificate",
        "layout": page(1123, 794, 297, 210, accent="#6B1F2E", layers=[
            {**text("s", "", width=520, height=81, size=18, lineHeight=1.5, maxLines=3, fit="shrink", minSize=9),
             "spans": [{"text": "for completing "},
                       {"text": "{{course_title}}", "weight": 600},
                       {"text": " at "},
                       {"text": "{{institute_name | upper}}", "family": "Inter", "style": "italic", "size": 13.5,
                        "color": "var:accent", "letterSpacing": 0.1, "transform": "upper"},
                       {"text": "{% if grade %}, grade {{grade}}{% endif %}."}]},
        ]),
        "values": {"course_title": "Full Stack Web Development", "institute_name": "Sample Institute", "grade": "A+"},
        "context": {"qr": "https://example.com/verify/C-1"},
    },
    "text-tokens": {
        "about": "Filters, if/else, not, aliases ({{name}}), a design token ({{role}} on a badge), field answers, unknown values.",
        "kind": "badge",
        "layout": page(layers=[
            text("a", "{{name | upper}} · {{role | lower}} · {{field.t_shirt_size}}"),
            text("b", "{% if organization %}{{organization}}{% else %}Independent{% endif %}", y=50),
            text("c", "{% if not organization %}no org{% endif %}{{missing_token}}", y=90),
            text("d", "{{first_name | title}} {{last_name | title}}", y=130),
        ]),
        "values": VALUES, "context": CTX,
    },
    "text-box": {
        "about": "A text layer's own box: background fill, padding, radius (all and per corner), shadows (text-shadow).",
        "kind": "badge",
        "layout": page(accent="#FF8A3D", layers=[
            text("pill", "ADMIT ONE", background={"type": "solid", "color": "var:accent"}, padding=[8, 16, 8, 16], radius=999,
                 shadow={"x": 0, "y": 0, "blur": 6, "color": "#FFFFFF"}),
            text("tag", "{{ticket_type}}", y=60, background=LINEAR, padding=[1, 2, 3, 4], radius=[1, 2, 3, 4],
                 shadow=[{"x": 0, "y": 0, "blur": 6, "spread": 3, "color": "#FFFFFF"},
                         {"x": 5, "y": 5, "blur": 0, "color": "#1A0507CC"}]),
        ]),
        "values": VALUES, "context": CTX,
    },
    "show-if": {
        "about": "showIf one token, all ('a b'), any ('a|b'), a picture token; hidden layers; text that fills to nothing.",
        "kind": "badge",
        "layout": page(layers=[
            text("one", "{{first_name}}", showIf="first_name"),
            text("all-yes", "both", showIf="first_name last_name", y=40),
            text("all-no", "both", showIf="first_name organization", y=70),
            text("any-yes", "either", showIf="organization|last_name", y=100),
            text("any-no", "either", showIf="organization|zone", y=130),
            text("hidden", "never", hidden=True, y=160),
            text("empty", "{{organization}}", y=190),
            shape("logo-frame", showIf="logo_src", fill=SOLID),
            shape("photo-frame", showIf="photo_url", fill=SOLID, y=300),
        ]),
        "values": VALUES, "context": {**CTX, "images": {"guest_photo": PNG}},
    },
    "shape-css": {
        "about": "rect and ellipse as CSS: solid (hex6, hex8), gradients, border, radius, box shadows (inset, list), rotation, opacity.",
        "kind": "ticket",
        "layout": page(1280, 600, 338.667, 158.75, accent="#C6F432", background=RADIAL, layers=[
            shape("a", fill=SOLID, radius=12),
            shape("b", fill={"type": "solid", "color": "#00000080"}, stroke={"color": "#FFFFFF", "width": 2},
                  radius=[28, 0, 0, 28], y=70),
            shape("c", shape="ellipse", fill=LINEAR, y=130, rotation=-12.5, opacity=0.25),
            shape("d", fill=RADIAL, y=200, shadow=[{"x": 0, "y": 0, "spread": 4, "color": "#FFFFFF"},
                                                   {"x": 0, "y": 0, "spread": 6, "color": "var:accent"},
                                                   {"x": 0, "y": 0, "blur": 30, "color": "#2DE2E6B3", "inset": True}]),
            shape("e", shape="ellipse", stroke={"color": "ticket_type", "width": 3}, y=270, rotation=45),
        ]),
        "values": {}, "context": CTX,
    },
    "shape-svg": {
        "about": "A path, dashed rect/ellipse, gradient fills in SVG, drop shadows.",
        "kind": "ticket",
        "layout": page(1280, 600, 338.667, 158.75, accent="#FF2E97", layers=[
            shape("path", shape="path", path="M0 25 L50 0 L100 25 Q50 50 0 25 Z", fill=SOLID,
                  stroke={"color": "#FFFFFF", "width": 1.5}, shadow={"x": 0, "y": 22, "blur": 34, "color": "#173F2E47"}),
            shape("dash-rect", stroke={"color": "var:accent", "width": 2, "dash": [6, 4.14]}, radius=[10, 10, 0, 0], y=70),
            shape("dash-ellipse", shape="ellipse", fill=SOLID, stroke={"color": "#000000", "width": 2, "dash": [6, 4]}, y=130),
            shape("lin", shape="path", path="M0 0H100V50H0Z", fill=LINEAR, y=200),
            shape("rad", shape="path", path="M0 0H100V50H0Z", fill=RADIAL, y=260),
            shape("line", shape="path", path="M1.5 0V460", width=3, height=460, x=790, y=0,
                  stroke={"color": "#14122B", "width": 3, "dash": [6, 3.02]}),
        ]),
        "values": {}, "context": CTX,
    },
    "svg-art": {
        "about": "SVG art: recolour (#RRGGBB literals), ids prefixed, fit contain/cover/stretch, drop shadow.",
        "kind": "certificate",
        "layout": page(1123, 794, 297, 210, accent="#14503F", layers=[
            {"id": "art", "name": "Art", "kind": "svg", "x": 0, "y": 0, "width": 1123, "height": 794, "fit": "contain",
             "markup": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 110 110"><defs><linearGradient id="g">'
                       '<stop offset="0" stop-color="#6B1F2E"></stop><stop offset="1" stop-color="#F6F7F9"></stop></linearGradient>'
                       '<clipPath id="c"><circle cx="55" cy="55" r="50"></circle></clipPath></defs>'
                       '<g clip-path="url(#c)"><rect width="110" height="110" fill="url(#g)"></rect></g>'
                       '<circle cx="55" cy="55" r="40" fill="none" stroke="#6b1f2e" style="stroke-width: 2"></circle>'
                       '<use href="#c"></use></svg>',
             "recolor": {"#6B1F2E": "var:accent"}},
            {"id": "cover", "name": "Cover", "kind": "svg", "x": 10, "y": 10, "width": 100, "height": 40, "fit": "cover",
             "markup": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#FFFFFF" '
                       'stroke-width="2"><path d="M12 2v20M2 12h20"></path></svg>',
             "shadow": {"x": 0, "y": 2, "blur": 8, "color": "#00000066"}},
            {"id": "stretch", "name": "Stretch", "kind": "svg", "x": 10, "y": 60, "width": 100, "height": 40, "fit": "stretch",
             "markup": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#123456"></rect></svg>'},
        ]),
        "values": {}, "context": {"qr": "https://example.com/verify/C-1"},
    },
    "images": {
        "about": "Pictures as data: URIs, fit, align/valign, radius, border; a missing picture; the initials tile.",
        "kind": "badge",
        "layout": page(accent="#5B3DF5", layers=[
            {"id": "logo", "name": "Logo", "kind": "image", "source": "org_logo", "x": 10, "y": 10, "width": 52, "height": 52,
             "fit": "contain", "align": "left", "valign": "bottom"},
            {"id": "upload", "name": "Upload", "kind": "image", "source": "upload", "imageKey": "uploads/a.png",
             "x": 70, "y": 10, "width": 100, "height": 50, "fit": "cover", "radius": [4, 4, 0, 0],
             "border": {"color": "#FFFFFF", "width": 2}, "shadow": {"x": 4, "y": 4, "color": "#111111"}},
            {"id": "gone", "name": "Gone", "kind": "image", "source": "event_icon", "x": 0, "y": 0, "width": 10, "height": 10},
            {"id": "photo", "name": "Photo", "kind": "image", "source": "guest_photo", "x": 100, "y": 100, "width": 180,
             "height": 180, "fit": "cover", "radius": 90, "placeholder": "initials",
             "placeholderStyle": {"color": "var:accent", "background": {"type": "solid", "color": "#EEF0FF"},
                                  "family": "Space Grotesk", "weight": 700}},
            {"id": "photo2", "name": "Photo 2", "kind": "image", "source": "guest_photo", "x": 10, "y": 300, "width": 80,
             "height": 80, "placeholder": "initials"},
            {"id": "sig", "name": "Signature", "kind": "image", "source": "signature_1", "x": 10, "y": 400, "width": 230,
             "height": 44, "valign": "bottom"},
        ]),
        "values": {"first_name": "Meera", "last_name": "Pillai"},
        "context": {**CTX, "images": {"org_logo": SVG_IMG, "upload:uploads/a.png": PNG, "event_icon": "https://evil.example/x.png",
                                      "signature_1": PNG}},
    },
    "qr": {
        "about": "The QR: its quiet zone, a background and radius round it, a shadow. (The normaliser keeps only that it is there.)",
        "kind": "badge",
        "layout": page(layers=[
            {"id": "qr", "name": "QR code", "kind": "qr", "x": 250, "y": 440, "width": 108, "height": 108, "quietZone": 2,
             "background": {"type": "solid", "color": "#FFFFFF"}, "radius": 14,
             "shadow": {"x": 4, "y": 4, "color": "#111111"}},
        ]),
        "values": {}, "context": CTX,
    },
    "colors": {
        "values": {}, "kind": "ticket",
        "about": "Every colour reference: hex6, hex8 (upper-cased), var:accent with and without vars.accent, brand and ticket type.",
        "layout": page(layers=[
            shape("hex", fill={"type": "solid", "color": "#abcdef"}),
            shape("hex8", fill={"type": "solid", "color": "#abcdef80"}, y=60),
            shape("accent", fill={"type": "solid", "color": "var:accent"}, y=120),
            shape("primary", fill={"type": "solid", "color": "brand.primary"}, y=180),
            shape("secondary", fill={"type": "solid", "color": "brand.secondary"}, y=240),
            shape("type", fill={"type": "solid", "color": "ticket_type"}, y=300),
        ]),
        "context": CTX,
    },
    "preprinted": {
        "about": "Printing on pre-printed stock leaves out preprinted layers, except text that names a guest's own value.",
        "kind": "badge", "skipPreprinted": True,
        "layout": page(background={"type": "solid", "color": "#FFFFFF"}, layers=[
            shape("stock", fill=SOLID, preprinted=True),
            text("static", "DEVFEST", preprinted=True, y=80),
            text("guest", "{{first_name}}", preprinted=True, y=120),
            text("normal", "{{last_name}}", y=160),
        ]),
        "values": VALUES, "context": CTX,
    },
    "numbers": {
        "about": "How numbers are written: half up to 3 decimals, no trailing zeros, never -0.",
        "kind": "ticket",
        "layout": page(layers=[
            {**shape("n1", fill=SOLID), "x": 0.1 + 0.2, "y": -0.0004, "width": 12.3455, "height": 1e-3, "rotation": -0.00049,
             "opacity": 0.66666},
            text("n2", "x", x=1 / 3, y=2 / 3, width=100.0005, height=20.9999, size=13.3333, letterSpacing=0.0125, lineHeight=1.04),
        ]),
        "values": {}, "context": CTX,
    },
}


# ─── The normaliser ──────────────────────────────────────────────────────────

VOID = {"img", "br", "hr", "meta", "link", "input", "source", "area", "col", "embed", "track", "wbr"}


def _split_style(value: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == ";" and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    decls = []
    for d in out:
        if ":" not in d:
            continue
        prop, _, val = d.partition(":")
        prop, val = prop.strip().lower(), " ".join(val.split())
        if prop and val:
            decls.append(f"{prop}:{val}")
    return decls


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


class _Normaliser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0  # inside a QR's <svg>

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag not in VOID:
                self.skip += 1
            return
        if tag == "svg" and self.stack and self.stack[-1] == "div.moa-qr":
            self.out.append("<svg data-qr></svg>")
            self.skip = 1
            return
        parts = []
        for k, v in sorted(attrs, key=lambda kv: kv[0]):
            v = "" if v is None else v
            if k == "style":
                v = ";".join(_split_style(v))
            parts.append(f'{k}="{_esc(v)}"' if v != "" or k != "data-qr" else k)
        self.out.append(f"<{tag}{''.join(' ' + p for p in parts)}>")
        if tag not in VOID:
            cls = dict(attrs).get("class") or ""
            self.stack.append("div.moa-qr" if tag == "div" and "moa-qr" in cls.split() else tag)

    def handle_startendtag(self, tag, attrs):
        if self.skip:
            return
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= 1
            return
        if tag in VOID:
            return
        if self.stack:
            self.stack.pop()
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self.skip or not data.strip():
            return
        self.out.append(_esc(data))


def normalise(fragment: str) -> str:
    """The HTML as both builders are compared: attributes sorted by name,
    style declarations trimmed and whitespace-collapsed (in their order),
    whitespace-only text between tags dropped, text and attribute values
    decoded then escaped the same way (& < > " ' → &amp; &lt; &gt; &quot;
    &#39;), void elements without an end tag, and a QR's <svg> (inside
    div.moa-qr) as <svg data-qr></svg>."""
    p = _Normaliser()
    p.feed(fragment)
    p.close()
    return "".join(p.out)


def unescape(s: str) -> str:
    return _html.unescape(s)


__all__ = ["CASES", "normalise", "re"]
