"""What the landing pages, the players' pages and the venue screens read:
public facts only (names as "Anjali R.", never an id or an address)."""

from __future__ import annotations

from typing import Optional

from app.api.games import boards, common, hunt
from app.api.games.models import CARDS, HUNT, GameSettings


async def game_block(session, event, settings: Optional[GameSettings], kind: str) -> Optional[dict]:
    if not await common.active(session, event, settings, kind):
        return None
    return {"on": True, "name": common.game_name(settings, kind)}


def look_of(settings: Optional[GameSettings]) -> dict:
    s = settings
    return {
        "theme": s.theme if s else "neon_arcade",
        "accent": s.accent if s else None,
        "logo_url": common.logo_of(s.logo_url) if s else None,
        "board_layout": s.board_layout if s else "podium",
        "board_rows": s.board_rows if s else 7,
        "ticker_on": s.ticker_on if s else True,
        "join_box_on": s.join_box_on if s else True,
        "colors": colors_of(s),
    }


COLOR_KEYS = ("background", "surface", "text", "accent", "gold", "silver", "bronze")


def colors_of(s: Optional[GameSettings]) -> dict:
    """The saved custom colours, every key present (null = the theme's)."""
    saved = (s.colors if s is not None else None) or {}
    return {k: saved.get(k) for k in COLOR_KEYS}


def screen_rows(settings: Optional[GameSettings]) -> int:
    """How many rows a venue screen shows per board: board_rows, or everyone (0) up to SCREEN_MAX."""
    rows = settings.board_rows if settings is not None else 7
    return boards.SCREEN_MAX if not rows else max(rows, 10)


def event_block(event) -> dict:
    return {"id": event.id, "name": event.name, "slug": event.slug}


async def public(session, event, settings: Optional[GameSettings]) -> dict:
    """GET /games/{event_id}/public."""
    look = look_of(settings)
    return {
        "event": event_block(event),
        "cards": await game_block(session, event, settings, CARDS),
        "hunt": await game_block(session, event, settings, HUNT),
        "theme": look["theme"],
        "logo_url": look["logo_url"],
        "accent": look["accent"],
    }


async def settings_body(session, event, settings: Optional[GameSettings]) -> dict:
    return {
        **look_of(settings),
        "event": event_block(event),
        "cards": await game_block(session, event, settings, CARDS),
        "hunt": await game_block(session, event, settings, HUNT),
        "version": settings.screen_version if settings else 1,
    }


async def hunt_body(session, event, settings: Optional[GameSettings]) -> Optional[dict]:
    """The hunt's state and timer, for a screen or a phone's header."""
    if not await common.active(session, event, settings, HUNT):
        return None
    h = await hunt.hunt_of(session, event.id)
    if h is None:
        return None
    h = await hunt.tick(session, h)
    return {k: v for k, v in hunt.hunt_out(h).items() if k in ("status", "starts_at", "ends_at", "paused_at", "server_now", "duration_minutes", "mode")}


async def board_body(session, event, settings: Optional[GameSettings], limit: int = 10, cap: int = boards.MAX_LIMIT) -> dict:
    return {
        "cards": await boards.board(session, event.id, CARDS, limit, cap=cap) if await common.active(session, event, settings, CARDS) else None,
        "hunt": await boards.board(session, event.id, HUNT, limit, cap=cap) if await common.active(session, event, settings, HUNT) else None,
    }


async def feed_body(session, event, settings: Optional[GameSettings]) -> list[dict]:
    if settings is not None and not settings.ticker_on:
        return []
    return await boards.feed(session, event.id, 3)


async def screen(session, event, settings: Optional[GameSettings]) -> dict:
    """GET /games/screen/{token}: the look, both boards, the ticker and the hunt's timer."""
    return {
        **await settings_body(session, event, settings),
        # The whole board the screen shows (the stream's `board` carries the top
        # 10 only, so a screen showing more re-reads this when one arrives).
        "boards": await board_body(session, event, settings, screen_rows(settings), cap=boards.SCREEN_MAX),
        "feed": await feed_body(session, event, settings),
        "hunt_timer": await hunt_body(session, event, settings),
        "server_now": common.now(),
        # The logo strip along the screen's foot (old screens ignore both).
        "club": await club_of(session, event),
        "partners": await partners_of(session, event),
    }


#: The most partner logos one screen shows, in the event page's order.
SCREEN_PARTNERS_MAX = 12


async def club_of(session, event) -> Optional[dict]:
    """The host club: its name and the logo the event page shows."""
    from app.api.clubs.models import Clubs
    from app.api.event_partners.service import _logo_url

    if not event.club_id:
        return None
    club = await session.get(Clubs, event.club_id)
    if club is None:
        return None
    return {"name": club.name, "logo_url": _logo_url(club.logo)}


async def partners_of(session, event) -> list[dict]:
    """The event's sponsors then partners, as on its page; only those with a logo."""
    from app.api.event_partners.service import _logo_url, list_rows

    out = []
    for p in await list_rows(session, event.id):
        url = _logo_url(p.logo)
        if url:
            out.append({"name": p.name, "logo_url": url, "kind": p.kind, "label": p.label})
        if len(out) >= SCREEN_PARTNERS_MAX:
            break
    return out


async def topic(session, event, settings, name: str):
    if name == "board":
        return await board_body(session, event, settings)
    if name == "feed":
        return await feed_body(session, event, settings)
    if name == "hunt":
        return await hunt_body(session, event, settings)
    return await settings_body(session, event, settings)
