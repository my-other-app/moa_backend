"""An event's live theme, its pictures and the big screen's controls, as
the console edits them and the room reads them.

The room reads the theme once a second at most per event (`state`, a
snapshot like the room's own), with the pictures it names resolved to their
links. Saving it, deleting a picture it uses or changing the screen's
controls forgets that snapshot and tells every stream of the room, the quiz
and the screen at once (room.room_changed, room.quiz_changed).
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from PIL import Image
from sqlalchemy import delete, func, select

from app.api.agenda.models import AgendaItems
from app.api.clubs.models import Clubs
from app.api.discover.cache import TTLCache
from app.api.event_partners.models import EventPartners
from app.api.events.models import Events
from app.api.live import room, theme
from app.api.live.models import MEDIA_KINDS, LiveMedia, LiveThemes
from app.core.storage.fields import upload
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: Pictures an event's room may hold.
MEDIA_MAX = 60
MEDIA_BYTES = 10 * 1024 * 1024
MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/jpg")

themes = TTLCache(ttl=room.SNAPSHOT_SECONDS, maxsize=4096)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ─── Pictures ────────────────────────────────────────────────────────────


def media_out(m: LiveMedia) -> dict:
    img = m.image if isinstance(m.image, dict) else {}
    return {
        "id": str(m.id),
        "kind": m.kind,
        "thumbnail": img.get("thumbnail"),
        "medium": img.get("medium"),
        "large": img.get("large"),
        "original": img.get("original"),
        "width": m.width,
        "height": m.height,
    }


def _club_logo(club) -> Optional[dict]:
    logo = getattr(club, "logo", None)
    if not isinstance(logo, dict) or not logo.get("original"):
        return None
    return {
        "id": None,
        "kind": "logo",
        "thumbnail": logo.get("thumbnail") or logo.get("original"),
        "medium": logo.get("medium") or logo.get("original"),
        "large": logo.get("large") or logo.get("original"),
        "original": logo.get("original"),
        "width": None,
        "height": None,
    }


async def media_of(session, event_id: int, ids=None) -> dict:
    """The event's pictures by id (text), as Media dicts: all of them, or those in `ids`."""
    q = select(LiveMedia).where(LiveMedia.event_id == event_id)
    if ids is not None:
        wanted = []
        for i in ids:
            try:
                wanted.append(uuid.UUID(str(i)))
            except ValueError:
                continue
        if not wanted:
            return {}
        q = q.where(LiveMedia.id.in_(wanted))
    return {str(m.id): media_out(m) for m in (await session.scalars(q)).all()}


def _size_of(raw: bytes) -> tuple[Optional[int], Optional[int]]:
    try:
        with Image.open(io.BytesIO(raw)) as im:
            return im.size
    except Exception:  # noqa: BLE001 - the pipeline refuses what is not an image
        return None, None


async def add_media(session, event: Events, kind: str, file, user_id: Optional[int]) -> dict:
    if kind not in MEDIA_KINDS:
        raise CustomHTTPException(422, message=f"kind is one of: {', '.join(MEDIA_KINDS)}.", error_code="LIVE_MEDIA_TYPE")
    if (file.content_type or "").lower() not in MEDIA_TYPES:
        raise CustomHTTPException(422, message="Upload a JPG, PNG or WebP picture.", error_code="LIVE_MEDIA_TYPE")
    count = await session.scalar(select(func.count()).select_from(LiveMedia).where(LiveMedia.event_id == event.id)) or 0
    if count >= MEDIA_MAX:
        raise CustomHTTPException(409, message=f"This event's live room holds {MEDIA_MAX} pictures. Delete one to add another.", error_code="LIVE_MEDIA_FULL")
    raw = await file.read()
    if len(raw) > MEDIA_BYTES:
        raise CustomHTTPException(413, message="The picture is over 10 MB.", error_code="LIVE_MEDIA_TOO_BIG")
    key = await upload(LiveMedia.image, {"bytes": raw, "filename": file.filename or "picture"})
    width, height = await asyncio.to_thread(_size_of, raw)
    row = LiveMedia(event_id=event.id, kind=kind, image=key, width=width, height=height, created_by=user_id)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return media_out(row)


async def list_media(session, event: Events) -> list:
    rows = (await session.scalars(select(LiveMedia).where(LiveMedia.event_id == event.id).order_by(LiveMedia.created_at.desc()))).all()
    return [media_out(m) for m in rows]


async def delete_media(session, event: Events, media_id: uuid.UUID) -> None:
    gone = await session.scalar(delete(LiveMedia).where(LiveMedia.id == media_id, LiveMedia.event_id == event.id).returning(LiveMedia.id))
    if gone is None:
        raise CustomHTTPException(404, message="That picture is not in this event's live room.", error_code="LIVE_MEDIA_NOT_FOUND")
    # A theme that showed it now shows none: its version moves.
    row = await session.get(LiveThemes, event.id)
    if row is not None:
        row.updated_at = now_utc()
    await session.commit()
    changed(event.id)


# ─── The theme ───────────────────────────────────────────────────────────


def changed(event_id: int) -> None:
    """The theme or the screen's controls changed (after the commit)."""
    themes.forget(("theme", event_id))
    room.room_changed(event_id)
    room.quiz_changed(event_id)


async def _club_of(session, event_id: int):
    return await session.scalar(select(Clubs).join(Events, Events.club_id == Clubs.id).where(Events.id == event_id))


async def _build(session, event_id: int) -> tuple:
    row = await session.get(LiveThemes, event_id, populate_existing=True)
    saved = row.theme if row is not None and isinstance(row.theme, dict) else None
    club = await _club_of(session, event_id)
    club_logo = _club_logo(club)
    if saved is None:
        resolved = theme.resolve(theme.default_saved(), {}, None)
        return False, resolved, theme.version(resolved, False)
    ids = [saved.get("logo"), (saved.get("background") or {}).get("image")]
    media = await media_of(session, event_id, [i for i in ids if i and i != "club"])
    resolved = theme.resolve(saved, media, club_logo)
    return True, resolved, theme.version(resolved, True)


async def state(session, event_id: int) -> tuple:
    """(custom, the resolved theme, its version), at most a second old."""
    return await themes.remember(("theme", event_id), lambda: _build(session, event_id))


def theme_out(custom: bool, resolved: dict, version: str, warnings=None, updated_at=None) -> dict:
    return {"custom": custom, "theme": resolved, "version": version, "warnings": list(warnings or []), "updated_at": updated_at}


async def get_theme(session, event: Events) -> dict:
    themes.forget(("theme", event.id))
    custom, resolved, version = await state(session, event.id)
    row = await session.get(LiveThemes, event.id)
    warnings = theme.judge(row.theme).warnings if custom and row is not None else []
    return theme_out(custom, resolved, version, warnings, row.updated_at if custom and row is not None else None)


async def _checked(session, event: Events, body: dict):
    club = await _club_of(session, event.id)
    club_logo = _club_logo(club)
    saved, verdict = theme.clean(body, club, club_logo)
    ids = [saved.get("logo"), saved["background"].get("image")]
    wanted = [i for i in ids if i and i != "club"]
    media = await media_of(session, event.id, wanted)
    for field, ref in (("logo", saved.get("logo")), ("background.image", saved["background"].get("image"))):
        if ref and ref != "club" and ref not in media:
            raise CustomHTTPException(422, message="Pick a picture uploaded for this event.", error_code="THEME_MEDIA", errors={"field": field})
    resolved = theme.resolve(saved, media, club_logo)
    return saved, verdict, resolved


async def check_theme(session, event: Events, body: dict) -> dict:
    _saved, verdict, resolved = await _checked(session, event, body)
    return {"ok": not verdict.errors, "errors": verdict.errors, "warnings": verdict.warnings, "theme": resolved, "version": theme.version(resolved, True)}


async def put_theme(session, event: Events, body: dict) -> dict:
    saved, verdict, resolved = await _checked(session, event, body)
    if verdict.errors:
        first = verdict.errors[0]
        raise CustomHTTPException(
            422, message=first["message"], error_code="THEME_CONTRAST", errors={k: v for k, v in first.items() if k not in ("message", "code")}
        )
    row = await session.get(LiveThemes, event.id)
    if row is None:
        row = LiveThemes(event_id=event.id)
        session.add(row)
    row.theme, row.updated_at = saved, now_utc()
    await session.commit()
    changed(event.id)
    return theme_out(True, resolved, theme.version(resolved, True), verdict.warnings, row.updated_at)


async def delete_theme(session, event: Events) -> dict:
    row = await session.get(LiveThemes, event.id)
    if row is not None:
        row.theme, row.updated_at = None, now_utc()
        await session.commit()
    changed(event.id)
    return await get_theme(session, event)


async def presets(session, event: Events) -> list:
    club = await _club_of(session, event.id)
    club_logo = _club_logo(club)
    out = []
    for key in theme.PRESETS:
        saved, _ = theme.clean({"preset": key}, club, club_logo)
        out.append({"key": key, "name": theme.PRESET_NAMES[key], "theme": theme.resolve(saved, {}, club_logo)})
    return out


async def public_theme(session, event_id: int) -> dict:
    custom, resolved, version = await state(session, event_id)
    return {"custom": custom, "theme": resolved, "version": version}


# ─── The big screen's controls ───────────────────────────────────────────

SCREEN_DEFAULTS = {"mode": "auto", "results": "live", "ticker": False, "idle_slides": [], "slide_seconds": 12, "wall_size": 6}
SLIDES = ("agenda", "sponsors", "join")


def screen_control(saved: Optional[dict]) -> dict:
    return {**SCREEN_DEFAULTS, **(saved if isinstance(saved, dict) else {})}


def _sc_bad(field: str, message: str) -> CustomHTTPException:
    return CustomHTTPException(422, message=message, error_code="SCREEN_CONTROL", errors={"field": field})


def clean_control(c: dict) -> dict:
    unknown = set(c) - set(SCREEN_DEFAULTS)
    if unknown:
        raise _sc_bad(sorted(unknown)[0], f"Unknown control: {sorted(unknown)[0]}.")
    if c["mode"] not in ("auto", "quiz", "poll", "qa", "idle", "leaderboard"):
        raise _sc_bad("mode", "mode is one of: auto, quiz, poll, qa, idle, leaderboard.")
    if c["results"] not in ("live", "hidden"):
        raise _sc_bad("results", "results is live or hidden.")
    if not isinstance(c["ticker"], bool):
        raise _sc_bad("ticker", "ticker is true or false.")
    slides = c["idle_slides"]
    if not isinstance(slides, list) or len(set(slides)) != len(slides) or any(s not in SLIDES for s in slides):
        raise _sc_bad("idle_slides", "idle_slides lists agenda, sponsors and join, each at most once.")
    for field, lo, hi in (("slide_seconds", 5, 60), ("wall_size", 3, 12)):
        v = c[field]
        if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
            raise _sc_bad(field, f"{field} is {lo} to {hi}.")
    return c


async def get_control(session, event_id: int) -> dict:
    row = await session.get(LiveThemes, event_id)
    return screen_control(row.screen if row is not None else None)


async def put_control(session, event: Events, change: dict) -> dict:
    row = await session.get(LiveThemes, event.id)
    merged = clean_control(screen_control({**((row.screen if row is not None else None) or {}), **change}))
    if row is None:
        row = LiveThemes(event_id=event.id)
        session.add(row)
    row.screen = merged
    await session.commit()
    themes.forget(("theme", event.id))
    room.room_changed(event.id)
    return merged


async def agenda(session, event_id: int, now: Optional[datetime] = None, limit: int = 6) -> list:
    """The item running now and the next ones."""
    now = now or now_utc()
    rows = (
        await session.scalars(
            select(AgendaItems)
            .where(AgendaItems.event_id == event_id, AgendaItems.starts_at >= now - timedelta(hours=12))
            .order_by(AgendaItems.starts_at, AgendaItems.sort_order)
            .limit(40)
        )
    ).all()
    started = [r for r in rows if r.starts_at <= now]
    current = [r for r in started if r.ends_at is not None and r.ends_at > now]
    if not current and started and started[-1].ends_at is None:
        current = [started[-1]]
    items = current + [r for r in rows if r.starts_at > now]
    return [
        {
            "id": str(r.id),
            "title": r.title,
            "starts_at": r.starts_at,
            "ends_at": r.ends_at,
            "speaker": r.speaker,
            "location": r.location,
            "now": r in current,
        }
        for r in items[:limit]
    ]


async def sponsors(session, event_id: int) -> list:
    rows = (
        await session.scalars(
            select(EventPartners)
            .where(EventPartners.event_id == event_id, EventPartners.kind == "sponsor", EventPartners.is_deleted == False)  # noqa: E712
            .order_by(EventPartners.position, EventPartners.id)
        )
    ).all()
    out = []
    for p in rows:
        logo = p.logo if isinstance(p.logo, dict) else None
        out.append({"name": p.name, "label": p.label, "logo": {k: logo.get(k) for k in ("thumbnail", "medium", "large")} if logo else None})
    return out


def forget_for_tests() -> None:
    themes.clear()
