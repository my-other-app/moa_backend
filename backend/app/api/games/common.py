"""What every part of the games shares: the event and its settings, the plan
gate, who may play, the player's number and name, the feed."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.checkins.models import CheckIns
from app.api.events.models import EventRegistrationsLink, Events
from app.api.games import bus, tokens
from app.api.games.models import CARDS, HUNT, GameFeed, GamePlayers, GameSettings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

FEATURE = {CARDS: "game_cards", HUNT: "game_hunt"}
#: A session outlives the event by this long.
SESSION_TAIL = timedelta(hours=12)
#: How often a signed-in player's "seen" time is written.
SEEN_EVERY = timedelta(seconds=30)
LOCK_MINUTES = 10
PIN_TRIES = 5


def now() -> datetime:
    return datetime.now(timezone.utc)


def err(status: int, code: str, message: str, **errors) -> CustomHTTPException:
    return CustomHTTPException(status, message=message, error_code=code, errors=errors or None)


# ─── The event, its settings and the plan ──────────────────────────────


async def event_of(session, event_id: int) -> Events:
    event = await session.scalar(select(Events).where(Events.id == event_id, Events.is_deleted == False))  # noqa: E712
    if event is None:
        raise err(404, "EVENT_NOT_FOUND", "Event not found")
    return event


def event_end(event: Events) -> datetime:
    start = event.event_datetime
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start + timedelta(hours=float(event.duration or 0))


async def settings_of(session, event_id: int) -> Optional[GameSettings]:
    return await session.scalar(select(GameSettings).where(GameSettings.event_id == event_id))


async def ensure_settings(session, event_id: int) -> GameSettings:
    """The event's settings, made (both games off) on first use."""
    row = await settings_of(session, event_id)
    if row is None:
        await session.execute(pg_insert(GameSettings).values(id=uuid.uuid4(), event_id=event_id).on_conflict_do_nothing(index_elements=["event_id"]))
        await session.flush()
        row = await settings_of(session, event_id)
    return row


async def plan_allows(session, event: Events, kind: str, *, actor=None) -> bool:
    from app.api.subscriptions import gates

    return await gates.allows(session, event.club_id, FEATURE[kind], actor=actor, event=event)


async def require_plan(session, event: Events, kind: str, *, actor=None) -> None:
    """The existing plan gate (403 PLAN_FEATURE_LOCKED, with the plan to upgrade to)."""
    from app.api.subscriptions import gates

    await gates.require_feature(session, event.club_id, FEATURE[kind], actor=actor, event=event)


async def active(session, event: Events, settings: Optional[GameSettings], kind: str) -> bool:
    """The game is switched on for the event and the club's plan has it."""
    if settings is None or not getattr(settings, f"{kind}_on"):
        return False
    return await plan_allows(session, event, kind)


async def require_active(session, event: Events, settings: Optional[GameSettings], kind: str) -> None:
    if settings is None or not getattr(settings, f"{kind}_on"):
        raise err(404, "GAME_NOT_ON", "This game is not on for this event.")
    await require_plan(session, event, kind)


def game_name(settings: GameSettings, kind: str) -> str:
    return settings.cards_name if kind == CARDS else settings.hunt_name


# ─── Players ───────────────────────────────────────────────────────────


@dataclass
class Ctx:
    """The signed-in player on one request."""

    event: Events
    settings: Optional[GameSettings]
    reg: EventRegistrationsLink
    player: GamePlayers


def display_name(full_name: Optional[str], public: bool, player_no: int) -> str:
    """First name and the last initial ("Anjali R."); "Player #247" when they opted out."""
    parts = (full_name or "").split()
    if not public or not parts:
        return f"Player #{player_no}"
    if len(parts) == 1:
        return parts[0][:24]
    return f"{parts[0][:24]} {parts[-1][0].upper()}."


async def registration_of(session, event_id: int, registration_id) -> Optional[EventRegistrationsLink]:
    return await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.id == registration_id, EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.is_deleted == False  # noqa: E712
        )
    )


async def checked_in(session, registration_id) -> bool:
    return bool(await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.registration_id == registration_id)))


async def require_player_ok(session, reg: Optional[EventRegistrationsLink]) -> EventRegistrationsLink:
    """A live, un-revoked registration that has been checked in."""
    if reg is None:
        raise err(404, "NOT_REGISTERED", "We could not find your registration for this event.")
    if reg.ticket_revoked_at is not None:
        raise err(403, "TICKET_REVOKED", "This ticket is no longer valid.")
    if not await checked_in(session, reg.id):
        raise err(409, "NOT_CHECKED_IN", "Check in at the desk first.")
    return reg


async def ensure_player(session, event_id: int, registration_id) -> GamePlayers:
    """The registration's player row, made (with the next number) on first use."""
    player = await session.scalar(select(GamePlayers).where(GamePlayers.registration_id == registration_id))
    if player is not None:
        return player
    await ensure_settings(session, event_id)
    number = await session.scalar(update(GameSettings).where(GameSettings.event_id == event_id).values(player_seq=GameSettings.player_seq + 1).returning(GameSettings.player_seq))
    await session.execute(
        pg_insert(GamePlayers).values(id=uuid.uuid4(), event_id=event_id, registration_id=registration_id, player_no=number).on_conflict_do_nothing(constraint="uq_game_players_registration")
    )
    await session.flush()
    return await session.scalar(select(GamePlayers).where(GamePlayers.registration_id == registration_id))


async def start_session(session, event: Events, reg: EventRegistrationsLink, player: GamePlayers, *, reuse: bool = False) -> dict:
    """A session for the player, valid until the event ends and 12 hours more.

    A new one ends any older one (badge + PIN: the lost-phone case). With
    `reuse` (a signed-in account) a session already running is answered again,
    the same token, and no other device is signed out."""
    expires = max(event_end(event), now()) + SESSION_TAIL
    if reuse and player.session_version > 0:
        version = player.session_version
        await session.execute(update(GamePlayers).where(GamePlayers.id == player.id).values(last_seen_at=now()))
    else:
        version = await session.scalar(
            update(GamePlayers).where(GamePlayers.id == player.id).values(session_version=GamePlayers.session_version + 1, last_seen_at=now()).returning(GamePlayers.session_version)
        )
    token = tokens.session(event.id, reg.id, version, int(expires.timestamp()))
    await session.commit()
    # `token` is the name the website reads; `session` is the same value.
    return {"token": token, "session": token, "expires_at": expires, "player": {"name": display_name(reg.full_name, player.public, player.player_no), "player_no": player.player_no, "public": player.public}}


async def authenticate(session, header: Optional[str], event_id: int) -> Ctx:
    """The player behind `Authorization: Game <token>` for this event."""
    scheme, _, value = (header or "").partition(" ")
    if scheme.lower() != "game" or not value.strip():
        raise err(401, "SESSION_REQUIRED", "Sign in to the game first.")
    read = tokens.read_session(value.strip())
    if read is None or read.event_id != event_id:
        raise err(401, "SESSION_INVALID", "Sign in to the game again.")
    if read.expires < now().timestamp():
        raise err(401, "SESSION_ENDED", "Your game session has ended. Sign in again.")
    player = await session.scalar(select(GamePlayers).where(GamePlayers.registration_id == read.registration_id, GamePlayers.event_id == event_id))
    if player is None or player.session_version != read.version:
        raise err(401, "SESSION_ENDED", "You signed in on another phone. Sign in again here to play.")
    reg = await registration_of(session, event_id, read.registration_id)
    if reg is None or reg.ticket_revoked_at is not None:
        raise err(401, "SESSION_ENDED", "This ticket is no longer valid.")
    event = await event_of(session, event_id)
    settings = await settings_of(session, event_id)
    stamp = now()
    if player.last_seen_at is None or player.last_seen_at < stamp - SEEN_EVERY:
        await session.execute(update(GamePlayers).where(GamePlayers.id == player.id).values(last_seen_at=stamp))
        await session.commit()
    return Ctx(event=event, settings=settings, reg=reg, player=player)


# ─── The ticker ────────────────────────────────────────────────────────


async def add_feed(session, event_id: int, game: str, type_: str, message: str, payload: Optional[dict] = None) -> None:
    session.add(GameFeed(id=uuid.uuid4(), event_id=event_id, game=game, type=type_, message=message[:200], payload=payload))
    await session.flush()


async def player_label(session, registration_id) -> str:
    row = (
        await session.execute(
            select(EventRegistrationsLink.full_name, GamePlayers.public, GamePlayers.player_no)
            .join(GamePlayers, GamePlayers.registration_id == EventRegistrationsLink.id)
            .where(EventRegistrationsLink.id == registration_id)
        )
    ).first()
    return display_name(row[0], row[1], row[2]) if row else "A player"


def told(event_id: int, *topics: str) -> None:
    bus.changed(event_id, *topics)


def logo_of(value: Optional[str]) -> Optional[str]:
    """The look's logo as an address: an address as given, a stored upload through the media links."""
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    from app.core.storage import media

    return media.public_url(value)
