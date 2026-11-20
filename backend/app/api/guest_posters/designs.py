"""An event's poster designs: variants, which one a guest gets, the board's
DTO, saving (CONTRACT.md §3, §10.3).

A variant holds a layout v2 per format. For a guest and a format the design
is the first of: their role's variant, their ticket type's, the event's
default, the platform's starter -- the first that has a layout for that format.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.doc_presets import layouts
from app.api.guest_posters import values as poster_values
from app.api.guest_posters.models import PosterDesigns
from app.core.layout_v2 import poster
from app.response import CustomHTTPException

DEFAULT = "default"
STARTER = "starter"
VARIANT = re.compile(r"^(?:default|role:(?:attendee|speaker|volunteer|organiser)|type:[1-9][0-9]{0,9})$")
ROLE_OPTIONS = (("role:speaker", "Speakers"), ("role:volunteer", "Volunteers"), ("role:organiser", "Organisers"), ("role:attendee", "Attendees"))


def checked_variant(variant: Optional[str]) -> str:
    v = (variant or DEFAULT).strip()
    if not VARIANT.match(v):
        raise CustomHTTPException(422, message="Pick a variant: default, role:<role> or type:<ticket type>.", error_code="INVALID_VARIANT")
    return v


def checked_format(fmt: Optional[str]) -> str:
    if fmt not in poster.FORMATS:
        raise CustomHTTPException(422, message="Pick a format: square, portrait or story.", error_code="INVALID_FORMAT")
    return fmt


def chain(role: Optional[str], ticket_type_id: Optional[int]) -> list[str]:
    out = []
    if role:
        out.append(f"role:{role}")
    if ticket_type_id:
        out.append(f"type:{int(ticket_type_id)}")
    out.append(DEFAULT)
    return out


async def rows_of(session, event_id: int) -> dict[str, PosterDesigns]:
    rows = (await session.scalars(select(PosterDesigns).where(PosterDesigns.event_id == event_id))).all()
    return {r.variant: r for r in rows}


@dataclass
class Resolved:
    layout: Optional[dict]
    #: The variant that answered, or "starter".
    variant: str
    revision: int
    #: The starter's id when the platform's starter answered.
    starter: Optional[str] = None


def resolve_from(rows: dict, order: list[str], fmt: str) -> Resolved:
    for v in order:
        row = rows.get(v)
        layout = (row.layouts or {}).get(fmt) if row is not None else None
        if isinstance(layout, dict):
            return Resolved(layout, v, int(row.revision or 0))
    sid = poster.default_starter_id()
    layout = poster.starter(sid, fmt) if sid else None
    return Resolved(layout, STARTER, 0, sid)


async def resolve(session, event_id: int, *, role: Optional[str], ticket_type_id: Optional[int], fmt: str, rows: Optional[dict] = None) -> Resolved:
    rows = rows if rows is not None else await rows_of(session, event_id)
    return resolve_from(rows, chain(role, ticket_type_id), fmt)


def _role_of_variant(variant: str) -> str:
    return variant.split(":", 1)[1] if variant.startswith("role:") else "attendee"


def _fallbacks(variant: str) -> list[str]:
    """What a variant without a layout for a format shows: the rest of the chain."""
    return [] if variant == DEFAULT else [DEFAULT]


# ─── The board ──────────────────────────────────────────────────────────────


def board(event, variant: str, fmt: str, rows: dict) -> dict:
    """The layout v2 board's DTO for one variant and format (as the other
    kinds' `v2` block), with the sample guest's values and context."""
    row = rows.get(variant)
    own = (row.layouts or {}).get(fmt) if row is not None else None
    if isinstance(own, dict):
        layout, source, revision = own, "saved", int(row.revision or 0)
    else:
        found = resolve_from(rows, _fallbacks(variant), fmt)
        layout, revision = found.layout, int(row.revision or 0) if row is not None else 0
        source = "starter" if found.variant == STARTER else "default"
    role = _role_of_variant(variant)
    sample = poster_values.sample_values(event, role=role)
    return {
        "kind": poster.KIND, "variant": variant, "format": fmt,
        "layout": layout, "from": source, "revision": revision,
        "page": poster.page_mm(fmt), "canvas": {"width": poster.FORMATS[fmt][0], "height": poster.FORMATS[fmt][1]},
        "starters": poster.starters(),
        "fonts": layouts.fonts(),
        "tokens": layouts.tokens(poster.KIND, event),
        "images": {**layouts.picture_links(layout or {}, {}, event), "guest_photo": None},
        "values": sample,
        "context": {"qr": poster_values.SAMPLE_SHARE_URL, "colors": layouts.colors({}, event), "images": {}},
        "qrRequired": False,
    }


async def variants_out(session, event, rows: Optional[dict] = None) -> dict:
    from app.api.ticketing.models import TicketTypes

    rows = rows if rows is not None else await rows_of(session, event.id)
    types = (await session.execute(
        select(TicketTypes.id, TicketTypes.name).where(TicketTypes.event_id == event.id, TicketTypes.is_deleted == False)  # noqa: E712
        .order_by(TicketTypes.id)
    )).all()
    labels = {DEFAULT: "Everyone", **dict(ROLE_OPTIONS), **{f"type:{i}": f"Ticket: {n}" for i, n in types}}
    out = []
    for v in sorted(rows, key=lambda v: (v != DEFAULT, not v.startswith("role:"), v)):
        r = rows[v]
        fmts = {}
        for fmt in poster.ORDER:
            layout = (r.layouts or {}).get(fmt)
            fmts[fmt] = {"saved": True, "revision": int(r.revision or 0), "starter": layout.get("starter")} if isinstance(layout, dict) else {"saved": False}
        out.append({"variant": v, "label": labels.get(v, v), "formats": fmts, "starter": r.starter,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None})
    options = [{"variant": DEFAULT, "label": "Everyone"}] + [{"variant": v, "label": l} for v, l in ROLE_OPTIONS]
    options += [{"variant": f"type:{i}", "label": f"Ticket: {n}"} for i, n in types]
    return {"variants": out, "options": options, "order": "role, then ticket type, then default, then the platform's starter"}


# ─── Saving ─────────────────────────────────────────────────────────────────


def checked(body: dict, fmt: str, *, prefixes: tuple) -> tuple[dict, list]:
    """A poster layout as it is saved (layout v2's checks for kind poster,
    and the format it is saved as), or the 422."""
    try:
        layout, warnings = layouts.checked(poster.KIND, body, prefixes=prefixes)
    except layouts.Refused as exc:
        raise CustomHTTPException(422, message=exc.message, error_code="INVALID_LAYOUT", errors={"fields": exc.fields, "layout": exc.problems}) from exc
    if poster.format_of(layout) != fmt:
        w, h = poster.FORMATS[fmt]
        raise CustomHTTPException(
            422, message=f"This is the {poster.LABELS[fmt].lower()} poster: its canvas is {w} × {h}.", error_code="INVALID_LAYOUT",
            errors={"fields": {"canvas": f"{w} × {h} for {fmt}"}, "layout": []},
        )
    return layout, warnings


async def _locked_row(session, event_id: int, variant: str) -> PosterDesigns:
    now = datetime.now(timezone.utc)
    await session.execute(
        pg_insert(PosterDesigns.__table__).values(event_id=event_id, variant=variant, layouts={}, revision=0, created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=["event_id", "variant"])
    )
    return await session.scalar(
        select(PosterDesigns).where(PosterDesigns.event_id == event_id, PosterDesigns.variant == variant)
        .with_for_update().execution_options(populate_existing=True)
    )


async def save(session, event, variant: str, new_layouts: dict, *, user_id: Optional[int], starter: Optional[str] = None) -> PosterDesigns:
    """Store these formats' layouts on the variant (the others kept), one revision up."""
    row = await _locked_row(session, event.id, variant)
    row.layouts = {**(row.layouts or {}), **new_layouts}
    row.revision = int(row.revision or 0) + 1
    row.starter = starter or next((l.get("starter") for l in new_layouts.values() if isinstance(l, dict) and l.get("starter")), row.starter)
    row.updated_by = user_id
    row.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return row


async def drop(session, event, variant: str, fmt: Optional[str], *, user_id: Optional[int]) -> bool:
    row = await session.scalar(
        select(PosterDesigns).where(PosterDesigns.event_id == event.id, PosterDesigns.variant == variant).with_for_update()
    )
    if row is None:
        return False
    if fmt is None:
        await session.delete(row)
    else:
        rest = {k: v for k, v in (row.layouts or {}).items() if k != fmt}
        if rest:
            row.layouts = rest
            row.revision = int(row.revision or 0) + 1
            row.updated_by = user_id
            row.updated_at = datetime.now(timezone.utc)
        else:
            await session.delete(row)
    await session.flush()
    return True
