"""Raising and reading a club's notices (the dashboard's bell).

`notify` writes the row in the caller's transaction, so a notice about a
change that is rolled back never appears, and never raises: a notice that
cannot be written must not fail the send or the sweep it is about. A
`dedupe_key` the club already has is nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.club_alerts.models import ClubAlerts

logger = logging.getLogger(__name__)

#: Every kind, and what it means. A new one is added here first.
KINDS: dict[str, str] = {
    "message_quota_80": "The club has used 80% of a month's emails or WhatsApp messages",
    "message_quota_100": "The club has used all of a month's emails or WhatsApp messages",
    "whatsapp_failed": "The club's own WhatsApp number stopped working (Meta refused its keys or its account)",
    "whatsapp_quality": "The club's own WhatsApp number's quality rating or messaging limit dropped",
    "whatsapp_restricted": "Meta restricted the club's own WhatsApp account",
    "whatsapp_message": "A conversation in the club's WhatsApp inbox has unread messages (the inbox's people only)",
    "whatsapp_waiting": "A WhatsApp conversation has waited too long for an answer (the inbox's people only)",
    "mail_message": "A conversation in the club's mailbox has unread mail (the Mail permission's people only)",
    "mailbox_lapsed": "The club's plan no longer includes its mailbox: closed, its mail readable 30 days",
    "mailbox_expired": "A lapsed mailbox's 30 days are over: its mail can be exported or deleted",
}

#: How many the bell shows at most.
LIMIT = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def notify(
    session,
    club_id: int,
    kind: str,
    *,
    title: str,
    body: str = "",
    link: str | None = None,
    data: dict | None = None,
    dedupe_key: str | None = None,
) -> int | None:
    """Write the notice. Returns its id, or None when the club already has
    one with this `dedupe_key` or it could not be written."""
    try:
        async with session.begin_nested():
            return await session.scalar(
                pg_insert(ClubAlerts)
                .values(
                    club_id=club_id, kind=kind[:40], title=title[:200], body=body or "", link=link,
                    data=data, dedupe_key=dedupe_key[:160] if dedupe_key else None, created_at=_now(),
                )
                .on_conflict_do_nothing(constraint="uq_club_alerts_once")
                .returning(ClubAlerts.id)
            )
    except Exception:  # noqa: BLE001
        logger.warning("club %s: notice %s could not be written", club_id, kind, exc_info=True)
        return None


def alert_out(row: ClubAlerts) -> dict:
    return {
        "id": row.id, "kind": row.kind, "title": row.title, "body": row.body or "", "link": row.link,
        "data": row.data or {}, "created_at": row.created_at, "read": row.read_at is not None,
    }


async def listing(session, club_id: int, *, limit: int = LIMIT, hide: frozenset = frozenset()) -> dict:
    """`hide`: kinds this viewer may not see (the WhatsApp inbox's, without the inbox)."""
    seen = [ClubAlerts.club_id == club_id, *([ClubAlerts.kind.not_in(hide)] if hide else [])]
    rows = list(
        await session.scalars(
            select(ClubAlerts).where(*seen).order_by(ClubAlerts.created_at.desc(), ClubAlerts.id.desc()).limit(limit)
        )
    )
    unread = await session.scalar(
        select(func.count(ClubAlerts.id)).where(*seen, ClubAlerts.read_at.is_(None))
    )
    return {"items": [alert_out(r) for r in rows], "unread": int(unread or 0)}


async def mark_read(session, club_id: int, ids: list[int] | None = None, hide: frozenset = frozenset()) -> int:
    """Mark the club's notices read: these ids, or every one (but the `hide`
    kinds, which this viewer doesn't see). Returns how many changed."""
    query = update(ClubAlerts).where(ClubAlerts.club_id == club_id, ClubAlerts.read_at.is_(None))
    if hide:
        query = query.where(ClubAlerts.kind.not_in(hide))
    if ids:
        query = query.where(ClubAlerts.id.in_(ids))
    res = await session.execute(query.values(read_at=_now()).execution_options(synchronize_session=False))
    await session.commit()
    return int(res.rowcount or 0)
