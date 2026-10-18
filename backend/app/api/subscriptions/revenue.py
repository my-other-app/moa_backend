"""The admin's revenue page: how many clubs pay, what that is worth, and what
was actually collected.

Two kinds of number, kept apart on purpose:

- *Run rate* (MRR, ARR, the counts, by plan and cycle, upcoming renewals)
  comes from the clubs' subscription rows as they stand now. MRR is what
  paying subscriptions bring in a month (service.paying): never a
  complimentary plan, which has its own count, and never a trial. A yearly
  subscription contributes a twelfth of its price (service.mrr_for).
- *Money* (collected, refunded, the monthly series) comes only from the
  ledger of what Razorpay took (SubscriptionCharges; failed attempts never
  count). Gross: refunds are their own figure, attributed to the month of
  the payment they refund.

Months are calendar months in India. Ticket money is shown for context and
never added in: it is collected for clubs, not earned by the platform.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import String, cast, func, select

from app.api.clubs.models import Clubs
from app.api.subscriptions import service
from app.api.subscriptions.ledger import MONEY
from app.api.subscriptions.models import (
    BillingCycle,
    ClubSubscriptions,
    Plans,
    SubscriptionCharges,
    SubscriptionStatus,
)
from app.response import CustomHTTPException

IST = timezone(timedelta(hours=5, minutes=30))
#: Upcoming renewals look this far ahead.
RENEWAL_WINDOW = timedelta(days=30)
TICKET_NOTE = "Ticket money collected through the platform account for clubs — not platform revenue."

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Months in India ─────────────────────────────────────────────────────


def _shift(year: int, month: int, by: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + by
    return index // 12, index % 12 + 1


def month_start(year: int, month: int) -> datetime:
    """The first instant of a calendar month in India, in UTC."""
    return datetime(year, month, 1, tzinfo=IST).astimezone(timezone.utc)


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    return month_start(year, month), month_start(*_shift(year, month, 1))


def month_key(when: datetime) -> str:
    return when.astimezone(IST).strftime("%Y-%m")


def parse_month(raw: str) -> tuple[datetime, datetime]:
    """"2026-10" as its bounds; 422 for anything else."""
    found = _MONTH.match((raw or "").strip())
    if not found:
        raise CustomHTTPException(status_code=422, message="month must be YYYY-MM, e.g. 2026-10.")
    return month_bounds(int(found.group(1)), int(found.group(2)))


def _this_month(now: datetime) -> tuple[int, int]:
    local = now.astimezone(IST)
    return local.year, local.month


def _paise(rupees) -> int:
    from app.core.money import to_paise

    return to_paise(rupees or 0)


# ─── Run rate ────────────────────────────────────────────────────────────


def _status(row) -> SubscriptionStatus:
    return row.status if isinstance(row.status, SubscriptionStatus) else SubscriptionStatus(row.status)


def _cycle(row) -> str:
    return getattr(row.cycle, "value", row.cycle) or BillingCycle.monthly.value


def trialing(row, now: datetime) -> bool:
    return not row.complimentary and _status(row) == SubscriptionStatus.trial and service.is_entitled(row, now)


def _plan_held(row, now: datetime, plans: dict[int, Plans]) -> Plans | None:
    """The plan this row gives the club right now, as resolve_entitlements
    would decide it (None: the club falls back to the default plan)."""
    if service.is_entitled(row, now):
        return row.plan
    if (_status(row) == SubscriptionStatus.pending and service.previous_entitled(row, now)) or (
        service.comp_over(row, now) and service.restorable(row, now)
    ):
        return plans.get((row.previous_state or {}).get("plan_id"))
    return None


async def _plans(session) -> list[Plans]:
    return list(
        (
            await session.execute(
                select(Plans).order_by(Plans.sort_order, Plans.price_monthly_paise, Plans.id)
                # Deleted plans too, for a row still pointing at one; only
                # live ones are listed when nobody is on them.
                .execution_options(include_deleted=True)
            )
        )
        .scalars()
        .all()
    )


async def _last_charges(session, razorpay_ids: list[str]) -> dict[str, int]:
    """What Razorpay last took for each subscription: the best guess at its
    next renewal (it is the price that subscriber is on)."""
    if not razorpay_ids:
        return {}
    latest = (
        select(
            SubscriptionCharges.razorpay_subscription_id.label("rid"),
            func.max(SubscriptionCharges.charged_at).label("at"),
        )
        .where(SubscriptionCharges.razorpay_subscription_id.in_(razorpay_ids), MONEY)
        .group_by(SubscriptionCharges.razorpay_subscription_id)
        .subquery()
    )
    rows = await session.execute(
        select(SubscriptionCharges.razorpay_subscription_id, SubscriptionCharges.amount_paise).join(
            latest,
            (latest.c.rid == SubscriptionCharges.razorpay_subscription_id)
            & (latest.c.at == SubscriptionCharges.charged_at),
        ).where(MONEY)
    )
    return {rid: int(amount) for rid, amount in rows.all()}


async def run_rate(session, now: datetime) -> dict:
    rows = await service.current_subscriptions(session)
    plans = await _plans(session)
    by_id = {plan.id: plan for plan in plans}

    counts = {
        "paying": 0, "trialing": 0, "complimentary": 0, "past_due": 0, "in_grace": 0,
        "paused": 0, "pending": 0, "free_clubs": 0, "total_clubs": 0,
    }
    per_plan: dict[int, dict] = {}
    by_cycle = {cycle.value: {"paying": 0, "mrr_paise": 0} for cycle in BillingCycle}
    mrr = 0
    on_a_plan: set[int] = set()
    renewals: list[ClubSubscriptions] = []

    def plan_entry(plan_id: int) -> dict:
        return per_plan.setdefault(plan_id, {"paying": 0, "trialing": 0, "complimentary": 0, "mrr_paise": 0})

    for row in rows:
        status = _status(row)
        if status == SubscriptionStatus.past_due:
            counts["past_due"] += 1
        elif status == SubscriptionStatus.paused:
            counts["paused"] += 1
        elif status == SubscriptionStatus.pending:
            counts["pending"] += 1
        if service.in_grace(row, now):
            counts["in_grace"] += 1

        if service.paying(row, now):
            worth = int(row.mrr_paise or 0)
            counts["paying"] += 1
            mrr += worth
            entry = plan_entry(row.plan_id)
            entry["paying"] += 1
            entry["mrr_paise"] += worth
            cycle = by_cycle.setdefault(_cycle(row), {"paying": 0, "mrr_paise": 0})
            cycle["paying"] += 1
            cycle["mrr_paise"] += worth
            if (
                status == SubscriptionStatus.active
                and row.razorpay_subscription_id
                and not row.cancel_at_period_end
                and row.current_period_end is not None
                and now < row.current_period_end <= now + RENEWAL_WINDOW
            ):
                renewals.append(row)
        elif service.comp_live(row, now):
            counts["complimentary"] += 1
            plan_entry(row.plan_id)["complimentary"] += 1
        elif trialing(row, now):
            counts["trialing"] += 1
            plan_entry(row.plan_id)["trialing"] += 1

        held = _plan_held(row, now, by_id)
        if held is not None and held.key != service.DEFAULT_PLAN_KEY:
            on_a_plan.add(row.club_id)

    # Clubs that can use the product: approved and not deleted.
    live_clubs = set(
        (
            await session.execute(
                select(Clubs.id).where(Clubs.is_deleted == False, Clubs.is_verified == True)  # noqa: E712
            )
        ).scalars()
    )
    counts["total_clubs"] = len(live_clubs)
    counts["free_clubs"] = len(live_clubs - on_a_plan)

    last = await _last_charges(session, [row.razorpay_subscription_id for row in renewals])
    upcoming = 0
    for row in renewals:
        # What it pays a cycle, when Razorpay has not charged it before.
        guess = service.price_of(row)
        upcoming += last.get(row.razorpay_subscription_id, guess)

    by_plan = []
    for plan in plans:
        entry = per_plan.get(plan.id)
        if entry is None and plan.is_deleted:
            continue
        entry = entry or {"paying": 0, "trialing": 0, "complimentary": 0, "mrr_paise": 0}
        by_plan.append({"plan_id": plan.id, "plan_key": plan.key, "plan_name": plan.name, **entry})

    return {
        "mrr_paise": mrr,
        "arr_paise": mrr * 12,
        "counts": counts,
        "by_plan": by_plan,
        "by_cycle": by_cycle,
        "upcoming_renewals": {"next_30_days_paise": upcoming, "count": len(renewals)},
    }


# ─── Money ───────────────────────────────────────────────────────────────


async def collected(session, now: datetime) -> dict:
    year, month = _this_month(now)
    this_start, next_start = month_bounds(year, month)
    last_start = month_start(*_shift(year, month, -1))
    year_start = month_start(year, 1)
    amount = SubscriptionCharges.amount_paise
    at = SubscriptionCharges.charged_at
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(amount).filter(at >= this_start, at < next_start), 0),
                func.coalesce(func.sum(amount).filter(at >= last_start, at < this_start), 0),
                func.coalesce(func.sum(amount).filter(at >= year_start, at < next_start), 0),
                func.coalesce(func.sum(amount), 0),
                func.coalesce(func.sum(SubscriptionCharges.refunded_paise), 0),
            ).where(MONEY)
        )
    ).one()
    return {
        "this_month_paise": int(row[0]),
        "last_month_paise": int(row[1]),
        "year_to_date_paise": int(row[2]),
        "all_time_paise": int(row[3]),
        "refunded_paise": int(row[4]),
    }


async def series(session, months: int, now: datetime) -> list[dict]:
    """The last `months` calendar months in India, oldest first, every month
    present (zero when nothing happened)."""
    from app.api.admin_alerts.models import AdminNotifications
    from app.api.audit.models import AdminAuditLogs

    year, month = _this_month(now)
    keys = [f"{y:04d}-{m:02d}" for y, m in (_shift(year, month, -i) for i in range(months - 1, -1, -1))]
    since = month_start(*_shift(year, month, -(months - 1)))
    out = {
        key: {"month": key, "collected_paise": 0, "refunded_paise": 0, "new_paying": 0,
              "new_trials": 0, "cancelled": 0, "new_complimentary": 0}
        for key in keys
    }

    def add(when: datetime | None, field: str, value: int = 1) -> None:
        if when is None:
            return
        bucket = out.get(month_key(when))
        if bucket is not None:
            bucket[field] += value

    for charged_at, amount, refunded in (
        await session.execute(
            select(SubscriptionCharges.charged_at, SubscriptionCharges.amount_paise, SubscriptionCharges.refunded_paise)
            .where(SubscriptionCharges.charged_at >= since, MONEY)
        )
    ).all():
        add(charged_at, "collected_paise", int(amount))
        add(charged_at, "refunded_paise", int(refunded or 0))

    # A subscription becomes paying with its first charge. Keyed by the
    # Razorpay subscription: a club's row is reused when it subscribes again,
    # and that is a new paying subscription.
    first_charges = (
        select(func.min(SubscriptionCharges.charged_at))
        .where(MONEY)
        .group_by(
            func.coalesce(
                SubscriptionCharges.razorpay_subscription_id,
                cast(SubscriptionCharges.subscription_id, String),
                SubscriptionCharges.razorpay_payment_id,
            )
        )
    )
    for (first,) in (await session.execute(first_charges)).all():
        add(first, "new_paying")

    for (started,) in (
        await session.execute(
            select(ClubSubscriptions.trial_started_at)
            .where(ClubSubscriptions.trial_started_at >= since)
            .execution_options(include_deleted=True)
        )
    ).all():
        add(started, "new_trials")

    # Cancellations as the admins were told of them: by the club, by an
    # admin, at Razorpay, or a grace running out. A complimentary plan being
    # revoked is not a lost customer, and is left out.
    for created_at, data in (
        await session.execute(
            select(AdminNotifications.created_at, AdminNotifications.data).where(
                AdminNotifications.kind.in_(("subscription_cancelled", "subscription_downgraded")),
                AdminNotifications.created_at >= since,
            )
        )
    ).all():
        if not (data or {}).get("complimentary"):
            add(created_at, "cancelled")

    for (created_at,) in (
        await session.execute(
            select(AdminAuditLogs.created_at).where(
                AdminAuditLogs.action == "subscription.comp_grant", AdminAuditLogs.created_at >= since
            )
        )
    ).all():
        add(created_at, "new_complimentary")

    return [out[key] for key in keys]


async def ticket_sales(session, now: datetime) -> dict:
    """Captured ticket (and shelf) payments on the platform's Razorpay
    account. A payment is captured when its log says `captured`; a refund
    flips it to `refunded`, so these are what was kept."""
    from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus

    year, month = _this_month(now)
    start, end = month_bounds(year, month)
    criteria = [PaymentLogs.status == PaymentStatus.captured, PaymentLogs.is_deleted == False]  # noqa: E712
    # Clubs' own Razorpay accounts (payment_gateway_id, when that lands) are
    # not the platform's: left out.
    gateway = getattr(PaymentOrders, "payment_gateway_id", None)
    if gateway is not None:
        criteria.append(gateway.is_(None))
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(PaymentLogs.amount).filter(PaymentLogs.created_at >= start,
                                                                   PaymentLogs.created_at < end), 0),
                func.coalesce(func.sum(PaymentLogs.amount), 0),
            )
            .select_from(PaymentLogs)
            .join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
            .where(*criteria)
        )
    ).one()
    return {"this_month_paise": _paise(row[0]), "all_time_paise": _paise(row[1]), "note": TICKET_NOTE}


async def revenue(session, *, months: int = 12, now: datetime | None = None) -> dict:
    now = now or _now()
    figures = await run_rate(session, now)
    return {
        "mrr_paise": figures["mrr_paise"],
        "arr_paise": figures["arr_paise"],
        "counts": figures["counts"],
        "by_plan": figures["by_plan"],
        "by_cycle": figures["by_cycle"],
        "collected": await collected(session, now),
        "series": await series(session, months, now),
        "upcoming_renewals": figures["upcoming_renewals"],
        "ticket_sales": await ticket_sales(session, now),
    }


# ─── The ledger, listed ──────────────────────────────────────────────────


async def list_charges(
    session, *, offset: int, limit: int, club_id: int | None = None, month: str | None = None
) -> dict:
    criteria = []
    if club_id is not None:
        criteria.append(SubscriptionCharges.club_id == club_id)
    if month:
        start, end = parse_month(month)
        criteria += [SubscriptionCharges.charged_at >= start, SubscriptionCharges.charged_at < end]

    total, sum_paise, refunded = (
        await session.execute(
            select(
                func.count(SubscriptionCharges.id),
                func.coalesce(func.sum(SubscriptionCharges.amount_paise).filter(MONEY), 0),
                func.coalesce(func.sum(SubscriptionCharges.refunded_paise).filter(MONEY), 0),
            ).where(*criteria)
        )
    ).one()
    rows = (
        await session.execute(
            select(SubscriptionCharges, Clubs.id, Clubs.name, Clubs.slug, Plans.name)
            .outerjoin(Clubs, Clubs.id == SubscriptionCharges.club_id)
            .outerjoin(Plans, Plans.id == SubscriptionCharges.plan_id)
            .where(*criteria)
            .order_by(SubscriptionCharges.charged_at.desc(), SubscriptionCharges.id.desc())
            .offset(offset)
            .limit(limit)
            # A deleted club's or plan's payments were still money.
            .execution_options(include_deleted=True)
        )
    ).all()
    return {
        "items": [
            {
                "id": charge.id,
                "club": {"id": cid, "name": name, "slug": slug} if cid is not None else None,
                "plan_name": plan_name,
                "cycle": getattr(charge.cycle, "value", charge.cycle),
                "amount_paise": charge.amount_paise,
                "refunded_paise": charge.refunded_paise,
                "status": charge.status,
                "razorpay_payment_id": charge.razorpay_payment_id,
                "charged_at": charge.charged_at,
            }
            for charge, cid, name, slug, plan_name in rows
        ],
        "total": int(total or 0),
        "sum_paise": int(sum_paise or 0),
        "refunded_paise": int(refunded or 0),
        "offset": offset,
        "limit": limit,
    }
