"""Giving the free trial to the clubs that never had one: one admin button.

The free trial (service.start_free_trial: 14 days of Pro, no card) starts
when a club is approved. Clubs approved before it existed never had one.
`preview` says which clubs would get it now, and why the others would not;
`apply` starts it for each of them, exactly as a new club's starts.

A club is eligible when it is

- approved and not deleted;
- not a demo or showcase club (its owner signs in at a domain no mail goes
  to, settings.MAIL_SUPPRESS_DOMAINS: demo.myotherapp.in);
- has never had a trial, on any subscription row (trial_started_at);
- has no limits of its own set by an admin (club_message_quotas: a custom
  deal);
- is not on an Enterprise trial;
- has no checkout under way (pending at Razorpay);
- is not on a complimentary plan, or one that has just ended and waits for
  the sweep to put back what came before;
- is not paying or on a plan an admin set (any row that grants its plan,
  `is_entitled`, or a Razorpay subscription that can still charge).

In short: approved, real clubs on Free that never had a trial.

`apply` is idempotent: it holds a transaction lock while it reads and
starts, and a club that has had a trial is never eligible again, so running
it twice starts nothing the second time. Each club started is in the audit
log (`subscription.free_trial_gift`), and so is the run
(`subscription.free_trial_gift_run`). With `email`, each club's owner is
mailed once (notices.email_free_trial_gift, recorded in subscription_emails,
sent after the commit). When the trial ends the sweep puts the club on Free,
as for every trial (sweep.expire_free_trial): no card is ever asked for.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text

from app.api.subscriptions import service
from app.api.subscriptions.models import FEATURE_CATALOG, ClubSubscriptions, SubscriptionStatus, trial_allowances

#: How many eligible clubs the preview names.
SAMPLE = 12
#: Clubs started in one run; the rest (if ever) in the next.
MAX_PER_RUN = 2000

#: Why a club is left out, in the order the checks run.
REASONS = {
    "demo": "Demo or showcase club",
    "had_trial": "Had a trial before",
    "custom_limits": "Its own message limits (a custom deal)",
    "enterprise_trial": "On an Enterprise trial",
    "checkout": "A checkout under way",
    "complimentary": "On a complimentary plan",
    "paying": "Paying, or on a plan an admin set",
}

LOCK = "subscriptions:trial_gift"


@dataclass(frozen=True)
class Club:
    id: int
    name: str
    slug: Optional[str]
    created_at: Optional[datetime]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def why_not(row: Optional[ClubSubscriptions], now: datetime) -> Optional[str]:
    """Why a club with this live subscription row cannot be given the trial
    (a REASONS key), or None. Trials used and custom limits are found apart."""
    if row is None:
        return None
    if service.enterprise_trial_live(row, now):
        return "enterprise_trial"
    if service._status(row.status) == SubscriptionStatus.pending:
        return "checkout"
    if row.complimentary and (service.comp_live(row, now) or service.comp_over(row, now)):
        return "complimentary"
    if service.is_entitled(row, now) or service.live_at_razorpay(row):
        return "paying"
    return None


async def survey(session, now: Optional[datetime] = None) -> tuple[list[Club], dict[str, int]]:
    """The eligible clubs, oldest first, and how many were left out for each reason."""
    from app.api.clubs.models import Clubs
    from app.api.message_quota.models import ClubMessageQuotas
    from app.api.users.models import Users
    from app.core.mail import suppression

    now = now or _now()
    clubs = (
        await session.execute(
            select(Clubs.id, Clubs.name, Clubs.slug, Clubs.created_at, Users.email)
            .outerjoin(Users, Users.id == Clubs.user_id)
            .where(Clubs.is_deleted == False, Clubs.is_verified == True)  # noqa: E712
            .order_by(Clubs.id)
        )
    ).all()
    # Any row, ever, deleted ones too: a club gets one trial (service.trial_used).
    had_trial = set(
        await session.scalars(
            select(ClubSubscriptions.club_id)
            .where(ClubSubscriptions.trial_started_at.isnot(None))
            .distinct()
            .execution_options(include_deleted=True)
        )
    )
    custom = set(await session.scalars(select(ClubMessageQuotas.club_id)))
    rows = {
        r.club_id: r
        for r in await session.scalars(select(ClubSubscriptions).where(ClubSubscriptions.is_deleted == False))  # noqa: E712
    }
    eligible: list[Club] = []
    excluded = {key: 0 for key in REASONS}
    for club_id, name, slug, created_at, owner_email in clubs:
        if suppression.at_suppressed_domain(owner_email):
            reason = "demo"
        elif club_id in had_trial:
            reason = "had_trial"
        elif club_id in custom:
            reason = "custom_limits"
        else:
            reason = why_not(rows.get(club_id), now)
        if reason is None:
            eligible.append(Club(id=club_id, name=name, slug=slug, created_at=created_at))
        else:
            excluded[reason] += 1
    return eligible, excluded


async def _offer(session) -> dict:
    """The trial on offer: its plan, length, allowances and what it unlocks
    beyond Free."""
    plan = await service.plan_by_key(session, service.FREE_TRIAL_PLAN_KEY)
    free = await service.plan_by_key(session, service.DEFAULT_PLAN_KEY)
    if plan is None:
        return {"plan": None, "allowances": {}, "unlocks": []}

    def has(p, key: str) -> bool:
        return bool(p is not None and p.features is not None and getattr(p.features, key))

    unlocks = [label for key, label in FEATURE_CATALOG if has(plan, key) and not has(free, key)]
    return {"plan": plan, "allowances": trial_allowances(plan.limits), "unlocks": unlocks}


def _club_out(club: Club) -> dict:
    return {"id": club.id, "name": club.name, "slug": club.slug, "created_at": club.created_at}


async def preview(session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    eligible, excluded = await survey(session, now)
    offer = await _offer(session)
    plan = offer["plan"]
    return {
        "available": plan is not None,
        "plan_key": plan.key if plan is not None else None,
        "plan_name": plan.name if plan is not None else None,
        "days": service.FREE_TRIAL_DAYS,
        "then_plan_key": service.DEFAULT_PLAN_KEY,
        "allowances": offer["allowances"],
        "unlocks": offer["unlocks"],
        "eligible": len(eligible),
        "sample": [_club_out(c) for c in eligible[:SAMPLE]],
        "excluded": [{"reason": key, "label": REASONS[key], "count": n} for key, n in excluded.items() if n],
    }


async def apply(session, *, actor, email: bool = True, now: Optional[datetime] = None, request=None) -> dict:
    """Start the free trial for every eligible club, as a new club's starts.
    Flushes and audits; the caller commits (the mails go after the commit)."""
    from app.api.audit import service as audit
    from app.api.subscriptions import notices

    now = now or _now()
    # One run at a time: a second waits, then finds every club it would
    # have started already had its trial.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": LOCK})
    offer = await _offer(session)
    plan = offer["plan"]
    if plan is None:
        return {"started": 0, "emailed": 0, "skipped": 0, "remaining": 0, "ends_at": None, "clubs": []}
    eligible, _ = await survey(session, now)
    batch, remaining = eligible[:MAX_PER_RUN], max(0, len(eligible) - MAX_PER_RUN)
    started: list[tuple[Club, ClubSubscriptions]] = []
    skipped = emailed = 0
    for club in batch:
        row = await service.start_free_trial(session, club.id, now)
        if row is None:
            skipped += 1
            continue
        started.append((club, row))
        await audit.record(
            session, actor=actor, action="subscription.free_trial_gift", entity_type="club", entity_id=club.id,
            summary=f"Gave {club.name} the {service.FREE_TRIAL_DAYS}-day {plan.name} trial, to {notices.day(row.trial_ends_at)}",
            details={"subscription_id": row.id, "plan": plan.key, "days": service.FREE_TRIAL_DAYS,
                     "ends_at": row.trial_ends_at.isoformat(), "email": bool(email)},
            request=request,
        )
        if email and await notices.email_free_trial_gift(
            session, row, club.name, plan_name=plan.name, days=service.FREE_TRIAL_DAYS,
            unlocks=offer["unlocks"], allowances=offer["allowances"],
        ):
            emailed += 1
    ends_at = started[0][1].trial_ends_at if started else None
    await audit.record(
        session, actor=actor, action="subscription.free_trial_gift_run", entity_type="plan", entity_id=plan.id,
        summary=(f"Gave {len(started)} existing club{'s' if len(started) != 1 else ''} the "
                 f"{service.FREE_TRIAL_DAYS}-day {plan.name} trial" + (f" ({emailed} mailed)" if email else " (no mail)")),
        details={"started": len(started), "emailed": emailed, "skipped": skipped, "remaining": remaining,
                 "email": bool(email), "club_ids": [c.id for c, _ in started][:1000]},
        request=request,
    )
    await session.flush()
    return {
        "started": len(started),
        "emailed": emailed,
        "skipped": skipped,
        "remaining": remaining,
        "ends_at": ends_at,
        "clubs": [_club_out(c) for c, _ in started[:SAMPLE]],
    }
