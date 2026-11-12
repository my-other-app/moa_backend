"""Documents drawn from an editable (v2) layout, and what the editor reads.

feature-register/layout-v2-contract.md ("Q") is the contract. P's
app.core.layout_v2 holds the schema, the checks, the HTML builder, the
starters and the conversions; this module is how the rest of the backend
uses them -- the one door, as engine.py is to app.core.doc_designs:

- `checked(kind, body, prefixes)`: a layout as it is saved, or the 422.
- `editable(kind, style)`: the layout the editor opens, converted from a
  saved v1 canvas or preset when that is what prints (V4).
- `job(kind, registration, style, ...)`: a ticket or certificate as a
  RenderJob, kept in doc_cache under its fingerprint (D8), drawn by the
  render service, the classic renderer when it cannot (D9).
- `card_page` / `pages_pdf`: ID cards, a batch in as few calls as it can.
- `preview(...)`: the editor's iframe HTML or exact PNG, one render.

What a document is filled with is values.document_values (V5). Pictures go
in as data: URIs (`context`), only the ones the layout's visible layers
show: a ticket with no photo layer never reads a photo.

The builder's `context`: {"kind", "qr" (what the QR encodes: the ticket
code, or a certificate's verify page), "colors" {primary, secondary,
ticketType} for brand references, "images" {source -> data: URI or "",
"upload:<key>" -> data: URI or ""}, "initials" (a photo frame's tile)}.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from typing import Optional

from app.api.doc_presets import engine
from app.api.doc_presets.config import (
    DEFAULT_IDS,
    classic,
    layout_v1_of,
    layout_v2_of,
    preset_of,
    revision_of,
)
from app.api.doc_presets.values import Mapped, document_values, initials_of, subject_of

logger = logging.getLogger(__name__)

#: A document is rasterised at twice its CSS pixels (D2), as a preset's.
PNG_SCALE = engine.PNG_SCALE
#: The pictures a layer can name besides an upload (contract, image `source`).
SOURCES = ("org_logo", "event_icon", "guest_photo", "signature_1", "signature_2", "sponsor")


class Refused(Exception):
    """A layout that cannot be saved or previewed: `fields` by path, `problems` as P's checks gave them."""

    def __init__(self, message: str, *, fields: dict, problems: Optional[list] = None):
        super().__init__(message)
        self.message = message
        self.fields = fields
        self.problems = problems or []


def _lib():
    from app.core import layout_v2

    return layout_v2


def wire(parsed) -> dict:
    """A parsed layout as it is stored and sent: P's wire form, camelCase."""
    return _lib().dump(parsed)


# ─── Checking ───────────────────────────────────────────────────────────────


def upload_keys(layout: dict) -> dict:
    """Every bucket key the layout's image layers name, by the field that holds it."""
    out = {}
    for i, layer in enumerate(layout.get("layers") or []):
        if isinstance(layer, dict) and layer.get("kind") == "image" and layer.get("imageKey"):
            out[f"layers.{i}.imageKey"] = layer["imageKey"]
    return out


#: What an svg layer may not have held, even though the sanitiser would take
#: it out: a script, an event handler, an outside link or a frame. Refused
#: on save, so a club sees it; anything else it removes (an editor's own
#: attributes) is a warning and the cleaned markup is what is stored.
_UNSAFE_SVG = re.compile(r"^<(script|foreignobject|iframe|object|embed)>|^on[a-z]+ on <|href=|an outside url", re.I)
_REMOVED_ATTR = re.compile(r"^([A-Za-z:_-]+) on <")


def _dangerous(removed: str) -> bool:
    """Whether what the sanitiser removed was a danger rather than noise: a
    kept attribute removed for its value (javascript:, expression()) is."""
    if _UNSAFE_SVG.search(removed):
        return True
    from app.core.layout_v2.sanitize import ATTRS

    m = _REMOVED_ATTR.match(removed)
    return bool(m) and m.group(1).lower() in ATTRS


def _refusal(found: list[dict], fallback: str) -> Refused:
    errors = [p for p in found if p.get("level", "error") == "error"]
    return Refused(
        errors[0].get("message") or fallback if errors else fallback,
        fields={(p.get("field") or "layout"): p.get("message") for p in errors}, problems=errors,
    )


def _unsafe_svg(layout: dict) -> list[dict]:
    from app.core.layout_v2.sanitize import UnsafeSvg, problems_of

    out = []
    for i, layer in enumerate(layout.get("layers") or []):
        if layer.get("kind") != "svg":
            continue
        try:
            removed = problems_of(layer.get("markup") or "")
        except UnsafeSvg as exc:
            removed = [str(exc)]
        bad = [r for r in removed if _dangerous(r)]
        if bad:
            out.append({
                "code": "svg_unsafe", "field": f"layers.{i}.markup", "level": "error", "layer": layer.get("id"),
                "message": f"{layer.get('name') or 'A picture'}: it holds what a design may not ({', '.join(bad[:3])}). Export it again without scripts or links.",
            })
    return out


def _cleaned(layout: dict) -> dict:
    """The layout with each svg layer's markup as the sanitiser keeps it, so
    what is stored is what draws (and the editor's builder reads it as is)."""
    from app.core.layout_v2.sanitize import sanitize

    layers = []
    for layer in layout.get("layers") or []:
        if layer.get("kind") == "svg" and layer.get("markup"):
            layer = {**layer, "markup": sanitize(layer["markup"])}
        layers.append(layer)
    return {**layout, "layers": layers}


def checked(kind: str, body: dict, *, prefixes: tuple) -> tuple[dict, list]:
    """The layout as it is saved, once P's checks pass (Refused when not),
    and its warnings. Its images must be uploads to this design's folders:
    a key elsewhere would print another club's file. A picture that held a
    script, a handler or an outside link is refused; the stored markup is
    the sanitised one."""
    lib = _lib()
    try:
        parsed = lib.parse(body)
    except Exception as exc:  # noqa: BLE001 - pydantic's: the shape is wrong; P's checks say where
        found = lib.problems(body, kind) or [{"code": "shape", "field": "layout", "message": str(exc)[:300], "level": "error"}]
        raise _refusal(found, "This design has a problem.") from exc
    layout = {**wire(parsed), "version": 2}
    foreign = {f: "Upload the image through this designer." for f, key in upload_keys(layout).items() if not any(str(key).startswith(p) for p in prefixes)}
    if foreign:
        raise Refused("Some images in this design were not uploaded here.", fields=foreign)
    found = _unsafe_svg(layout) + list(lib.problems(parsed, kind) or [])
    if any(p.get("level", "error") == "error" for p in found):
        raise _refusal(found, "Some parts of this design need attention before it can be used.")
    return _cleaned(layout), [p for p in found if p.get("level") == "warning"]


# ─── What the editor opens ──────────────────────────────────────────────────


def editable(kind: str, style: dict) -> tuple[Optional[dict], str]:
    """The v2 layout the editor opens on, and where it came from: "preset"
    (the chosen designer's design is what prints: its starter with the
    preset's accent and wording, V4), "saved" (a v2 layout), "v1" (a v1
    canvas, converted mechanically) or "default" (nothing of the event's or
    club's: the default design's starter). Pure Python; nothing is drawn.

    The layout is None when what prints could not be opened as one (a
    conversion failed, or a build without starters): `from` still says what
    prints, and the editor must not offer to save over it unseen."""
    lib = _lib()
    preset = preset_of(style)
    if preset is not None:
        try:
            return lib.from_preset(preset), "preset"
        except Exception:  # noqa: BLE001 - what prints is the preset still; the editor cannot open it
            logger.warning("the preset %s could not be opened as a layout", preset.get("id"), exc_info=True)
            return None, "preset"
    saved = layout_v2_of(style)
    if saved is not None:
        return copy.deepcopy(saved), "saved"
    v1 = layout_v1_of(style)
    if v1 is not None and kind in DEFAULT_IDS:
        try:
            return lib.from_v1(v1), "v1"
        except Exception:  # noqa: BLE001
            logger.warning("a v1 %s layout could not be converted", kind, exc_info=True)
            return None, "v1"
    from app.api.doc_presets.config import default_layout

    layout = default_layout(kind)
    if layout is None:
        try:
            layout = lib.starter(f"{kind}/card_cr80" if kind == "badge" else f"{kind}/blank")
        except Exception:  # noqa: BLE001 - a build without starters: the editor starts from nothing
            return None, "none"
    return layout, "default"


def starters(kind: str) -> list[dict]:
    """The gallery, in the designer's order then the CR80 card and the
    blank: P's [{id, kind, name, use, hasPhoto, colour, accent, preview}],
    never the layouts -- that is POST .../layout/starters/{id} (copy-on-use)."""
    try:
        return list(_lib().starters(kind, with_layout=False))
    except Exception:  # noqa: BLE001 - a broken starter must not close the editor
        logger.exception("the %s starters could not be read", kind)
        return []


def starter_copy(kind: str, starter_id: str) -> Optional[dict]:
    """A starter of this kind, {id, name, layout} with a copy of its own; None when there is none."""
    lib = _lib()
    try:
        if starter_id not in lib.starter_ids(kind):
            return None
        meta = next((s for s in lib.starters(kind, with_layout=False) if s.get("id") == starter_id), {})
        return {"id": starter_id, "name": meta.get("name") or starter_id, "layout": lib.starter(starter_id)}
    except LookupError:
        return None


def fonts() -> list[dict]:
    """Every family a text layer may use: [{family, weights, styles, faces}]."""
    try:
        return list(_lib().fonts_available())
    except Exception:  # noqa: BLE001
        logger.exception("the font list could not be read")
        return []


def tokens(kind: str, event) -> list[dict]:
    """What a text layer can say, [{key, group, label, sample, note}]: the
    canvas's tokens, the designer's for the kind over them where a name is
    both (that meaning is what the values give: values.document_values), and
    the form's questions as field.<slug>."""
    from app.core.layout.tokens import TICKET_TOKENS, field_token

    out: dict = {}

    def add(t: dict) -> None:
        key = t.get("key")
        if key:
            out[key] = {"key": key, "group": t.get("group") or "", "label": t.get("label") or key, "sample": t.get("sample") or "", "note": t.get("note")}

    for t in TICKET_TOKENS:
        add(t)
    try:
        for t in _lib().tokens(kind) or []:
            add(t)
    except Exception:  # noqa: BLE001
        logger.exception("the %s tokens could not be read", kind)
    for f in getattr(event, "additional_details", None) or []:
        if isinstance(f, dict) and (f.get("label") or f.get("key")):
            label = f.get("label") or f.get("key")
            add({"key": field_token(str(f.get("key") or label)), "group": "Guest fields", "label": label, "sample": f"‹{label}›"})
    return list(out.values())


# ─── Pictures ───────────────────────────────────────────────────────────────


def _visible(layout: dict) -> list[dict]:
    return [layer for layer in layout.get("layers") or [] if isinstance(layer, dict) and not layer.get("hidden")]


def picture_refs(layout: dict, style: dict, event, mapped: Mapped, subject) -> dict:
    """What each picture the layout shows is, by its context name:
    ("key", bucket key) | ("url", address) | ("photo", subject) | None.
    The org logo is the design's own, else the club's (as a certificate
    preset's); the signatures are the design's."""
    from app.api.events.tickets import poster_url
    from app.api.templates_images import key_of
    from app.api.doc_presets.values import club_logo_key

    out: dict = {}
    for layer in _visible(layout):
        if layer.get("kind") != "image":
            continue
        source = layer.get("source") or "upload"
        if source == "upload":
            key = layer.get("imageKey")
            if key:
                out[f"upload:{key}"] = ("key", key)
        elif source == "org_logo":
            key = key_of(style, "logo") or club_logo_key(event)
            out[source] = ("key", key) if key else None
        elif source in ("signature_1", "signature_2"):
            key = key_of(style, source)
            out[source] = ("key", key) if key else None
        elif source == "event_icon":
            url = poster_url(event)
            out[source] = ("url", url) if url else None
        elif source == "guest_photo":
            out[source] = ("photo", subject)
        else:
            # Sponsor logos: none of our own yet (an upload layer carries one).
            out[source] = None
    return out


def ref_names(refs: dict) -> dict:
    """The pictures as names, for a fingerprint: keys and whose photo, never bytes."""
    out = {}
    for name, ref in refs.items():
        if ref is None:
            out[name] = None
        elif ref[0] == "photo":
            s = ref[1]
            out[name] = ["photo", s.photo_answer, s.profile_photo_key, s.full_name]
        else:
            from app.core.storage.bucket import key_from_url

            out[name] = ref[1] if ref[0] == "key" else (key_from_url(ref[1]) or ref[1])
    return out


def pictures(refs: dict, *, event_id) -> tuple[dict, bool]:
    """Each picture as a data: URI ("" for none), and whether every one
    named was read. Blocking: bucket reads (cached per process) and Pillow."""
    from app.api.doc_presets.photos import photo_uri
    from app.api.events.documents import fetch_bytes
    from app.api.templates_images import data_uri
    from app.core.storage import objects

    out: dict = {}
    complete = True
    for name, ref in refs.items():
        if ref is None:
            out[name] = ""
            continue
        if ref[0] == "photo":
            out[name] = photo_uri(ref[1], event_id)
            continue
        data = objects.get_bytes(ref[1]) if ref[0] == "key" else fetch_bytes(ref[1])
        if data:
            out[name] = data_uri(data)
        else:
            out[name] = ""
            complete = False
    return out, complete


def colors(style: dict, event) -> dict:
    """What brand.primary, brand.secondary and ticket_type resolve to (as v1)."""
    from app.api.events.documents import layout_colors

    return layout_colors(style or {}, getattr(event, "club", None))


def values_of(mapped: Mapped, subject) -> dict:
    """The values a page is built from: the mapping's, with the guest's
    initials for a photo frame's tile on every kind (a badge's own token)."""
    values = dict(mapped.values)
    if not values.get("initials"):
        values["initials"] = initials_of(getattr(subject, "full_name", "") or "")
    return values


def context(mapped: Mapped, images: dict, palette: dict, *, title: str = "") -> dict:
    """P's Context in its wire form: the QR's payload, the brand colours, the pictures, the PDF's title."""
    return {"qr": mapped.qr_text, "colors": dict(palette), "images": dict(images), "title": title}


def png_width(layout: dict) -> int:
    return int((layout.get("canvas") or {}).get("width") or 1000) * PNG_SCALE


# ─── Tickets and certificates ───────────────────────────────────────────────


def job(kind: str, registration, style: dict, *, fallback, revoked_at=None):
    """The render job of a ticket or certificate made from a v2 layout.

    Its fingerprint (doc_cache's key and the ETag) names the layout and the
    builder, fonts and runtime (P's fingerprint over the layout and the
    values), the QR's text, each picture by its key and the brand colours
    it may refer to. `fallback` is the classic job (config.classic), drawn
    instead and never kept when the render service cannot draw this (D9)."""
    from app.api.events import doc_cache
    from app.api.events.tickets import RenderJob

    layout = layout_v2_of(style)
    event = registration.event
    subject = subject_of(registration, event)
    mapped = document_values(kind, event, subject, style=style)
    values = values_of(mapped, subject)
    refs = picture_refs(layout, style, event, mapped, subject)
    palette = colors(style, event)
    title = f"Certificate {mapped.values.get('certificate_id') or ''}".strip() if kind == "certificate" else f"Ticket {registration.ticket_id or ''}".strip()
    try:
        # P's (D8): the layout, the values, the QR, the colours, each picture
        # by what names it (never its bytes), the fonts and the builder.
        design = str(_lib().fingerprint(layout, values, context(mapped, ref_names(refs), palette, title=title), kind=kind))
    except Exception:  # noqa: BLE001 - a layout that cannot be fingerprinted draws the fallback; its key still names it
        design = "unfingerprinted"
    print_ = doc_cache.fingerprint(f"{kind}-v2", design, values, mapped.qr_text, ref_names(refs), palette, revoked_at)
    state: dict = {}

    async def draw() -> tuple[bytes, bool]:
        try:
            images, complete = await asyncio.to_thread(pictures, refs, event_id=event.id)
            ctx = context(mapped, images, palette, title=title)
            html = await asyncio.to_thread(lambda: _lib().build_html(layout, values, context=ctx, kind=kind))
            return await engine.pdf(html), complete
        except engine.Unavailable as exc:
            engine.unavailable(kind, exc)
        except Exception:  # noqa: BLE001 - a layout that cannot be drawn must still give the guest their document
            logger.exception("a v2 %s layout could not be drawn; drawing the classic one", kind)
        state["fallback"] = True
        pdf, _ = await doc_cache.run_draw(fallback.draw)
        return pdf, False

    return RenderJob(draw, print_, revision_of(style), style, png_width=png_width(layout), state=state)


# ─── ID cards ───────────────────────────────────────────────────────────────


def card_page(event, registration, style: dict, *, door: Optional[str] = None, printed_at=None, skip_preprinted: bool = False) -> dict:
    """One card's page of a v2 badge, read from the rows here on the loop.
    `skip_preprinted`: printing on pre-printed stock, the layers already on
    it are left out (P's builder keeps any that print the guest's own words)."""
    layout = layout_v2_of(style)
    subject = subject_of(registration, event)
    mapped = document_values("badge", event, subject, style=style, door=door, printed_at=printed_at)
    return {
        "layout": layout, "mapped": mapped, "values": values_of(mapped, subject), "event_id": event.id,
        "refs": picture_refs(layout, style, event, mapped, subject), "colors": colors(style, event),
        "skip_preprinted": skip_preprinted, "title": f"ID card {registration.ticket_id or ''}".strip(),
    }


def _page_html(p: dict) -> str:
    images, _ = pictures(p["refs"], event_id=p["event_id"])
    ctx = context(p["mapped"], images, p["colors"], title=p["title"])
    return _lib().build_html(p["layout"], p["values"], context=ctx, kind="badge", skip_preprinted=p["skip_preprinted"])


def _paper(layout: dict) -> tuple:
    page = layout.get("page") or {}
    return (round(float(page.get("widthMm") or 0), 2), round(float(page.get("heightMm") or 0), 2))


async def pages_pdf(pages: list[dict]) -> bytes:
    """v2 cards as one PDF: each run of cards on the same paper in one call
    to the render service. engine.Unavailable when it cannot."""
    htmls = await asyncio.to_thread(lambda: [_page_html(p) for p in pages])
    runs: list[tuple[tuple, list[str]]] = []
    for p, html in zip(pages, htmls):
        paper = _paper(p["layout"])
        if runs and runs[-1][0] == paper:
            runs[-1][1].append(html)
        else:
            runs.append((paper, [html]))
    parts = []
    for _, items in runs:
        try:
            parts.append(await engine.pdf(items if len(items) > 1 else items[0]))
        except ValueError:
            # Pages the render service's combine cannot join: a call each.
            parts.extend([await engine.pdf(h) for h in items])
    if len(parts) == 1:
        return parts[0]
    from app.core.layout import pool

    return await pool.run(engine.join_pdfs, parts)


# ─── The editor's preview ───────────────────────────────────────────────────


def preview_parts(kind: str, event, style: dict, layout: dict, subject) -> tuple[Mapped, dict, dict]:
    mapped = document_values(kind, event, subject, style=style)
    return mapped, picture_refs(layout, style, event, mapped, subject), colors(style, event)


async def preview(kind: str, event, style: dict, layout: dict, subject, fmt: str):
    """An unsaved layout for a guest: ("html", page for the iframe, fonts
    from /static/doc-fonts) or ("png", the exact picture: one render, at
    twice its CSS pixels). engine.Unavailable when the service is down."""
    mapped, refs, palette = preview_parts(kind, event, style, layout, subject)
    values = values_of(mapped, subject)
    images, _ = await asyncio.to_thread(pictures, refs, event_id=getattr(event, "id", None))
    ctx = context(mapped, images, palette, title=f"{kind} preview")
    lib = _lib()
    if fmt == "html":
        return "html", await asyncio.to_thread(lambda: lib.build_html(layout, values, context=ctx, kind=kind, for_browser=True))
    html = await asyncio.to_thread(lambda: lib.build_html(layout, values, context=ctx, kind=kind))
    pdf = await engine.pdf(html)
    from app.core.layout import pool
    from app.core.layout.renderer import pdf_to_png

    return "png", await pool.run(pdf_to_png, pdf, width_px=png_width(layout))


def stage(kind: str, event, style: dict, subject) -> dict:
    """What the editor's own builder fills the stage with: the sample
    guest's values (text) and the context without pictures (the editor
    shows those from their links). Nothing is drawn or read from the bucket."""
    mapped = document_values(kind, event, subject, style=style)
    return {"values": values_of(mapped, subject), "context": context(mapped, {}, colors(style, event))}


def picture_links(layout: dict, style: dict, event) -> dict:
    """Links signed now for every picture the layout's layers can show, for
    the editor's stage: {source: url | None, "uploads": {key: url}}."""
    from app.api.events.tickets import poster_url
    from app.api.templates_images import key_of
    from app.api.doc_presets.values import club_logo_key
    from app.core.storage import objects

    def signed(key):
        try:
            return objects.presigned_url(key) if key else None
        except Exception:  # noqa: BLE001
            return None

    return {
        "org_logo": signed(key_of(style, "logo") or club_logo_key(event)),
        "signature_1": signed(key_of(style, "signature_1")),
        "signature_2": signed(key_of(style, "signature_2")),
        "event_icon": poster_url(event),
        "uploads": {key: signed(key) for key in set(upload_keys(layout).values())},
    }


def accent_of(style: dict) -> Optional[str]:
    """A v2 design's accent colour (vars.accent), when it is a hex colour."""
    layout = layout_v2_of(style) or {}
    accent = (layout.get("vars") or {}).get("accent")
    return accent if isinstance(accent, str) and accent.startswith("#") and len(accent) in (7, 9) else None


__all__ = [
    "Refused", "SOURCES", "accent_of", "card_page", "checked", "classic", "colors", "context", "editable", "fonts",
    "job", "pages_pdf", "picture_links", "picture_refs", "pictures", "png_width", "preview", "ref_names",
    "stage", "starter_copy", "starters", "tokens", "upload_keys", "values_of", "wire",
]
