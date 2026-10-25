"""Two-factor for every admin.

An admin account can refund money, download every user's data and act as
other people, so a password alone is not enough. With `admin_2fa_required`
on (the default, app.api.admin_ops.runtime), an admin without two-factor is
refused by every admin route (403 ADMIN_2FA_REQUIRED) until they turn it on.
The routes that turn it on, sign out and say who you are take any signed-in
account (DependsAuth), so they stay open: the portal sends the admin to its
set-up page.

The grace: admins who were already admins when the rule began get
GRACE_DAYS from that moment (GRACE_STARTED_AT, written by the migration,
and again whenever an owner turns the rule back on); the portal shows a
banner with the days left. An admin added after that (admin_invites) has no
grace: they set it up before they use the portal.

Only admins without two-factor pay for this: the rule is read from the
database on each of their admin requests, and an admin with two-factor on
never gets past the first check. Nothing is cached in the process, so a
change applies on the next request in every worker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import status
from sqlalchemy import select

from app.api.admin_ops import runtime
from app.api.admin_ops.models import AdminInvites, PlatformSettings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

GRACE_DAYS = 7
GRACE = timedelta(days=GRACE_DAYS)
REQUIRED_CODE = "ADMIN_2FA_REQUIRED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


@dataclass
class Standing:
    required: bool
    enabled: bool
    grace_ends_at: Optional[datetime]
    blocked: bool

    def as_dict(self) -> dict:
        return {
            "required": self.required,
            "enabled": self.enabled,
            "grace_ends_at": self.grace_ends_at.isoformat() if self.grace_ends_at else None,
            "grace_days": GRACE_DAYS,
            "blocked": self.blocked,
        }


async def _policy(session) -> tuple[bool, Optional[datetime]]:
    """Whether the rule is on, and when its grace began. A database the
    migration has not reached yet (no table) is "not on", in a SAVEPOINT so
    the request's own transaction carries on: a deploy whose migration is
    still to run must not refuse every admin."""
    try:
        async with session.begin_nested():
            rows = dict((await session.execute(
                select(PlatformSettings.key, PlatformSettings.value).where(
                    PlatformSettings.key.in_(("admin_2fa_required", runtime.GRACE_STARTED_AT))
                )
            )).all())
    except Exception:  # noqa: BLE001 - the rule unreadable is the rule off; logged
        logger.warning("the admin two-factor rule could not be read; not enforced", exc_info=True)
        return False, None
    required = rows.get("admin_2fa_required", runtime.SPECS["admin_2fa_required"].default)
    try:
        required = runtime.coerce(runtime.SPECS["admin_2fa_required"], required)
    except ValueError:
        required = True
    return bool(required), _parse(rows.get(runtime.GRACE_STARTED_AT))


async def standing(session, user, *, now: Optional[datetime] = None) -> Standing:
    """Where an admin stands with the rule."""
    now = now or _now()
    enabled = bool(getattr(user, "totp_enabled", False))
    required, started = await _policy(session)
    if enabled:
        return Standing(required=required, enabled=True, grace_ends_at=None, blocked=False)
    if not required:
        return Standing(required=False, enabled=False, grace_ends_at=None, blocked=False)
    # No start recorded (a database the migration did not make): the grace
    # counts from now, so nobody is locked out by a missing row.
    started = started or now
    added_since = await session.scalar(
        select(AdminInvites.id).where(AdminInvites.user_id == user.id, AdminInvites.created_at > started).limit(1)
    )
    if added_since is not None:
        return Standing(required=True, enabled=False, grace_ends_at=None, blocked=True)
    ends = started + GRACE
    return Standing(required=True, enabled=False, grace_ends_at=ends, blocked=now >= ends)


async def enforce(session, user) -> None:
    """Refuse an admin route to an admin who must set up two-factor first."""
    if getattr(user, "totp_enabled", False):
        return
    if (await standing(session, user)).blocked:
        raise CustomHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            message="Turn on two-factor sign-in to keep using the admin portal.",
            error_code=REQUIRED_CODE,
        )
