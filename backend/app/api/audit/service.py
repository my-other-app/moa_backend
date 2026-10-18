"""Writing and reading the admin audit log."""

import logging
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.audit.models import AdminAuditLogs

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
        async with session.begin_nested():
            entry = AdminAuditLogs(
                actor_id=getattr(actor, "id", None),
                actor_email=getattr(actor, "email", None),
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


async def list_entries(
    session: AsyncSession,
    *,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
):
    filters = []
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
    rows = await session.scalars(
        select(AdminAuditLogs)
        .where(*filters)
        .options(joinedload(AdminAuditLogs.actor))
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
