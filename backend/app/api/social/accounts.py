"""A club's connected accounts: kept, read out, refreshed, disconnected.

A token is sealed (crypto) the moment it arrives and opened only for the
call that needs it. Disconnecting revokes it on the platform where the
platform allows, then empties both tokens; the row stays as history.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, update

from app.api.social import alerts, crypto
from app.api.social import http as h
from app.api.social.adapters import for_platform
from app.api.social.adapters.base import Candidate, Creds
from app.api.social.models import (
    CONNECTED, DISCONNECTED, HELD, NEEDS_RECONNECT, T_SCHEDULED, WAITING, SocialAccounts, SocialPostTargets,
)
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: Refreshed this close to its expiry, before a post is sent with it.
REFRESH_AHEAD = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def creds(account: SocialAccounts) -> Creds:
    """The account's keys for one call (crypto.Unavailable when they cannot be read)."""
    return Creds(
        token=crypto.open_(account.access_token_enc), external_id=account.external_id, handle=account.handle,
        refresh_token=crypto.open_or_none(account.refresh_token_enc), connector_id=account.connector_external_id,
    )


async def of_club(session, club_id: int, *, live_only: bool = True) -> list[SocialAccounts]:
    q = select(SocialAccounts).where(SocialAccounts.club_id == club_id)
    if live_only:
        q = q.where(SocialAccounts.status != DISCONNECTED)
    return list(await session.scalars(q.order_by(SocialAccounts.platform, SocialAccounts.connected_at)))


async def get_for_club(session, club_id: int, account_id) -> SocialAccounts:
    account = await session.get(SocialAccounts, account_id)
    if account is None or account.club_id != club_id or account.status == DISCONNECTED:
        raise CustomHTTPException(404, message="No such connected account", error_code="SOCIAL_ACCOUNT_NOT_FOUND")
    return account


async def waiting_counts(session, account_ids: list) -> dict:
    if not account_ids:
        return {}
    rows = (await session.execute(
        select(SocialPostTargets.account_id, func.count())
        .where(SocialPostTargets.account_id.in_(account_ids), SocialPostTargets.status.in_(WAITING))
        .group_by(SocialPostTargets.account_id)
    )).all()
    return {a: int(n) for a, n in rows}


def out(account: SocialAccounts, waiting: int = 0) -> dict:
    return {
        "id": account.id, "platform": account.platform, "name": account.name, "handle": account.handle,
        "picture_url": account.picture_url,
        # The team member who connected it (club_members.id); null for the club's own account.
        "connected_by": {"id": account.connected_by_member_id, "name": account.connected_by_name},
        "connected_at": account.connected_at, "expires_at": account.token_expires_at, "status": account.status,
        "waiting_posts": int(waiting), "last_error": account.last_error,
    }


async def save(session, *, club_id: int, platform: str, cand: Candidate, connector_id: Optional[str], scopes: Optional[str],
               by: dict) -> tuple[SocialAccounts, bool]:
    """Keep a connection: a new one, or new tokens for one the club already
    has (a reconnect). Targets held for it go back to waiting. Returns the
    account and whether it was new. Flushed, not committed."""
    now = _now()
    account = await session.scalar(
        select(SocialAccounts).where(
            SocialAccounts.club_id == club_id, SocialAccounts.platform == platform,
            SocialAccounts.external_id == cand.external_id, SocialAccounts.status != DISCONNECTED,
        ).with_for_update()
    )
    new = account is None
    if new:
        account = SocialAccounts(club_id=club_id, platform=platform, external_id=cand.external_id)
        session.add(account)
    account.name, account.handle, account.picture_url = cand.name, cand.handle, (cand.picture or None) and cand.picture[:1000]
    account.connector_external_id = connector_id
    account.access_token_enc = crypto.seal(cand.token)
    account.refresh_token_enc = crypto.seal(cand.refresh_token)
    account.token_expires_at, account.refresh_expires_at = cand.expires_at, cand.refresh_expires_at
    account.scopes = (scopes or "")[:500] or None
    account.status, account.last_error = CONNECTED, None
    account.connected_by_user_id, account.connected_by_member_id = by.get("user_id"), by.get("member_id")
    account.connected_by_name = (by.get("name") or "")[:120] or None
    account.connected_at, account.refreshed_at, account.disconnected_at, account.expiry_warned_for = now, now, None, None
    await session.flush()
    # Posts held for an earlier connection of the same account (disconnected, then connected again) follow it.
    old = select(SocialAccounts.id).where(
        SocialAccounts.club_id == club_id, SocialAccounts.platform == platform, SocialAccounts.external_id == cand.external_id,
        SocialAccounts.status == DISCONNECTED,
    )
    await session.execute(
        update(SocialPostTargets).where(SocialPostTargets.account_id.in_(old), SocialPostTargets.status.in_(WAITING))
        .values(account_id=account.id).execution_options(synchronize_session=False)
    )
    await release_held(session, account)
    return account, new


async def release_held(session, account: SocialAccounts) -> int:
    """Held targets of a working account wait again (and go when due)."""
    from app.api.social import scheduler

    rows = list(await session.scalars(
        select(SocialPostTargets).where(SocialPostTargets.account_id == account.id, SocialPostTargets.status == HELD)
    ))
    for t in rows:
        t.status, t.error_code, t.error_message, t.error_action, t.next_attempt_at = T_SCHEDULED, None, None, None, None
    await session.flush()
    for t in rows:
        await scheduler.enqueue_due(session, t)
    return len(rows)


async def hold_waiting(session, account: SocialAccounts, *, code: str = "reconnect", message: Optional[str] = None) -> int:
    """Its waiting targets wait for a reconnect ("Reconnect Instagram to send")."""
    label = alerts.LABELS.get(account.platform, account.platform)
    res = await session.execute(
        update(SocialPostTargets)
        .where(SocialPostTargets.account_id == account.id, SocialPostTargets.status.in_((T_SCHEDULED, "waiting_limit")))
        .values(status=HELD, error_code=code, error_message=(message or f"Reconnect {label} to send.")[:400], error_action="reconnect")
        .execution_options(synchronize_session=False)
    )
    return int(res.rowcount or 0)


async def needs_reconnect(session, account: SocialAccounts, message: str) -> None:
    """The platform no longer takes its token: hold what waits on it and tell the club."""
    if account.status == DISCONNECTED:
        return
    first = account.status != NEEDS_RECONNECT
    account.status, account.last_error = NEEDS_RECONNECT, h.scrub(message)[:400]
    await session.flush()
    await hold_waiting(session, account)
    if first:
        waiting = (await waiting_counts(session, [account.id])).get(account.id, 0)
        await alerts.account_needs_reconnect(session, account, waiting)


async def fresh_creds(session, account: SocialAccounts) -> Creds:
    """Its keys, refreshed first when they are about to run out and the
    platform gives a refresh (X every two hours). The refresh holds the
    account's row: X replaces the refresh token each time, and two refreshes
    at once would leave one of them dead."""
    c = creds(account)
    expires = account.token_expires_at
    if expires is None or expires - _now() > REFRESH_AHEAD or not c.refresh_token:
        return c
    locked = await session.scalar(
        select(SocialAccounts).where(SocialAccounts.id == account.id).with_for_update().execution_options(populate_existing=True)
    )
    if locked is None or locked.status == DISCONNECTED:
        raise h.PlatformError(h.AUTH, "The account was disconnected.", code="disconnected")
    c = creds(locked)
    if locked.token_expires_at and locked.token_expires_at - _now() > REFRESH_AHEAD:
        await session.commit()
        return c
    try:
        tokens = await for_platform(locked.platform).refresh(c)
    except h.PlatformError:
        await session.rollback()
        raise
    if tokens is None:
        await session.commit()
        return c
    locked.access_token_enc = crypto.seal(tokens.token)
    locked.refresh_token_enc = crypto.seal(tokens.refresh_token)
    locked.token_expires_at = tokens.expires_at
    if tokens.refresh_expires_at:
        locked.refresh_expires_at = tokens.refresh_expires_at
    locked.refreshed_at = _now()
    await session.commit()
    return creds(locked)


async def _others_on_connector(session, account: SocialAccounts) -> int:
    if not account.connector_external_id:
        return 0
    return int(await session.scalar(
        select(func.count()).where(
            SocialAccounts.id != account.id, SocialAccounts.connector_external_id == account.connector_external_id,
            SocialAccounts.platform.in_(("facebook", "instagram")), SocialAccounts.status != DISCONNECTED,
        )
    ) or 0)


async def disconnect(session, account: SocialAccounts, *, revoke: bool = True) -> None:
    """Revoke where the platform allows (never Meta's grant while another
    live connection rests on it), then empty the tokens; held and waiting
    posts wait for a reconnect. The caller commits."""
    if revoke and account.access_token_enc:
        try:
            c = creds(account)
        except crypto.Unavailable:
            c = None
        if c is not None:
            meta = account.platform in ("facebook", "instagram")
            if not meta or not await _others_on_connector(session, account):
                try:
                    await for_platform(account.platform).revoke(c)
                except Exception:  # noqa: BLE001 - a revoke never stops the tokens going
                    logger.info("social: revoking %s account %s failed", account.platform, account.id)
    account.access_token_enc = None
    account.refresh_token_enc = None
    account.status = DISCONNECTED
    account.disconnected_at = _now()
    await session.flush()
    await hold_waiting(session, account, code="disconnected")


async def disconnect_all(session, query, *, revoke: bool = True) -> int:
    """Disconnect every live account `query` (a select of SocialAccounts) finds."""
    rows = list(await session.scalars(query.where(SocialAccounts.status != DISCONNECTED)))
    for a in rows:
        await disconnect(session, a, revoke=revoke)
    return len(rows)
