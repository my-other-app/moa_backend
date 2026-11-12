"""The one door to app.core.doc_designs (Backend A's package).

Everything the backend asks of the designer's designs -- the catalogue,
checking a choice, filling a template, rendering it, its fingerprint, the
browser preview, the render service's health -- goes through here, so A's
interface is used in one place (its package docstring).

A applies a preset's bindings and options and draws the QR itself: what is
handed to `fill` is our values (values.py), the stored options and bindings
as they are, and the accent.

Degraded mode (D9): when the render service is down or times out on A's two
tries, `pdf` raises Unavailable. Tickets and certificates then fall back for
that request to the classic renderer (jobs.py) and badges answer 503
(BADGE_UNAVAILABLE). Either way the admins hear of it, at most once every
ALERT_EVERY seconds from a process (and once in ten minutes across
processes, admin_alerts' own window).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Union

from app.core import doc_designs

logger = logging.getLogger(__name__)

#: A document is rasterised at twice its CSS pixels (D2).
PNG_SCALE = 2
#: How many pages one call to the render service carries at most; a longer
#: batch is cut into calls of this size and the PDFs joined.
PAGES_PER_CALL = 100
#: At most one alert this often from one process, in seconds.
ALERT_EVERY = 600.0

BADGE_UNAVAILABLE = "Printing is unavailable, try again."
PREVIEW_UNAVAILABLE = "The exact preview is unavailable right now, try again in a minute."


class Unavailable(Exception):
    """The render service could not draw this (D9)."""


class UnknownDesign(Exception):
    """A preset names a design this build does not have."""


def design(design_id: str):
    """A's DesignInfo, or None when there is no such design."""
    try:
        return doc_designs.get(design_id)
    except (doc_designs.DesignError, KeyError, LookupError, ValueError):
        return None


def as_dict(d) -> dict:
    """A design as the editor reads it: A's DesignInfo.to_dict(). Its
    preview is the picture's https address (A serves it under
    /static/doc-designs/), or null for one without -- never a file path."""
    out = dict(d.to_dict())
    preview = out.get("preview")
    if not (isinstance(preview, str) and preview.startswith(("https://", "http://"))):
        out["preview"] = None
    return out


def info(design_id: str) -> Optional[dict]:
    """The design's editor shape (as_dict), or None when there is no such design."""
    d = design(design_id)
    return as_dict(d) if d is not None else None


def catalogue(kind: str) -> list[dict]:
    """Every design of a kind, for the editor's gallery, in the designer's order."""
    try:
        return [as_dict(d) for d in doc_designs.catalogue(kind)]
    except Exception:  # noqa: BLE001 - a broken manifest must not close the editor
        logger.exception("the %s catalogue could not be read", kind)
        return []


def page_of(design_id: str) -> dict:
    found = info(design_id)
    if found is None:
        raise UnknownDesign(design_id)
    return dict(found.get("page") or {})


def png_width(design_id: str) -> int:
    """The PNG's width: the page's CSS pixels at PNG_SCALE."""
    return int(page_of(design_id).get("width_px") or 1000) * PNG_SCALE


def accent_default(design_id: str) -> Optional[str]:
    accent = (info(design_id) or {}).get("accent")
    return accent.get("default") if isinstance(accent, dict) else None


def problems(preset: dict) -> list[dict]:
    """What the design says about these options, bindings and accent:
    [{code, field, message, level}]. An "error" must be fixed before the
    choice is saved; a "warning" (an accent hard to read) may be saved."""
    if design(preset["id"]) is None:
        return [{"code": "unknown_design", "field": "id", "message": f"There is no design {preset['id']}.", "level": "error"}]
    try:
        found = doc_designs.validate(preset["id"], preset.get("options") or {}, preset.get("bindings") or {}, preset.get("accent"))
    except Exception as exc:  # noqa: BLE001
        return [{"code": "unchecked", "field": "id", "message": str(exc)[:300] or "This design's settings could not be checked.", "level": "error"}]
    out = []
    for p in found or []:
        if isinstance(p, dict):
            out.append({"level": "error", **p})
        else:
            out.append({"code": "problem", "field": "", "message": str(p), "level": "error"})
    return out


def _args(preset: dict) -> tuple:
    return preset.get("options") or {}, preset.get("bindings") or {}, preset.get("accent")


def fill(preset: dict, values: dict) -> str:
    """The complete HTML of one document, fonts inline, for the render service."""
    if design(preset["id"]) is None:
        raise UnknownDesign(preset["id"])
    return doc_designs.fill(preset["id"], values, *_args(preset))


def preview_html(preset: dict, values: dict) -> str:
    """The same document for a browser's iframe, its fonts from /static/doc-fonts."""
    if design(preset["id"]) is None:
        raise UnknownDesign(preset["id"])
    return doc_designs.preview_html(preset["id"], values, *_args(preset))


def design_fingerprint(preset: dict, values: dict) -> str:
    """A's fingerprint of a render (D8): the template's digest, the manifest's
    version, the font lock, its runtime, the options, bindings, accent and
    these values."""
    return str(doc_designs.fingerprint(preset["id"], values, *_args(preset)))


async def pdf(html_or_pages: Union[str, list]) -> bytes:
    """The PDF of one document, or of many pages of one size as one; each
    page's size is its own (A reads it from the page). Unavailable when the
    render service cannot draw it. A long batch goes in several calls."""
    pages = html_or_pages if isinstance(html_or_pages, list) else [html_or_pages]
    try:
        if len(pages) <= PAGES_PER_CALL:
            return await doc_designs.render_pdf(html_or_pages)
        parts = [await doc_designs.render_pdf(pages[i:i + PAGES_PER_CALL]) for i in range(0, len(pages), PAGES_PER_CALL)]
    except doc_designs.RenderUnavailable as exc:
        raise Unavailable(str(exc)[:300]) from exc
    from app.core.layout import pool

    return await pool.run(join_pdfs, parts)


def join_pdfs(parts: list[bytes]) -> bytes:
    """Several PDFs as one, in order (pdfium, under its lock)."""
    import io

    import pypdfium2 as pdfium

    from app.core.layout.pool import PDFIUM

    if len(parts) == 1:
        return parts[0]
    with PDFIUM:
        out = pdfium.PdfDocument.new()
        try:
            for part in parts:
                src = pdfium.PdfDocument(part)
                try:
                    out.import_pages(src)
                finally:
                    src.close()
            buf = io.BytesIO()
            out.save(buf)
            return buf.getvalue()
        finally:
            out.close()


# ─── Degraded mode ──────────────────────────────────────────────────────────

_last_alert: Optional[float] = None


def unavailable(kind: str, error: object) -> None:
    """Note that the render service failed a `kind` document, and tell the
    admins -- at most once every ALERT_EVERY seconds from this process. Never
    raises and never waits: the alert is written on a task of its own."""
    global _last_alert
    logger.warning("render service unavailable for a %s: %s", kind, error)
    now = time.monotonic()
    if _last_alert is not None and now - _last_alert < ALERT_EVERY:
        return
    _last_alert = now
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_send_alert(kind, str(error)[:300]), name="render-unavailable alert")


async def _send_alert(kind: str, error: str) -> None:
    try:
        from app.api.admin_alerts import notify
        from app.core import jobs

        async with jobs.scope() as s:
            await notify(
                s, "render_unavailable",
                title="Documents are being drawn by the classic renderer",
                body=(
                    f"The render service (moa-render) did not draw a {kind}: {error}. Tickets and certificates "
                    "fall back to the classic design until it answers; ID cards cannot print."
                ),
                data={"kind": kind, "error": error[:200]},
            )
            await s.commit()
    except Exception:  # noqa: BLE001 - the log line above says it anyway
        logger.warning("could not alert the admins that the render service is down", exc_info=True)


async def check() -> dict:
    """The render service's health (A's render_health: Chromium up?), for the ops check."""
    try:
        return await doc_designs.render_health()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "unreachable", "error": str(exc)[:300]}
