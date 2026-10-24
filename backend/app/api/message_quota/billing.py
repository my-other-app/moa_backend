"""A month's extra messages, billed.

Run by the hourly billing sweep (app/api/subscriptions/sweep.py), one step
after another, each club in its own transaction:

1. `close_months`: every club with a counter in a month that has ended (ten
   minutes ago at least, so the month's last sends are in) gets its
   statement: what it used of each channel, its limit, the extra and what
   it comes to. The limit and price are the kinder of the terms the club
   was last sending under and those it ends the month on -- a club that
   upgraded after its last send has the bigger limit; one whose plan ran
   out keeps the one it sent under. Then:

   - nothing past the limits: `nothing_due`;
   - Free or the free trial (no payment method): `not_billable`;
   - a complimentary plan, an Enterprise deal or trial, a deal by hand:
     `manual`, and the admins are alerted to invoice it;
   - a plan Razorpay bills: `pending` -- or `waived` under Razorpay's
     smallest charge (₹1).

   One statement per club and month (a unique key): closing twice, or two
   sweeps at once, makes one.

2. `charge_due`: each `pending` statement is added to the club's Razorpay
   subscription as an add-on (POST /subscriptions/{id}/addons), which
   Razorpay puts on the subscription's next invoice. Never twice:

   - the statement is claimed (`charging`, committed) before Razorpay is
     asked, under a row lock no other sweep can take;
   - the add-on's id is kept on the statement, and a statement with one is
     never charged again;
   - Razorpay saying no (a 4xx) made nothing: `failed`, tried again with a
     growing wait, given up after MAX_ATTEMPTS (`gave_up`, an admin
     invoices it); the admins are alerted each time;
   - Razorpay not answering (a timeout, a 5xx, the connection) may have made
     it: `unconfirmed`. Before anything is asked again, Razorpay's add-ons
     are searched for the statement's reference (in the add-on's
     description); one found is the charge, and nothing is asked again;
   - a claim left `charging` by a sweep that stopped is `unconfirmed` after
     STALE_CHARGE, and found out the same way;
   - a club with no Razorpay subscription left to add it to (cancelled, run
     out) is `manual`, and the admins are alerted.

   Each charge is audit-logged (`messages.overage_charged`) and the admins
   alerted. `settings.MESSAGE_OVERAGE_BILLING=false` stops this step:
   statements are still made, and wait `pending`.

3. `prune_refs`: counted messages' idempotency keys older than REF_DAYS.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.message_quota import service
from app.api.message_quota.models import MessageStatements, MessageUsage, MessageUsageRefs, StatementStatus as S
from app.config import settings

logger = logging.getLogger(__name__)

#: A month is closed this long after it ends.
CLOSE_AFTER = timedelta(minutes=10)
#: Tries at Razorpay before an admin is asked to invoice it.
MAX_ATTEMPTS = 5
#: A claim older than this is a sweep that stopped mid-call.
STALE_CHARGE = timedelta(minutes=15)
#: Razorpay's smallest charge, in paise.
MIN_CHARGE_PAISE = 100
#: Statements per step per run; the rest wait an hour.
BATCH = 200
#: How far back Razorpay's add-ons are searched for an unconfirmed one.
SEARCH_BACK = timedelta(hours=2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def backoff(attempts: int) -> timedelta:
    """1, 2, 4, 8 hours... at most a day."""
    return timedelta(hours=min(24, 2 ** max(attempts - 1, 0)))


def reference(row: MessageStatements) -> str:
    """What the add-on's description carries, to find it again."""
    return f"[moa-messages:{row.id}:{row.club_id}:{service.key(row.period)}]"


# ─── 1. Closing a month ──────────────────────────────────────────────────


def _kinder_limit(sent_under: Optional[int], ends_on: Optional[int]) -> Optional[int]:
    if sent_under is None or ends_on is None:
        return None
    return max(sent_under, ends_on)


def _billing_of(rows: list[MessageUsage], ends_on: str) -> str:
    seen = {r.billing for r in rows if r.used} | {ends_on}
    for mode in (service.RAZORPAY, service.MANUAL):
        if mode in seen:
            return mode
    return service.NONE


async def close_month(session, club_id: int, period: date, now: Optional[datetime] = None) -> Optional[MessageStatements]:
    """The club's statement for `period`. Returns it when made now; None
    when the club already had one. Flushes; the caller commits."""
    now = now or _now()
    rows = list(
        await session.scalars(
            select(MessageUsage).where(MessageUsage.club_id == club_id, MessageUsage.period == period)
        )
    )
    ends_on = await service.terms_for(session, club_id)
    by = {r.channel: r for r in rows}
    figures: dict = {}
    for ch in service.CHANNELS:
        row = by.get(ch)
        used = int(row.used) if row is not None else 0
        limit = _kinder_limit(row.quota, ends_on.limit(ch)) if row is not None else ends_on.limit(ch)
        rate = min(int(row.rate_paise or 0), ends_on.rate(ch)) if row is not None and row.used else ends_on.rate(ch)
        extra = max(0, used - limit) if limit is not None else 0
        figures[ch] = (used, limit, extra, rate)
    billing = _billing_of(rows, ends_on.billing)
    amount = sum(extra * rate for _, _, extra, rate in figures.values())
    if amount <= 0:
        status = S.NOTHING_DUE
    elif billing == service.NONE:
        status = S.NOT_BILLABLE
    elif billing == service.MANUAL:
        status = S.MANUAL
    elif amount < MIN_CHARGE_PAISE:
        status = S.WAIVED
    else:
        status = S.PENDING
    plan_key = ends_on.plan_key or next((r.plan_key for r in rows if r.plan_key), None)
    (e_used, e_limit, e_extra, e_rate), (w_used, w_limit, w_extra, w_rate) = figures[service.EMAIL], figures[service.WHATSAPP]
    made = await session.scalar(
        pg_insert(MessageStatements)
        .values(
            club_id=club_id, period=period, plan_key=plan_key, billing=billing,
            email_used=e_used, email_limit=e_limit, email_extra=e_extra, email_rate_paise=e_rate,
            whatsapp_used=w_used, whatsapp_limit=w_limit, whatsapp_extra=w_extra, whatsapp_rate_paise=w_rate,
            amount_paise=amount, status=status, attempts=0,
            next_attempt_at=now if status == S.PENDING else None, created_at=now, updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_message_statements_club_period")
        .returning(MessageStatements.id)
    )
    if made is None:
        return None
    statement = await session.get(MessageStatements, made)
    if status == S.MANUAL:
        await _alert(session, statement, "message_overage_manual",
                     f"{{club}} went past its message limits in {service.label(period)}: invoice {service.rupees(amount)} by hand",
                     _lines(statement) + " Nothing was charged automatically: the club's plan is not billed by Razorpay.")
    return statement


async def close_months(session, now: Optional[datetime] = None) -> int:
    """Statements for every club and month that has ended without one."""
    now = now or _now()
    open_from = service.period_of(now - CLOSE_AFTER)
    has_one = exists().where(
        MessageStatements.club_id == MessageUsage.club_id, MessageStatements.period == MessageUsage.period
    )
    due = (
        await session.execute(
            select(MessageUsage.club_id, MessageUsage.period)
            .where(MessageUsage.period < open_from, ~has_one)
            .group_by(MessageUsage.club_id, MessageUsage.period)
            .order_by(MessageUsage.period, MessageUsage.club_id)
            .limit(BATCH)
        )
    ).all()
    made = 0
    for club_id, period in due:
        try:
            if await close_month(session, club_id, period, now) is not None:
                made += 1
            await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("message statements: closing %s for club %s failed", period, club_id)
            await session.rollback()
    return made


# ─── 2. Charging ─────────────────────────────────────────────────────────


def _lines(row: MessageStatements) -> str:
    parts = []
    if row.email_extra:
        parts.append(f"{row.email_extra:,} extra {service.noun(service.EMAIL, row.email_extra)} at {service.rupees(row.email_rate_paise)}")
    if row.whatsapp_extra:
        parts.append(
            f"{row.whatsapp_extra:,} extra {service.noun(service.WHATSAPP, row.whatsapp_extra)} at {service.rupees(row.whatsapp_rate_paise)}"
        )
    return (", ".join(parts) + ".") if parts else "No extra messages."


async def _alert(session, row: MessageStatements, kind: str, title: str, body: str, **data) -> None:
    from app.api.admin_alerts import notify
    from app.api.clubs.models import Clubs

    club = await session.get(Clubs, row.club_id)
    name = club.name if club is not None else f"Club {row.club_id}"
    await notify(
        session, kind, title=title.replace("{club}", name), body=body, club=club if club is not None else row.club_id,
        data={"statement_id": row.id, "period": service.key(row.period), "amount_paise": row.amount_paise, **data},
        dedupe_on={"statement_id": row.id},
    )


def _live(sub) -> bool:
    from app.api.subscriptions import service as subs
    from app.api.subscriptions.models import SubscriptionStatus

    return bool(
        sub is not None
        and sub.razorpay_subscription_id
        and not sub.complimentary
        and subs._status(sub.status) in (SubscriptionStatus.active, SubscriptionStatus.past_due, SubscriptionStatus.trial)
    )


def addon_payload(row: MessageStatements) -> dict:
    return {
        "item": {
            "name": f"Extra messages, {row.period:%b %Y}",
            "amount": int(row.amount_paise),
            "currency": "INR",
            "description": f"{_lines(row)} {reference(row)}"[:250],
        },
        "quantity": 1,
    }


async def _found_at_razorpay(row: MessageStatements) -> Optional[str]:
    """The add-on Razorpay made for this statement, by its reference, or
    None when it made none. Raises when Razorpay cannot be asked."""
    from app.api.payments import rzp
    from app.api.subscriptions import billing as subs_billing

    since = (row.charging_since or row.updated_at or _now()) - SEARCH_BACK
    ref = reference(row)
    skip = 0
    while True:
        page = await rzp.call(
            subs_billing.razorpay_client.addon.all, {"from": int(since.timestamp()), "count": 100, "skip": skip}
        ) or {}
        items = page.get("items") or []
        for addon in items:
            item = addon.get("item") or {}
            if ref in (item.get("description") or "") and addon.get("subscription_id") in (None, row.razorpay_subscription_id):
                return addon.get("id")
        if len(items) < 100:
            return None
        skip += 100


async def _claim(session, statement_id: int) -> Optional[MessageStatements]:
    return await session.scalar(
        select(MessageStatements)
        .where(MessageStatements.id == statement_id, MessageStatements.status.in_((S.PENDING, S.FAILED, S.UNCONFIRMED)))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )


async def _charged(session, row: MessageStatements, addon_id: str, now: datetime, *, found: bool = False) -> None:
    from app.api.audit import service as audit

    row.status = S.CHARGED
    row.razorpay_addon_id = addon_id
    row.charged_at = now
    row.last_error = None
    row.next_attempt_at = None
    await audit.record(
        session, actor=None, action="messages.overage_charged", entity_type="club", entity_id=row.club_id,
        summary=f"Added {service.rupees(row.amount_paise)} for {service.label(row.period)}'s extra messages to the next invoice",
        details={"statement_id": row.id, "period": service.key(row.period), "amount_paise": row.amount_paise,
                 "addon_id": addon_id, "razorpay_subscription_id": row.razorpay_subscription_id,
                 "email_extra": row.email_extra, "whatsapp_extra": row.whatsapp_extra, "found_after_timeout": found},
    )
    await _alert(session, row, "message_overage_charged",
                 f"{{club}}: {service.rupees(row.amount_paise)} of extra messages added to its next invoice",
                 f"{service.label(row.period)}: {_lines(row)}", addon_id=addon_id)


async def charge(session, statement_id: int, now: Optional[datetime] = None) -> Optional[str]:
    """Add one statement to its club's next invoice. Returns what became of
    it ("charged", "failed", "gave_up", "unconfirmed", "manual"), or None
    when it was not this sweep's to charge. Commits as it goes."""
    from app.api.payments import rzp
    from app.api.subscriptions import billing as subs_billing
    from app.api.subscriptions import service as subs

    now = now or _now()
    row = await _claim(session, statement_id)
    if row is None:
        return None
    if row.razorpay_addon_id:
        # Charged already; only the status was behind.
        row.status = S.CHARGED
        await session.commit()
        return "charged"
    if row.status == S.UNCONFIRMED and row.razorpay_subscription_id:
        try:
            found = await _found_at_razorpay(row)
        except Exception as exc:  # noqa: BLE001 - not known yet: nothing is asked again
            row.next_attempt_at = now + timedelta(minutes=30)
            row.last_error = f"Could not ask Razorpay whether the add-on was made: {str(exc)[:200]}"
            await session.commit()
            return "unconfirmed"
        if found:
            await _charged(session, row, found, now, found=True)
            await session.commit()
            return "charged"
        # Razorpay made none: it may be asked again.
    sub = await subs.get_club_subscription(session, row.club_id)
    if not _live(sub):
        row.status = S.MANUAL
        row.next_attempt_at = None
        row.last_error = "The club has no Razorpay subscription left to add it to"
        await _alert(session, row, "message_overage_manual",
                     f"{{club}}'s extra messages for {service.label(row.period)} need invoicing by hand",
                     _lines(row) + f" {service.rupees(row.amount_paise)}: its Razorpay subscription has ended, so it could not go on an invoice.")
        await session.commit()
        return "manual"
    if row.attempts >= MAX_ATTEMPTS:
        row.status = S.GAVE_UP
        row.next_attempt_at = None
        await _alert(session, row, "message_overage_failed",
                     f"{{club}}'s extra-messages add-on for {service.label(row.period)} was given up on",
                     f"{service.rupees(row.amount_paise)}: tried {row.attempts} times. Invoice it by hand.",
                     error=row.last_error, attempts=row.attempts)
        await session.commit()
        return "gave_up"
    sub_id = sub.razorpay_subscription_id
    row.status = S.CHARGING
    row.charging_since = now
    row.attempts = (row.attempts or 0) + 1
    row.razorpay_subscription_id = sub_id
    payload = addon_payload(row)
    # The claim is on disk before Razorpay is asked: a sweep that dies in the
    # call leaves `charging`, which is found out, never asked again blindly.
    await session.commit()
    import razorpay.errors as rzp_errors

    try:
        made = await rzp.call(subs_billing.razorpay_client.subscription.createAddon, sub_id, payload)
    except rzp_errors.BadRequestError as exc:
        row = await _reload(session, statement_id)
        gave_up = row.attempts >= MAX_ATTEMPTS
        row.status = S.GAVE_UP if gave_up else S.FAILED
        row.charging_since = None
        row.last_error = f"Razorpay refused the add-on: {str(exc)[:300]}"
        row.next_attempt_at = None if gave_up else now + backoff(row.attempts)
        await _alert(session, row, "message_overage_failed",
                     f"Razorpay refused {{club}}'s extra-messages add-on for {service.label(row.period)}",
                     f"{service.rupees(row.amount_paise)}: {row.last_error}. "
                     + ("Tried too often: invoice it by hand." if gave_up else "The sweep tries again."),
                     error=row.last_error, attempts=row.attempts)
        await session.commit()
        return "gave_up" if gave_up else "failed"
    except Exception as exc:  # noqa: BLE001 - a timeout, a 5xx: it may have been made
        row = await _reload(session, statement_id)
        row.status = S.UNCONFIRMED
        row.last_error = f"Razorpay did not answer ({exc.__class__.__name__}); checking before asking again"[:400]
        row.next_attempt_at = now + timedelta(minutes=10)
        await session.commit()
        return "unconfirmed"
    addon_id = str((made or {}).get("id") or "")
    row = await _reload(session, statement_id)
    if not addon_id:
        # Taken without an id: found by its reference next time.
        row.status = S.UNCONFIRMED
        row.next_attempt_at = now + timedelta(minutes=10)
        await session.commit()
        return "unconfirmed"
    row.charging_since = None
    await _charged(session, row, addon_id, now)
    await session.commit()
    return "charged"


async def _reload(session, statement_id: int) -> MessageStatements:
    return await session.scalar(
        select(MessageStatements).where(MessageStatements.id == statement_id).with_for_update().execution_options(populate_existing=True)
    )


async def charge_due(session, now: Optional[datetime] = None) -> dict:
    """Every statement waiting to be charged whose time has come."""
    now = now or _now()
    done = {"charged": 0, "failed": 0, "unconfirmed": 0, "manual": 0, "gave_up": 0}
    if not settings.MESSAGE_OVERAGE_BILLING:
        return done
    # A claim a stopped sweep left behind: found out before it is asked again.
    await session.execute(
        update(MessageStatements)
        .where(MessageStatements.status == S.CHARGING, MessageStatements.charging_since < now - STALE_CHARGE)
        .values(status=S.UNCONFIRMED, next_attempt_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    ids = list(
        await session.scalars(
            select(MessageStatements.id)
            .where(
                MessageStatements.status.in_((S.PENDING, S.FAILED, S.UNCONFIRMED)),
                func.coalesce(MessageStatements.next_attempt_at, now) <= now,
            )
            .order_by(MessageStatements.id)
            .limit(BATCH)
        )
    )
    for statement_id in ids:
        try:
            outcome = await charge(session, statement_id, now)
        except Exception:  # noqa: BLE001
            logger.exception("message statements: charging %s failed", statement_id)
            await session.rollback()
            continue
        if outcome in done:
            done[outcome] += 1
    return done


async def retry(session, statement: MessageStatements, now: Optional[datetime] = None) -> None:
    """An admin's "try again now" on one that failed or was given up on.
    Never on one that may have been made: that is found out first."""
    if statement.status not in (S.FAILED, S.GAVE_UP):
        return
    statement.status = S.FAILED
    statement.attempts = min(statement.attempts or 0, MAX_ATTEMPTS - 1)
    statement.next_attempt_at = now or _now()


# ─── 3. Keeping the refs small ───────────────────────────────────────────


async def prune_refs(session, now: Optional[datetime] = None) -> int:
    now = now or _now()
    res = await session.execute(
        delete(MessageUsageRefs).where(MessageUsageRefs.created_at < now - timedelta(days=service.REF_DAYS))
    )
    await session.commit()
    return int(res.rowcount or 0)


async def run(session, now: Optional[datetime] = None) -> dict:
    """The sweep's message steps. Returns what each did."""
    now = now or _now()
    out = {"message_statements": await close_months(session, now)}
    charged = await charge_due(session, now)
    out["overage_charged"] = charged["charged"]
    out["overage_failed"] = charged["failed"] + charged["gave_up"]
    await prune_refs(session, now)
    return out
