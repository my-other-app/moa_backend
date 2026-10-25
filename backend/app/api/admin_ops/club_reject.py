"""Telling a club's applicant it was not approved, and why.

POST /clubs/{id}/reject (app/api/clubs/router.py) takes a reason now; for a
club still waiting for approval, the applicant's sign-in address is mailed
it (a job after the answer, like the approval mail). Rejecting a pending
application is a moderator's call; rejecting a club that was approved
already is a deletion and still needs DESTROY.
"""
from __future__ import annotations

import html as html_mod
import logging
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.users.models import Users
from app.config import settings
from app.core import jobs

logger = logging.getLogger(__name__)


class RejectIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=1000)


async def tell_applicant(session, club, reason: Optional[str]) -> bool:
    """Queue the mail to the club's sign-in address. False when it has none."""
    to = await session.scalar(select(Users.email).where(Users.id == club.user_id).execution_options(include_deleted=True))
    if not to:
        return False
    await jobs.enqueue(session, "clubs.rejected_email", {"to": to, "club_name": club.name, "reason": (reason or "").strip() or None},
                       dedupe_key=f"clubs:rejected:{club.id}")
    return True


@jobs.handler("clubs.rejected_email", max_attempts=4, timeout=60.0, priority=jobs.HIGH)
async def _rejected_email_job(p: dict) -> None:
    import asyncio

    await asyncio.to_thread(send_rejected_email, p["to"], club_name=p["club_name"], reason=p.get("reason"))


def send_rejected_email(to: str, *, club_name: str, reason: Optional[str]) -> None:
    from app.core.email import email as email_mod

    contact = (settings.ADMIN_ALERT_EMAILS or "").split(",")[0].strip() or "support@myotherapp.in"
    why = f"The reason given: {reason}\n\n" if reason else ""
    text = (
        f"Thank you for applying to bring {club_name} to MyOtherApp.\n\n"
        f"We weren't able to approve the application this time.\n\n{why}"
        f"If you think this is a mistake, or you can tell us more, reply to {contact}.\n"
    )
    safe = html_mod.escape
    html = (
        f"<p>Thank you for applying to bring <strong>{safe(club_name)}</strong> to MyOtherApp.</p>"
        "<p>We weren't able to approve the application this time.</p>"
        + (f"<p><strong>The reason given:</strong> {safe(reason)}</p>" if reason else "")
        + f"<p>If you think this is a mistake, or you can tell us more, write to <a href=\"mailto:{safe(contact)}\">{safe(contact)}</a>.</p>"
    )
    email_mod.send_email(recipients=[to], subject=f"Your application for {club_name}", text=text, html=html, reply_to=contact)
