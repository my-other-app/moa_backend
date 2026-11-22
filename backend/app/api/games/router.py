"""The players' and the venue screens' routes, /api/v1/games (CONTRACT §3).

Player calls carry `Authorization: Game <session token>`. The landing, the
sign-in routes, the public facts, the board and the streams need no login.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.api.games import access, boards, cards, common, hunt, stream, tokens, views
from app.api.games.common import Ctx, err
from app.api.games.models import CARDS, HUNT, GameCardScores, GameHuntScores, GameReveals
from app.api.games.schemas import BadgeIn, CollectIn, HintIn, MePatch, PinIn, ScanIn
from app.core.auth.dependencies import GuestAuth, OptionalAuth
from app.core.rate_limit import account_or_address, limiter
from app.db.core import SessionDep

router = APIRouter(prefix="/games", tags=["games"])


async def _player(request: Request, session: SessionDep, event_id: int) -> Ctx:
    return await common.authenticate(session, request.headers.get("Authorization"), event_id)


Player = Annotated[Ctx, Depends(_player)]


async def _screen_event(session, token: str):
    read = tokens.read_screen(token)
    if read is None:
        raise err(404, "SCREEN_NOT_FOUND", "This screen link is not valid.")
    event = await common.event_of(session, read[0])
    settings = await common.settings_of(session, event.id)
    # Rotating the link makes the old one stop at once.
    if settings is None or settings.screen_version != read[1]:
        raise err(404, "SCREEN_NOT_FOUND", "This screen link has been replaced.")
    return event, settings


# ─── Venue screens (before the /{event_id} routes) ─────────────────────


@router.get("/screen/{screen_token}", summary="A venue screen: the look, both boards, the ticker, the hunt's timer")
@limiter.limit("120/minute")
async def screen_get(request: Request, session: SessionDep, screen_token: str) -> dict:
    event, settings = await _screen_event(session, screen_token)
    return await views.screen(session, event, settings)


@router.get("/screen/{screen_token}/stream", summary="A venue screen's server-sent events")
async def screen_stream(request: Request, session: SessionDep, screen_token: str):
    event, _ = await _screen_event(session, screen_token)
    await session.commit()
    return stream.response(event.id, request.headers.get("last-event-id"))


# ─── Signing in ────────────────────────────────────────────────────────


@router.post("/session/badge", summary="What a badge QR opens: the event, the games, whether a PIN is asked")
@limiter.limit("600/minute")
async def session_badge(request: Request, session: SessionDep, body: BadgeIn) -> dict:
    return await access.badge_landing(session, body.token)


@router.post("/session/pin", summary="Badge + PIN: a game session; ends this player's older sessions")
@limiter.limit("600/minute")
async def session_pin(request: Request, session: SessionDep, body: PinIn) -> dict:
    return await access.pin_sign_in(session, body.token, body.pin)


@router.post("/{event_id}/session", summary="A session for the signed-in account's checked-in registration (no PIN)")
@limiter.limit("120/minute", key_func=account_or_address)
async def session_account(request: Request, session: SessionDep, event_id: int, user: GuestAuth) -> dict:
    return await access.account_sign_in(session, event_id, user)


# ─── Public facts ──────────────────────────────────────────────────────


@router.get("/{event_id}/public", summary="What the landing pages need; `me` for a signed-in account")
@limiter.limit("300/minute")
async def public(request: Request, session: SessionDep, event_id: int, user: OptionalAuth = None) -> dict:
    event = await common.event_of(session, event_id)
    settings = await common.settings_of(session, event.id)
    out = await views.public(session, event, settings)
    out["me"] = await access.can_play(session, event, settings, user)
    return out


@router.get("/{event_id}/board", summary="A leaderboard: rows {rank, name, score, reached_at, move} and the caller's own rank")
@limiter.limit("300/minute")
async def board(request: Request, session: SessionDep, event_id: int, game: str = Query("cards"), limit: int = Query(10, ge=1, le=50)) -> dict:
    event = await common.event_of(session, event_id)
    settings = await common.settings_of(session, event.id)
    kind = game if game in (CARDS, HUNT) else None
    if kind is None:
        raise err(422, "BAD_GAME", "game must be cards or hunt")
    await common.require_active(session, event, settings, kind)
    me = None
    if request.headers.get("Authorization", "").lower().startswith("game "):
        try:
            me = (await common.authenticate(session, request.headers.get("Authorization"), event_id)).reg.id
        except Exception:  # noqa: BLE001 - a stale session reads the public board
            me = None
    return await boards.board(session, event.id, kind, limit, me)


@router.get("/{event_id}/stream", summary="A player's server-sent events: board, feed, hunt, settings")
async def player_stream(request: Request, session: SessionDep, event_id: int):
    event = await common.event_of(session, event_id)
    await session.commit()
    return stream.response(event.id, request.headers.get("last-event-id"))


# ─── The player ────────────────────────────────────────────────────────


async def _me(session, ctx: Ctx) -> dict:
    event, settings, reg, player = ctx.event, ctx.settings, ctx.reg, ctx.player
    out = {
        "player": {"name": common.display_name(reg.full_name, player.public, player.player_no), "player_no": player.player_no, "public": player.public},
        "cards": None,
        "hunt": None,
    }
    if await common.active(session, event, settings, CARDS):
        reveal = await session.scalar(select(GameReveals).where(GameReveals.registration_id == reg.id))
        score = await session.scalar(select(GameCardScores).where(GameCardScores.registration_id == reg.id))
        rank = await boards.rank_of(session, event.id, CARDS, reg.id)
        hero = None
        if reveal is not None:
            from app.api.games.models import GameHeroes

            hero = {**cards.hero_out(await session.get(GameHeroes, reveal.hero_id)), "tier": reveal.tier}
        out["cards"] = {
            "name": settings.cards_name,
            "revealed": hero,
            "deck_count": score.distinct_heroes if score else 0,
            "total": len(await cards.heroes_of(session, event.id)),
            "rank": rank["rank"] if rank else None,
            "of": rank["of"] if rank else await boards.count_of(session, event.id, CARDS),
        }
    if await common.active(session, event, settings, HUNT):
        h = await hunt.hunt_of(session, event.id)
        if h is not None:
            h = await hunt.tick(session, h)
        score = await session.scalar(select(GameHuntScores).where(GameHuntScores.registration_id == reg.id))
        rank = await boards.rank_of(session, event.id, HUNT, reg.id)
        out["hunt"] = {
            "name": settings.hunt_name,
            "status": h.status if h else "draft",
            "points": score.total if score else 0,
            "found": score.found if score else 0,
            "rank": rank["rank"] if rank else None,
            "of": rank["of"] if rank else await boards.count_of(session, event.id, HUNT),
        }
    return out


@router.get("/{event_id}/me", summary="The player: name, number, and where they stand in each game")
@limiter.limit("240/minute", key_func=lambda r: (r.headers.get("authorization") or "")[:60] or "anon")
async def me(request: Request, session: SessionDep, event_id: int, ctx: Player) -> dict:
    return await _me(session, ctx)


@router.patch("/{event_id}/me", summary="Show the player's name on the boards, or as Player #N")
async def patch_me(request: Request, session: SessionDep, event_id: int, body: MePatch, ctx: Player) -> dict:
    from sqlalchemy import update

    from app.api.games.models import GamePlayers

    await session.execute(update(GamePlayers).where(GamePlayers.id == ctx.player.id).values(public=body.public))
    await session.commit()
    await session.refresh(ctx.player)
    common.told(event_id, "board")
    return await _me(session, ctx)


@router.get("/{event_id}/me/code", summary="The player's own badge QR address and PIN, for others to scan")
async def my_code(request: Request, session: SessionDep, event_id: int, ctx: Player) -> dict:
    if not (await common.active(session, ctx.event, ctx.settings, CARDS) or await common.active(session, ctx.event, ctx.settings, HUNT)):
        raise err(404, "GAME_NOT_ON", "No game is on for this event.")
    return tokens.code_of(event_id, ctx.reg.id, ctx.player.pin_version)


# ─── Hero Cards ────────────────────────────────────────────────────────


@router.post("/{event_id}/cards/reveal", summary="Unlock the player's hero: once, final; a repeat answers the same hero")
async def reveal(request: Request, session: SessionDep, event_id: int, ctx: Player) -> dict:
    return await cards.reveal(session, ctx)


@router.get("/{event_id}/cards/deck", summary="The deck: held heroes, locked places, progress, rank")
async def deck(request: Request, session: SessionDep, event_id: int, ctx: Player) -> dict:
    return await cards.deck(session, ctx)


@router.post("/{event_id}/cards/collect", summary="Scan another player's badge QR to collect their hero")
async def collect(request: Request, session: SessionDep, event_id: int, body: CollectIn, ctx: Player) -> dict:
    return await cards.retry_deadlocks(session, lambda: cards.collect(session, ctx, body.token))


# ─── Scavenger Hunt ────────────────────────────────────────────────────


@router.get("/{event_id}/hunt", summary="The hunt: state, clock, the player's clue(s), points and rank")
async def hunt_get(request: Request, session: SessionDep, event_id: int, ctx: Player) -> dict:
    return await hunt.view(session, ctx)


@router.post("/{event_id}/hunt/scan", summary="Scan a QR point")
async def hunt_scan(request: Request, session: SessionDep, event_id: int, body: ScanIn, ctx: Player) -> dict:
    return await hunt.scan(session, ctx, body.token)


@router.post("/{event_id}/hunt/hint", summary="Buy the hint for the current clue (or a given point in free mode)")
async def hunt_hint(request: Request, session: SessionDep, event_id: int, body: HintIn, ctx: Player) -> dict:
    return await hunt.hint(session, ctx, body.point_id)
