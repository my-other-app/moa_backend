"""An event's designs, for an assistant: tickets, certificates, ID cards
(badges) and "I'm attending" posters (scopes designs:read, designs:write).

Each tool does what the event's design board does, through the same code:
a layout is checked by the board's own checks (app/api/doc_presets/layouts
`checked`, and for posters guest_posters.designs `checked`, which refuses
contact details), saved the way PUT /events/{id}/designs/{kind}/layout saves
it (the event's own copy forked first, the revision one up, the designer's
design cleared) and drawn by the render service as the board's preview is,
for the board's made-up guest -- never a real one. An ID card design needs
the plan's `badges`, as on the board.

Certificates already issued keep the design they were issued with
(doc_presets/pins.py): nothing here touches them. Issuing certificates
reaches guests, so it is a request a person approves in the dashboard
(`request_issue_certificates`, actions.py).

Every change keeps the design as it was just before it, in its audit line
(`details.before`, entity `event_design`, id "<event>:<kind>:<variant>"):
`list_design_revisions` lists those, `revert_event_design` puts one back
(and keeps what it replaced, so a revert can be undone too). Only changes
made through an assistant are kept this way; the dashboard's own saves are
as they were.
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from app.api.audit.models import AdminAuditLogs
from app.api.events.models import Events
from app.api.mcp.tools import EVENT_ID, Call, ToolError, WithImage, _event, _iso, _record, from_http, obj, tool
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

DOC_KINDS = ("ticket", "certificate", "badge")
KINDS = (*DOC_KINDS, "poster")
FORMATS = ("square", "portrait", "story")
#: A ticket's, certificate's or ID card's versions: the board's audiences.
DOC_VARIANTS = {"default": "event_default", "attendee": "attendee", "speaker": "speaker", "staff": "staff"}
KIND_WORDS = {"ticket": "ticket", "certificate": "certificate", "badge": "ID card", "poster": "poster"}
#: What a design change's audit line is filed under.
ENTITY = "event_design"
CHANGES = ("mcp.design.updated", "mcp.design.starter_applied", "mcp.design.reverted")
#: A kept design larger than this (as JSON) is not kept: it could not be put back.
MAX_KEPT = 2_000_000

KIND = {"type": "string", "enum": list(KINDS),
        "description": "ticket, certificate, badge (the ID card printed at the door) or poster (the guest's \"I'm attending\" poster)."}
VARIANT = {
    "type": "string", "maxLength": 40, "default": "default",
    "description": (
        "Which version of the design. default: everyone's. Tickets, certificates and ID cards may also have their own "
        "attendee, speaker or staff version (used before the default for those people). Posters: role:speaker, "
        "role:volunteer, role:organiser, role:attendee or type:<ticket type id>."
    ),
}
FORMAT = {"type": "string", "enum": list(FORMATS), "default": "square",
          "description": "Posters only: square (1080 × 1080), portrait (1080 × 1350) or story (1080 × 1920)."}
LAYOUT = {
    "type": "object",
    "description": (
        "A whole layout v2, as get_event_design returns it: {version: 2, page: {widthMm, heightMm}, canvas: {width, "
        "height, background}, vars: {accent}, layers: [...]}. get_design_guide(kind) has every field, the tokens, the "
        "fonts, the page sizes and a minimal valid example."
    ),
}


# ─── Where a design is ───────────────────────────────────────────────────


@dataclass
class Spot:
    """One design: an event's kind, in one version (and a poster's format)."""

    event: Events
    kind: str
    variant: str
    #: Tickets, certificates, ID cards: the board's audience.
    audience: object = None
    #: Posters: the format.
    fmt: Optional[str] = None

    @property
    def entity_id(self) -> str:
        return f"{self.event.id}:{self.kind}:{self.variant}"[:64]

    @property
    def role(self) -> str:
        """The made-up guest's role, for the preview."""
        if self.kind == "poster":
            return self.variant.split(":", 1)[1] if self.variant.startswith("role:") else "attendee"
        return "attendee" if self.variant == "default" else self.variant

    def where(self) -> dict:
        from app.api.mcp.oauth import dashboard_url

        out = {"event_id": self.event.id, "event": self.event.name, "kind": self.kind, "variant": self.variant}
        if self.fmt:
            out["format"] = self.fmt
        out["dashboard_url"] = dashboard_url(f"/events/{self.event.id}/{'posters' if self.kind == 'poster' else 'design'}")
        return out


async def _spot(call: Call, args: dict) -> Spot:
    from app.api.templates_audience import TemplateAudience

    event = await _event(call, args["event_id"], load=(joinedload(Events.club), selectinload(Events.cohost_clubs)))
    kind = args["kind"]
    variant = (args.get("variant") or "default").strip()
    if kind == "poster":
        from app.api.guest_posters import designs

        try:
            variant = designs.checked_variant(variant)
        except CustomHTTPException as e:
            raise ToolError(e.message, "INVALID_VARIANT")
        return Spot(event, kind, variant, fmt=args.get("format") or "square")
    if variant not in DOC_VARIANTS:
        raise ToolError(f"A {KIND_WORDS[kind]} design's version is one of {', '.join(DOC_VARIANTS)}.", "INVALID_VARIANT")
    # A format means nothing to a ticket, certificate or ID card: it is left aside.
    return Spot(event, kind, variant, audience=TemplateAudience(DOC_VARIANTS[variant]))


async def _plan(call: Call, spot: Spot) -> None:
    """An ID card design is badge printing, which the plan must include (as on the board)."""
    if spot.kind != "badge":
        return
    from app.api.subscriptions import gates

    try:
        await gates.require_feature(call.session, call.club.id, "badges", actor=call.principal.user, event=spot.event)
    except CustomHTTPException as e:
        raise from_http(e)


def _compact(layout: Optional[dict]) -> Optional[dict]:
    """The layout without the fields that hold their defaults (lossless), to keep answers short."""
    if not isinstance(layout, dict):
        return layout
    from app.core.layout_v2.schema import compact

    try:
        return compact(layout)
    except Exception:  # noqa: BLE001 - a layout the schema no longer reads is shown as it is
        return layout


def _refused(message: str, fields: dict, problems: list) -> ToolError:
    lines = [p.get("message") for p in problems if p.get("message")] or [f"{k}: {v}" for k, v in fields.items()]
    text = message if not lines or lines[0] == message else f"{message} " + " ".join(lines[:5])
    return ToolError(text, "INVALID_LAYOUT", {"fields": fields, "problems": problems})


def _checked(spot: Spot, body) -> tuple[dict, list]:
    """The layout as the board saves it, and its warnings; INVALID_LAYOUT naming each thing to fix."""
    from app.api.doc_presets import layouts
    from app.api.event_designs import router as event_designs

    if not isinstance(body, dict):
        raise ToolError("The layout is an object: {version: 2, page, canvas, vars, layers}.", "INVALID_LAYOUT")
    prefixes = event_designs._prefixes_for(spot.event)
    if spot.kind == "poster":
        from app.api.guest_posters import designs

        try:
            return designs.checked(body, spot.fmt, prefixes=prefixes, event=spot.event)
        except CustomHTTPException as e:
            errors = e.errors if isinstance(e.errors, dict) else {}
            raise _refused(e.message, errors.get("fields") or {}, errors.get("layout") or [])
    try:
        return layouts.checked(spot.kind, body, prefixes=prefixes)
    except layouts.Refused as exc:
        raise _refused(exc.message, exc.fields, exc.problems)


# ─── Reading what is there ───────────────────────────────────────────────


def _mode(style: dict) -> str:
    """What prints: "preset" (one of the designer's designs), "layout" (a
    layout v2), "v1" (an older layered design) or "classic" (the HTML template)."""
    from app.api.doc_presets import config as presets
    from app.api.events.documents import in_layout_mode

    if presets.preset_of(style) is not None:
        return "preset"
    if presets.layout_v2_of(style) is not None:
        return "layout"
    return "v1" if in_layout_mode(style) else "classic"


async def _doc_state(call: Call, spot: Spot) -> dict:
    """A ticket's, certificate's or ID card's design as the board opens it."""
    from app.api.doc_presets import config as presets
    from app.api.doc_presets import layouts
    from app.api.event_designs import router as event_designs

    template, style = await event_designs._current(call.session, spot.event, event_designs.DesignKind(spot.kind), spot.audience)
    layout, source = layouts.editable(spot.kind, style)
    if template is None and source in ("saved", "v1", "preset"):
        source = "default"
    is_own = template is not None and template.event_id == spot.event.id and template.audience is spot.audience
    if template is None:
        uses = "MyOtherApp's default design (nothing saved for this event or the club)"
    elif template.event_id != spot.event.id:
        uses = "the club's default design (this event has none of its own yet)"
    elif is_own:
        uses = "this event's own design" if spot.variant == "default" else f"this event's own {spot.variant} version"
    else:
        uses = f"this event's design for everyone (there is no {spot.variant} version of its own)"
    return {"template": template, "style": style, "layout": layout, "source": source, "is_own": is_own, "uses": uses,
            "revision": presets.revision_of(style), "mode": _mode(style), "preset": presets.preset_of(style)}


async def _poster_state(call: Call, spot: Spot) -> dict:
    from app.api.guest_posters import designs

    rows = await designs.rows_of(call.session, spot.event.id)
    b = designs.board(spot.event, spot.variant, spot.fmt, rows)
    if b["from"] == "saved":
        uses = "this variant's own poster" if spot.variant != "default" else "this event's own poster"
    elif b["from"] == "default":
        uses = "this event's poster for everyone (this variant has none of its own in this format)"
    else:
        uses = "MyOtherApp's starter poster (nothing saved for this event)"
    return {"rows": rows, "board": b, "layout": b["layout"], "source": b["from"], "is_own": b["from"] == "saved",
            "uses": uses, "revision": b["revision"], "mode": "layout"}


async def _state(call: Call, spot: Spot) -> dict:
    return await (_poster_state(call, spot) if spot.kind == "poster" else _doc_state(call, spot))


def _notes(spot: Spot, state: dict) -> list[str]:
    out = []
    if state["mode"] == "preset":
        out.append(f"What prints now is one of the designer's designs ({(state['preset'] or {}).get('id')}); the layout here is that "
                   "design opened for editing. Saving a layout makes it this event's own design instead.")
    elif state["mode"] == "v1":
        out.append("What prints now is an older layered design; the layout here is it converted. Saving a layout replaces it.")
    elif state["mode"] == "classic":
        out.append("What prints now is the classic template, which has no layout; the layout here is the default design to start from.")
    if spot.kind == "certificate":
        out.append("Certificates already issued keep the design they were issued with: a change reaches only certificates issued after it.")
    if spot.kind == "ticket":
        out.append("A guest's ticket is drawn when they open it, so a change shows on tickets already sent the next time they are opened.")
    if spot.kind == "badge":
        out.append("An ID card is printed at the door or the kiosk: a change shows on cards printed after it.")
    if spot.kind == "poster":
        out.append("A poster never shows an email address or a phone number: such tokens are refused.")
    return out


# ─── Keeping what a change replaced ──────────────────────────────────────


async def _own_row(call: Call, spot: Spot):
    from app.api.event_designs import router as event_designs

    model = event_designs._MODELS[event_designs.DesignKind(spot.kind)]
    return await call.session.scalar(
        select(model).where(model.event_id == spot.event.id, model.audience == spot.audience, model.is_deleted == False)  # noqa: E712
        .execution_options(populate_existing=True)
    )


async def _snapshot(call: Call, spot: Spot, formats: Optional[list] = None) -> Optional[dict]:
    """The design as it is now, enough to put it back exactly: a ticket's,
    certificate's or ID card's own copy on the event (its whole stored config)
    or that it had none; a poster variant's layouts in the formats a change
    touches. None when it is too large to keep."""
    if spot.kind == "poster":
        from app.api.guest_posters import designs

        row = (await designs.rows_of(call.session, spot.event.id)).get(spot.variant)
        kept = {
            "kind": "poster", "variant": spot.variant,
            "layouts": {f: copy.deepcopy((row.layouts or {}).get(f)) if row is not None else None for f in formats or [spot.fmt]},
            "revision": int(row.revision or 0) if row is not None else 0,
        }
    else:
        from app.api.doc_presets import config as presets

        own = await _own_row(call, spot)
        kept = {
            "kind": spot.kind, "variant": spot.variant, "own": own is not None,
            "config": copy.deepcopy(own.config) if own is not None else None,
            "revision": presets.revision_of(own.config) if own is not None else 0,
            "mode": _mode(own.config or {}) if own is not None else None,
        }
    if len(json.dumps(kept, default=str)) > MAX_KEPT:
        logger.warning("the %s design of event %s is too large to keep before a change", spot.kind, spot.event.id)
        return None
    return kept


def _kept_words(spot: Spot, kept: Optional[dict]) -> str:
    """What putting a kept design back gives, in words."""
    if not isinstance(kept, dict):
        return "nothing was kept for this change, so it can't be undone"
    if kept.get("kind") == "poster":
        parts = [f"{f}: {'its own layout' if layout else 'no layout of its own'}" for f, layout in (kept.get("layouts") or {}).items()]
        return f"the poster as it was (revision {kept.get('revision', 0)}; " + ", ".join(parts) + ")"
    if not kept.get("own"):
        return "no design of its own for this version: it follows the event's, the club's or the default design again"
    mode = kept.get("mode")
    what = {"preset": "one of the designer's designs", "layout": "its own layout", "v1": "an older layered design",
            "classic": "the classic template"}.get(mode, "its own design")
    return f"this version's own design as it was: {what} (revision {kept.get('revision', 0)})"


async def _record_change(call: Call, spot: Spot, action: str, tool_name: str, *, before, summary: str, **extra) -> None:
    await _record(
        call, action, entity_type=ENTITY, entity_id=spot.entity_id, summary=summary, tool=tool_name,
        event_id=spot.event.id, kind=spot.kind, variant=spot.variant, before=before, **extra,
    )


# ─── Saving ──────────────────────────────────────────────────────────────


async def _save_doc(call: Call, spot: Spot, layout: dict) -> int:
    """The board's save: the event's own copy (forked first), the layout v2
    in it, the designer's design cleared, the revision one up."""
    from app.api.doc_presets import config as presets
    from app.api.event_designs import router as event_designs
    from app.api.templates_resolve import fork_for_event

    kind = event_designs.DesignKind(spot.kind)
    template = await fork_for_event(
        call.session, event_designs._MODELS[kind], event_id=spot.event.id, club_id=spot.event.club_id,
        audience=spot.audience, name=f"{spot.event.name} {kind.value}", default=event_designs._DEFAULTS[kind](),
    )
    template.config = presets.with_layout(template.config or {}, layout)
    await call.session.flush()
    return presets.revision_of(template.config)


async def _save_poster(call: Call, spot: Spot, found: dict, *, starter: Optional[str] = None) -> int:
    from app.api.guest_posters import designs

    row = await designs.save(call.session, spot.event, spot.variant, found, user_id=call.principal.user.id, starter=starter)
    return int(row.revision or 0)


def _saved_out(spot: Spot, revision: int, warnings: list, layout: dict, *, changed: str) -> dict:
    return {
        **spot.where(), "saved": True, "revision": revision, "changed": changed,
        "warnings": [w.get("message") for w in warnings if w.get("message")],
        "layout": _compact(layout),
        "next_step": "Check it with preview_event_design. list_design_revisions and revert_event_design undo this change.",
    }


# ─── The guide ───────────────────────────────────────────────────────────

_WHAT = {
    "ticket": "A guest's ticket: emailed when they register and scanned at the gate (its QR is their ticket code).",
    "certificate": "A certificate of participation, issued after the event; its QR opens the public check of its number.",
    "badge": "An ID card printed at the door or the kiosk; its QR is the guest's ticket code.",
    "poster": "An \"I'm attending\" poster each guest makes with their own photo and shares in public (no QR is required; "
              "its QR, if any, is the poster's share link).",
}

_LAYERS = {
    "every layer": "id (letters, digits, - or _; unique), name, x, y, width, height (canvas pixels, from the top left), "
                   "optional rotation (degrees), opacity (0-1), hidden, showIf (a token: the layer is left out when it is "
                   "empty; \"a b\" needs both, \"a|b\" either), shadow ({x, y, blur, spread, color, inset} or a list of them).",
    "text": "kind \"text\", text (the words, with tokens), font {family, weight 100-900, style normal|italic}, size (px), "
            "color, align left|center|right, valign top|middle|bottom, lineHeight (default 1.2), letterSpacing (em), "
            "transform none|upper|lower, maxLines (default 1), fit shrink|ellipsis|none (shrink: smaller until it fits), "
            "minSize, background (a fill), padding and radius ([t, r, b, l] or one number), spans (optional "
            "[{text, family?, weight?, style?, size?, color?}] for differently styled words in one paragraph).",
    "shape": "kind \"shape\", shape rect|ellipse|path, path (SVG path commands in the layer's own pixels, for path), "
             "fill (a fill), stroke {color, width, dash?: [dash, gap]}, radius.",
    "svg": "kind \"svg\", markup (one <svg> element: no scripts, event handlers or outside links), fit contain|cover|stretch, "
           "recolor {\"#RRGGBB\": colour}.",
    "image": "kind \"image\", source org_logo (the club's logo) | event_icon (the event's poster image) | guest_photo (the "
             "guest's photo; a poster's is the photo they upload) | signature_1 | signature_2 | upload (imageKey: only a "
             "picture already in this event's design can be reused; uploading is done in the dashboard), fit "
             "contain|cover, radius, border {color, width}, placeholder initials|none (a guest_photo with no photo).",
    "qr": "kind \"qr\": always black on white and square; what it encodes is the kind's (see what). Optional "
          "quietZone 0-4, background, radius. At least 10 mm on paper (12 mm or more scans best).",
}

_RULES = {
    "ticket": ["Exactly one QR code, never hidden, wholly on the page: it is what the gate scans."],
    "badge": ["Exactly one QR code, never hidden, wholly on the page: it is what the door scans.",
              "A guest_photo layer cannot be marked preprinted."],
    "certificate": ["A QR code is optional (at most one)."],
    "poster": ["The canvas is 1080 × 1080 (square), 1080 × 1350 (portrait) or 1080 × 1920 (story), on a page of the same "
               "size at 96 dpi; each format is its own layout.",
               "Never an email address or a phone number: such tokens (and the form's email and phone questions) are refused."],
}
_COMMON_RULES = [
    "The canvas's shape is the page's (within 1.5%); the canvas is 300 to 3000 pixels a side.",
    "At most 150 layers; the first is drawn at the bottom.",
    "Fonts are only the families listed here, in the weights listed.",
    "Colours: #RRGGBB, #RRGGBBAA, var:accent (vars.accent, which must then be set), brand.primary, brand.secondary or "
    "ticket_type (the guest's ticket type's colour).",
    "Fills: {type: solid, color} | {type: linear, angle, stops: [{offset 0-1, color}]} | {type: radial, cx, cy, stops}.",
    "Tokens are only the ones listed here (and field.<slug> for the event's form questions).",
]
_LANGUAGE = ("{{token}} prints a value; {{token | upper}}, lower or title change its case; "
             "{% if token %}...{% else %}...{% endif %} prints one part or the other. Anything else is printed as written.")


def _font(f: dict) -> dict:
    return {"family": f["family"], "weights": f["weights"], "italic": "italic" in (f.get("styles") or [])}


def _pages(kind: str) -> list[dict]:
    """The page sizes the starters use, each with its canvas."""
    if kind == "poster":
        from app.core.layout_v2 import poster

        return [{"format": f, "page": poster.page_mm(f), "canvas": {"width": w, "height": h}} for f, (w, h) in poster.FORMATS.items()]
    from app.core import layout_v2

    seen: dict = {}
    for sid in layout_v2.starter_ids(kind):
        try:
            s = layout_v2.starter(sid)
        except Exception:  # noqa: BLE001 - a starter that cannot be read says nothing about pages
            continue
        key = (s["page"]["widthMm"], s["page"]["heightMm"], s["canvas"]["width"], s["canvas"]["height"])
        seen.setdefault(key, []).append(sid)
    return [{"page": {"widthMm": k[0], "heightMm": k[1]}, "canvas": {"width": k[2], "height": k[3]}, "starters": len(ids), "e.g.": ids[0]}
            for k, ids in seen.items()]


def _t(id_: str, text: str, x, y, w, h, size, weight=400, family="Inter", **more) -> dict:
    return {"id": id_, "name": id_.title(), "kind": "text", "x": x, "y": y, "width": w, "height": h, "text": text,
            "font": {"family": family, "weight": weight, "style": "normal"}, "size": size, **more}


def example(kind: str) -> dict:
    """A small, valid layout of the kind: a start an assistant can grow."""
    band = {"type": "solid", "color": "var:accent"}
    if kind == "ticket":
        return {
            "version": 2, "page": {"widthMm": 203.2, "heightMm": 89.877},
            "canvas": {"width": 1560, "height": 690, "background": {"type": "solid", "color": "#FFFFFF"}},
            "vars": {"accent": "#1F3BD8"},
            "layers": [
                {"id": "band", "name": "Band", "kind": "shape", "x": 0, "y": 0, "width": 1560, "height": 60, "fill": band},
                _t("event", "{{event_name}}", 72, 110, 1000, 130, 64, 700, maxLines=2),
                _t("guest", "{{attendee_name}}", 72, 290, 1000, 80, 48, 600),
                _t("when", "{{date}} · {{start_time}} · {{venue}}", 72, 400, 1000, 50, 28, color="#444444"),
                _t("code", "{{ticket_type | upper}} · {{ticket_id}}", 72, 590, 1000, 40, 22, color="#666666"),
                {"id": "qr", "name": "QR code", "kind": "qr", "x": 1190, "y": 190, "width": 300, "height": 300},
            ],
        }
    if kind == "certificate":
        return {
            "version": 2, "page": {"widthMm": 297, "heightMm": 210},
            "canvas": {"width": 1123, "height": 794, "background": {"type": "solid", "color": "#FFFDF7"}},
            "vars": {"accent": "#6B1F2E"},
            "layers": [
                {"id": "frame", "name": "Frame", "kind": "shape", "x": 30, "y": 30, "width": 1063, "height": 734,
                 "stroke": {"color": "var:accent", "width": 4}},
                _t("title", "Certificate of Participation", 80, 110, 963, 70, 48, 400, "DM Serif Display", align="center", color="var:accent"),
                _t("lead", "This is to certify that", 80, 230, 963, 40, 22, align="center"),
                _t("name", "{{recipient_name}}", 80, 290, 963, 100, 64, 400, "DM Serif Display", align="center"),
                _t("course", "took part in {{course_title}} on {{event_date}}", 80, 420, 963, 50, 24, align="center", maxLines=2),
                _t("issuer", "{{institute_name}}", 80, 600, 600, 40, 22, 600),
                _t("number", "{{certificate_id}} · {{verify_url}}", 80, 700, 800, 30, 14, color="#666666"),
                {"id": "qr", "name": "QR code", "kind": "qr", "x": 940, "y": 600, "width": 120, "height": 120},
            ],
        }
    if kind == "badge":
        return {
            "version": 2, "page": {"widthMm": 101.6, "heightMm": 152.4},
            "canvas": {"width": 384, "height": 576, "background": {"type": "solid", "color": "#FFFFFF"}},
            "vars": {"accent": "#FF8A3D"},
            "layers": [
                {"id": "band", "name": "Band", "kind": "shape", "x": 0, "y": 0, "width": 384, "height": 70, "fill": band},
                _t("event", "{{event_name}}", 24, 18, 336, 36, 22, 700, color="#FFFFFF"),
                {"id": "photo", "name": "Photo", "kind": "image", "x": 132, "y": 96, "width": 120, "height": 120,
                 "source": "guest_photo", "fit": "cover", "placeholder": "initials", "radius": 60},
                _t("first", "{{first_name}}", 24, 236, 336, 60, 44, 700, align="center"),
                _t("last", "{{last_name}}", 24, 296, 336, 36, 24, align="center", showIf="last_name"),
                _t("role", "{{role}}", 24, 344, 336, 30, 18, 700, align="center", color="var:accent"),
                {"id": "qr", "name": "QR code", "kind": "qr", "x": 132, "y": 400, "width": 120, "height": 120},
            ],
        }
    return {
        "version": 2, "page": {"widthMm": 285.75, "heightMm": 285.75},
        "canvas": {"width": 1080, "height": 1080, "background": {"type": "solid", "color": "#101828"}},
        "vars": {"accent": "#FFD23F"},
        "layers": [
            {"id": "photo", "name": "Photo", "kind": "image", "x": 340, "y": 120, "width": 400, "height": 400,
             "source": "guest_photo", "fit": "cover", "placeholder": "initials", "radius": 200},
            _t("phrase", "{{moment_phrase}}", 60, 570, 960, 90, 72, 700, align="center", color="var:accent"),
            _t("name", "{{name}}", 60, 680, 960, 70, 48, 600, align="center", color="#FFFFFF"),
            _t("event", "{{event_name}}", 60, 790, 960, 60, 40, 700, align="center", color="#FFFFFF"),
            _t("when", "{{event_date}} · {{venue}}", 60, 870, 960, 44, 28, align="center", color="#D0D5DD"),
        ],
    }


def _tokens(kind: str, event) -> list[dict]:
    from app.api.doc_presets import layouts

    found = layouts.tokens(kind, event)
    if kind == "poster":
        from app.core.layout_v2 import poster

        slugs = poster.contact_field_slugs(getattr(event, "additional_details", None))
        found = [t for t in found if not poster.is_contact_token(t["key"], slugs)]
    return [{"key": t["key"], "group": t["group"], "label": t["label"], "sample": t["sample"]} for t in found]


# ─── Reads ───────────────────────────────────────────────────────────────


@tool(
    "list_design_starters",
    title="List design starters",
    scope="designs:read",
    description=(
        "The ready-made designs a ticket, certificate, ID card or poster can start from: each starter's id, name, what it "
        "suits, whether it shows the guest's photo, its paper and accent colour. Put one on an event with "
        "apply_design_starter, then change it with update_event_design."
    ),
    schema=obj({"kind": KIND}, ("kind",)),
)
async def list_design_starters(call: Call, args: dict) -> dict:
    kind = args["kind"]
    if kind == "poster":
        from app.core.layout_v2 import poster

        found = poster.starters()
        starters = [{"id": s["id"], "name": s["name"], "use": s.get("use"), "has_photo": bool(s.get("hasPhoto")),
                     "formats": s.get("formats") or list(FORMATS)} for s in found]
    else:
        from app.api.doc_presets import layouts

        starters = [
            {"id": s["id"], "name": s["name"], "use": s.get("use"), "has_photo": bool(s.get("hasPhoto")), "page": s.get("page"),
             "accent": (s.get("accent") or {}).get("default") if isinstance(s.get("accent"), dict) else None,
             "preview_url": s.get("preview")}
            for s in layouts.starters(kind)
        ]
    return {"kind": kind, "starters": starters}


@tool(
    "get_design_guide",
    title="How to write a design",
    scope="designs:read",
    description=(
        "Everything needed to write a ticket, certificate, ID card or poster design from a description: the layout v2 "
        "shape and every layer's fields, the rules a design must keep (the QR, the page's shape, fonts, contact details "
        "on posters), the page sizes, the fonts and their weights, the tokens a text can print (with the event's own "
        "form questions when event_id is given) and a minimal valid example to start from. Read it before "
        "update_event_design."
    ),
    schema=obj({"kind": KIND, "event_id": {**EVENT_ID, "description": "Optional: adds this event's form questions to the tokens."}}, ("kind",)),
)
async def get_design_guide(call: Call, args: dict) -> dict:
    from app.api.doc_presets import layouts

    kind = args["kind"]
    event = await _event(call, args["event_id"], load=(joinedload(Events.club),)) if args.get("event_id") else None
    return {
        "kind": kind,
        "what": _WHAT[kind],
        "shape": ("{version: 2, page: {widthMm, heightMm}, canvas: {width, height, background: fill or null}, "
                  "vars: {accent: \"#RRGGBB\"}, layers: [layer, ...]}. Layers are absolutely placed on the canvas, in "
                  "canvas pixels; the first layer is at the bottom."),
        "layers": _LAYERS,
        "text_language": _LANGUAGE,
        "rules": _RULES[kind] + _COMMON_RULES,
        "pages": _pages(kind),
        "fonts": [_font(f) for f in layouts.fonts()],
        "tokens": _tokens(kind, event),
        "example": example(kind),
        "steps": [
            "get_event_design shows the design in use (its layout is a good start too).",
            "Write the whole layout, then update_event_design: it is checked as the dashboard checks it, and each problem is named with its field.",
            "preview_event_design draws it for a made-up guest; look at the picture and adjust.",
            "list_design_revisions and revert_event_design undo a change.",
        ],
    }


@tool(
    "get_event_design",
    title="Get an event's design",
    scope="designs:read",
    description=(
        "The ticket, certificate, ID card or poster design an event uses now, as a layout v2 to edit: whose design it "
        "is (the event's own, the club's or the default), its revision, the layout itself, and notes (a certificate "
        "already issued keeps its design). For a version (speaker, staff, a poster's role or ticket type) or a poster's "
        "format, pass variant and format."
    ),
    schema=obj({"event_id": EVENT_ID, "kind": KIND, "variant": VARIANT, "format": FORMAT}, ("event_id", "kind")),
)
async def get_event_design(call: Call, args: dict) -> dict:
    from app.core import layout_v2

    spot = await _spot(call, args)
    state = await _state(call, spot)
    layout = state["layout"]
    problems = []
    if isinstance(layout, dict):
        problems = [p.get("message") for p in layout_v2.problems(layout, spot.kind) if p.get("message")]
    return {
        **spot.where(), "uses": state["uses"], "is_own": state["is_own"], "from": state["source"], "revision": state["revision"],
        "layout": _compact(layout), "problems": problems, "notes": _notes(spot, state),
        "next_step": "Change it with update_event_design (the whole layout) or apply_design_starter; check with preview_event_design.",
    }


@tool(
    "preview_event_design",
    title="Preview a design",
    scope="designs:read",
    description=(
        "A picture (PNG) of a ticket, certificate, ID card or poster design, drawn exactly as guests get it but for a "
        "made-up guest (Aisha Rahman) -- never a real one. Without `layout`: the design in use now. With `layout`: that "
        "layout, unsaved, checked as a save would be. Look at it before and after a change."
    ),
    schema=obj({"event_id": EVENT_ID, "kind": KIND, "variant": VARIANT, "format": FORMAT, "layout": LAYOUT}, ("event_id", "kind")),
)
async def preview_event_design(call: Call, args: dict) -> WithImage:
    spot = await _spot(call, args)
    unsaved = args.get("layout")
    if unsaved is not None:
        layout, warnings = _checked(spot, unsaved)
    else:
        layout, warnings = None, []
    png = await (_preview_poster(call, spot, layout) if spot.kind == "poster" else _preview_doc(call, spot, layout))
    png, width, height = await asyncio.to_thread(_shrink, png, settings.MCP_PREVIEW_MAX_PX)
    return WithImage(
        data={
            **spot.where(), "drawn": "the unsaved layout given" if unsaved is not None else "the design in use now",
            "guest": "a made-up guest (Aisha Rahman); a real guest's name, photo and answers are never used here",
            "image": {"width": width, "height": height, "type": "image/png"},
            "warnings": [w.get("message") for w in warnings if w.get("message")],
        },
        png=png,
    )


def _shrink(png: bytes, longest: int) -> tuple[bytes, int, int]:
    """The picture with its longest side at most `longest` pixels."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        im.load()
        if max(im.size) > longest > 0:
            im.thumbnail((longest, longest), Image.LANCZOS)
            out = io.BytesIO()
            im.save(out, format="PNG", optimize=True)
            png = out.getvalue()
        return png, im.size[0], im.size[1]


def _unavailable(kind: str, exc) -> ToolError:
    from app.api.doc_presets import engine as preset_engine

    preset_engine.unavailable(kind, exc)
    return ToolError(preset_engine.PREVIEW_UNAVAILABLE, "RENDER_UNAVAILABLE")


async def _preview_doc(call: Call, spot: Spot, layout: Optional[dict]) -> bytes:
    """The board's preview, for its made-up guest: the layout v2 given or in use,
    one of the designer's designs, or an older layered design, each as it prints."""
    from app.api.doc_presets import engine as preset_engine
    from app.api.doc_presets import layouts
    from app.api.doc_presets import values as preset_values
    from app.api.event_designs import router as event_designs

    kind = event_designs.DesignKind(spot.kind)
    state = await _doc_state(call, spot)
    style = state["style"]
    try:
        if layout is None and state["mode"] == "preset":
            response = await event_designs.preset_preview(kind, spot.event, style, state["preset"], None, spot.audience, "png")
            return bytes(response.body)
        if layout is None and state["mode"] == "v1":
            return await _preview_v1(spot, style)
        if layout is None and state["mode"] == "classic":
            raise ToolError(
                f"This {KIND_WORDS[spot.kind]} prints from the classic template, which can't be pictured here. Apply a starter "
                "(apply_design_starter) or pass a layout to preview it.", "NO_PICTURE",
            )
        drawn = layout if layout is not None else state["layout"]
        if drawn is None:
            raise ToolError("This design can't be opened as a layout; preview it in the dashboard.", "NO_PICTURE")
        subject = preset_values.sample_subject(spot.event, spot.kind, spot.role)
        _, png = await layouts.preview(spot.kind, spot.event, style, drawn, subject, "png")
        return png
    except preset_engine.Unavailable as exc:
        raise _unavailable(spot.kind, exc)
    except CustomHTTPException as e:
        raise from_http(e)
    except ValueError as exc:
        raise ToolError(f"This design cannot be drawn: {exc}", "DESIGN_INCOMPLETE")


async def _preview_v1(spot: Spot, style: dict) -> bytes:
    """An older layered design, as the board previews one: its own renderer, the sample guest."""
    from app.api.event_designs import router as event_designs
    from app.api.events.documents import layout_sample_values
    from app.core.layout import pool

    values = layout_sample_values(spot.event, spot.kind)
    code = values["ticket_code"]
    qr_text = f"certificate:{code}" if spot.kind == "certificate" else code
    club = getattr(spot.event, "club", None)
    return await pool.run(event_designs._preview_png, style, values, qr_text, f"{spot.kind} preview", None, club)


async def _preview_poster(call: Call, spot: Spot, layout: Optional[dict]) -> bytes:
    """The poster board's preview: the sample guest's words, the initials for a photo."""
    from app.api.doc_presets import engine as preset_engine
    from app.api.guest_posters import designs, render
    from app.api.guest_posters import values as poster_values

    if layout is not None:
        resolved = designs.Resolved(layout, spot.variant, 0)
    else:
        b = (await _poster_state(call, spot))["board"]
        resolved = designs.Resolved(b["layout"], spot.variant, b["revision"])
    if resolved.layout is None:
        raise ToolError("This poster has no design yet: apply a starter first.", "NO_DESIGN")
    values = poster_values.sample_values(spot.event, role=spot.role)
    p = render.prepare(spot.event, None, resolved, spot.fmt, values)
    try:
        return await render.draw(p, None, spot.event)
    except preset_engine.Unavailable as exc:
        raise _unavailable("poster", exc)
    except ValueError as exc:
        raise ToolError(f"This poster cannot be drawn: {exc}", "DESIGN_INCOMPLETE")


@tool(
    "list_design_revisions",
    title="List design changes",
    scope="designs:read",
    description=(
        "The changes an assistant made to one of an event's designs, newest first: when, by which app and person, and "
        "what putting each one back gives (the design as it was just before that change). Pass a revision_id to "
        "revert_event_design to undo back to it. Changes made in the dashboard itself are not listed."
    ),
    schema=obj({"event_id": EVENT_ID, "kind": KIND, "variant": VARIANT,
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20, "description": "How many (at most 50)."}},
               ("event_id", "kind")),
)
async def list_design_revisions(call: Call, args: dict) -> dict:
    spot = await _spot(call, args)
    state = await _state(call, spot)
    # A poster's changes are its variant's, whichever formats each touched.
    spot.fmt = None
    rows = list(await call.session.scalars(
        select(AdminAuditLogs).where(AdminAuditLogs.entity_type == ENTITY, AdminAuditLogs.entity_id == spot.entity_id,
                                     AdminAuditLogs.action.in_(CHANGES))
        .order_by(AdminAuditLogs.created_at.desc(), AdminAuditLogs.id.desc()).limit(args.get("limit", 20))
    ))
    out = []
    for r in rows:
        d = r.details or {}
        before = d.get("before")
        out.append({
            "revision_id": r.id, "changed_at": _iso(r.created_at), "change": d.get("tool"), "summary": r.summary,
            "by": {"app": d.get("client"), "person": d.get("member_email") or "the club's own account"},
            "revision_after": d.get("revision"),
            "formats": sorted((before or {}).get("layouts") or {}) if spot.kind == "poster" else None,
            "revert_gives": _kept_words(spot, before), "can_revert": isinstance(before, dict),
        })
    return {**spot.where(), "current_revision": state["revision"], "uses": state["uses"], "revisions": out}


# ─── Writes ──────────────────────────────────────────────────────────────


@tool(
    "update_event_design",
    title="Change an event's design",
    scope="designs:write",
    description=(
        "Save a whole layout v2 as the event's ticket, certificate, ID card or poster design (the given version and, "
        "for a poster, format). It is checked exactly as the dashboard checks a save -- the QR a ticket or ID card "
        "needs, known fonts and tokens, the page's shape, no contact details on a poster -- and refused with each "
        "problem and its field when it is not right. Saved, it is the design guests get from now on; certificates "
        "already issued keep theirs. Returns the new revision and any warnings. Undo with revert_event_design."
    ),
    schema=obj({"event_id": EVENT_ID, "kind": KIND, "layout": LAYOUT, "variant": VARIANT, "format": FORMAT}, ("event_id", "kind", "layout")),
    read_only=False,
)
async def update_event_design(call: Call, args: dict) -> dict:
    spot = await _spot(call, args)
    await _plan(call, spot)
    layout, warnings = _checked(spot, args["layout"])
    before = await _snapshot(call, spot)
    revision = await (_save_poster(call, spot, {spot.fmt: layout}) if spot.kind == "poster" else _save_doc(call, spot, layout))
    await _record_change(
        call, spot, "mcp.design.updated", "update_event_design", before=before, revision=revision,
        summary=f"{KIND_WORDS[spot.kind].capitalize()} design of “{spot.event.name}” changed by {call.principal.client_name}",
    )
    await call.session.commit()
    return _saved_out(spot, revision, warnings, layout, changed="the layout")


@tool(
    "apply_design_starter",
    title="Use a starter design",
    scope="designs:write",
    description=(
        "Put one of the ready-made starters (list_design_starters) on an event as its ticket, certificate, ID card or "
        "poster design, optionally in your own accent colour. A poster starter sets all three formats of the variant. "
        "Saved at once, as update_event_design is; undo with revert_event_design."
    ),
    schema=obj({
        "event_id": EVENT_ID, "kind": KIND,
        "starter_id": {"type": "string", "minLength": 1, "maxLength": 80, "description": "From list_design_starters, e.g. certificate/01-heritage."},
        "variant": VARIANT,
        "accent": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$", "description": "Optional accent colour, #RRGGBB."},
    }, ("event_id", "kind", "starter_id")),
    read_only=False,
)
async def apply_design_starter(call: Call, args: dict) -> dict:
    spot = await _spot(call, {**args, "format": "square"} if args["kind"] == "poster" else args)
    await _plan(call, spot)
    starter_id, accent = args["starter_id"].strip(), args.get("accent")

    def accented(layout: dict) -> dict:
        if accent:
            layout = {**layout, "vars": {**(layout.get("vars") or {}), "accent": accent.upper()}}
        return layout

    if spot.kind == "poster":
        from app.core.layout_v2 import poster

        if starter_id not in poster.starter_ids():
            raise ToolError(f"There is no poster starter {starter_id!r}. They are: {', '.join(poster.starter_ids())}.", "STARTER_NOT_FOUND")
        found, warnings = {}, []
        for fmt, layout in poster.starter_formats(starter_id).items():
            if fmt in FORMATS and isinstance(layout, dict):
                spot.fmt = fmt
                found[fmt], w = _checked(spot, accented(layout))
                warnings += w
        spot.fmt = None
        before = await _snapshot(call, spot, formats=list(found))
        revision = await _save_poster(call, spot, found, starter=starter_id)
        shown = found.get("square") or next(iter(found.values()), None)
    else:
        from app.api.doc_presets import layouts

        copied = layouts.starter_copy(spot.kind, starter_id)
        if copied is None:
            ids = [s["id"] for s in layouts.starters(spot.kind)]
            raise ToolError(f"There is no {KIND_WORDS[spot.kind]} starter {starter_id!r}. They are: {', '.join(ids)}.", "STARTER_NOT_FOUND")
        shown, warnings = _checked(spot, accented(copied["layout"]))
        before = await _snapshot(call, spot)
        revision = await _save_doc(call, spot, shown)
    await _record_change(
        call, spot, "mcp.design.starter_applied", "apply_design_starter", before=before, revision=revision, starter_id=starter_id,
        summary=f"{KIND_WORDS[spot.kind].capitalize()} design of “{spot.event.name}” set to {starter_id} by {call.principal.client_name}",
    )
    await call.session.commit()
    out = _saved_out(spot, revision, warnings, shown, changed=f"the starter {starter_id}")
    if spot.kind == "poster":
        out["formats"] = sorted(found)
        out["layout_note"] = "The square format's layout; get_event_design with format gives the others."
    return out


async def _restore(call: Call, spot: Spot, kept: dict) -> int:
    """Put a kept design back, as it was; the revision moves on, never back."""
    if spot.kind == "poster":
        from app.api.guest_posters import designs

        layouts_kept = kept.get("layouts") or {}
        back = {f: layout for f, layout in layouts_kept.items() if f in FORMATS and isinstance(layout, dict)}
        gone = [f for f, layout in layouts_kept.items() if f in FORMATS and not isinstance(layout, dict)]
        if back:
            await designs.save(call.session, spot.event, spot.variant, back, user_id=call.principal.user.id)
        for f in gone:
            await designs.drop(call.session, spot.event, spot.variant, f, user_id=call.principal.user.id)
        row = (await designs.rows_of(call.session, spot.event.id)).get(spot.variant)
        return int(row.revision or 0) if row is not None else 0
    from app.api.doc_presets import config as presets
    from app.api.event_designs import router as event_designs
    from app.api.events.tickets import design_version_of
    from app.api.templates_resolve import fork_for_event

    own = await _own_row(call, spot)
    if not kept.get("own"):
        if own is not None:
            own.is_deleted = True
            await call.session.flush()
        return (await _doc_state(call, spot))["revision"]
    config = copy.deepcopy(kept.get("config") or {})
    at = design_version_of(own.config or {}) if own is not None else 0
    if presets.layout_v2_of(config) is not None:
        config[presets.REVISION] = max(at, presets.revision_of(config), presets.revision_of(own.config if own is not None else {})) + 1
    elif _mode(config) == "v1":
        config["layout"] = {**config["layout"], "version": max(at, int(config["layout"].get("version") or 0)) + 1}
    if own is None:
        kind = event_designs.DesignKind(spot.kind)
        own = await fork_for_event(
            call.session, event_designs._MODELS[kind], event_id=spot.event.id, club_id=spot.event.club_id,
            audience=spot.audience, name=f"{spot.event.name} {kind.value}", default=event_designs._DEFAULTS[kind](),
        )
    own.config = config
    await call.session.flush()
    return presets.revision_of(config)


@tool(
    "revert_event_design",
    title="Undo a design change",
    scope="designs:write",
    description=(
        "Put one of an event's designs back as it was just before a change an assistant made (revision_id from "
        "list_design_revisions): the layout, colours and pictures exactly, or -- when the event had no design of its "
        "own then -- following the club's or the default design again. What it replaces is kept, so this can be "
        "undone too. Certificates already issued are not touched."
    ),
    schema=obj({
        "event_id": EVENT_ID, "kind": KIND, "variant": VARIANT,
        "revision_id": {"type": "integer", "minimum": 1, "description": "From list_design_revisions."},
    }, ("event_id", "kind", "revision_id")),
    read_only=False,
    idempotent=False,
)
async def revert_event_design(call: Call, args: dict) -> dict:
    spot = await _spot(call, args)
    await _plan(call, spot)
    row = await call.session.scalar(
        select(AdminAuditLogs).where(AdminAuditLogs.id == args["revision_id"], AdminAuditLogs.entity_type == ENTITY,
                                     AdminAuditLogs.entity_id == spot.entity_id, AdminAuditLogs.action.in_(CHANGES))
    )
    if row is None:
        raise ToolError("There is no such change to this design (list_design_revisions lists them).", "REVISION_NOT_FOUND")
    kept = (row.details or {}).get("before")
    if not isinstance(kept, dict) or kept.get("kind") != spot.kind:
        raise ToolError("That change kept no copy of the design before it, so it can't be undone here.", "REVISION_NOT_KEPT")
    formats = sorted(kept.get("layouts") or {}) if spot.kind == "poster" else None
    before = await _snapshot(call, spot, formats=formats)
    revision = await _restore(call, spot, kept)
    await _record_change(
        call, spot, "mcp.design.reverted", "revert_event_design", before=before, revision=revision, reverted_to=row.id,
        summary=f"{KIND_WORDS[spot.kind].capitalize()} design of “{spot.event.name}” put back as before change {row.id} by {call.principal.client_name}",
    )
    await call.session.commit()
    state = await _state(call, spot)
    spot.fmt = None
    return {
        **spot.where(), "reverted_to": row.id, "gives": _kept_words(spot, kept), "revision": state["revision"], "uses": state["uses"],
        **({"formats": formats} if formats else {}),
        "next_step": "Check it with preview_event_design. This revert is itself listed by list_design_revisions, so it can be undone.",
    }


# ─── Issuing certificates: a person approves ─────────────────────────────


def _guest_requests_on() -> bool:
    return bool(settings.MCP_GUEST_REQUESTS_ENABLED)


@tool(
    "request_issue_certificates",
    title="Ask to issue certificates",
    scope="designs:write",
    description=(
        "Ask a person to issue an event's certificates. Nothing is issued now: this returns a confirm_url for someone "
        "on the club's team to open in the dashboard and approve within 24 hours. audience: checked_in (the usual), "
        "everyone registered, or roles (with roles: attendee, speaker, staff). Nobody is issued twice, and certificates "
        "already issued keep their design."
    ),
    schema=obj({
        "event_id": EVENT_ID,
        "audience": {"type": "string", "enum": ["checked_in", "everyone", "roles"], "default": "checked_in"},
        "roles": {"type": "array", "items": {"type": "string", "enum": ["attendee", "speaker", "staff"]}, "maxItems": 3, "uniqueItems": True},
    }, ("event_id",)),
    read_only=False,
    idempotent=False,
    available=_guest_requests_on,
)
async def request_issue_certificates(call: Call, args: dict) -> dict:
    from app.api.certificates import service as certificates
    from app.api.mcp import actions

    event = await _event(call, args["event_id"])
    audience, roles = args.get("audience", "checked_in"), list(args.get("roles") or [])
    if audience == "roles" and not roles:
        raise ToolError("Give the roles to issue certificates to: attendee, speaker or staff.", "INVALID_ARGUMENT")
    if audience != "roles":
        roles = []
    try:
        counts = await certificates.issue_preview(call.session, event, audience, roles)
    except CustomHTTPException as e:
        raise from_http(e)
    if not counts["to_issue"]:
        raise ToolError(
            f"Nobody in that audience is without a certificate ({counts['eligible']} in it, {counts['already_issued']} already issued).",
            "NOTHING_TO_ISSUE", counts,
        )
    row, token = await actions.request(
        call.session, call.principal, kind="issue_certificates", target_type="event", target_id=event.id,
        payload={"event_id": event.id, "name": event.name, "audience": audience, "roles": roles, "to_issue": counts["to_issue"]},
        request=call.request,
    )
    await call.session.commit()
    return {**actions.requested_out(row, token), "would_issue": counts["to_issue"], "already_issued": counts["already_issued"]}
