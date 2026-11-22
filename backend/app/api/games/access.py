"""How a player gets in (CONTRACT §1): signed in on the website, or badge QR plus PIN."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import select, update

from app.api.events.models import EventRegistrationsLink
from app.api.games import common, tokens, views
from app.api.games.common import LOCK_MINUTES, PIN_TRIES, err
from app.api.games.models import CARDS, HUNT, GamePlayers


async def _badge(session, token: str):
    """The badge's event, settings and registration, or the refusal."""
    badge = tokens.read_badge(token)
    if badge is None:
        raise err(422, "BAD_CODE", "That code is not valid.")
    event = await common.event_of(session, badge.event_id)
    settings = await common.settings_of(session, event.id)
    cards = await common.active(session, event, settings, CARDS)
    hunt = await common.active(session, event, settings, HUNT)
    if not (cards or hunt):
        raise err(404, "GAME_NOT_ON", "No game is on for this event.")
    reg = await common.registration_of(session, event.id, badge.registration_id)
    if reg is None:
        raise err(422, "BAD_CODE", "That code is not valid.")
    player = await session.scalar(select(GamePlayers).where(GamePlayers.registration_id == reg.id))
    # A PIN reset ended the old badge too.
    if badge.pin_version != (player.pin_version if player is not None else 0):
        raise err(422, "BAD_CODE", "That code is not valid any more. Ask the help desk.")
    return event, settings, reg, player


async def _needs_pin(session, event) -> bool:
    from app.api.games.cards import config_of

    try:
        return bool((await config_of(session, event.id)).pin_required)
    except Exception:  # noqa: BLE001 - the hunt alone has no card config: no PIN to ask
        return False


async def badge_landing(session, token: str) -> dict:
    """POST /session/badge: what the QR opens. 409 NOT_CHECKED_IN until the desk has checked them in."""
    event, settings, reg, player = await _badge(session, token)
    await common.require_player_ok(session, reg)
    out = {
        "event": views.event_block(event),
        "cards": await views.game_block(session, event, settings, CARDS),
        "hunt": await views.game_block(session, event, settings, HUNT),
        "theme": settings.theme,
        "logo_url": settings.logo_url,
        "needs_pin": await _needs_pin(session, event),
        "name": ((reg.full_name or "").split() or [""])[0],
    }
    if not out["needs_pin"]:
        out.update(await _sign_in(session, event, reg))
    return out


async def _sign_in(session, event, reg, *, reuse: bool = False) -> dict:
    player = await common.ensure_player(session, event.id, reg.id)
    return await common.start_session(session, event, reg, player, reuse=reuse)


async def pin_sign_in(session, token: str, pin: str) -> dict:
    """POST /session/pin: five wrong tries lock the player for ten minutes."""
    event, settings, reg, player = await _badge(session, token)
    await common.require_player_ok(session, reg)
    player = await common.ensure_player(session, event.id, reg.id)
    now = common.now()
    if player.pin_locked_until is not None and player.pin_locked_until > now:
        raise err(423, "PIN_LOCKED", "Too many wrong PINs. Try again later or ask the help desk.", locked_until=player.pin_locked_until.isoformat())
    if tokens.pin_matches(event.id, reg.id, player.pin_version, pin):
        await session.execute(update(GamePlayers).where(GamePlayers.id == player.id).values(pin_failed=0, pin_locked_until=None))
        await session.flush()
        return {"event": views.event_block(event), **await common.start_session(session, event, reg, player)}
    # The failure is kept before the answer: a refusal that rolled back would give unlimited tries.
    row = (
        await session.execute(
            update(GamePlayers).where(GamePlayers.id == player.id).values(pin_failed=GamePlayers.pin_failed + 1).returning(GamePlayers.pin_failed)
        )
    ).first()
    failed = int(row[0])
    if failed >= PIN_TRIES:
        until = now + timedelta(minutes=LOCK_MINUTES)
        await session.execute(update(GamePlayers).where(GamePlayers.id == player.id).values(pin_failed=0, pin_locked_until=until))
        await session.commit()
        raise err(423, "PIN_LOCKED", "Too many wrong PINs. Try again in 10 minutes or ask the help desk.", locked_until=until.isoformat())
    await session.commit()
    raise err(403, "PIN_WRONG", "That PIN is not right.", attempts_left=PIN_TRIES - failed)


async def account_sign_in(session, event_id: int, user) -> dict:
    """POST /{event_id}/session: the signed-in account's own checked-in registration; no PIN."""
    event = await common.event_of(session, event_id)
    settings = await common.settings_of(session, event.id)
    if not (await common.active(session, event, settings, CARDS) or await common.active(session, event, settings, HUNT)):
        raise err(404, "GAME_NOT_ON", "No game is on for this event.")
    reg = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.user_id == user.id, EventRegistrationsLink.is_deleted == False  # noqa: E712
        )
    )
    await common.require_player_ok(session, reg)
    out = await _sign_in(session, event, reg, reuse=True)
    return {"event": views.event_block(event), **out}


async def reset_pin(session, event, registration_id) -> dict:
    """The help desk: a new PIN version (the old badge and PIN die, the lock clears)."""
    reg = await common.registration_of(session, event.id, registration_id)
    if reg is None:
        raise err(404, "NOT_REGISTERED", "No such guest on this event.")
    player = await common.ensure_player(session, event.id, reg.id)
    version = await session.scalar(
        update(GamePlayers).where(GamePlayers.id == player.id).values(pin_version=GamePlayers.pin_version + 1, pin_failed=0, pin_locked_until=None).returning(GamePlayers.pin_version)
    )
    await session.commit()
    return {"registration_id": reg.id, **tokens.code_of(event.id, reg.id, version)}


async def can_play(session, event, settings, user) -> Optional[dict]:
    """`me` of GET /public: whether this signed-in account may play now. No session is made."""
    if user is None:
        return None
    if not (await common.active(session, event, settings, CARDS) or await common.active(session, event, settings, HUNT)):
        on = bool(settings and (settings.cards_on or settings.hunt_on))
        return {"can_play": False, "reason": "PLAN_FEATURE_REQUIRED" if on else None}
    reg = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.user_id == user.id, EventRegistrationsLink.is_deleted == False  # noqa: E712
        )
    )
    if reg is None or reg.ticket_revoked_at is not None:
        return {"can_play": False, "reason": "NOT_REGISTERED"}
    if not await common.checked_in(session, reg.id):
        return {"can_play": False, "reason": "NOT_CHECKED_IN"}
    return {"can_play": True, "reason": None}
