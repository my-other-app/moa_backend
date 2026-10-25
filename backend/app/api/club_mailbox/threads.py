"""Which conversation a mail belongs to.

By its headers first: a reply names what it answers (In-Reply-To) and the
conversation before it (References). A message of the club's with one of
those ids -- its own Message-ID, or the one SES gave it when it went out --
is in the thread the reply joins.

Without them (a client that drops them, a forward written by hand): the
same subject, Re: and Fwd: and their kind taken off, with the sender among
the thread's outside people, and the thread's latest mail within 30 days.

Spam stays with spam: a mail SES called spam joins only a thread in Spam,
and a clean one never joins one there.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import or_, select

from app.api.club_mailbox.models import SPAM, ClubMailMessages, ClubMailThreads

#: A reply without headers joins a thread whose latest mail is this recent.
SUBJECT_WINDOW = timedelta(days=30)

#: Re:, Fwd: and their kind, in the languages mail clients write them in,
#: with a count ("Re[2]:") or a list's tag before them.
_PREFIX = re.compile(
    r"^\s*(?:\[[^\]]{0,40}\]\s*)?(?:re|fw|fwd|aw|wg|sv|vs|antw|rif|tr|res|enc|odp|pd|"
    r"回复|回覆|答复|转发|轉寄|返信|転送)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*",
    re.I,
)


def subject_key(subject: Optional[str]) -> str:
    """The subject without its reply and forward marks, lower case, spaces
    folded: "Re: FW: Parking?" and "parking?" are one."""
    value = " ".join((subject or "").split())
    while True:
        stripped = _PREFIX.sub("", value, count=1)
        if stripped == value:
            break
        value = stripped
    return value.strip().lower()[:300]


def outside(addresses: Iterable[Optional[str]], ours: Iterable[str]) -> list[str]:
    """The addresses that aren't the club's own, lower case, each once, in order."""
    mine = {a.lower() for a in ours if a}
    seen: list[str] = []
    for a in addresses:
        value = (a or "").strip().lower()
        if value and "@" in value and value not in mine and value not in seen:
            seen.append(value)
    return seen


async def by_headers(session, club_id: int, ids: list[str], *, spam: bool) -> Optional[ClubMailThreads]:
    """The thread of the club's message with one of these Message-IDs (the
    nearest reference first), in the same kind of folder."""
    if not ids:
        return None
    M, T = ClubMailMessages, ClubMailThreads
    wanted = list(dict.fromkeys(ids))[-50:]
    rows = (await session.execute(
        select(M.thread_id, M.message_id, M.alt_message_id)
        .join(T, T.id == M.thread_id)
        .where(M.club_id == club_id, or_(M.message_id.in_(wanted), M.alt_message_id.in_(wanted)),
               (T.folder == SPAM) if spam else (T.folder != SPAM))
    )).all()
    if not rows:
        return None
    rank = {mid: i for i, mid in enumerate(wanted)}
    best = max(rows, key=lambda r: max(rank.get(r.message_id, -1), rank.get(r.alt_message_id, -1)))
    return await session.get(T, best.thread_id)


async def by_subject(session, club_id: int, subject: Optional[str], sender: Optional[str], *, spam: bool,
                     now: Optional[datetime] = None) -> Optional[ClubMailThreads]:
    """The latest thread with this subject, its sender among its people,
    written in within SUBJECT_WINDOW."""
    key = subject_key(subject)
    sender = (sender or "").strip().lower()
    if not key or not sender:
        return None
    now = now or datetime.now(timezone.utc)
    T = ClubMailThreads
    rows = (await session.scalars(
        select(T).where(
            T.club_id == club_id, T.subject_key == key, T.last_message_at >= now - SUBJECT_WINDOW,
            (T.folder == SPAM) if spam else (T.folder != SPAM),
        ).order_by(T.last_message_at.desc(), T.id.desc()).limit(20)
    )).all()
    for thread in rows:
        if sender in (thread.participants or []):
            return thread
    return None
