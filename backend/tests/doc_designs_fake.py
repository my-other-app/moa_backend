"""A stand-in for app.core.doc_designs (Backend A's package) and its render
service, for the wiring's tests: the interface as A built it.

`install(monkeypatch)` puts it in place of the module's functions and returns
it. It knows a few designs by the ids the contract names; `fill` applies the
bindings and options the way A's does (auto/text/field/hidden; `field` reads
values["field.<value>"]) and raises A's MissingValue for an empty required
token; the "page" it returns is every value it printed, and `render_pdf`
makes a real PDF of that text (so pdf_text and pdf_to_png work on it).
`down = True` makes every render fail as the service being down does.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
from dataclasses import dataclass, field
from typing import Optional

TICKET_PAGE = {"width_px": 1280, "height_px": 600, "width_mm": 338.67, "height_mm": 158.75}
CERTIFICATE_PAGE = {"width_px": 1123, "height_px": 794, "width_mm": 297, "height_mm": 210}
BADGE_PAGE = {"width_px": 384, "height_px": 576, "width_mm": 101.6, "height_mm": 152.4}

TICKET_TOKENS = {"app_name": "required", "attendee_name": "required", "date": "required", "doors_time": "optional",
                 "event_name": "required", "gate": "optional", "qr_code_src": "required", "row": "optional", "seat": "optional",
                 "tagline": "optional", "ticket_id": "required", "ticket_type": "required", "venue": "required"}
CERTIFICATE_TOKENS = {"accent_color": "required", "certificate_id": "required", "course_title": "required", "grade": "optional",
                      "institute_name": "required", "issue_date": "required", "qr_code_src": "required", "recipient_name": "required",
                      "signatory_designation": "optional", "signatory_name": "optional", "verify_url": "required", "logo_src": "optional"}
THERMAL_TOKENS = {"access_level": "optional", "badge_id": "required", "checkin_time": "optional", "event_date": "required",
                  "event_name": "required", "first_name": "required", "kiosk_id": "optional", "last_name": "optional",
                  "organization": "optional", "qr_code_url": "required", "role": "required", "zone": "optional"}
STUB_TOKENS = {"access_level": "optional", "badge_id": "required", "event_date": "required", "event_name": "required",
               "event_venue": "optional", "first_name": "required", "initials": "required", "job_title": "optional",
               "last_name": "required", "organization": "optional", "photo_url": "optional", "qr_code_url": "required", "role": "required"}
IMAGES = {"qr_code_src", "qr_code_url", "logo_src", "photo_url", "sponsor_src"}
WORDINGS = {"type": "choice", "choices": ["participation", "achievement", "appreciation", "completion"], "default": "participation", "label": "Wording"}
TEXT = {"type": "text", "slots": ["title", "statement"], "max_length": 200, "label": "Your own words for a line"}


@dataclass
class Info:
    id: str
    name: str
    kind: str
    page: dict
    tokens: dict
    qr_token: str
    has_photo: bool = False
    accent: Optional[dict] = None
    options: dict = field(default_factory=dict)

    @property
    def preview(self):
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "use": "event", "has_photo": self.has_photo, "colour": "colour",
            "page": dict(self.page), "accent": self.accent, "tokens": dict(self.tokens),
            "bindable": sorted(t for t, n in self.tokens.items() if n == "optional"),
            "options": {k: dict(v) for k, v in self.options.items()},
            "preview": f"https://api.example.test/static/doc-designs/{self.id}.png?v=1",
            "version": "digest-" + self.id,
        }


def _cert(key, name, accent):
    return Info(f"certificate/{key}", name, "certificate", CERTIFICATE_PAGE, CERTIFICATE_TOKENS, "qr_code_src",
                accent={"default": accent, "suggestions": [accent], "against": ["#FFFFFF"], "min_contrast": 3.0, "note": ""},
                options={"wording": WORDINGS, "text": TEXT})


DESIGNS = {d.id: d for d in (
    Info("ticket/03-daylight-boarding-pass", "Daylight Boarding Pass", "ticket", TICKET_PAGE, TICKET_TOKENS, "qr_code_src"),
    Info("ticket/01-dusk-horizon", "Dusk Horizon", "ticket", TICKET_PAGE, TICKET_TOKENS, "qr_code_src"),
    _cert("07-swiss-minimal", "Swiss Minimal", "#D90429"),
    _cert("01-heritage", "Heritage", "#6B1F2E"),
    Info("badge/06-thermal-mono", "Thermal Mono", "badge", BADGE_PAGE, THERMAL_TOKENS, "qr_code_url",
         options={"lanyard": {"type": "bool", "default": True, "label": "Lanyard slot"}}),
    Info("badge/01-ticket-stub", "Ticket Stub", "badge", BADGE_PAGE, STUB_TOKENS, "qr_code_url", has_photo=True,
         accent={"default": "#FF8A3D", "suggestions": [], "against": ["#FFFFFF"], "min_contrast": 3.0, "note": ""},
         options={"lanyard": {"type": "bool", "default": True, "label": "Lanyard slot"}}),
)}


def _luminance(h: str) -> float:
    def c(v):
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * c(int(h[1:3], 16)) + 0.7152 * c(int(h[3:5], 16)) + 0.0722 * c(int(h[5:7], 16))


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class FakeDesigns:
    def __init__(self):
        self.down = False
        #: Every fill: (design id, the values it was given, options, bindings, accent).
        self.fills: list = []
        #: What each fill printed, token by token, after the bindings.
        self.printed: list = []
        #: Every render: the number of pages.
        self.renders: list = []
        #: Changed by a test to stand for a new template file or font lock.
        self.version = "1"

    def catalogue(self, kind):
        return [d for d in DESIGNS.values() if d.kind == kind]

    def get(self, design_id):
        from app.core.doc_designs import UnknownDesign

        if design_id not in DESIGNS:
            raise UnknownDesign(f"no such design: {design_id}")
        return DESIGNS[design_id]

    def validate(self, design_id, options=None, bindings=None, accent=None):
        d = DESIGNS.get(design_id)
        if d is None:
            return [{"code": "unknown_design", "field": "id", "message": f"no such design: {design_id}", "level": "error"}]
        out = []
        for name, value in (options or {}).items():
            spec = d.options.get(name)
            if spec is None:
                out.append({"code": "unknown_option", "field": f"options.{name}", "message": f"{d.name} has no option {name}", "level": "error"})
            elif spec["type"] == "text" and not isinstance(value, dict):
                out.append({"code": "bad_option", "field": f"options.{name}", "message": "text must map a line to its words", "level": "error"})
        for token, b in (bindings or {}).items():
            if token not in d.tokens:
                out.append({"code": "unknown_token", "field": f"bindings.{token}", "message": f"{d.name} does not print {token}", "level": "error"})
            elif b.get("source") not in ("auto", "text", "field", "hidden"):
                out.append({"code": "bad_binding", "field": f"bindings.{token}", "message": "source must be one of auto, text, field, hidden", "level": "error"})
            elif b["source"] == "hidden" and d.tokens[token] == "required":
                out.append({"code": "required", "field": f"bindings.{token}", "message": f"{d.name} must print {token}", "level": "error"})
        if accent is not None:
            if not d.accent:
                out.append({"code": "no_accent", "field": "accent", "message": f"{d.name} has no accent colour", "level": "error"})
            elif _contrast(accent, "#FFFFFF") < 3.0:
                out.append({"code": "low_contrast", "field": "accent", "message": f"{accent} is hard to read", "level": "warning"})
        return out

    def _resolved(self, d, values, bindings) -> dict:
        from app.core.doc_designs import BadValue, MissingValue

        printed = {}
        for token, need in d.tokens.items():
            b = (bindings or {}).get(token) or {}
            source = b.get("source", "auto")
            if token == d.qr_token:
                value = f"QR({values.get('qr', '')})"
            elif token == "accent_color":
                value = "accent"
            elif source == "hidden":
                value = ""
            elif source == "text":
                value = str(b.get("value") or "")
            elif source == "field":
                value = str(values.get(f"field.{b.get('value')}", "") or "")
            else:
                value = str(values.get(token, "") or "")
            if token in IMAGES and value and token != d.qr_token and not value.startswith("data:image/"):
                raise BadValue(f"{token} must be a data:image/...;base64 URI")
            if not value and need == "required":
                raise MissingValue(f"{d.id}: no value for {token}")
            printed[token] = value
        for key in ("signature_src", "second_signature_src"):
            if values.get(key):
                printed[key] = values[key]
        return printed

    def fill(self, design_id, values, options=None, bindings=None, accent=None):
        d = self.get(design_id)
        printed = self._resolved(d, values, bindings)
        self.fills.append((design_id, dict(values), dict(options or {}), dict(bindings or {}), accent))
        self.printed.append(printed)
        body = "\n".join(f"{k}={'<data>' if str(v).startswith('data:') else v}" for k, v in sorted(printed.items()))
        return f'<!doctype html><meta name="moa-design" content="{design_id}"><pre>{html.escape(design_id)}\n{html.escape(body)}</pre>'

    def preview_html(self, design_id, values, options=None, bindings=None, accent=None, *, base_url=None):
        return self.fill(design_id, values, options, bindings, accent).replace("<pre>", '<link rel="stylesheet" href="/static/doc-fonts/fonts.css"><pre>')

    async def render_pdf(self, html_or_pages, page=None, *, timeout=None):
        from app.core.doc_designs import RenderUnavailable

        if self.down:
            raise RenderUnavailable("render service unavailable after 2 tries: ConnectError")
        pages = html_or_pages if isinstance(html_or_pages, list) else [html_or_pages]
        self.renders.append(len(pages))
        return pdf_of(pages)

    def fingerprint(self, design_id, values, options=None, bindings=None, accent=None):
        blob = json.dumps([design_id, values, options, bindings, accent, self.version], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    async def render_health(self, timeout: float = 2.0):
        if self.down:
            return {"ok": False, "status": "unreachable", "chromium": None, "error": "ConnectError", "ms": 1}
        return {"ok": True, "status": "up", "chromium": "up", "ms": 1}

    # For tests.
    def last_values(self) -> dict:
        return self.fills[-1][1]


def pdf_of(pages: list[str]) -> bytes:
    """A real PDF, a page per HTML, of the design's page size, whose text is that HTML's lines."""
    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()
    c = None
    for doc in pages:
        found = re.search(r'name="moa-design" content="([^"]+)"', doc)
        page = DESIGNS[found.group(1)].page if found and found.group(1) in DESIGNS else {"width_px": 400, "height_px": 400}
        w, h = float(page["width_px"]) * 0.75, float(page["height_px"]) * 0.75
        if c is None:
            c = rl_canvas.Canvas(buf, pagesize=(w, h))
        else:
            c.setPageSize((w, h))
        c.setFont("Helvetica", 6)
        y = h - 10
        text = re.sub(r"<[^>]+>", "\n", doc)
        for line in html.unescape(text).split("\n"):
            if line.strip():
                c.drawString(4, y, line[:160])
                y -= 7
        c.showPage()
    c.save()
    return buf.getvalue()


def install(monkeypatch) -> FakeDesigns:
    from app.core import doc_designs

    fake = FakeDesigns()
    for name in ("catalogue", "get", "validate", "fill", "preview_html", "render_pdf", "fingerprint", "render_health"):
        monkeypatch.setattr(doc_designs, name, getattr(fake, name))
    return fake
