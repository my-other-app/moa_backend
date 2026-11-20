"""The settings' words, and which moment applies to a guest (CONTRACT.md §5).

- before: the ticket was sent -- a `deliveries` row for the registration,
  kind ticket, status sent (any channel: the approval's ticket email is one).
- during: checked in (`is_attended`) and the event not over.
- after: the event over, and the ticket sent or they checked in.

The first of after, during, before that is reached and switched on applies;
so with only `before` on, a guest keeps their poster through the day and
after it, with the before phrase. Every moment also needs the event not
deleted or cancelled, and the registration neither removed nor revoked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

MOMENTS = ("before", "during", "after")
#: The order a moment is chosen in: the most specific first.
CHOICE = ("after", "during", "before")
DEFAULT_CAPTION = "{{moment_phrase}} {{event_name}}! Join me: {{share_url}}"
DEFAULTS = {
    "before": {"enabled": True, "phrase": "I'm attending", "caption": DEFAULT_CAPTION},
    "during": {"enabled": False, "phrase": "I'm here at", "caption": DEFAULT_CAPTION},
    "after": {"enabled": False, "phrase": "I attended", "caption": DEFAULT_CAPTION},
}
PHRASE_MAX = 60
CAPTION_MAX = 500

#: Why a guest has no poster, most specific first (CONTRACT.md §5).
OFF, EVENT_CANCELLED, CANCELLED, REVOKED, NOT_SENT, MOMENT_OFF = (
    "off", "event_cancelled", "cancelled", "revoked", "not_sent", "moment_off",
)


def moments_of(row) -> dict:
    """The three moments with the defaults filled in."""
    saved = getattr(row, "moments", None) if row is not None else None
    saved = saved if isinstance(saved, dict) else {}
    out = {}
    for key in MOMENTS:
        mine = saved.get(key) if isinstance(saved.get(key), dict) else {}
        d = DEFAULTS[key]
        out[key] = {
            "enabled": bool(mine.get("enabled", d["enabled"])),
            "phrase": (str(mine.get("phrase")).strip() if mine.get("phrase") else "") or d["phrase"],
            "caption": str(mine["caption"]) if isinstance(mine.get("caption"), str) else d["caption"],
        }
    return out


def ends_at(event) -> datetime:
    start = event.event_datetime
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start + timedelta(hours=float(event.duration or 0))


def event_cancelled(event) -> bool:
    return bool(getattr(event, "is_deleted", False)) or getattr(event, "status", None) == "cancelled"


async def ticket_sent(session, registration_id) -> bool:
    from app.api.deliveries.models import Deliveries, DeliveryKind, DeliveryStatus

    return bool(await session.scalar(
        select(Deliveries.id).where(
            Deliveries.registration_id == registration_id,
            Deliveries.kind == DeliveryKind.ticket,
            Deliveries.status == DeliveryStatus.sent,
        ).limit(1)
    ))


@dataclass
class Standing:
    """Where a guest is: the moment that applies, or why none does."""

    moment: Optional[str]
    reason: Optional[str]
    sent: bool = False
    attended: bool = False
    ended: bool = False


def choose(moments: dict, *, sent: bool, attended: bool, ended: bool) -> tuple[Optional[str], Optional[str]]:
    """(moment, None), or (None, reason) when no moment is reached and on."""
    reached = {"before": sent, "during": attended and not ended, "after": ended and (sent or attended)}
    any_reached = False
    for key in CHOICE:
        if reached[key]:
            any_reached = True
            if moments[key]["enabled"]:
                return key, None
    return None, (MOMENT_OFF if any_reached else NOT_SENT)


async def standing(session, event, registration, moments: dict, now: Optional[datetime] = None) -> Standing:
    """The moment for this guest now (the switch is the caller's to check)."""
    now = now or datetime.now(timezone.utc)
    if event_cancelled(event):
        return Standing(None, EVENT_CANCELLED)
    if getattr(registration, "is_deleted", False):
        return Standing(None, CANCELLED)
    if getattr(registration, "ticket_revoked_at", None) is not None:
        return Standing(None, REVOKED)
    sent = await ticket_sent(session, registration.id)
    attended = bool(getattr(registration, "is_attended", False))
    ended = now >= ends_at(event)
    moment, reason = choose(moments, sent=sent, attended=attended, ended=ended)
    return Standing(moment, reason, sent=sent, attended=attended, ended=ended)
