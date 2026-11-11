"""What the rest of the backend tells social posting, each call in a
SAVEPOINT of the caller's transaction that never raises: a publish, an edit,
a cancel or a removal must never fail because of a social post.

- event_published: events.service.create_event, closeout.update_settings;
- event_changed: events.service.update_event (with `snapshot` taken before);
- event_stopped: closeout.update_settings (unpublished), club_ops.cancel
  (cancelled), events.service.delete_event (deleted);
- cohost_ended: cohosts.service.end and leave -- the club's own waiting posts go;
- member_removed / club_deleted: the accounts they connected are disconnected.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

logger = logging.getLogger(__name__)


async def _safely(session, what: str, fn) -> None:
    try:
        async with session.begin_nested():
            await fn()
    except Exception:  # noqa: BLE001 - see the module's doc
        logger.exception("social: %s failed", what)


def snapshot(event) -> dict:
    from app.api.social import content

    return content.snapshot(event)


async def event_published(session, event) -> None:
    from app.api.social import scheduler

    await _safely(session, f"publishing event {event.id}", lambda: scheduler.on_published(session, event))


async def event_changed(session, event, before: dict) -> None:
    from app.api.social import scheduler

    await _safely(session, f"event {event.id} changing", lambda: scheduler.on_changed(session, event, before))


async def event_stopped(session, event, reason: str) -> None:
    from app.api.social import scheduler

    await _safely(session, f"event {event.id} stopping ({reason})", lambda: scheduler.on_stopped(session, event, reason))


async def cohost_ended(session, event_id: int, club_id: int) -> None:
    from app.api.social import scheduler

    await _safely(session, f"co-host {club_id} leaving event {event_id}",
                  lambda: scheduler.stop_posts(session, event_id, "cohost_left", club_id=club_id))


async def member_removed(session, member) -> None:
    from app.api.social import accounts
    from app.api.social.models import SocialAccounts

    await _safely(session, f"member {member.id} removed", lambda: accounts.disconnect_all(
        session, select(SocialAccounts).where(SocialAccounts.club_id == member.club_id, SocialAccounts.connected_by_member_id == member.id)))


async def club_deleted(session, club) -> None:
    from app.api.social import accounts
    from app.api.social.models import SocialAccounts

    await _safely(session, f"club {club.id} deleted",
                  lambda: accounts.disconnect_all(session, select(SocialAccounts).where(SocialAccounts.club_id == club.id)))
