"""The organiser's side, /api/v1/events/{event_id}/games/... (CONTRACT §3).

Club auth like the event's other pages (the club's owner, admins and editors
write; door staff read, and download nothing: app/api/club_access/policy.py
decides from the method and path). Every write also asks the plan gate
(403 PLAN_FEATURE_LOCKED, the usual notice); reads never do, so a club that
dropped the plan still sees its data.
"""

from __future__ import annotations

import io
import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select

from app.api.deliveries import service as deliveries
from app.api.events.models import EventRegistrationsLink
from app.api.games import access, boards, bus, cards, common, hunt, tokens, views
from app.api.live import theme as live_theme
from app.api.games.common import err
from app.api.games.models import (
    CARDS,
    DRAFT,
    HUNT,
    PRESET_SHARES,
    SCHEDULED,
    TIERS,
    GameCardScores,
    GameFeed,
    GameHeroes,
    GameHuntClaims,
    GameHuntPoints,
    GameHuntScores,
    GamePlayers,
    GameReveals,
    GameSettings,
)
from app.api.games.schemas import (
    CardConfigPatch,
    ExtendIn,
    FeedPatch,
    HeroIn,
    HeroPatch,
    HuntPut,
    OrderIn,
    PointIn,
    PointPatch,
    SettingsPatch,
)
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/events/{event_id}/games", tags=["games"])

ART_MAX_BYTES = 5 * 1024 * 1024
MIN_HEROES, MAX_HEROES = 4, 20


async def _event(session, user, event_id: int):
    return await deliveries.event_for_club(session, user, event_id)


async def _plan(session, event, user) -> dict:
    return {
        "game_cards": await common.plan_allows(session, event, CARDS, actor=user),
        "game_hunt": await common.plan_allows(session, event, HUNT, actor=user),
    }


async def _gate_any(session, event, user) -> None:
    """A write that belongs to neither game (the look, the screen, the feed): the plan must have one of them."""
    plan = await _plan(session, event, user)
    if not (plan["game_cards"] or plan["game_hunt"]):
        await common.require_plan(session, event, CARDS, actor=user)


# ─── Settings ──────────────────────────────────────────────────────────


def color_warnings(s: GameSettings) -> list[dict]:
    """Text that would be hard to read on the screen (WCAG AA 4.5:1, as the live room checks); a warning, not a refusal."""
    c = views.colors_of(s)
    out = []
    if c["text"]:
        for fill in ("background", "surface"):
            if c[fill] and live_theme.contrast(c["text"], c[fill]) < live_theme.TEXT_MIN:
                out.append({"field": "colors.text", "code": "LOW_CONTRAST", "message": f"The text colour is hard to read on the {fill} colour.", "ratio": round(live_theme.contrast(c["text"], c[fill]), 2)})
    return out


def settings_out(s: GameSettings, plan: dict) -> dict:
    return {
        "cards_on": s.cards_on,
        "cards_name": s.cards_name,
        "hunt_on": s.hunt_on,
        "hunt_name": s.hunt_name,
        "theme": s.theme,
        "accent": s.accent,
        "logo_url": common.logo_of(s.logo_url),
        "board_layout": s.board_layout,
        "board_rows": s.board_rows,
        "ticker_on": s.ticker_on,
        "join_box_on": s.join_box_on,
        "colors": views.colors_of(s),
        "color_warnings": color_warnings(s),
        "screen_version": s.screen_version or 1,
        "plan": plan,
        "updated_at": s.updated_at,
    }


@router.get("/settings", summary="Both games' switches and names, and the look")
async def get_settings(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    event = await _event(session, user, event_id)
    s = await common.settings_of(session, event.id)
    if s is None:
        s = GameSettings(event_id=event.id, cards_on=False, cards_name="Hero Cards", hunt_on=False, hunt_name="Scavenger Hunt", theme="neon_arcade", board_layout="podium", board_rows=7, ticker_on=True, join_box_on=True)
    return settings_out(s, await _plan(session, event, user))


@router.patch("/settings", summary="Switch each game on or off, name it, set the look")
async def patch_settings(session: SessionDep, user: ClubAuth, event_id: int, body: SettingsPatch) -> dict:
    event = await _event(session, user, event_id)
    data = body.model_dump(exclude_unset=True)
    if any(k.startswith("cards_") for k in data):
        await common.require_plan(session, event, CARDS, actor=user)
    if any(k.startswith("hunt_") for k in data):
        await common.require_plan(session, event, HUNT, actor=user)
    if any(not k.startswith(("cards_", "hunt_")) for k in data):
        await _gate_any(session, event, user)
    s = await common.ensure_settings(session, event.id)
    await session.execute(select(GameSettings.id).where(GameSettings.id == s.id).with_for_update())
    for key, value in data.items():
        if value is None and key not in ("accent", "logo_url", "colors"):
            continue
        if key == "colors" and value is not None:
            value = {k: v.upper() for k, v in value.items() if v} or None
        setattr(s, key, value)
    s.updated_at = common.now()
    if data.get("cards_on"):
        await cards.ensure_cards(session, event.id)
    if data.get("hunt_on"):
        await hunt.ensure_hunt(session, event.id)
    await session.commit()
    bus.changed(event.id, "settings", "board", "hunt")
    return settings_out(s, await _plan(session, event, user))


@router.post("/settings/logo", summary="The event's game logo (a square picture); answers the settings")
async def upload_logo(session: SessionDep, user: ClubAuth, event_id: int, logo: UploadFile = File(...)) -> dict:
    import asyncio

    from app.core.layout import pool
    from app.core.storage import shapes
    from app.core.storage.fields import S3ImageField

    event = await _event(session, user, event_id)
    await _gate_any(session, event, user)
    if not (logo.content_type or "").startswith("image/"):
        raise err(422, "LOGO_NOT_IMAGE", "The logo must be a picture.")
    raw = await logo.read(10 * 1024 * 1024 + 1)
    field = S3ImageField(upload_to="events/games/logos/", variations=shapes.LOGO_SIZES, public=True)
    try:
        prepared = await pool.run(field.prepare, raw)
    except Exception as exc:  # noqa: BLE001 - not a picture we can read
        if getattr(exc, "status_code", None):
            raise
        raise err(422, "LOGO_NOT_IMAGE", "That is not a picture we can read.")
    s = await common.ensure_settings(session, event.id)
    s.logo_url = await asyncio.to_thread(field.store, prepared)
    s.updated_at = common.now()
    await session.commit()
    bus.changed(event.id, "settings")
    return settings_out(s, await _plan(session, event, user))


# ─── Hero Cards ────────────────────────────────────────────────────────


async def _warnings(session, event, config) -> list[dict]:
    people = int(await session.scalar(select(func.count()).select_from(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)) or 0)  # noqa: E712
    if config.preset == "hard" and people < 20:
        return [{"code": "SMALL_EVENT_HARD", "message": f"Only {people} registered: on Hard, few players will see a Rare or Legendary hero."}]
    return []


async def _config_view(session, event) -> dict:
    config = await cards.config_of(session, event.id)
    return {**cards.config_out(config), "warnings": await _warnings(session, event, config)}


@router.get("/cards/config", summary="Rules of the card game")
async def get_config(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    event = await _event(session, user, event_id)
    return await _config_view(session, event)


async def _need_a_hero_per_tier(session, event_id: int, shares: dict, *, ignoring=None, changing: Optional[tuple] = None) -> None:
    heroes = [h for h in await cards.heroes_of(session, event_id) if h.id != ignoring]
    tiers = {h.tier for h in heroes}
    if changing:
        tiers.add(changing[1])
        tiers -= set() if any(h.tier == changing[0] for h in heroes) else {changing[0]}
    missing = [t for t in TIERS if shares[t] > 0 and t not in tiers]
    if missing:
        raise err(422, "TIER_NEEDS_HERO", f"Every tier with a share needs at least one hero: add one to {missing[0].title()}.", tier=missing[0])


@router.patch("/cards/config", summary="Preset, tier shares (must total 100), Legendary minimum, switches")
async def patch_config(session: SessionDep, user: ClubAuth, event_id: int, body: CardConfigPatch) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, CARDS, actor=user)
    config = await cards.config_of(session, event.id)
    data = body.model_dump(exclude_unset=True)
    if data.get("shares") is not None:
        shares = {k: round(float(v), 2) for k, v in data["shares"].items()}
        if abs(sum(shares.values()) - 100.0) > 0.001:
            raise err(422, "SHARES_NOT_100", f"The four shares total {sum(shares.values()):g}; they must total 100.", total=round(sum(shares.values()), 2))
        preset = data.get("preset")
        same = next((p for p, v in PRESET_SHARES.items() if all(abs(v[t] - shares[t]) < 0.001 for t in TIERS)), None)
        from decimal import Decimal

        config.share_common, config.share_uncommon = Decimal(str(shares["common"])), Decimal(str(shares["uncommon"]))
        config.share_rare, config.share_legendary = Decimal(str(shares["rare"])), Decimal(str(shares["legendary"]))
        config.preset = preset if preset in (same, "custom") and preset else (same or "custom")
    elif data.get("preset") in PRESET_SHARES:
        cards.apply_preset(config, data["preset"])
    elif data.get("preset") == "custom":
        config.preset = "custom"
    await _need_a_hero_per_tier(session, event.id, cards.shares_of(config))
    for key in ("legendary_floor", "pin_required", "collect_on", "two_way", "show_holders"):
        if data.get(key) is not None:
            setattr(config, key, data[key])
    config.updated_at = common.now()
    await session.commit()
    return await _config_view(session, event)


def _heroes_view(heroes, holders: dict, reveals: dict) -> list[dict]:
    return [{**cards.hero_out(h, holders=holders.get(h.id, 0), reveals=reveals.get(h.id, 0)), "revealed": reveals.get(h.id, 0) > 0} for h in heroes]


async def _reveal_counts(session, event_id: int) -> dict:
    rows = await session.execute(select(GameReveals.hero_id, func.count()).where(GameReveals.event_id == event_id).group_by(GameReveals.hero_id))
    return {h: int(n) for h, n in rows.all()}


async def _revealed_ids(session, event_id: int) -> set:
    return set(await _reveal_counts(session, event_id))


async def _heroes_now(session, event_id: int) -> list[dict]:
    return _heroes_view(await cards.heroes_of(session, event_id), await cards.hero_counts(session, event_id), await _reveal_counts(session, event_id))


@router.get("/cards/heroes", summary="The hero list, in card-number order")
async def list_heroes(session: SessionDep, user: ClubAuth, event_id: int) -> list[dict]:
    event = await _event(session, user, event_id)
    return await _heroes_now(session, event.id)


@router.post("/cards/heroes", summary="Add a hero (4 to 20 in all)", status_code=201)
async def add_hero(session: SessionDep, user: ClubAuth, event_id: int, body: HeroIn) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, CARDS, actor=user)
    await cards.ensure_cards(session, event.id)
    heroes = await cards.heroes_of(session, event.id)
    if len(heroes) >= MAX_HEROES:
        raise err(422, "MAX_HEROES", f"A deck has at most {MAX_HEROES} heroes.")
    hero = GameHeroes(id=uuid.uuid4(), event_id=event.id, number=max((h.number for h in heroes), default=0) + 1, name=body.name.strip(), tagline=body.tagline.strip(), tier=body.tier, emblem_key=body.emblem_key)
    session.add(hero)
    await session.commit()
    return cards.hero_out(hero)


async def _hero(session, event, hero_id: uuid.UUID) -> GameHeroes:
    hero = await session.scalar(select(GameHeroes).where(GameHeroes.id == hero_id, GameHeroes.event_id == event.id))
    if hero is None:
        raise err(404, "HERO_NOT_FOUND", "No such hero.")
    return hero


@router.put("/cards/heroes/order", summary="Reorder the heroes: every id once, in the new order (renumbers the cards)")
async def order_heroes(session: SessionDep, user: ClubAuth, event_id: int, body: OrderIn) -> list[dict]:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, CARDS, actor=user)
    heroes = {h.id: h for h in await cards.heroes_of(session, event.id)}
    if len(set(body.ids)) != len(body.ids) or set(body.ids) != set(heroes):
        raise err(422, "ORDER_MISMATCH", "List every hero once, in the new order. Reload and try again.")
    for i, hid in enumerate(body.ids, start=1):
        heroes[hid].number = i
    await session.commit()
    return await _heroes_now(session, event.id)


@router.patch("/cards/heroes/{hero_id}", summary="Edit a hero; a revealed hero keeps its tier")
async def patch_hero(session: SessionDep, user: ClubAuth, event_id: int, hero_id: uuid.UUID, body: HeroPatch) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, CARDS, actor=user)
    hero = await _hero(session, event, hero_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("tier") and data["tier"] != hero.tier:
        if hero.id in await _revealed_ids(session, event.id):
            raise err(409, "HERO_REVEALED", "A hero someone has already unlocked cannot move to another tier.")
        config = await cards.config_of(session, event.id)
        await _need_a_hero_per_tier(session, event.id, cards.shares_of(config), changing=(hero.tier, data["tier"]), ignoring=hero.id)
    for key, value in data.items():
        if value is None and key != "emblem_key":
            continue
        setattr(hero, key, value.strip() if isinstance(value, str) and key != "emblem_key" else value)
    hero.updated_at = common.now()
    await session.commit()
    return cards.hero_out(hero)


@router.delete("/cards/heroes/{hero_id}", summary="Remove a hero nobody has unlocked")
async def delete_hero(session: SessionDep, user: ClubAuth, event_id: int, hero_id: uuid.UUID) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, CARDS, actor=user)
    hero = await _hero(session, event, hero_id)
    if hero.id in await _revealed_ids(session, event.id):
        raise err(409, "HERO_REVEALED", "A hero someone holds cannot be deleted.")
    if len(await cards.heroes_of(session, event.id)) <= MIN_HEROES:
        raise err(422, "MIN_HEROES", f"A deck needs at least {MIN_HEROES} heroes.")
    config = await cards.config_of(session, event.id)
    await _need_a_hero_per_tier(session, event.id, cards.shares_of(config), ignoring=hero.id)
    await session.delete(hero)
    await session.flush()
    for i, h in enumerate(await cards.heroes_of(session, event.id), start=1):
        h.number = i
    await session.commit()
    return {"deleted": True}


def _check_art(raw: bytes) -> None:
    """Portrait 3:4, at least 900 x 1200, at most 5 MB, PNG, JPG or WebP."""
    from PIL import Image, UnidentifiedImageError

    if len(raw) > ART_MAX_BYTES:
        raise err(422, "ART_TOO_BIG", "The picture is over 5 MB.")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            fmt, (w, h) = im.format, im.size
    except (UnidentifiedImageError, OSError):
        raise err(422, "ART_NOT_IMAGE", "That is not a picture we can read.")
    if fmt not in ("PNG", "JPEG", "WEBP"):
        raise err(422, "ART_FORMAT", "Use a PNG, JPG or WebP picture.")
    if w < 900 or h < 1200:
        raise err(422, "ART_TOO_SMALL", f"The picture is {w} x {h}; it must be at least 900 x 1200.")
    if abs(w / h - 0.75) > 0.03:
        raise err(422, "ART_SHAPE", "Cards are portrait 3:4 (for example 900 x 1200).")


@router.post("/cards/heroes/{hero_id}/art", summary="Upload a hero's artwork (portrait 3:4, 900 x 1200 or more, up to 5 MB)")
async def hero_art(session: SessionDep, user: ClubAuth, event_id: int, hero_id: uuid.UUID, file: UploadFile = File(...)) -> dict:
    from app.core.storage.fields import upload

    event = await _event(session, user, event_id)
    await common.require_plan(session, event, CARDS, actor=user)
    hero = await _hero(session, event, hero_id)
    raw = await file.read(ART_MAX_BYTES + 1)
    _check_art(raw)
    old = hero.art if isinstance(hero.art, dict) else None
    hero.art = await upload(GameHeroes.art, {"bytes": raw, "filename": file.filename or "hero.png"})
    hero.updated_at = common.now()
    await session.commit()
    await session.refresh(hero)
    if old is not None and hasattr(old, "delete"):
        old.delete()
    return cards.hero_out(hero)


@router.delete("/cards/heroes/{hero_id}/art", summary="Go back to the built-in emblem")
async def clear_art(session: SessionDep, user: ClubAuth, event_id: int, hero_id: uuid.UUID) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, CARDS, actor=user)
    hero = await _hero(session, event, hero_id)
    hero.art = None
    await session.commit()
    return cards.hero_out(hero)


@router.get("/cards/live", summary="The live numbers of the card game")
async def cards_live(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    event = await _event(session, user, event_id)
    return await cards.live_numbers(session, event)


# ─── Scavenger Hunt ────────────────────────────────────────────────────


def point_out(p: GameHuntPoints, scans: int = 0, *, url: bool = True) -> dict:
    out = {"id": p.id, "number": p.number, "label": p.label, "clue": p.clue, "zone": p.zone, "base_points": p.base_points, "hint": p.hint, "scans": scans}
    if url:
        out["code"] = tokens.point(p.event_id, p.id)
    return out


async def _hunt_view(session, event) -> dict:
    h = await hunt.hunt_of(session, event.id)
    if h is None:
        raise err(404, "GAME_NOT_ON", "The hunt is not set up for this event. Switch it on first.")
    h = await hunt.tick(session, h)
    counts = dict((pid, int(n)) for pid, n in (await session.execute(select(GameHuntClaims.point_id, func.count()).where(GameHuntClaims.event_id == event.id).group_by(GameHuntClaims.point_id))).all())
    return {**hunt.hunt_out(h), "points": [point_out(p, counts.get(p.id, 0)) for p in await hunt.points_of(session, event.id)]}


@router.get("/hunt", summary="The hunt: settings, state and its QR points")
async def get_hunt(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    return await _hunt_view(session, await _event(session, user, event_id))


@router.put("/hunt", summary="Duration, start time, mode, early bonus, hint cost")
async def put_hunt(session: SessionDep, user: ClubAuth, event_id: int, body: HuntPut) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, HUNT, actor=user)
    h = await hunt.ensure_hunt(session, event.id)
    data = body.model_dump(exclude_unset=True)
    if "mode" in data and data["mode"] != h.mode:
        if await session.scalar(select(func.count()).select_from(GameHuntClaims).where(GameHuntClaims.event_id == event.id)):
            raise err(409, "HUNT_HAS_CLAIMS", "Players have already found points: the mode cannot change now.")
    for key in ("duration_minutes", "mode", "bonus_n", "bonus_pts", "hint_cost"):
        if data.get(key) is not None:
            setattr(h, key, data[key])
    if h.status in (DRAFT, SCHEDULED):
        if data.get("starts_at") is not None:
            h.starts_at, h.status = data["starts_at"], SCHEDULED
        elif data.get("clear_start"):
            h.starts_at, h.status = None, DRAFT
        if h.status == SCHEDULED and h.starts_at is not None:
            h.ends_at = h.starts_at + timedelta(minutes=h.duration_minutes)
    elif data.get("duration_minutes") is not None or data.get("starts_at") is not None:
        raise err(409, "HUNT_STARTED", "The hunt has started: use extend to change its time.")
    h.updated_at = common.now()
    await session.commit()
    bus.changed(event.id, "hunt")
    return await _hunt_view(session, event)


async def _points_open(session, event) -> None:
    h = await hunt.hunt_of(session, event.id)
    if h is not None:
        h = await hunt.tick(session, h)
        if h.status not in (DRAFT, SCHEDULED):
            raise err(409, "HUNT_STARTED", "Points cannot be added or removed once the hunt has started.")


@router.post("/hunt/points", summary="Add a QR point", status_code=201)
async def add_point(session: SessionDep, user: ClubAuth, event_id: int, body: PointIn) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, HUNT, actor=user)
    await hunt.ensure_hunt(session, event.id)
    await _points_open(session, event)
    points = await hunt.points_of(session, event.id)
    p = GameHuntPoints(id=uuid.uuid4(), event_id=event.id, number=max((x.number for x in points), default=0) + 1, label=body.label.strip(), clue=body.clue.strip(), zone=(body.zone or None), base_points=body.base_points, hint=(body.hint or None))
    session.add(p)
    await session.commit()
    return point_out(p)


@router.patch("/hunt/points/{point_id}", summary="Edit a QR point")
async def patch_point(session: SessionDep, user: ClubAuth, event_id: int, point_id: uuid.UUID, body: PointPatch) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, HUNT, actor=user)
    p = await _point(session, event, point_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None and key not in ("zone", "hint"):
            continue
        setattr(p, key, (value.strip() or None) if isinstance(value, str) and key in ("zone", "hint") else value)
    p.updated_at = common.now()
    await session.commit()
    return point_out(p)


async def _point(session, event, point_id) -> GameHuntPoints:
    p = await session.scalar(select(GameHuntPoints).where(GameHuntPoints.id == point_id, GameHuntPoints.event_id == event.id))
    if p is None:
        raise err(404, "POINT_NOT_FOUND", "No such QR point.")
    return p


@router.delete("/hunt/points/{point_id}", summary="Remove a QR point (before the hunt starts)")
async def delete_point(session: SessionDep, user: ClubAuth, event_id: int, point_id: uuid.UUID) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, HUNT, actor=user)
    p = await _point(session, event, point_id)
    await _points_open(session, event)
    await session.delete(p)
    await session.flush()
    for i, x in enumerate(await hunt.points_of(session, event.id), start=1):
        x.number = i
    await session.commit()
    return {"deleted": True}


async def _action(session, user, event_id: int, name: str, minutes: Optional[int] = None) -> dict:
    event = await _event(session, user, event_id)
    await common.require_plan(session, event, HUNT, actor=user)
    await hunt.act(session, event, name, minutes)
    return await _hunt_view(session, event)


@router.post("/hunt/start", summary="Start the hunt now")
async def hunt_start(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    return await _action(session, user, event_id, "start")


@router.post("/hunt/pause", summary="Pause the hunt (phones say Hunt paused)")
async def hunt_pause(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    return await _action(session, user, event_id, "pause")


@router.post("/hunt/resume", summary="Resume a paused hunt; the paused time is given back")
async def hunt_resume(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    return await _action(session, user, event_id, "resume")


@router.post("/hunt/end", summary="End the hunt now; the final board locks")
async def hunt_end(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    return await _action(session, user, event_id, "end")


@router.post("/hunt/extend", summary="Add minutes; an ended hunt reopens")
async def hunt_extend(session: SessionDep, user: ClubAuth, event_id: int, body: ExtendIn) -> dict:
    return await _action(session, user, event_id, "extend", body.minutes)


@router.get("/hunt/live", summary="Scan counts per point and players flagged for fast claims")
async def hunt_live(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    return await hunt.live_numbers(session, await _event(session, user, event_id))


@router.get("/hunt/points.pdf", summary="Print the QR codes: one per page with its label and number, never the clue")
async def points_pdf(session: SessionDep, user: ClubAuth, event_id: int) -> Response:
    event = await _event(session, user, event_id)
    points = await hunt.points_of(session, event.id)
    if not points:
        raise err(422, "NO_POINTS", "Add at least one QR point first.")
    from app.api.games import pdf

    return Response(
        pdf.points_pdf(event.name, [(p.number, p.label, tokens.point(event.id, p.id)) for p in points]),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="hunt-qr-codes.pdf"', "Cache-Control": "no-store"},
    )


# ─── Screens, feed, players, exports ───────────────────────────────────


async def _screen_view(session, event) -> dict:
    s = await common.ensure_settings(session, event.id)
    token = tokens.screen(event.id, s.screen_version)
    from app.core import public_urls

    return {"url": public_urls.site_url(f"/screen/{token}"), "token": token, "version": s.screen_version}


@router.get("/screen", summary="The venue screen's address")
async def get_screen(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    event = await _event(session, user, event_id)
    out = await _screen_view(session, event)
    await session.commit()
    return out


@router.post("/screen/rotate", summary="A new venue screen address; the old one stops working")
async def rotate_screen(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    event = await _event(session, user, event_id)
    await _gate_any(session, event, user)
    s = await common.ensure_settings(session, event.id)
    s.screen_version += 1
    await session.commit()
    return await _screen_view(session, event)


@router.get("/feed", summary="The ticker's events, hidden ones too")
async def get_feed(session: SessionDep, user: ClubAuth, event_id: int, limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    event = await _event(session, user, event_id)
    return await boards.feed(session, event.id, limit, include_hidden=True)


@router.patch("/feed/{feed_id}", summary="Hide or show one ticker event")
async def patch_feed(session: SessionDep, user: ClubAuth, event_id: int, feed_id: uuid.UUID, body: FeedPatch) -> dict:
    event = await _event(session, user, event_id)
    await _gate_any(session, event, user)
    row = await session.scalar(select(GameFeed).where(GameFeed.id == feed_id, GameFeed.event_id == event.id))
    if row is None:
        raise err(404, "NOT_FOUND", "No such ticker event.")
    row.hidden = body.hidden
    await session.commit()
    bus.changed(event.id, "feed")
    return {"id": row.id, "hidden": row.hidden}


@router.get("/players", summary="Players: who has signed in, their games, opt-outs")
async def list_players(
    session: SessionDep, user: ClubAuth, event_id: int, q: Optional[str] = Query(None, max_length=60), public: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
) -> dict:
    event = await _event(session, user, event_id)
    query = (
        select(GamePlayers, EventRegistrationsLink.full_name, EventRegistrationsLink.email, GameCardScores.distinct_heroes, GameHuntScores.total)
        .join(EventRegistrationsLink, EventRegistrationsLink.id == GamePlayers.registration_id)
        .outerjoin(GameCardScores, GameCardScores.registration_id == GamePlayers.registration_id)
        .outerjoin(GameHuntScores, GameHuntScores.registration_id == GamePlayers.registration_id)
        .where(GamePlayers.event_id == event.id)
    )
    if q:
        query = query.where(EventRegistrationsLink.full_name.ilike(f"%{q.strip()}%"))
    if public is not None:
        query = query.where(GamePlayers.public == public)
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (await session.execute(query.order_by(GamePlayers.player_no).limit(limit).offset(offset))).all()
    now = common.now()
    return {
        "total": total,
        "items": [
            {
                "registration_id": p.registration_id,
                "player_no": p.player_no,
                "name": name,
                "email": email,
                "public": p.public,
                "heroes": int(heroes or 0),
                "points": int(points or 0),
                "pin_locked": bool(p.pin_locked_until and p.pin_locked_until > now),
                "last_seen_at": p.last_seen_at,
            }
            for p, name, email, heroes, points in rows
        ],
    }


@router.post("/players/{registration_id}/reset-pin", summary="Help desk: a new PIN and badge code for a guest")
async def reset_pin(session: SessionDep, user: ClubAuth, event_id: int, registration_id: uuid.UUID) -> dict:
    event = await _event(session, user, event_id)
    await _gate_any(session, event, user)
    return await access.reset_pin(session, event, registration_id)


@router.get("/players/{registration_id}/code", summary="A guest's badge QR address and PIN (the help desk, a reprint)")
async def player_code(session: SessionDep, user: ClubAuth, event_id: int, registration_id: uuid.UUID) -> dict:
    event = await _event(session, user, event_id)
    reg = await common.registration_of(session, event.id, registration_id)
    if reg is None:
        raise err(404, "NOT_REGISTERED", "No such guest on this event.")
    player = await session.scalar(select(GamePlayers).where(GamePlayers.registration_id == reg.id))
    return tokens.code_of(event.id, reg.id, player.pin_version if player else 0)


@router.get("/export.csv", summary="A board for prizes: the top 20, or all with all=1")
async def export_csv(session: SessionDep, user: ClubAuth, event_id: int, game: str = Query("cards"), all: int = Query(0)) -> Response:
    event = await _event(session, user, event_id)
    if game not in (CARDS, HUNT):
        raise err(422, "BAD_GAME", "game must be cards or hunt")
    data = await boards.csv_of(session, event.id, game, bool(all))
    return Response(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{game}-board.csv"'})
