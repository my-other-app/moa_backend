"""A preset document as a render job, and an ID card's pages.

A ticket or certificate made from a preset is a RenderJob like any other
(events.tickets.RenderJob): its fingerprint names everything it prints, so
doc_cache keeps it under its own key (D8), and its `draw` -- async here,
since the drawing is a call to the render service -- fills the template and
renders it. When the service cannot (D9) the same request is drawn by the
classic renderer instead: the design without its preset, through the paths
that drew it before. That PDF is marked incomplete, so it is never kept and
never tagged, and the next request tries the service again.

ID cards are not cached; `preset_pages_pdf` draws a card, or a batch of
them, in as few calls as it can.

What is handed to A's fill (engine.fill): our values (values.py, with the
pictures as data: URIs and the QR's text as "qr"), and the preset's options,
bindings and accent as stored -- A applies them.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.api.doc_presets import engine
from app.api.doc_presets.config import preset_of
from app.api.doc_presets.photos import photo_uri
from app.api.doc_presets.values import (
    Mapped,
    badge_values,
    certificate_values,
    subject_of,
    ticket_values,
)

logger = logging.getLogger(__name__)

#: A PNG of a document drawn by the classic renderer instead (D9), in px.
FALLBACK_PNG_WIDTH = 1200

def club_logo_key(event) -> Optional[str]:
    """The club's logo in the bucket: its large variant, else the original."""
    club = getattr(event, "club", None)
    logo = getattr(club, "logo", None) if club is not None else None
    variations = getattr(logo, "variations", None)
    if isinstance(variations, dict):
        return variations.get("large") or variations.get("original")
    return logo if isinstance(logo, str) and logo else None


def accent_of(preset: dict) -> Optional[str]:
    return preset.get("accent") or engine.accent_default(preset["id"])


def mapped_for(kind: str, event, subject, style: dict, preset: dict, *, door: Optional[str] = None, printed_at=None) -> Mapped:
    """The document's data (D5). The bindings are A's fill's to apply."""
    if kind == "ticket":
        return ticket_values(event, subject)
    if kind == "certificate":
        from app.api.templates_images import key_of

        return certificate_values(
            event, subject,
            logo_key=key_of(style, "logo") or club_logo_key(event),
            signatures=(key_of(style, "signature_1"), key_of(style, "signature_2")),
        )
    return badge_values(event, subject, door=door, printed_at=printed_at)


def image_refs(mapped: Mapped) -> dict:
    """What names each picture, for a fingerprint: a key, or whose photo it is."""
    out = {}
    for token, ref in mapped.images.items():
        if ref is None:
            out[token] = None
        elif ref[0] == "key":
            out[token] = ref[1]
        else:
            subject = ref[1]
            out[token] = ["photo", subject.photo_answer, subject.profile_photo_key, subject.full_name]
    return out


def materialise(mapped: Mapped, *, event_id) -> tuple[dict, bool]:
    """Every value the template is filled with: the text, the QR's text (A
    draws it, as its manifest says), and each picture as a data: URI. And
    whether every picture named was read. Blocking."""
    from app.api.templates_images import data_uri
    from app.core.storage import objects

    values = {**mapped.values, "qr": mapped.qr_text}
    complete = True
    for token, ref in mapped.images.items():
        if ref is None:
            values[token] = ""
        elif ref[0] == "key":
            data = objects.get_bytes(ref[1])
            if data:
                values[token] = data_uri(data)
            else:
                values[token] = ""
                complete = False
        else:
            values[token] = photo_uri(ref[1], event_id)
    return values, complete


def _png_width(preset: dict) -> Optional[int]:
    try:
        return engine.png_width(preset["id"])
    except engine.UnknownDesign:
        return None


def preset_job(kind: str, registration, style: dict, *, fallback, revoked_at=None):
    """The render job of a ticket or certificate made from a preset.

    `fallback` is the job the classic renderer would draw for this
    registration (the design without its preset): drawn instead, and not
    kept, when the render service cannot draw this one (D9)."""
    from app.api.events import doc_cache
    from app.api.events.tickets import RenderJob

    preset = preset_of(style)
    event = registration.event
    subject = subject_of(registration, event)
    mapped = mapped_for(kind, event, subject, style, preset)
    try:
        # A's (D8): the template, manifest, fonts and runtime, the preset, these values.
        design = engine.design_fingerprint(preset, {**mapped.values, "qr": mapped.qr_text})
    except Exception:  # noqa: BLE001 - an unknown design draws the fallback; its key still names it
        design = f"unknown:{preset['id']}"
    print_ = doc_cache.fingerprint(f"{kind}-preset", design, mapped.values, mapped.qr_text, image_refs(mapped), revoked_at)
    state: dict = {}

    async def draw() -> tuple[bytes, bool]:
        try:
            values, complete = await asyncio.to_thread(materialise, mapped, event_id=event.id)
            html = await asyncio.to_thread(engine.fill, preset, values)
            pdf = await engine.pdf(html)
            return pdf, complete
        except engine.Unavailable as exc:
            engine.unavailable(kind, exc)
        except Exception:  # noqa: BLE001 - a design that cannot be filled must still give the guest their document
            logger.exception("the %s design %s could not be drawn; drawing the classic one", kind, preset["id"])
        state["fallback"] = True
        pdf, _ = await doc_cache.run_draw(fallback.draw)
        return pdf, False

    return RenderJob(draw, print_, 0, style, png_width=_png_width(preset), state=state)


async def png_of(job, pdf: bytes) -> bytes:
    """A preset job's PNG: its PDF at twice its CSS pixels (D2)."""
    from app.core.layout import pool
    from app.core.layout.renderer import pdf_to_png

    width = FALLBACK_PNG_WIDTH if job.state.get("fallback") or not job.png_width else job.png_width
    return await pool.run(pdf_to_png, pdf, width_px=width)


# ─── ID cards ───────────────────────────────────────────────────────────────


def card_page(event, registration, style: dict, *, door: Optional[str] = None, printed_at=None) -> dict:
    """One card's page of a preset badge, read from the rows here on the loop."""
    preset = preset_of(style)
    subject = subject_of(registration, event)
    return {"preset": preset, "mapped": mapped_for("badge", event, subject, style, preset, door=door, printed_at=printed_at), "event_id": event.id}


async def preset_pages_pdf(pages: list[dict]) -> bytes:
    """Preset cards as one PDF: each run of cards on the same page size in
    one call to the render service. Unavailable when it cannot."""
    for p in pages:
        if engine.info(p["preset"]["id"]) is None:
            raise engine.UnknownDesign(p["preset"]["id"])

    def html_of(p: dict) -> str:
        values, _ = materialise(p["mapped"], event_id=p["event_id"])
        return engine.fill(p["preset"], values)

    htmls = await asyncio.to_thread(lambda: [html_of(p) for p in pages])
    runs: list[tuple[dict, list[str]]] = []
    for p, html in zip(pages, htmls):
        page = engine.page_of(p["preset"]["id"])
        if runs and runs[-1][0] == page:
            runs[-1][1].append(html)
        else:
            runs.append((page, [html]))
    parts = [await engine.pdf(items if len(items) > 1 else items[0]) for _, items in runs]
    if len(parts) == 1:
        return parts[0]
    from app.core.layout import pool

    return await pool.run(engine.join_pdfs, parts)
