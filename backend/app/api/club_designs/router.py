"""The club's own designs: what every event starts from.

One default per kind — ticket, ID card, certificate, programme — designed
in the same layer editor an event uses, held on the club's row in the same
template table the resolver already reads (club default sits below the
event's own design and above the system's). An event that has been designed
keeps its copy; one that has not follows this.

The editor previews the club's design on a stand-in event with the club's
real name and colours, because there is no event to borrow dates from.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import APIRouter, File, Query, Response, UploadFile
from sqlalchemy import select

from app.api.agenda import service as agenda_service
from app.api.clubs.models import Clubs
from app.api.event_designs.router import (
    _CATALOGUES,
    _DEFAULTS,
    _MODELS,
    DesignKind,
    PresetBody,
    PresetPreviewBody,
    _checked_preset,
    preset_preview,
    _club_layout_prefix,
    _fields_of,
    _layout_dto,
    _checked_v2,
    _plan_allows,
    _preview_png,
    _starter_of,
    _store_layout_image,
    _v2_body,
    _validate_layout,
    layout_v2_preview,
    starter_answer,
)
from app.api.doc_presets.config import is_v2, layout_v1_of, preset_of, with_layout, with_v1_layout
from app.api.events.documents import in_layout_mode, layout_sample_values
from app.api.templates_audience import TemplateAudience
from app.api.templates_images import shown
from app.core.layout import pool
from app.core.auth.dependencies import ClubAuth
from app.core.layout.model import PageSpec, TicketLayoutFields, dump
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/clubs/designs")


async def _club_for(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


def _sample_event(club):
    """A stand-in: the club's real details on a fictional event a week away."""
    start = (datetime.now(timezone.utc) + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
    return SimpleNamespace(
        id=0, name="Sample event", club=club, club_id=club.id, additional_details=[], location_name="Main Hall",
        event_datetime=start, duration=2.0, admission_prefix=None, admission_digits=4, admission_include_code=True,
        is_online=False,
    )


async def _default_row(session, club_id: int, kind: DesignKind):
    model = _MODELS[kind]
    return await session.scalar(
        select(model).where(model.club_id == club_id, model.event_id.is_(None), model.is_default == True, model.is_deleted == False)  # noqa: E712
    )


async def _current(session, club, kind: DesignKind):
    template = await _default_row(session, club.id, kind)
    style = shown(template.config or {}, template, fields=_fields_of(template)) if template is not None else _DEFAULTS[kind]()
    return template, style


async def _agenda(session, club, kind: DesignKind):
    if kind is not DesignKind.agenda:
        return None
    return agenda_service.entries_json(await agenda_service.entries_for(session, _sample_event(club)))


def _dto(club, kind: DesignKind, template, style: dict, agenda) -> dict:
    out = _layout_dto(_sample_event(club), kind, TemplateAudience.event_default, template, style, agenda)
    out["eventId"] = 0
    # The club owns a design once it has saved one in the editor, or chosen
    # one of the designer's designs.
    out["isOwn"] = template is not None and (in_layout_mode(style) or preset_of(style) is not None)
    return out


@router.get("/{kind}/layout", summary="The club's default design, as the editor works on it")
async def get_layout(session: SessionDep, user: ClubAuth, kind: DesignKind) -> dict:
    club = await _club_for(session, user)
    template, style = await _current(session, club, kind)
    return _dto(club, kind, template, style, await _agenda(session, club, kind))


@router.get("/{kind}/layout/starters/{name}", summary="A complete design to start from")
async def get_starter(session: SessionDep, user: ClubAuth, kind: DesignKind, name: str) -> dict:
    club = await _club_for(session, user)
    if name not in _CATALOGUES[kind]:
        raise CustomHTTPException(404, message="No such starter")
    page = None
    if kind is DesignKind.badge:
        _, style = await _current(session, club, kind)
        layout = layout_v1_of(style)
        if layout and layout.get("page"):
            page = PageSpec.model_validate(layout["page"])
    s = _starter_of(kind, name, page)
    return dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"]))


@router.put("/{kind}/layout", summary="Save the club's default design")
async def save_layout(session: SessionDep, user: ClubAuth, kind: DesignKind, body: dict) -> dict:
    """Becomes the one default of its kind for the club; the version climbs so
    an event forked later can say which design it came from. A v2 layout is
    checked and saved as the event's is (V4); answers with `warnings` then."""
    club = await _club_for(session, user)
    await _plan_allows(session, club.id, kind, user)
    v2 = is_v2(body)
    if v2:
        layout_v2, warnings = _checked_v2(kind, body, prefixes=(_club_layout_prefix(club.id),))
    else:
        layout = _validate_layout(kind, body, prefixes=(_club_layout_prefix(club.id),))
    model = _MODELS[kind]
    template = await _default_row(session, club.id, kind)
    if template is None:
        template = model(
            club_id=club.id, event_id=None, audience=TemplateAudience.event_default,
            name=f"{club.name} {kind.value}", is_default=True, config={},
        )
        session.add(template)
    config = dict(template.config or {})
    # Saving the custom design makes it the one in use (D6, V4).
    template.config = with_layout(config, layout_v2) if v2 else with_v1_layout(config, dump(layout))
    await session.commit()
    await session.refresh(template)
    out = _dto(club, kind, template, shown(template.config, template, fields=_fields_of(template)), await _agenda(session, club, kind))
    return {**out, "warnings": warnings} if v2 else out


@router.delete("/{kind}/layout", summary="Drop the club's default; events without their own follow the system design")
async def reset_layout(session: SessionDep, user: ClubAuth, kind: DesignKind) -> dict:
    club = await _club_for(session, user)
    template = await _default_row(session, club.id, kind)
    if template is None:
        raise CustomHTTPException(404, message="The club has no design of its own for this yet.")
    template.is_default = False
    template.is_deleted = True
    await session.commit()
    return {"deleted": True}


@router.post("/{kind}/layout/preview", summary="The unsaved design rendered exactly: a PNG, or HTML for an iframe")
async def preview_layout(session: SessionDep, user: ClubAuth, kind: DesignKind, body: dict, format: str = Query("png", pattern="^(html|png)$")) -> Response:
    """As the event's, on a stand-in event with the club's details and the sample guest."""
    club = await _club_for(session, user)
    body.pop("ticket_id", None)
    v2 = _v2_body(body)
    if v2 is not None:
        layout_v2, _ = _checked_v2(kind, v2, prefixes=(_club_layout_prefix(club.id),))
        _, style = await _current(session, club, kind)
        return await layout_v2_preview(kind, _sample_event(club), style, layout_v2, None, TemplateAudience.event_default, format)
    layout = _validate_layout(kind, body, prefixes=(_club_layout_prefix(club.id),))
    _, style = await _current(session, club, kind)
    style = {**style, "layout": dump(layout)}
    sample = _sample_event(club)
    values = layout_sample_values(sample, kind.value)
    code = values["ticket_code"]
    qr_text = f"certificate:{code}" if kind is DesignKind.certificate else code
    entries = await agenda_service.entries_for(session, sample) if kind is DesignKind.agenda else None
    return Response(
        content=await pool.run(_preview_png, style, values, qr_text, f"{kind.value} preview", entries, club),
        media_type="image/png", headers={"Cache-Control": "no-store"},
    )


@router.post("/{kind}/layout/starters/{starter_id:path}", summary="A v2 starter to begin the club's design from (nothing is saved)")
async def use_starter(session: SessionDep, user: ClubAuth, kind: DesignKind, starter_id: str) -> dict:
    await _club_for(session, user)
    return starter_answer(kind, starter_id)


@router.put("/{kind}/preset", summary="Use one of the designer's designs as the club's default")
async def save_preset(session: SessionDep, user: ClubAuth, kind: DesignKind, body: PresetBody) -> dict:
    """What every event of the club without its own design uses. The
    custom layout, if any, stays where it was. Answers with GET .../layout."""
    club = await _club_for(session, user)
    preset, warnings = _checked_preset(kind, body)
    await _plan_allows(session, club.id, kind, user)
    model = _MODELS[kind]
    template = await _default_row(session, club.id, kind)
    if template is None:
        template = model(
            club_id=club.id, event_id=None, audience=TemplateAudience.event_default,
            name=f"{club.name} {kind.value}", is_default=True, config=_DEFAULTS[kind](),
        )
        session.add(template)
    template.config = {**(template.config or {}), "preset": preset}
    await session.commit()
    await session.refresh(template)
    out = _dto(club, kind, template, shown(template.config, template, fields=_fields_of(template)), await _agenda(session, club, kind))
    return {**out, "warnings": warnings}


@router.delete("/{kind}/preset", summary="Stop using the chosen design as the club's default")
async def clear_preset(session: SessionDep, user: ClubAuth, kind: DesignKind) -> dict:
    """With a custom layout saved, that is the club's default now; without
    one the club's default is dropped and its events use the system's (D7)."""
    club = await _club_for(session, user)
    template = await _default_row(session, club.id, kind)
    if template is not None:
        config = {k: v for k, v in (template.config or {}).items() if k != "preset"}
        if in_layout_mode(config):
            template.config = config
        else:
            template.is_default = False
            template.is_deleted = True
        await session.commit()
    template, style = await _current(session, club, kind)
    return _dto(club, kind, template, style, await _agenda(session, club, kind))


@router.post("/{kind}/preset/preview", summary="One of the designer's designs, drawn for the sample guest")
async def preview_preset(session: SessionDep, user: ClubAuth, kind: DesignKind, body: PresetPreviewBody, format: str = Query("html", pattern="^(html|png)$")) -> Response:
    """As the event's preview, on a stand-in event with the club's details."""
    club = await _club_for(session, user)
    preset, _ = _checked_preset(kind, body)
    _, style = await _current(session, club, kind)
    return await preset_preview(kind, _sample_event(club), style, preset, None, TemplateAudience.event_default, format)


@router.post("/{kind}/layout/images", summary="A template or a picture for the club's design")
async def upload_layout_image(
    session: SessionDep, user: ClubAuth, kind: DesignKind, file: UploadFile = File(...), purpose: str = Query("image", pattern="^(template|image)$")
) -> dict:
    club = await _club_for(session, user)
    return await asyncio.to_thread(_store_layout_image, await file.read(), _club_layout_prefix(club.id), purpose)
