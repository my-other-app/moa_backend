"""Telling a club about its own WhatsApp number: a mail to its owner.

When the number stops working (Meta refused its token, locked the account,
a send failed for the account) or its quality rating drops, the club hears
of it by mail -- at most once a day for each kind of notice -- besides what
its dashboard shows. The audit log is the record of having told it, as for
the plan's paused-registrations mail: durable, shared by every worker, and
readable by an admin. The mail is a durable job (app.core.after_commit), so
an outage of the mail provider is retried, and it goes only if the change it
is about commits. It never says what any message said.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.after_commit import run_after_commit
from app.core.email import plain

logger = logging.getLogger(__name__)

#: How often one kind of notice may reach a club.
EVERY = timedelta(hours=24)
ACTION = "club_whatsapp.notice"


def send(to: str, subject: str, paragraphs: list[str], button, footer: str | None) -> None:
    """The mail, raising when the provider did not take it: what the durable
    job runs (app.core.after_commit), so an outage is retried."""
    from app.core.email import email as email_mod

    email_mod.send_email(
        recipients=[to], subject=subject,
        text=plain.text(paragraphs, button=button, footer=footer),
        html=plain.render(paragraphs, button=button, footer=footer),
    )


def _send(to: str, subject: str, paragraphs: list[str], button, footer: str | None) -> None:
    """What callers queue after commit: a job that runs `send` when there is a
    loop, else this, in line. Logged, never raised."""
    try:
        send(to, subject, paragraphs, button, footer)
    except Exception:  # noqa: BLE001
        logger.warning("club WhatsApp notice (%s) could not be sent", subject, exc_info=True)


def dashboard_url(path: str = "/whatsapp/settings") -> str:
    from app.config import settings

    return f"{settings.MCP_DASHBOARD_URL.rstrip('/')}{path}"


async def tell_owner(session, club_id: int, kind: str, *, subject: str, paragraphs: list[str],
                     button: tuple[str, str] | None = None, now: datetime | None = None) -> bool:
    """Mail the club's owner about `kind`, unless it was told in the last
    day. Written in the caller's transaction. True when a mail was queued."""
    from app.api.audit import service as audit
    from app.api.audit.models import AdminAuditLogs
    from app.api.subscriptions.notices import owner_address

    now = now or datetime.now(timezone.utc)
    action = f"{ACTION}:{kind}"
    recent = await session.scalar(
        select(AdminAuditLogs.id)
        .where(
            AdminAuditLogs.action == action,
            AdminAuditLogs.entity_type == "club",
            AdminAuditLogs.entity_id == str(club_id),
            AdminAuditLogs.created_at >= now - EVERY,
        )
        .limit(1)
    )
    if recent is not None:
        return False
    to = await owner_address(session, club_id)
    await audit.record(
        session, actor=None, action=action, entity_type="club", entity_id=club_id,
        summary=subject[:255], details={"kind": kind, "emailed": bool(to)},
    )
    if not to:
        return False
    run_after_commit(session, _send, to, subject, paragraphs, button or ("Open WhatsApp settings", dashboard_url()), None)
    return True
