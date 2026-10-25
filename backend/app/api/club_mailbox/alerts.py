"""Telling the club's mail people that mail is waiting: the dashboard's bell.

Only the people who can read the mailbox -- the club's owner and the members
it gave the Mail permission -- see these notices, and never what a mail
says: who it is from (their name, else their address) and a count. One
notice per thread while it has unread mail (`mail_message`), brought to the
top as each arrives, gone once the thread is read, answered, moved to Spam
or deleted. Spam raises nothing.

The bell's list (`GET /clubs/alerts`) and "mark all read" leave them out for
anyone else (`hidden_kinds`), as they do the WhatsApp inbox's.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.club_mailbox.models import SPAM, ClubMailThreads

logger = logging.getLogger(__name__)

MESSAGE_KIND = "mail_message"
#: The bell's notices only the mail's people see.
PRIVATE_KINDS = frozenset({MESSAGE_KIND})


def hidden_kinds(request) -> frozenset:
    """The bell's kinds this viewer may not see: the mailbox's, without the Mail permission."""
    from app.api.club_mailbox import access

    return frozenset() if access.can_mail(request) else PRIVATE_KINDS


def _key(thread_id: int) -> str:
    return f"{MESSAGE_KIND}:{thread_id}"


async def sync_thread(session, club_id: int, thread_id: int, *, arrived: bool = False) -> None:
    """The thread's notice in line with it: raised (brought to the top when
    mail just `arrived`) while it has unread mail outside Spam, gone
    otherwise. Never raises."""
    from app.api.club_alerts.models import ClubAlerts

    T, A = ClubMailThreads, ClubAlerts
    try:
        async with session.begin_nested():
            row = (await session.execute(
                select(T.unread_count, T.folder, T.last_from).where(T.id == thread_id, T.club_id == club_id)
            )).first()
            if row is None or row.unread_count <= 0 or row.folder == SPAM:
                await session.execute(delete(A).where(A.club_id == club_id, A.dedupe_key == _key(thread_id)))
                return
            n = int(row.unread_count)
            who = (row.last_from or "Someone")[:80]
            title = f"{who}: {n} unread email{'s' if n != 1 else ''}"
            now = datetime.now(timezone.utc)
            values = dict(club_id=club_id, kind=MESSAGE_KIND, title=title[:200], body="", link=f"/mail?t={thread_id}",
                          data={"thread_id": thread_id, "count": n}, dedupe_key=_key(thread_id), created_at=now)
            changes = {"title": title[:200], "data": values["data"]}
            if arrived:
                changes.update(created_at=now, read_at=None)
            await session.execute(pg_insert(A).values(**values).on_conflict_do_update(constraint="uq_club_alerts_once", set_=changes))
    except Exception:  # noqa: BLE001
        logger.warning("club %s: the mail notice for a thread could not be written", club_id, exc_info=True)


async def forget_thread(session, club_id: int, thread_ids) -> None:
    from app.api.club_alerts.models import ClubAlerts

    keys = [_key(t) for t in thread_ids]
    if keys:
        await session.execute(delete(ClubAlerts).where(ClubAlerts.club_id == club_id, ClubAlerts.dedupe_key.in_(keys)))
