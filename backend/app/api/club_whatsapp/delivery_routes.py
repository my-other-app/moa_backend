"""Which of a club's WhatsApp deliveries go from its own number, why one went
from MyOtherApp's instead, and how many did this month.

- Routing by kind: under the account's `routing` (the master switch, which
  stays as it was), a switch for each of tickets, certificates and
  reminders (`route_tickets` ...). service.route_for asks `kind_on`; a kind
  switched off goes from MyOtherApp's number as for any club, and that is
  the club's choice, not a fallback.
- Why not: a WhatsApp delivery the club wanted from its own number (a live
  number, routing on for the kind) that went from MyOtherApp's says why --
  Meta's refusal (its code and words, from the send or its webhook) or
  ours (the number failed, restricted, the plan, no approved template).
  deliveries.service keeps it on the delivery and on each attempt
  (`via`, `fallback_code`, `fallback_reason`).
- This month: the sends from the club's number (deliveries, broadcasts,
  inbox) and the fallbacks by reason, for the club's settings; for the
  admins, each club's fallbacks and the clubs with the most.

Nothing here says what any message said: a reason is Meta's error or ours.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.api.club_whatsapp import crypto
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import FAILED, ClubWhatsAppAccounts

TICKETS, CERTIFICATES, REMINDERS = "tickets", "certificates", "reminders"
KINDS = (TICKETS, CERTIFICATES, REMINDERS)
#: The template each kind is sent with (deliveries.service.TEMPLATE_OF) and its switch.
KIND_OF_TEMPLATE = {"ticket_confirmed": TICKETS, "certificate_ready": CERTIFICATES, "event_reminder": REMINDERS}
COLUMN_OF_KIND = {TICKETS: "route_tickets", CERTIFICATES: "route_certificates", REMINDERS: "route_reminders"}

#: Our own fallback codes, beside Meta's numbers.
FAILED_CODE = "ACCOUNT_FAILED"
KEYS_CODE = "KEYS"
RESTRICTED_CODE = "RESTRICTED"
PLAN_CODE = "PLAN"
NO_TEMPLATE_CODE = "NO_TEMPLATE"
TEMPLATE_UNUSABLE_CODE = "TEMPLATE_UNUSABLE"


def kind_on(account: ClubWhatsAppAccounts, template: Optional[str]) -> bool:
    """Whether this template's kind goes from the club's number (its own
    switch; the master `routing` is the caller's). A template of no kind
    of ours is never stopped here."""
    kind = KIND_OF_TEMPLATE.get(template or "")
    return kind is None or bool(getattr(account, COLUMN_OF_KIND[kind], True))


def kinds(account: Optional[ClubWhatsAppAccounts]) -> dict[str, bool]:
    return {k: True if account is None else bool(getattr(account, COLUMN_OF_KIND[k], True)) for k in KINDS}


async def set_kinds(session, club, user, values: dict[str, Optional[bool]], *, request=None) -> ClubWhatsAppAccounts:
    """The kinds switched on or off (None: left as it is); audited."""
    from app.api.audit import service as audit

    account = await accounts.live_account(session, club.id) or await accounts.latest_account(session, club.id)
    if account is None:
        raise accounts._refuse(404, "Connect your WhatsApp number first.", "NO_WHATSAPP")
    before = kinds(account)
    for kind, value in values.items():
        if kind in COLUMN_OF_KIND and value is not None:
            setattr(account, COLUMN_OF_KIND[kind], bool(value))
    after = kinds(account)
    if before != after:
        await audit.record(
            session, actor=user, action="club_whatsapp.settings", entity_type="club", entity_id=club.id,
            summary="Changed which WhatsApp deliveries go from the club's number",
            details={"account_id": account.id, "before": {"kinds": before}, "after": {"kinds": after}}, request=request,
        )
    await session.commit()
    return account


# ─── Why a delivery went from MyOtherApp's number ────────────────────────


async def why_not(session, club_id: Optional[int], template: Optional[str], *, cache: Optional[dict] = None) -> Optional[tuple[str, str]]:
    """(code, words) when the club wants this template's deliveries from its
    own number and it can't take them now; None when the club has no live
    number, or chose MyOtherApp's (routing off, or off for this kind).
    `cache` is a send's own (deliveries' run.routes), so a thousand guests
    ask once."""
    key = ("why", club_id, template)
    if cache is not None and key in cache:
        return cache[key]
    out = await _why_not(session, club_id, template)
    if cache is not None:
        cache[key] = out
    return out


async def _why_not(session, club_id: Optional[int], template: Optional[str]) -> Optional[tuple[str, str]]:
    from app.api.club_whatsapp import templates

    if club_id is None or not template:
        return None
    account = await accounts.live_account(session, club_id)
    if account is None or not account.routing or not kind_on(account, template):
        return None
    if account.status == FAILED:
        return (account.last_error_code or FAILED_CODE)[:40], (account.last_error or "Your WhatsApp number isn't working.")[:300]
    if not crypto.available():
        return KEYS_CODE, "MyOtherApp can't read your number's keys right now."
    if accounts.sending_restricted(account):
        return RESTRICTED_CODE, "Meta has restricted your WhatsApp account."
    if not await accounts.plan_allows(session, club_id):
        return PLAN_CODE, "Your plan no longer includes your own WhatsApp number."
    if await templates.usable_language(session, account.id, template) is None:
        unusable = await session.scalar(
            select(templates.ClubWhatsAppTemplates).where(
                templates.ClubWhatsAppTemplates.account_id == account.id, templates.ClubWhatsAppTemplates.name == template,
                templates.ClubWhatsAppTemplates.status == templates.UNUSABLE,
            ).limit(1)
        )
        if unusable is not None:
            return TEMPLATE_UNUSABLE_CODE, f"Meta refused your {template} template: {unusable.rejected_reason or 'it can not be used'}"[:300]
        return NO_TEMPLATE_CODE, f"Your account has no approved {template} template in the shape MyOtherApp sends."[:300]
    return None


def refusal_words(exc: Exception) -> tuple[str, str]:
    """A refusal from the club's number as a fallback: Meta's code and its words."""
    code = getattr(exc, "code", None) or type(exc).__name__
    return str(code)[:40], str(getattr(exc, "reason", None) or exc or "Meta refused it")[:300]


# ─── This month ──────────────────────────────────────────────────────────


def month_start(now: Optional[datetime] = None) -> datetime:
    """The first moment of this month, India's calendar (the dashboard's)."""
    from app.api.events.documents import EVENT_TIMEZONE

    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(EVENT_TIMEZONE))
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _attempts(start: datetime):
    """WhatsApp delivery attempts since `start`, with their club."""
    from app.api.deliveries.models import Deliveries, DeliveryAttempts
    from app.api.events.models import Events

    A = DeliveryAttempts
    return (
        select(A).join(Deliveries, Deliveries.id == A.delivery_id).join(Events, Events.id == Deliveries.event_id)
        .where(A.created_at >= start, A.via.isnot(None))
    )


async def club_month(session, club_id: int, now: Optional[datetime] = None) -> dict:
    """What went from the club's own number this month, and what fell back
    to MyOtherApp's, by reason. Counts only: no message, no person."""
    from app.api.club_whatsapp.models import FAILED_MESSAGE, OUT, VIA_INBOX, ClubWhatsAppMessages
    from app.api.club_whatsapp.reach_models import ClubWhatsAppBroadcastRecipients
    from app.api.events.models import Events

    start = month_start(now)
    q = _attempts(start).where(Events.club_id == club_id).subquery()
    deliveries = int(await session.scalar(select(func.count()).select_from(q).where(q.c.via == "club")) or 0)
    rows = (await session.execute(
        select(q.c.fallback_code, func.count(), func.min(q.c.fallback_reason))
        .where(q.c.fallback_code.isnot(None)).group_by(q.c.fallback_code).order_by(func.count().desc(), q.c.fallback_code)
    )).all()
    R = ClubWhatsAppBroadcastRecipients
    broadcasts = int(await session.scalar(
        select(func.count()).select_from(R).where(R.club_id == club_id, R.sent_at >= start)
    ) or 0)
    M = ClubWhatsAppMessages
    inbox = int(await session.scalar(
        select(func.count()).select_from(M).where(
            M.club_id == club_id, M.created_at >= start, M.direction == OUT, M.via == VIA_INBOX, M.status != FAILED_MESSAGE,
        )
    ) or 0)
    fallbacks = [{"code": code, "reason": reason, "count": int(n)} for code, n, reason in rows]
    return {
        "since": start,
        "from_your_number": {"deliveries": deliveries, "broadcasts": broadcasts, "inbox": inbox,
                             "total": deliveries + broadcasts + inbox},
        "fallbacks": fallbacks,
        "fallback_total": sum(f["count"] for f in fallbacks),
    }


async def admin_month(session, club_id: int, now: Optional[datetime] = None) -> dict:
    """The admin's club card: this month's sends from the club's number and fallbacks."""
    from app.api.events.models import Events

    q = _attempts(month_start(now)).where(Events.club_id == club_id).subquery()
    row = (await session.execute(
        select(func.count().filter(q.c.via == "club"), func.count().filter(q.c.fallback_code.isnot(None))).select_from(q)
    )).one()
    return {"club_sends_this_month": int(row[0] or 0), "fallbacks_this_month": int(row[1] or 0)}


async def top_fallbacks(session, *, limit: int = 10, now: Optional[datetime] = None) -> list[dict]:
    """The clubs whose WhatsApp deliveries fell back to MyOtherApp's number
    most this month, with each one's commonest reason."""
    from app.api.clubs.models import Clubs
    from app.api.deliveries.models import Deliveries, DeliveryAttempts as A
    from app.api.events.models import Events

    start = month_start(now)
    base = (
        select(Events.club_id.label("club_id"), A.fallback_code.label("code"), A.fallback_reason.label("reason"))
        .select_from(A).join(Deliveries, Deliveries.id == A.delivery_id).join(Events, Events.id == Deliveries.event_id)
        # ix_delivery_attempts_fallback: this month's fallbacks, and no other attempt.
        .where(A.fallback_code.isnot(None), A.created_at >= start)
    ).subquery()
    top = (await session.execute(
        select(base.c.club_id, Clubs.name, func.count().label("n"))
        .join(Clubs, Clubs.id == base.c.club_id)
        .group_by(base.c.club_id, Clubs.name).order_by(func.count().desc(), base.c.club_id).limit(max(1, min(limit, 50)))
    )).all()
    if not top:
        return []
    ids = [r[0] for r in top]
    reasons: dict[int, tuple] = {}
    for club_id, code, n, reason in (await session.execute(
        select(base.c.club_id, base.c.code, func.count(), func.min(base.c.reason))
        .where(base.c.club_id.in_(ids)).group_by(base.c.club_id, base.c.code)
        .order_by(base.c.club_id, func.count().desc(), base.c.code)
    )).all():
        reasons.setdefault(club_id, (code, reason, int(n)))
    return [
        {"club_id": club_id, "club_name": name, "fallbacks": int(n), "top_code": reasons.get(club_id, (None,))[0],
         "top_reason": reasons.get(club_id, (None, None))[1]}
        for club_id, name, n in top
    ]
