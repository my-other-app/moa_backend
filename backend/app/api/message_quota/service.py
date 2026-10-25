"""Counting what a club sends, against its plan's monthly limits.

The one call the rest of the code makes is `record_message`: a message to
the club's people (a guest, an attendee, a follower) was handed to the
provider. It adds one to the club's counter for the month in India and the
channel, atomically, and -- once the count passes 80% and then 100% of the
plan's limit -- tells the club, once each a month, by email and in the
dashboard's bell.

What counts: tickets, certificates, payment messages, reminders,
announcements, campaigns, journey emails and survey mails, by email or by
WhatsApp through the PLATFORM's number. Not counted: a message a club
sends through its own WhatsApp number (`via="club"`: it pays Meta itself),
a send skipped or failed (nothing was handed over), a test send to the
organiser, and the platform's own mail to the club (billing, sign-in).

Past the limit:

- a plan Razorpay bills goes on sending, and each extra message is added to
  the club's next invoice at the plan's price (billing.py);
- Free and the free trial have no payment method, so nothing extra can be
  billed: what is not a ticket, certificate or payment message is held back
  (`held_back`), with the reason on the delivery row; tickets, certificates
  and payment messages still go -- an attendee is never refused for the
  club's plan -- and are counted;
- a complimentary plan, an Enterprise deal or trial, or a deal invoiced by
  hand goes on sending; the extra is on a statement for an admin to invoice.

Promotional WhatsApp (MARKETING templates, `is_marketing`) costs us about
7x a utility one, so each plan caps it at `whatsapp_marketing_per_month`
(20% of its WhatsApp): past that, on every plan, a promotional WhatsApp is
held back. Tickets, certificates, payment messages and reminders (UTILITY)
never are. Today the platform's number sends no MARKETING template at all;
the cap is there for the first send that does.
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

# How the extra is paid for (Terms.billing).
#: The club's Razorpay subscription: an add-on on its next invoice.
RAZORPAY = "razorpay"
#: Nothing automatic: a statement an admin invoices by hand.
MANUAL = "manual"
#: No payment method (Free, the free trial): nothing extra can be billed,
#: so what is not transactional is held back past the limit.
NONE = "none"

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
        """Past the limit, what is not transactional is held back."""
        return self.billing == NONE

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


# ─── Holding back (Free past its limit) ──────────────────────────────────


def _marketing_reason(terms: Terms, cap: int) -> str:
    if cap == 0:
        return f"Not sent: promotional WhatsApp messages aren't included on {terms.where}."
    return (
        f"Not sent: this month's {cap:,} promotional WhatsApp {'message' if cap == 1 else 'messages'} on {terms.where} "
        "are used up. Tickets, certificates and reminders still go."
    )


def _held_reason(terms: Terms, channel: str, limit: int, kind: str) -> str:
    if limit == 0:
        what = "WhatsApp reminders" if (channel == WHATSAPP and kind == "reminder") else noun(channel).capitalize()
        return f"Not sent: {what} aren't included on {terms.where}. Upgrade to send them."
    return (
        f"Not sent: this month's {limit:,} {noun(channel, limit)} on {terms.where} are used up. "
        "Upgrade to send more; tickets and certificates still go."
    )


async def held_back(
    session, terms: Optional[Terms], channel: str, kind: str, *, via: str = PLATFORM, at: Optional[datetime] = None,
    category: Optional[str] = None,
) -> Optional[str]:
    """Why this send is held back, or None when it may go. Never a
    transactional message, never one through the club's own number. Two
    reasons:

    - a promotional WhatsApp (`is_marketing`) past the plan's
      `whatsapp_marketing_per_month`, on every plan: what the plan includes
      of them is used up, and they are not sold as extras;
    - anything else not transactional, only on a plan with no way to pay for
      more (`Terms.blocks`), past the month's limit.

    A held-back send is counted as `blocked` for the month.

    Checked before the send rather than reserved: workers sending for one
    club at once can each find room for the last message, so Free may go a
    message or two (at most the pipeline's MAIL_CONCURRENCY) past its limit.
    """
    from app.api.subscriptions import gates

    if terms is None or via == CLUB or kind in TRANSACTIONAL or not gates.enforcing():
        return None
    marketing_cap = terms.whatsapp_marketing_limit if is_marketing(channel, kind, category) else None
    limit = terms.limit(channel) if terms.blocks else None
    if marketing_cap is None and limit is None:
        return None
    period = period_of(at)
    counts = (await session.execute(
        select(MessageUsage.used, MessageUsage.marketing).where(
            MessageUsage.club_id == terms.club_id, MessageUsage.period == period, MessageUsage.channel == channel
        )
    )).first()
    used, marketing = (int(counts[0] or 0), int(counts[1] or 0)) if counts is not None else (0, 0)
    if marketing_cap is not None and marketing >= marketing_cap:
        reason = _marketing_reason(terms, marketing_cap)
    elif limit is not None and used >= limit:
        reason = _held_reason(terms, channel, limit, kind)
    else:
        return None
    await _upsert(session, terms, channel, period, used=0, blocked=1, kind=None)
    return reason


# ─── Counting ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Recorded:
    #: False when this ref was counted before (nothing added now).
    counted: bool
    used: Optional[int] = None
    limit: Optional[int] = None

    @property
    def extra(self) -> int:
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


async def _upsert(
    session, terms: Terms, channel: str, period: date, *, used: int, blocked: int, kind: Optional[str], marketing: int = 0,
):
    """Add to the month's counter, creating it: one statement, atomic.
    `marketing`: how many of `used` are promotional WhatsApp messages."""
    now = _now()
    table = MessageUsage.__table__
    values = dict(
        club_id=terms.club_id, period=period, channel=channel, used=used, blocked=blocked, marketing=marketing,
        kinds={kind: used} if kind and used else {}, quota=terms.limit(channel), rate_paise=terms.rate(channel),
        billing=terms.billing, plan_key=terms.plan_key, created_at=now, updated_at=now,
    )
    stmt = pg_insert(table).values(**values)
    change = {
        "used": table.c.used + stmt.excluded.used,
        "blocked": table.c.blocked + stmt.excluded.blocked,
        "marketing": table.c.marketing + stmt.excluded.marketing,
        "updated_at": now,
    }
    if used:
        # The terms the club is sending under now, and one more of this kind.
        current = func.coalesce(cast(table.c.kinds[kind].astext, Integer), 0)
        change.update(
            kinds=table.c.kinds.op("||")(func.jsonb_build_object(kind, current + used)),
            quota=stmt.excluded.quota, rate_paise=stmt.excluded.rate_paise, billing=stmt.excluded.billing,
            plan_key=stmt.excluded.plan_key,
        )
    stmt = stmt.on_conflict_do_update(constraint="uq_message_usage_club_period_channel", set_=change).returning(
        table.c.id, table.c.used, table.c.warned_80_at, table.c.warned_100_at
    )
    return (await session.execute(stmt)).one()


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
    row = await _upsert(session, terms, channel, period, used=count, blocked=0, kind=kind, marketing=marketing)
    limit = terms.limit(channel)
    await _maybe_warn(session, terms, channel, period, row, limit)
    return Recorded(counted=True, used=int(row.used), limit=limit)


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
    """Tell the club the first time the count reaches 80%, and 100%, of the
    month's limit. The claim is one UPDATE ... WHERE warned_N_at IS NULL, so
    of two workers crossing it at once one tells. When both are reached at
    once (a limit lowered mid-month), only the 100% one is said."""
    if limit is None or limit <= 0:
        return
    used = int(row.used)
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


def warning_words(terms: Terms, channel: str, period: date, *, level: int, used: int, limit: int) -> dict:
    """The notice and the mail: title, body and the mail's paragraphs."""
    many = noun(channel, limit)
    month = label(period)
    if level >= 100:
        title = f"You've used all {limit:,} {many} for {month}"
    else:
        title = f"You've used {level}% of your {many} for {month}"
    counted = f"{used:,} of {limit:,} {many} sent this month on {terms.where}."
    if terms.billing == RAZORPAY:
        then = (
            f"Messages keep going past {limit:,}: each extra {noun(channel, 1)} is "
            f"{rupees(terms.rate(channel))}, added to your next bill. A bigger plan includes more."
        )
    elif terms.billing == MANUAL:
        then = f"Messages keep going past {limit:,}; any extra is on your monthly statement."
    else:
        then = (
            f"Past {limit:,}, announcements, campaigns and reminders stop until next month. "
            "Tickets, certificates and payment messages still go. Upgrade to send more."
        )
    return {"title": title, "body": f"{counted} {then}", "paragraphs": [counted, then]}


async def warn(session, terms: Terms, channel: str, period: date, *, level: int, used: int, limit: int) -> None:
    """The bell, and a mail to the club's owner after the commit."""
    from app.api import club_alerts
    from app.api.clubs.models import Clubs
    from app.api.subscriptions import notices
    from app.core.after_commit import run_after_commit

    words = warning_words(terms, channel, period, level=level, used=used, limit=limit)
    await club_alerts.notify(
        session, terms.club_id, f"message_quota_{level}",
        title=words["title"], body=words["body"], link="/billing",
        data={"channel": channel, "period": key(period), "level": level, "used": used, "limit": limit,
              "billing": terms.billing, "rate_paise": terms.rate(channel)},
        dedupe_key=f"messages:{key(period)}:{channel}:{level}",
    )
    to = await notices.owner_address(session, terms.club_id)
    if not to:
        return
    club = await session.get(Clubs, terms.club_id)
    name = club.name if club is not None else "there"
    button = ("See your plan", notices.billing_url())
    run_after_commit(
        session, notices._send, to, words["title"], [f"Hi {name},", *words["paragraphs"]], list(button),
        "You get this at 80% and at 100% of a month's limit, once each.",
    )


# ─── What the dashboards read ────────────────────────────────────────────


def _state(terms: Terms, used: int, limit: Optional[int]) -> str:
    if limit is None:
        return "unlimited"
    if limit == 0 and used == 0:
        return "not_included"
    if used >= limit:
        return "blocked" if terms.blocks else "over"
    if limit > 0 and used >= threshold(limit, 80):
        return "near"
    return "ok"


async def _upgrade(session, terms: Terms, channels: dict) -> Optional[dict]:
    """The cheapest public plan with room for more of what is near or past
    its limit, when the club is not already on something kinder."""
    from app.api.subscriptions import gates

    if terms.billing == MANUAL:
        return None
    tight = [ch for ch, c in channels.items() if c["state"] in ("near", "over", "blocked")]
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


def _channel_view(terms: Terms, channel: str, row: Optional[MessageUsage]) -> dict:
    used = int(row.used) if row is not None else 0
    limit = terms.limit(channel)
    extra = max(0, used - limit) if limit is not None else 0
    rate = terms.rate(channel)
    out = {
        "used": used,
        "limit": limit,
        "extra": extra,
        "blocked": int(row.blocked) if row is not None else 0,
        "rate_paise": rate,
        # What the extra comes to; billed only on a Razorpay plan.
        "extra_paise": extra * rate if terms.billing != NONE else 0,
        "percent": _percent(used, limit),
        "state": _state(terms, used, limit),
        "kinds": dict(row.kinds or {}) if row is not None else {},
        "warned": [lvl for lvl in WARN_AT if row is not None and getattr(row, f"warned_{lvl}_at") is not None],
    }
    if channel == WHATSAPP:
        # Of those, the promotional ones, against the plan's cap on them.
        out["marketing"] = {
            "used": int(row.marketing or 0) if row is not None else 0,
            "limit": terms.whatsapp_marketing_limit,
        }
    return out


async def usage(session, club_id: int, now: Optional[datetime] = None) -> dict:
    """This month so far: each channel against its limit, the extra and what
    it is estimated to cost, and the plan to move to when it would help."""
    now = now or _now()
    terms = await terms_for(session, club_id)
    period = period_of(now)
    rows = {
        r.channel: r
        for r in await session.scalars(
            select(MessageUsage).where(MessageUsage.club_id == club_id, MessageUsage.period == period)
        )
    }
    channels = {ch: _channel_view(terms, ch, rows.get(ch)) for ch in CHANNELS}
    start, end = bounds(period)
    return {
        "period": key(period),
        "period_label": label(period),
        "period_start": start,
        "period_end": end,
        "plan_key": terms.plan_key,
        "plan_name": terms.plan_name,
        "billing": terms.billing,
        "blocks_past_limit": terms.blocks,
        "custom": terms.custom,
        "trial": terms.trial,
        "channels": channels,
        "estimated_extra_paise": sum(c["extra_paise"] for c in channels.values()),
        "upgrade": await _upgrade(session, terms, channels),
    }


def statement_out(row: MessageStatements) -> dict:
    return {
        "id": row.id,
        "period": key(row.period),
        "period_label": label(row.period),
        "plan_key": row.plan_key,
        "billing": row.billing,
        "email": {"used": row.email_used, "limit": row.email_limit, "extra": row.email_extra, "rate_paise": row.email_rate_paise},
        "whatsapp": {"used": row.whatsapp_used, "limit": row.whatsapp_limit, "extra": row.whatsapp_extra,
                     "rate_paise": row.whatsapp_rate_paise},
        "amount_paise": row.amount_paise,
        "status": row.status,
        "charged_at": row.charged_at,
        "razorpay_addon_id": row.razorpay_addon_id,
    }


async def history(session, club_id: int, *, months: int = 12) -> list[dict]:
    """The closed months' statements, newest first."""
    rows = await session.scalars(
        select(MessageStatements)
        .where(MessageStatements.club_id == club_id)
        .order_by(MessageStatements.period.desc())
        .limit(months)
    )
    return [statement_out(r) for r in rows]


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
