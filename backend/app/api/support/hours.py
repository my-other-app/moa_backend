"""The desk's settings, and the working hours its reply timers count in.

Kept on the support team's row (admin_teams.settings), each key falling back
to DEFAULTS: Monday to Saturday, 09:30 to 18:30 India time; a first reply
within 4 working hours and a resolution within 18 (two working days); closed
cases' messages kept 365 days.

A timer is the moment that many working hours after the case came in: a
case that arrives on Saturday at 18:00 with a 4-hour target is due on Monday
at 13:00 (half an hour on Saturday, three and a half on Monday).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.api.support.models import SUPPORT, AdminTeams
from app.response import CustomHTTPException

IST = "Asia/Kolkata"
DEFAULTS: dict = {
    # Days are Monday 0 to Sunday 6.
    "working_hours": {"days": [0, 1, 2, 3, 4, 5], "start": "09:30", "end": "18:30", "timezone": IST},
    "retention_days": 365,
    "first_reply_target_hours": 4,
    "resolution_target_hours": 18,
}
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
RETENTION_RANGE = (30, 3650)
TARGET_RANGE = (1, 400)


def _refuse(message: str, field: str) -> CustomHTTPException:
    return CustomHTTPException(422, message=message, error_code="INVALID_DESK_SETTINGS", errors={"field": field})


def merged(stored: Optional[dict]) -> dict:
    """The settings with every key: what is stored over DEFAULTS."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for key, value in (stored or {}).items():
        if key == "working_hours" and isinstance(value, dict):
            out["working_hours"] = {**out["working_hours"], **value}
        elif key in out and value is not None:
            out[key] = value
    return out


async def get(session) -> dict:
    row = await session.get(AdminTeams, SUPPORT)
    return merged(row.settings if row is not None else None)


def validate(body: dict) -> dict:
    """The settings an owner sent, checked; 422 INVALID_DESK_SETTINGS with the field."""
    out = merged(body)
    wh = out["working_hours"]
    days = wh.get("days")
    if not isinstance(days, list) or not days or any(not isinstance(d, int) or d < 0 or d > 6 for d in days):
        raise _refuse("Pick at least one working day.", "working_hours.days")
    wh["days"] = sorted(set(days))
    for name in ("start", "end"):
        if not isinstance(wh.get(name), str) or not _TIME.match(wh[name]):
            raise _refuse("Times are HH:MM, 24-hour.", f"working_hours.{name}")
    if wh["start"] >= wh["end"]:
        raise _refuse("The day has to end after it starts.", "working_hours.end")
    try:
        ZoneInfo(str(wh.get("timezone") or IST))
    except Exception:  # noqa: BLE001
        raise _refuse("That isn't a time zone.", "working_hours.timezone") from None
    for key, (low, high) in (("retention_days", RETENTION_RANGE), ("first_reply_target_hours", TARGET_RANGE),
                             ("resolution_target_hours", TARGET_RANGE)):
        value = out.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not (low <= value <= high):
            raise _refuse(f"{key.replace('_', ' ').capitalize()} must be between {low} and {high}.", key)
    if out["resolution_target_hours"] < out["first_reply_target_hours"]:
        raise _refuse("The resolution target can't be shorter than the first reply's.", "resolution_target_hours")
    return {k: out[k] for k in DEFAULTS}


def _hm(text: str) -> tuple[int, int]:
    h, m = text.split(":")
    return int(h), int(m)


def add_working_hours(start: datetime, hours: float, working_hours: Optional[dict] = None) -> datetime:
    """The moment `hours` working hours after `start` (UTC)."""
    wh = {**DEFAULTS["working_hours"], **(working_hours or {})}
    tz = ZoneInfo(str(wh.get("timezone") or IST))
    days = set(wh.get("days") or DEFAULTS["working_hours"]["days"])
    (sh, sm), (eh, em) = _hm(wh["start"]), _hm(wh["end"])
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    t = start.astimezone(tz)
    left = timedelta(hours=float(hours))
    for _ in range(800):
        day_start = t.replace(hour=sh, minute=sm, second=0, microsecond=0)
        day_end = t.replace(hour=eh, minute=em, second=0, microsecond=0)
        if t.weekday() not in days or t >= day_end:
            t = (day_start + timedelta(days=1))
            continue
        if t < day_start:
            t = day_start
        room = day_end - t
        if left <= room:
            return (t + left).astimezone(timezone.utc)
        left -= room
        t = day_start + timedelta(days=1)
    return (start + timedelta(hours=float(hours))).astimezone(timezone.utc)


def timers(created_at: datetime, desk: dict) -> tuple[datetime, datetime]:
    """(first reply due, resolution due) for a case that came in at `created_at`."""
    wh = desk["working_hours"]
    return (
        add_working_hours(created_at, desk["first_reply_target_hours"], wh),
        add_working_hours(created_at, desk["resolution_target_hours"], wh),
    )


def today_bounds(now: Optional[datetime] = None, tz: str = IST) -> tuple[datetime, datetime]:
    """The start and end of today in India time, as UTC moments."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def local_date(now: Optional[datetime] = None, tz: str = IST):
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date()
