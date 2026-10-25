"""Writing and reading the admin audit log."""

import logging
from typing import Any

from fastapi import Request
from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.audit.models import AdminAuditLogs, search_sql

#: At most this many words in a search, each at most SEARCH_WORD_MAX long.
SEARCH_WORDS_MAX = 8
SEARCH_WORD_MAX = 60

logger = logging.getLogger(__name__)


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    # The caller as app.core.net reads it: the forwarded headers only from
    # our own proxy, right to left, so a client cannot write its own.
    from app.core.net import client_address

    return client_address(request)[:45] or None


async def record(
    session: AsyncSession,
    *,
    actor,
    action: str,
    entity_type: str | None = None,
    entity_id: Any = None,
    summary: str | None = None,
    details: dict | None = None,
    request: Request | None = None,
) -> AdminAuditLogs | None:
    """
    Append one entry. Returns it (flushed, with its id), or None when it
    could not be written.

    Never raises, and never leaves the session unusable. An audit write that
    fails must not fail the action it is describing -- an admin blocked from
    approving a club because the log was unavailable is a worse outcome than a
    missing line.

    The caller commits. Writing in the same transaction as the change means
    the log and the change land together or not at all; a separate commit
    here could record something that was then rolled back.
    """
    try:
        # A SAVEPOINT, not a bare flush. Catching the exception is not enough
        # on its own: a failed flush marks the whole transaction as needing a
        # rollback, so the caller's own commit would then fail with
        # PendingRollbackError -- the action would be lost because its audit
        # line could not be written, which is the opposite of the intent.
        # Rolling back to the savepoint discards only the log row.
        name = getattr(actor, "full_name", None)
        async with session.begin_nested():
            entry = AdminAuditLogs(
                actor_id=getattr(actor, "id", None),
                actor_email=getattr(actor, "email", None),
                actor_name=str(name)[:100] if name else None,
                action=action,
                entity_type=entity_type,
                entity_id=None if entity_id is None else str(entity_id)[:64],
                summary=summary[:255] if summary else None,
                details=details,
                ip_address=_client_ip(request),
            )
            session.add(entry)
        return entry
    except Exception:
        logger.exception("Failed to write audit log for action %s", action)
        return None


def search_words(q: str | None) -> list[str]:
    """The words of a search: split on spaces, each kept whole, at most
    SEARCH_WORDS_MAX of them. Empty when there is nothing to look for."""
    words = (q or "").split()
    return [w[:SEARCH_WORD_MAX] for w in words[:SEARCH_WORDS_MAX]]


def _like(word: str) -> str:
    """A word as an ILIKE pattern that finds it anywhere, its own % and _
    taken literally."""
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_entries(
    session: AsyncSession,
    *,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_id: int | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    """A page of the log, newest first, and how many rows match in all.

    `q`: every word must appear, in any case, somewhere in who acted (name or
    address), the summary, the record ("club #12") or the reason given --
    on the trigram index ix_audit_search_trgm.
    """
    filters = []
    haystack = literal_column(search_sql(AdminAuditLogs.__tablename__))
    for word in search_words(q):
        filters.append(haystack.ilike(_like(word), escape="\\"))
    if action:
        filters.append(AdminAuditLogs.action == action)
    if entity_type:
        filters.append(AdminAuditLogs.entity_type == entity_type)
    if entity_id:
        filters.append(AdminAuditLogs.entity_id == str(entity_id))
    if actor_id:
        filters.append(AdminAuditLogs.actor_id == actor_id)

    total = await session.scalar(
        select(func.count(AdminAuditLogs.id)).where(*filters)
    )
    from app.api.reports.service import picture_options

    rows = await session.scalars(
        select(AdminAuditLogs)
        .where(*filters)
        # With what the actor's picture needs (reports.avatar), loaded for
        # the whole page at once rather than per row.
        .options(joinedload(AdminAuditLogs.actor).options(*picture_options()))
        .order_by(AdminAuditLogs.created_at.desc(), AdminAuditLogs.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), total or 0


async def distinct_actions(session: AsyncSession) -> list[str]:
    """Powers the filter dropdown without hard-coding a list that drifts."""
    rows = await session.scalars(
        select(AdminAuditLogs.action).distinct().order_by(AdminAuditLogs.action)
    )
    return list(rows)
