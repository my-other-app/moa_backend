"""Counting what a club sends, against its plan's monthly allowance and
its prepaid credits.

The one call the rest of the code makes is `record_message`: a message to
the club's people (a guest, an attendee, a follower) was handed to the
provider. It adds one to the club's counter for the month in India and the
channel, under the counter's row lock, and says what paid for it: the
plan's allowance first ("plan credits", `plan_used`), then the club's
credit lots, the first to expire first (credits.consume, `credits_used`),
else nothing (`uncovered`, shown as "over": never charged). Once the plan's allowance passes 80%
and then 100% it tells the club, once each a month, by email and in the
dashboard's bell, with a "Buy credits" link.

What counts: tickets, certificates, payment messages, reminders,
announcements, campaigns, journey emails and survey mails, by email or by
WhatsApp through the PLATFORM's number. Not counted: a message a club
sends through its own WhatsApp number (`via="club"`: it pays Meta itself),
a send skipped or failed (nothing was handed over), a test send to the
organiser, and the platform's own mail to the club (billing, sign-in).

Past the plan's allowance, on every plan, sends use the club's bought (or
granted) credits. With none left, what is not a ticket, certificate or
payment message is held back (`held_back`), with the reason on the delivery
row; tickets, certificates and payment messages still go -- an attendee is
never refused for the club's plan -- and are counted as `uncovered`. Nothing is
ever charged after the fact: the month-end overage billing is retired
(billing.py).

Promotional WhatsApp (MARKETING templates, `is_marketing`) costs us about
7x a utility one, so each plan caps the share of its allowance they may use
at `whatsapp_marketing_per_month` (20% of its WhatsApp). Past that, a
promotional WhatsApp needs bought credits, which the cap does not limit;
with none, it is held back. Tickets, certificates, payment messages and
reminders (UTILITY) never are. Today the platform's number sends no
MARKETING template at all; the cap is there for the first send that does.
"""
from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Integer, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.message_quota.models import (
    ClubMessageQuotas,
    MessageStatements,
    MessageUsage,
    MessageUsageRefs,
)

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

EMAIL = "email"
WHATSAPP = "whatsapp"
CHANNELS = (EMAIL, WHATSAPP)

#: Through the platform's own provider (SES/Resend, the platform's WhatsApp
#: number): counted. Through the club's own WhatsApp number: not counted.
PLATFORM = "platform"
CLUB = "club"

#: How each limit is named on the plan.
LIMIT_KEY = {EMAIL: "emails_per_month", WHATSAPP: "whatsapp_per_month"}

#: What a message is. The transactional ones are never held back.
TRANSACTIONAL = frozenset({"ticket", "certificate", "payment"})
#: `mailbox` is held back like an announcement when nothing is left.
KINDS = frozenset({
    "ticket", "certificate", "payment",
    "reminder", "announcement", "campaign", "journey", "survey", "update", "promotion",
    # A mail the club's team writes from its mailbox (app/api/club_mailbox): one email each.
    "mailbox",
})

#: The kinds that, sent by WhatsApp, are MARKETING templates at Meta (a
#: promotion, an announcement to followers, a campaign): about ₹1.02 each to
#: us with GST against ₹0.14 for a UTILITY one, so each plan caps them at
#: `whatsapp_marketing_per_month`. The platform's number sends none of them
#: today -- its templates (tickets, certificates, reminders, payment and event
#: updates) are all UTILITY, and a club's promotions go from its own number,
#: which is never counted -- so the cap is there for the first one that does.
#: A caller that knows the template's category says so (`category=`).
MARKETING_KINDS = frozenset({"promotion", "announcement", "campaign"})

# How the club's plan is paid for (Terms.billing). Since prepaid credits it
# no longer changes what happens past the allowance -- that is the same on
# every plan -- only which upgrade is suggested.
#: The club's Razorpay subscription.
RAZORPAY = "razorpay"
#: Nothing automatic: complimentary, an Enterprise deal or trial, by hand.
MANUAL = "manual"
#: No payment method (Free, the free trial).
NONE = "none"
#: What the dashboards are told past the allowance is paid with.
PREPAID = "prepaid"

#: The warnings, as a share of the limit.
WARN_AT = (80, 100)

#: How long a counted message's idempotency key is kept.
REF_DAYS = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Months ──────────────────────────────────────────────────────────────


def period_of(at: Optional[datetime] = None) -> date:
    """The month `at` falls in, in India, as its first day."""
    local = (at or _now()).astimezone(IST)
    return date(local.year, local.month, 1)


def next_period(period: date) -> date:
    return date(period.year + (period.month == 12), period.month % 12 + 1, 1)


def bounds(period: date) -> tuple[datetime, datetime]:
    """The month's first instant and the next month's, in India, as UTC."""
    start = datetime(period.year, period.month, 1, tzinfo=IST)
    end_day = next_period(period)
    end = datetime(end_day.year, end_day.month, 1, tzinfo=IST)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def label(period: date) -> str:
    """October 2026."""
    return f"{period:%B %Y}"


def key(period: date) -> str:
    """2026-10."""
    return f"{period:%Y-%m}"


def noun(channel: str, n: Optional[int] = None) -> str:
    one, many = ("email", "emails") if channel == EMAIL else ("WhatsApp message", "WhatsApp messages")
    return one if n == 1 else many


def rupees(paise: int) -> str:
    """₹0.10, ₹1, ₹1,234.50."""
    whole, part = divmod(int(paise or 0), 100)
    return f"₹{whole:,}" if part == 0 else f"₹{whole:,}.{part:02d}"


# ─── What the club's plan allows ─────────────────────────────────────────


@dataclass(frozen=True)
class Terms:
    """A club's message limits now: from its plan (or an admin's own limits
    on Enterprise), the price of an extra one, and how the extra is paid."""

    club_id: int
    plan_key: Optional[str]
    plan_name: Optional[str]
    email_limit: Optional[int]
    whatsapp_limit: Optional[int]
    email_rate_paise: int
    whatsapp_rate_paise: int
    #: RAZORPAY | MANUAL | NONE
    billing: str
    #: An admin's own limits apply (Enterprise).
    custom: bool = False
    #: "free_trial" | "enterprise_trial" when a trial gives the plan.
    trial: Optional[str] = None
    #: Of the WhatsApp limit, how many may be promotional (null: no cap of their own).
    whatsapp_marketing_limit: Optional[int] = None

    def limit(self, channel: str) -> Optional[int]:
        return self.email_limit if channel == EMAIL else self.whatsapp_limit

    def rate(self, channel: str) -> int:
        return self.email_rate_paise if channel == EMAIL else self.whatsapp_rate_paise

    @property
    def blocks(self) -> bool:
        """Past the allowance and the credits, what is not transactional is
        held back: on every plan, since prepaid credits."""
        return True

    @property
    def where(self) -> str:
        """The plan, as a sentence says it: "Free", "your free trial"."""
        if self.trial == "free_trial":
            return "your free trial"
        return self.plan_name or "your plan"


def _billing(resolved) -> str:
    from app.api.subscriptions import service as subs
    from app.api.subscriptions.models import SubscriptionStatus

    plan = resolved.plan
    if plan is None:
        return NONE
    if resolved.trial is not None:
        # The free trial has no card; an Enterprise trial is a sales deal.
        return NONE if resolved.trial[0] == "free_trial" else MANUAL
    free_plan = (
        not plan.contact_sales and int(plan.price_monthly_paise or 0) == 0 and int(plan.price_yearly_paise or 0) == 0
    )
    sub = resolved.subscription
    if sub is None:
        # The default plan, or what the club had before a checkout pending now.
        return NONE if plan.key == subs.DEFAULT_PLAN_KEY or free_plan else MANUAL
    if sub.complimentary:
        return MANUAL
    status = subs._status(sub.status)
    if sub.razorpay_subscription_id and status in (
        SubscriptionStatus.active, SubscriptionStatus.past_due, SubscriptionStatus.trial,
    ):
        return RAZORPAY
    if free_plan:
        return NONE
    # A deal invoiced by hand, or a subscription cancelled and running out.
    return MANUAL


async def terms_for(session, club_id: int) -> Terms:
    """The club's message terms as of now."""
    from app.api.subscriptions import service as subs

    resolved = await subs.resolve_entitlements(session, club_id)
    plan = resolved.plan
    email_limit = resolved.limits.get(LIMIT_KEY[EMAIL])
    whatsapp_limit = resolved.limits.get(LIMIT_KEY[WHATSAPP])
    email_rate = int(plan.email_overage_paise or 0) if plan is not None else 0
    whatsapp_rate = int(plan.whatsapp_overage_paise or 0) if plan is not None else 0
    custom = False
    if plan is not None and plan.key == subs.ENTERPRISE_PLAN_KEY:
        own = await session.get(ClubMessageQuotas, club_id)
        if own is not None:
            custom = True
            email_limit, whatsapp_limit = own.emails_per_month, own.whatsapp_per_month
            if own.email_overage_paise is not None:
                email_rate = own.email_overage_paise
            if own.whatsapp_overage_paise is not None:
                whatsapp_rate = own.whatsapp_overage_paise
    return Terms(
        club_id=club_id,
        plan_key=plan.key if plan is not None else None,
        plan_name=plan.name if plan is not None else None,
        email_limit=email_limit,
        whatsapp_limit=whatsapp_limit,
        email_rate_paise=email_rate,
        whatsapp_rate_paise=whatsapp_rate,
        billing=_billing(resolved),
        custom=custom,
        trial=resolved.trial[0] if resolved.trial is not None else None,
        whatsapp_marketing_limit=resolved.limits.get(MARKETING_LIMIT_KEY),
    )


#: The plan's cap on promotional WhatsApp messages a month.
MARKETING_LIMIT_KEY = "whatsapp_marketing_per_month"


def is_marketing(channel: str, kind: Optional[str], category: Optional[str] = None) -> bool:
    """A promotional (MARKETING-category) WhatsApp message: by the template's
    `category` when the caller knows it, else by its kind (MARKETING_KINDS).
    Never a ticket, certificate or payment message, never an email."""
    if channel != WHATSAPP or (kind or "") in TRANSACTIONAL:
        return False
    if category:
        return category.strip().upper() == "MARKETING"
    return (kind or "") in MARKETING_KINDS


# ─── Holding back (nothing left) ────────────────────────────────────────


def _credits_word(channel: str) -> str:
    return "email credits" if channel == EMAIL else "WhatsApp credits"


def _marketing_reason(terms: Terms, cap: int) -> str:
    if cap == 0:
        return (
            f"Not sent: promotional WhatsApp messages aren't included on {terms.where}, and there are no "
            "WhatsApp credits left. Buy credits to send them."
        )
    return (
        f"Not sent: this month's {cap:,} promotional WhatsApp {'message' if cap == 1 else 'messages'} on {terms.where} "
        "are used up and there are no WhatsApp credits left. Tickets, certificates and reminders still go."
    )


def _held_reason(terms: Terms, channel: str, limit: int, kind: str) -> str:
    if limit == 0:
        many = noun(channel)
        what = "WhatsApp reminders" if (channel == WHATSAPP and kind == "reminder") else many[0].upper() + many[1:]
        return (
            f"Not sent: {what} aren't included on {terms.where}, and there are no {_credits_word(channel)} left. "
            "Buy credits to send them."
        )
    return (
        f"Not sent: this month's {limit:,} {noun(channel, limit)} on {terms.where} are used up and there are no "
        f"{_credits_word(channel)} left. Buy credits to send more; tickets and certificates still go."
    )


async def held_back(
    session, terms: Optional[Terms], channel: str, kind: str, *, via: str = PLATFORM, at: Optional[datetime] = None,
    category: Optional[str] = None,
) -> Optional[str]:
    """Why this send is held back, or None when it may go. Never a
    transactional message, never one through the club's own number. Held
    back when the club has no bought credits left on the channel and:

    - its plan's monthly allowance is used up (any plan with a limit);
    - or it is a promotional WhatsApp (`is_marketing`) and the plan's
      `whatsapp_marketing_per_month` share of the allowance is used up.

    A held-back send is counted as `blocked` for the month.

    Checked before the send rather than reserved: workers sending for one
    club at once can each find room for the last message, so a club may go
    a message or two (at most the pipeline's MAIL_CONCURRENCY) past what it
    has; those are counted as `over`, never charged.

    Contract for other senders (the club mailbox): keep this signature.
    """
    from app.api.message_quota import credits
    from app.api.subscriptions import gates

    if terms is None or via == CLUB or kind in TRANSACTIONAL or not gates.enforcing():
        return None
    limit = terms.limit(channel)
    marketing_cap = terms.whatsapp_marketing_limit if is_marketing(channel, kind, category) else None
    if limit is None and marketing_cap is None:
        return None
    period = period_of(at)
    counts = (await session.execute(
        select(MessageUsage.plan_used, MessageUsage.marketing).where(
            MessageUsage.club_id == terms.club_id, MessageUsage.period == period, MessageUsage.channel == channel
        )
    )).first()
    plan_used, marketing = (int(counts[0] or 0), int(counts[1] or 0)) if counts is not None else (0, 0)
    if limit is not None and plan_used >= limit:
        reason = _held_reason(terms, channel, limit, kind)
    elif marketing_cap is not None and marketing >= marketing_cap:
        reason = _marketing_reason(terms, marketing_cap)
    else:
        return None
    if await credits.bought_remaining(session, terms.club_id, channel, at) > 0:
        return None
    await _upsert_blocked(session, terms, channel, period)
    return reason


# ─── Counting ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Recorded:
    #: False when this ref was counted before (nothing added now).
    counted: bool
    used: Optional[int] = None
    limit: Optional[int] = None
    #: How these messages were paid for: the plan's allowance, credit lots,
    #: or neither (a ticket with nothing left; never charged).
    from_plan: int = 0
    from_credits: int = 0
    over: int = 0

    @property
    def extra(self) -> int:
        """Past the plan's allowance this month."""
        if self.used is None or self.limit is None:
            return 0
        return max(0, self.used - self.limit)


@asynccontextmanager
async def session_scope():
    """A session of its own, for a caller that has none. Tests swap it."""
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


def _new_row(terms: Terms, channel: str, period: date, now: datetime, **counts) -> dict:
    return dict(
        club_id=terms.club_id, period=period, channel=channel, quota=terms.limit(channel), rate_paise=0,
        billing=terms.billing, plan_key=terms.plan_key, created_at=now, updated_at=now, **counts,
    )


async def _upsert_blocked(session, terms: Terms, channel: str, period: date) -> None:
    """One more send held back this month: one statement, atomic."""
    now = _now()
    table = MessageUsage.__table__
    stmt = pg_insert(table).values(**_new_row(terms, channel, period, now, blocked=1))
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_message_usage_club_period_channel",
            set_={"blocked": table.c.blocked + 1, "updated_at": now},
        )
    )


async def _month_row(session, terms: Terms, channel: str, period: date) -> MessageUsage:
    """The month's counter, made if need be, under its row lock (held to the
    caller's commit): two senders for one club take turns at the split
    between the plan's allowance and the credits."""
    now = _now()
    await session.execute(
        pg_insert(MessageUsage.__table__)
        .values(**_new_row(terms, channel, period, now))
        .on_conflict_do_nothing(constraint="uq_message_usage_club_period_channel")
    )
    return await session.scalar(
        select(MessageUsage)
        .where(MessageUsage.club_id == terms.club_id, MessageUsage.period == period, MessageUsage.channel == channel)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def record_message(
    club_id: Optional[int],
    channel: str,
    kind: str,
    *,
    via: str = PLATFORM,
    session=None,
    ref: Optional[str] = None,
    count: int = 1,
    at: Optional[datetime] = None,
    terms: Optional[Terms] = None,
    category: Optional[str] = None,
) -> Optional[Recorded]:
    """Count `count` messages of `kind` the club's provider took on
    `channel` ("email" or "whatsapp"). Call it once the provider accepted
    the message -- never for one skipped or failed.

    `category`: the WhatsApp template's category at Meta ("UTILITY" or
    "MARKETING"), when the caller knows it; a promotional one
    (`is_marketing`) is also counted against the plan's promotional cap.

    `via="club"`: sent through the club's own WhatsApp number, which the
    club pays Meta for. Nothing is counted (returns None).

    `ref`: the message's idempotency key ("delivery:<id>:<queued>"). A ref
    already counted counts nothing (returns `counted=False`), so recording
    the same message twice -- a retried job, a worker's record run again --
    adds one, once.

    `session`: counted in the caller's transaction (it commits); without
    one, in a session of its own, committed here. `terms`: the club's, when
    the caller already has them (one read per send, not per message).

    Crossing 80% and 100% of the month's limit tells the club, once each a
    month: a notice in the dashboard's bell and a mail to the club's owner
    (after the commit). Returns what the counter says now.
    """
    if via == CLUB:
        return None
    if via != PLATFORM:
        raise ValueError(f"via is 'platform' or 'club', not {via!r}")
    if channel not in CHANNELS:
        raise ValueError(f"channel is 'email' or 'whatsapp', not {channel!r}")
    if club_id is None or count <= 0:
        return None
    kind = (kind or "message")[:30]
    if session is None:
        async with session_scope() as own:
            out = await _record(own, club_id, channel, kind, ref=ref, count=count, at=at, terms=terms, category=category)
            await own.commit()
            return out
    return await _record(session, club_id, channel, kind, ref=ref, count=count, at=at, terms=terms, category=category)


async def _record(session, club_id, channel, kind, *, ref, count, at, terms, category=None) -> Recorded:
    from app.api.message_quota import credits

    if ref:
        fresh = await session.scalar(
            pg_insert(MessageUsageRefs)
            .values(ref=ref[:200], created_at=_now())
            .on_conflict_do_nothing(index_elements=["ref"])
            .returning(MessageUsageRefs.ref)
        )
        if fresh is None:
            return Recorded(counted=False)
    if terms is None or terms.club_id != club_id:
        terms = await terms_for(session, club_id)
    period = period_of(at)
    marketing = count if is_marketing(channel, kind, category) else 0
    row = await _month_row(session, terms, channel, period)
    limit = terms.limit(channel)
    # What is left of the plan's allowance; for a promotional WhatsApp, also
    # of the plan's promotional share. Bought credits have no such share.
    room = None if limit is None else max(0, limit - int(row.plan_used or 0))
    cap = terms.whatsapp_marketing_limit if marketing else None
    if cap is not None:
        promo_room = max(0, cap - int(row.marketing or 0))
        room = promo_room if room is None else min(room, promo_room)
    from_plan = count if room is None else min(count, room)
    from_credits = await credits.consume(session, terms, channel, count - from_plan, at) if count > from_plan else 0
    over = count - from_plan - from_credits
    now = _now()
    table = MessageUsage.__table__
    current = func.coalesce(cast(table.c.kinds[kind].astext, Integer), 0)
    updated = (await session.execute(
        update(table)
        .where(table.c.id == row.id)
        .values(
            used=table.c.used + count, plan_used=table.c.plan_used + from_plan,
            credits_used=table.c.credits_used + from_credits, uncovered=table.c.uncovered + over,
            marketing=table.c.marketing + marketing,
            kinds=table.c.kinds.op("||")(func.jsonb_build_object(kind, current + count)),
            # The terms the club is sending under now.
            quota=limit, rate_paise=0, billing=terms.billing, plan_key=terms.plan_key, updated_at=now,
        )
        .returning(table.c.id, table.c.used, table.c.plan_used, table.c.warned_80_at, table.c.warned_100_at)
    )).one()
    await _maybe_warn(session, terms, channel, period, updated, limit)
    return Recorded(counted=True, used=int(updated.used), limit=limit, from_plan=from_plan,
                    from_credits=from_credits, over=over)


async def count(
    session, *, club_id: Optional[int], channel: str, kind: str, via: str = PLATFORM, ref: Optional[str] = None,
    terms: Optional[Terms] = None, category: Optional[str] = None,
) -> Optional[Recorded]:
    """`record_message` in a SAVEPOINT of the caller's transaction, never
    raising: the senders call it in the transaction that records a message
    sent, and a count that cannot be written must not undo that record (the
    message would be sent again)."""
    try:
        async with session.begin_nested():
            return await record_message(
                club_id, channel, kind, via=via, session=session, ref=ref, terms=terms, category=category,
            )
    except Exception:  # noqa: BLE001
        logger.warning("club %s: a %s %s could not be counted", club_id, kind, channel, exc_info=True)
        return None


async def check(
    session, club_id: Optional[int], channel: str, kind: str, *, via: str = PLATFORM, terms: Optional[Terms] = None,
    cache: Optional[dict] = None, category: Optional[str] = None,
) -> tuple[Optional[Terms], Optional[str]]:
    """The club's terms and why this send is held back (None: it may go), in
    a SAVEPOINT, never raising: when the plan cannot be read the send goes.
    `cache` (club id -> Terms) is a send's own, so a thousand guests read the
    club's plan once."""
    if club_id is None:
        return None, None
    try:
        async with session.begin_nested():
            if terms is None and cache is not None:
                terms = cache.get(club_id)
            if terms is None:
                terms = await terms_for(session, club_id)
                if cache is not None:
                    cache[club_id] = terms
            return terms, await held_back(session, terms, channel, kind, via=via, category=category)
    except Exception:  # noqa: BLE001
        logger.warning("club %s: could not check its %s quota; sending", club_id, channel, exc_info=True)
        return terms, None


def threshold(limit: int, level: int) -> int:
    """The count at which `level`% of `limit` is reached (at least one)."""
    return max(1, math.ceil(limit * level / 100))


async def _maybe_warn(session, terms: Terms, channel: str, period: date, row, limit: Optional[int]) -> None:
    """Tell the club the first time its plan's allowance is 80%, and 100%,
    used this month. The claim is one UPDATE ... WHERE warned_N_at IS NULL,
    so of two workers crossing it at once one tells. When both are reached
    at once (a limit lowered mid-month), only the 100% one is said."""
    if limit is None or limit <= 0:
        return
    used = int(row.plan_used)
    due = [lvl for lvl in sorted(WARN_AT, reverse=True) if used >= threshold(limit, lvl) and getattr(row, f"warned_{lvl}_at") is None]
    if not due:
        return
    level = due[0]
    now = _now()
    marks = {f"warned_{lvl}_at": func.coalesce(getattr(MessageUsage, f"warned_{lvl}_at"), now) for lvl in WARN_AT if lvl <= level}
    claimed = await session.scalar(
        update(MessageUsage)
        .where(MessageUsage.id == row.id, getattr(MessageUsage, f"warned_{level}_at").is_(None))
        .values(**marks)
        .returning(MessageUsage.id)
        .execution_options(synchronize_session=False)
    )
    if claimed is None:
        return
    try:
        await warn(session, terms, channel, period, level=level, used=used, limit=limit)
    except Exception:  # noqa: BLE001 - a warning that cannot go must not undo the count
        logger.warning("club %s: the %s%% %s warning could not be raised", terms.club_id, level, channel, exc_info=True)


def warning_words(terms: Terms, channel: str, period: date, *, level: int, used: int, limit: int, bought: int = 0) -> dict:
    """The notice and the mail: title, body and the mail's paragraphs.
    `bought`: the club's credits left on the channel."""
    many = noun(channel, limit)
    month = label(period)
    if level >= 100:
        title = f"You've used all {limit:,} {many} in your plan for {month}"
    else:
        title = f"You've used {level}% of your plan's {many} for {month}"
    counted = f"{used:,} of the {limit:,} {many} on {terms.where} used this month."
    if bought:
        then = (
            f"Past {limit:,}, messages use your bought credits: {bought:,} {_credits_word(channel)} left. "
            "With none left, announcements, campaigns and reminders pause until next month; tickets, "
            "certificates and payment messages always go."
        )
    else:
        then = (
            f"Past {limit:,}, announcements, campaigns and reminders pause until next month unless you buy credits. "
            "Tickets, certificates and payment messages always go."
        )
    return {"title": title, "body": f"{counted} {then}", "paragraphs": [counted, then]}


async def warn(session, terms: Terms, channel: str, period: date, *, level: int, used: int, limit: int) -> None:
    """The bell, with a "Buy credits" link, and a mail to the club's owner after the commit."""
    from app.api import club_alerts
    from app.api.clubs.models import Clubs
    from app.api.message_quota import credits
    from app.api.subscriptions import notices
    from app.core.after_commit import run_after_commit

    bought = await credits.bought_remaining(session, terms.club_id, channel)
    words = warning_words(terms, channel, period, level=level, used=used, limit=limit, bought=bought)
    await club_alerts.notify(
        session, terms.club_id, f"message_quota_{level}",
        title=words["title"], body=words["body"], link=credits.BILLING_LINK,
        data={"channel": channel, "period": key(period), "level": level, "used": used, "limit": limit,
              "credits_left": bought, "action_label": "Buy credits"},
        dedupe_key=f"messages:{key(period)}:{channel}:{level}",
    )
    to = await notices.owner_address(session, terms.club_id)
    if not to:
        return
    club = await session.get(Clubs, terms.club_id)
    name = club.name if club is not None else "there"
    run_after_commit(
        session, notices._send, to, words["title"], [f"Hi {name},", *words["paragraphs"]],
        ["Buy credits", credits.credits_url()],
        "You get this at 80% and at 100% of your plan's monthly allowance, once each.",
    )


# ─── What the dashboards read ────────────────────────────────────────────


def _state(plan_used: int, used: int, limit: Optional[int], bought: int) -> str:
    """unlimited | not_included | ok | near (80% of the plan's allowance) |
    credits (the allowance used up, sending on bought credits) | blocked
    (nothing left: what is not transactional is held back)."""
    if limit is None:
        return "unlimited"
    if plan_used < limit:
        return "near" if plan_used >= threshold(limit, 80) else "ok"
    if bought > 0:
        return "credits"
    if limit == 0 and used == 0:
        return "not_included"
    return "blocked"


async def _upgrade(session, terms: Terms, channels: dict) -> Optional[dict]:
    """The cheapest public plan with room for more of what is near or past
    its allowance, when the club is not already on something kinder."""
    from app.api.subscriptions import gates

    if terms.billing == MANUAL:
        return None
    tight = [ch for ch, c in channels.items() if c["state"] in ("near", "credits", "blocked")]
    if not tight:
        return None
    cards = [c for c in await gates._public_cards(session) if c.key != terms.plan_key]
    best = None
    for ch in tight:
        c = channels[ch]
        card = gates.plan_for_limit(cards, LIMIT_KEY[ch], current=c["limit"], needed=c["used"] + 1)
        if card is not None and (best is None or (card.contact_sales, card.price_monthly_paise) > (best.contact_sales, best.price_monthly_paise)):
            best = card
    if best is None:
        return None
    return {"plan_key": best.key, "plan_name": best.name, "contact_sales": best.contact_sales,
            "limits": {ch: best.limits.get(LIMIT_KEY[ch]) for ch in CHANNELS}}


def _percent(used: int, limit: Optional[int]) -> Optional[int]:
    """How much of the limit is used, in whole percent (null: unlimited)."""
    if limit is None:
        return None
    if limit == 0:
        return 100 if used else 0
    return min(999, math.floor(used * 100 / limit))


def _channel_view(terms: Terms, channel: str, row: Optional[MessageUsage], bought: int) -> dict:
    used = int(row.used) if row is not None else 0
    plan_used = int(row.plan_used or 0) if row is not None else 0
    limit = terms.limit(channel)
    plan_left = None if limit is None else max(0, limit - plan_used)
    out = {
        "used": used,
        "limit": limit,
        # Past the plan's allowance (paid from credits, or over).
        "extra": max(0, used - limit) if limit is not None else 0,
        "blocked": int(row.blocked) if row is not None else 0,
        # Nothing is billed per message any more: kept for older dashboards.
        "rate_paise": 0,
        "extra_paise": 0,
        "percent": _percent(plan_used, limit),
        "state": _state(plan_used, used, limit, bought),
        "kinds": dict(row.kinds or {}) if row is not None else {},
        "warned": [lvl for lvl in WARN_AT if row is not None and getattr(row, f"warned_{lvl}_at") is not None],
        "plan_used": plan_used,
        "plan_remaining": plan_left,
        "credits_used": int(row.credits_used or 0) if row is not None else 0,
        "over": int(row.uncovered or 0) if row is not None else 0,
        "credits_remaining": bought,
        "total_remaining": None if plan_left is None else plan_left + bought,
    }
    if channel == WHATSAPP:
        # Of those, the promotional ones, against the plan's cap on them.
        out["marketing"] = {
            "used": int(row.marketing or 0) if row is not None else 0,
            "limit": terms.whatsapp_marketing_limit,
        }
    return out


async def usage(session, club_id: int, now: Optional[datetime] = None) -> dict:
    """This month so far: each channel against the plan's allowance, the
    credits used and left, and the plan to move to when it would help."""
    from app.api.message_quota import credits

    now = now or _now()
    terms = await terms_for(session, club_id)
    period = period_of(now)
    rows = {
        r.channel: r
        for r in await session.scalars(
            select(MessageUsage).where(MessageUsage.club_id == club_id, MessageUsage.period == period)
        )
    }
    channels = {
        ch: _channel_view(terms, ch, rows.get(ch), await credits.bought_remaining(session, club_id, ch, now))
        for ch in CHANNELS
    }
    start, end = bounds(period)
    return {
        "period": key(period),
        "period_label": label(period),
        "period_start": start,
        "period_end": end,
        "plan_key": terms.plan_key,
        "plan_name": terms.plan_name,
        "billing": PREPAID,
        "blocks_past_limit": any(terms.limit(ch) is not None for ch in CHANNELS),
        "custom": terms.custom,
        "trial": terms.trial,
        "channels": channels,
        "estimated_extra_paise": 0,
        "upgrade": await _upgrade(session, terms, channels),
    }


def _month_channel(row: Optional[MessageUsage]) -> dict:
    if row is None:
        return {"used": 0, "limit": None, "extra": 0, "rate_paise": 0, "plan_used": 0, "credits_used": 0, "over": 0, "blocked": 0}
    used, limit = int(row.used), row.quota
    return {
        "used": used, "limit": limit, "extra": max(0, used - limit) if limit is not None else 0, "rate_paise": 0,
        "plan_used": int(row.plan_used or 0), "credits_used": int(row.credits_used or 0), "over": int(row.uncovered or 0),
        "blocked": int(row.blocked or 0),
    }


async def history(session, club_id: int, *, months: int = 12, now: Optional[datetime] = None) -> list[dict]:
    """The closed months, newest first: what was sent and how it was paid
    for. A month the retired overage billing had a statement for carries
    its status (`void`: cancelled, never charged)."""
    current = period_of(now)
    periods = list(
        await session.scalars(
            select(MessageUsage.period)
            .where(MessageUsage.club_id == club_id, MessageUsage.period < current)
            .group_by(MessageUsage.period)
            .order_by(MessageUsage.period.desc())
            .limit(months)
        )
    )
    if not periods:
        return []
    rows: dict = {}
    for r in await session.scalars(
        select(MessageUsage).where(MessageUsage.club_id == club_id, MessageUsage.period.in_(periods))
    ):
        rows.setdefault(r.period, {})[r.channel] = r
    statements = {
        s.period: s
        for s in await session.scalars(
            select(MessageStatements).where(MessageStatements.club_id == club_id, MessageStatements.period.in_(periods))
        )
    }
    out = []
    for period in periods:
        by = rows.get(period, {})
        st = statements.get(period)
        plan_key = next((r.plan_key for r in by.values() if r.plan_key), None)
        charged = st is not None and st.status == "charged"
        out.append({
            "id": period.year * 100 + period.month,
            "period": key(period),
            "period_label": label(period),
            "plan_key": plan_key,
            "billing": PREPAID,
            "email": _month_channel(by.get(EMAIL)),
            "whatsapp": _month_channel(by.get(WHATSAPP)),
            "amount_paise": int(st.amount_paise) if charged else 0,
            "status": st.status if st is not None and st.status != "nothing_due" else "nothing_due",
            "charged_at": st.charged_at if charged else None,
        })
    return out


def own_limits_out(row: Optional[ClubMessageQuotas]) -> Optional[dict]:
    if row is None:
        return None
    return {
        "emails_per_month": row.emails_per_month,
        "whatsapp_per_month": row.whatsapp_per_month,
        "email_overage_paise": row.email_overage_paise,
        "whatsapp_overage_paise": row.whatsapp_overage_paise,
        "note": row.note,
        "updated_at": row.updated_at,
    }
