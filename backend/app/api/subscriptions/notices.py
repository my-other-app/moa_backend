"""What a club and the admins hear when a club's billing changes.

The state machine (billing.apply_event) decides what changed; this says so.
Two audiences:

- the admins, through `admin_alerts.notify` (the bell, a mail, Discord);
- the club's owner, at the address it signs in with, for the few moments it
  has to act or should know: a failed payment, a grace starting and ending,
  a move to Free, a trial about to bill, and the welcome. Razorpay sends the
  receipts itself, so nothing here repeats one.

Each club mail goes at most once per subscription and kind -- recorded in
`subscription_emails` before it is sent, the unique key settling a race --
except a failed payment, which is once per failure. Mails leave after the
transaction commits (app.core.after_commit), so a change that is rolled back
tells nobody anything.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.admin_alerts import notify
from app.api.subscriptions import billing
from app.api.subscriptions.models import ClubSubscriptions, SubscriptionEmails, SubscriptionStatus
from app.config import settings
from app.core.after_commit import run_after_commit
from app.core.email import plain

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

#: Razorpay reports one failed charge more than once (`payment.failed`, then
#: `subscription.pending`); within this, they are one failure to the club.
FAILURE_WINDOW = timedelta(minutes=10)

#: The fields `after_webhook` compares before and after an event.
_TRACKED = (
    "status", "activated_at", "halted_at", "grace_ends_at", "cancel_requested_at",
    "trial_started_at", "current_period_end",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def capture(subscription: ClubSubscriptions) -> dict:
    """The row as it was before an event, for `after_webhook` to compare."""
    out = {field: getattr(subscription, field) for field in _TRACKED}
    if out["status"] is not None and not isinstance(out["status"], SubscriptionStatus):
        out["status"] = SubscriptionStatus(out["status"])
    return out


def day(value: datetime | None) -> str:
    """17 Oct 2026, in India."""
    if value is None:
        return "soon"
    local = value.astimezone(IST)
    return f"{local.day} {local:%b %Y}"


def rupees(paise: int | None) -> str:
    from app.core.money import from_paise

    amount = from_paise(paise)
    return f"₹{amount:,.0f}" if amount == amount.to_integral_value() else f"₹{amount:,.2f}"


def billing_url() -> str:
    return f"{settings.PUBLIC_WEB_URL.rstrip('/')}/billing"


def dashboard_url() -> str:
    return settings.PUBLIC_WEB_URL.rstrip("/")


def _value(enum_or_str):
    return getattr(enum_or_str, "value", enum_or_str)


def _plan_name(subscription) -> str:
    return subscription.plan.name if getattr(subscription, "plan", None) is not None else "your plan"


def _cycle_word(subscription) -> str:
    return "year" if _value(subscription.cycle) == "yearly" else "month"


def _price(subscription) -> str | None:
    """What the club pays a cycle: the price it signed up at."""
    from app.api.subscriptions.service import price_of

    if getattr(subscription, "plan", None) is None and getattr(subscription, "price_paise", None) is None:
        return None
    return f"{rupees(price_of(subscription))} a {_cycle_word(subscription)}"


def _data(subscription, **extra) -> dict:
    out = {
        "plan": subscription.plan.key if getattr(subscription, "plan", None) is not None else None,
        "cycle": _value(subscription.cycle),
        "status": _value(subscription.status),
        "razorpay_subscription_id": subscription.razorpay_subscription_id,
    }
    out.update({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in extra.items()})
    return {k: v for k, v in out.items() if v is not None}


async def _club(session, club_id: int):
    from app.api.clubs.models import Clubs

    return await session.get(Clubs, club_id)


async def owner_address(session, club_id: int) -> str | None:
    """The address the club's owner signs in with."""
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users

    return await session.scalar(
        select(Users.email).join(Clubs, Clubs.user_id == Users.id).where(Clubs.id == club_id)
    )


# ─── Club mails ─────────────────────────────────────────────────────────


def _send(to: str, subject: str, paragraphs: list[str], button, footer: str | None) -> None:
    """After commit, on a worker thread. Logged, never raised."""
    from app.core.email import email as email_mod

    try:
        email_mod.send_email(
            recipients=[to], subject=subject,
            text=plain.text(paragraphs, button=button, footer=footer),
            html=plain.render(paragraphs, button=button, footer=footer),
        )
    except Exception:  # noqa: BLE001
        logger.warning("billing email (%s) to %s could not be sent", subject, to, exc_info=True)


async def send_once(
    session,
    subscription: ClubSubscriptions,
    kind: str,
    episode: str,
    *,
    subject: str,
    paragraphs: list[str],
    button: tuple[str, str] | None = None,
    footer: str | None = None,
) -> bool:
    """Record the mail, and send it after commit, unless this (kind, episode)
    already went for the subscription. Returns whether it will go."""
    try:
        async with session.begin_nested():
            to = await owner_address(session, subscription.club_id)
            if not to:
                return False
            sent = await session.scalar(
                pg_insert(SubscriptionEmails)
                .values(subscription_id=subscription.id, kind=kind, episode=episode[:160], recipient=to[:254])
                .on_conflict_do_nothing(constraint="uq_subscription_email_once")
                .returning(SubscriptionEmails.id)
            )
        if sent is None:
            return False
        run_after_commit(session, _send, to, subject, paragraphs, button, footer)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("billing email %s for subscription %s could not be recorded", kind, subscription.id, exc_info=True)
        return False


def _episode(subscription) -> str:
    """The Razorpay subscription a mail is about (the row is reused when a
    club subscribes again), or the row itself for an admin's grant."""
    return subscription.razorpay_subscription_id or f"row:{subscription.id}"


def _grace_episode(subscription) -> str:
    started = subscription.halted_at.isoformat() if subscription.halted_at else "unknown"
    return f"{_episode(subscription)}:grace:{started}"


async def email_welcome(session, subscription, club_name: str) -> bool:
    plan = _plan_name(subscription)
    if subscription.status == SubscriptionStatus.trial:
        days = None
        if subscription.trial_ends_at and subscription.trial_started_at:
            days = max(1, round((subscription.trial_ends_at - subscription.trial_started_at).total_seconds() / 86400))
        first = (f"Your {days}-day {plan} trial is on" if days else f"Your {plan} trial is on") + \
            f" and runs until {day(subscription.trial_ends_at)}."
    else:
        first = f"{club_name} is on {plan} now" + (
            f", paid through {day(subscription.current_period_end)}." if subscription.current_period_end else "."
        )
    return await send_once(
        session, subscription, "subscription_activated", _episode(subscription),
        subject=f"Welcome to {plan}",
        paragraphs=[f"Hi {club_name},", first, f"Everything {plan} includes is switched on for your club."],
        button=("Open your dashboard", dashboard_url()),
        footer="Razorpay sends your payment receipts separately.",
    )


async def email_payment_failed(session, subscription, club_name: str, *, episode: str, reason: str | None,
                               short_url: str | None, entitled: bool) -> bool:
    """Once per failure: per failed payment, and never twice within
    FAILURE_WINDOW for one subscription."""
    try:
        async with session.begin_nested():
            recent = await session.scalar(
                select(SubscriptionEmails.id).where(
                    SubscriptionEmails.subscription_id == subscription.id,
                    SubscriptionEmails.kind == "payment_failed",
                    SubscriptionEmails.created_at >= _now() - FAILURE_WINDOW,
                ).limit(1)
            )
    except Exception:  # noqa: BLE001
        recent = None
    if recent:
        return False
    plan = _plan_name(subscription)
    why = f" ({reason})" if reason else ""
    keep = (
        f"{club_name} keeps {plan} while Razorpay tries again over the next few days."
        if entitled
        else f"Until a payment goes through, {club_name} is on the Free plan."
    )
    return await send_once(
        session, subscription, "payment_failed", episode,
        subject=f"Your payment for {plan} didn't go through",
        paragraphs=[
            f"Hi {club_name},",
            f"We couldn't take the payment for your {plan} plan{why}.",
            keep,
            "Update how you pay to make sure the next try works.",
        ],
        button=("Update payment", short_url or billing_url()),
        footer="Already updated your payment details? Then there's nothing more to do.",
    )


async def email_grace_started(session, subscription, club_name: str, *, short_url: str | None = None) -> bool:
    plan, ends = _plan_name(subscription), day(subscription.grace_ends_at)
    return await send_once(
        session, subscription, "grace_started", _grace_episode(subscription),
        subject=f"Update your payment by {ends} to keep {plan}",
        paragraphs=[
            f"Hi {club_name},",
            f"Razorpay tried several times but couldn't take the payment for your {plan} plan, and has stopped trying.",
            f"{club_name} keeps {plan} until {ends}. Update your payment by then, or the club moves to the Free plan.",
        ],
        button=("Update payment", short_url or billing_url()),
        footer="Your events, followers and data stay where they are either way.",
    )


async def email_grace_ending(session, subscription, club_name: str) -> bool:
    plan, ends = _plan_name(subscription), day(subscription.grace_ends_at)
    return await send_once(
        session, subscription, "grace_ending", _grace_episode(subscription),
        subject=f"Two days left: {plan} ends on {ends}",
        paragraphs=[
            f"Hi {club_name},",
            f"We still haven't been able to take the payment for {plan}.",
            f"Update your payment by {ends} to keep it. After that, {club_name} moves to the Free plan.",
        ],
        button=("Update payment", billing_url()),
    )


async def email_downgraded(session, subscription, club_name: str, *, plan_name: str, when: datetime) -> bool:
    return await send_once(
        session, subscription, "downgraded", _episode(subscription),
        subject=f"{club_name} has moved to the Free plan",
        paragraphs=[
            f"Hi {club_name},",
            f"We couldn't take payment for {plan_name}, so on {day(when)} {club_name} moved to the Free plan.",
            "Your events, followers and data are all still there. What you can do now is what Free includes.",
            f"To have {plan_name} back, subscribe again from Billing.",
        ],
        button=("Choose a plan", billing_url()),
    )


async def email_trial_ending(session, subscription, club_name: str) -> bool:
    plan, ends = _plan_name(subscription), day(subscription.trial_ends_at)
    price = _price(subscription)
    charge = (
        f"After that, Razorpay charges {price} with the payment method you set up."
        if price else "After that, Razorpay charges the plan's price with the payment method you set up."
    )
    return await send_once(
        session, subscription, "trial_ending", _episode(subscription),
        subject=f"Your {plan} trial ends on {ends}",
        paragraphs=[
            f"Hi {club_name},",
            f"Your free trial of {plan} ends on {ends}.",
            charge,
            f"Nothing to do if you want to keep it. To stop before you're charged, cancel from Billing before {ends}.",
        ],
        button=("Billing", billing_url()),
    )


def _comp_episode(subscription) -> str:
    """One complimentary grant: the row and the end it was given. Granting it
    again with another end is a new one, with mails of its own."""
    ends = subscription.comp_ends_at.isoformat() if subscription.comp_ends_at else "open"
    return f"row:{subscription.id}:comp:{ends}"


async def email_comp_ending(session, subscription, club_name: str, *, then: str) -> bool:
    """A week before a complimentary plan ends. `then` is what the club has
    after it: "the Free plan", or the plan it had before."""
    plan, ends = _plan_name(subscription), day(subscription.comp_ends_at)
    return await send_once(
        session, subscription, "comp_ending", _comp_episode(subscription),
        subject=f"Your complimentary {plan} ends on {ends}",
        paragraphs=[
            f"Hi {club_name},",
            f"The {plan} plan MyOtherApp has given {club_name} ends on {ends}.",
            f"After that, {club_name} is on {then}. Your events, followers and data stay where they are.",
            f"To keep everything {plan} includes, subscribe from Billing once it ends, or talk to us.",
        ],
        button=("Billing", billing_url()),
    )


async def email_comp_ended(session, subscription, club_name: str, *, plan_name: str, then: str) -> bool:
    """Once, when a complimentary plan has ended. Recorded against the comp
    before the row moves on."""
    return await send_once(
        session, subscription, "comp_ended", _comp_episode(subscription),
        subject=f"Your complimentary {plan_name} has ended",
        paragraphs=[
            f"Hi {club_name},",
            f"The {plan_name} plan MyOtherApp gave {club_name} has ended, and {club_name} is on {then} now.",
            "Your events, followers and data are all still there.",
            f"To have {plan_name} back, subscribe from Billing.",
        ],
        button=("Choose a plan", billing_url()),
    )


# ─── After a webhook ────────────────────────────────────────────────────


async def after_webhook(session, subscription: ClubSubscriptions, event: str, payload: dict, before: dict) -> None:
    """Tell whoever needs telling about what `event` just did to the row.
    `before` is `capture(row)` from before the event. Never raises."""
    try:
        # In a SAVEPOINT of its own: whatever fails in here takes only the
        # notices with it, never the state change they describe.
        async with session.begin_nested():
            await _after_webhook(session, subscription, event, payload, before)
    except Exception:  # noqa: BLE001
        logger.warning("billing notices for %s on subscription %s failed", event, subscription.id, exc_info=True)


async def _after_webhook(session, sub, event, payload, before) -> None:
    from app.api.subscriptions.service import is_entitled

    was, now_status = before["status"], sub.status
    if event in ("subscription.cancelled", "subscription.completed") and was in (
        SubscriptionStatus.pending, SubscriptionStatus.cancelled,
    ):
        # An unpaid checkout closing, or Razorpay confirming what is already
        # over here: nothing that was running stopped. (A pending row went
        # back to what the club had, and its `plan` is no longer loaded.)
        return
    club = await _club(session, sub.club_id)
    name = club.name if club is not None else f"Club {sub.club_id}"
    plan = _plan_name(sub)

    if event == "subscription.authenticated":
        if was != SubscriptionStatus.trial and now_status == SubscriptionStatus.trial:
            await notify(
                session, "subscription_trial_started",
                title=f"{name} started a {plan} trial",
                body=f"The trial runs until {day(sub.trial_ends_at)}; Razorpay then charges {_price(sub) or 'the plan price'}.",
                club=club, subscription=sub, data=_data(sub, trial_ends_at=sub.trial_ends_at),
            )
            await email_welcome(session, sub, name)
        return

    if event in ("subscription.activated", "subscription.charged"):
        recovered = was == SubscriptionStatus.past_due
        if before["activated_at"] is None:
            await notify(
                session, "subscription_activated",
                title=f"{name} subscribed to {plan}",
                body=f"{_price(sub) or plan}, paid through {day(sub.current_period_end)}.",
                club=club, subscription=sub, data=_data(sub, current_period_end=sub.current_period_end),
            )
            await email_welcome(session, sub, name)
        elif event == "subscription.charged":
            # Razorpay sends `activated` and `charged` for the first payment;
            # that one is the activation above, not a renewal.
            paid = billing.paid_count_from_payload(payload)
            if paid is None or paid > 1:
                amount = billing.payment_from_payload(payload).get("amount")
                await notify(
                    session, "payment_received",
                    title=f"{name} paid {rupees(amount)} for {plan}" if amount else f"{name} paid for {plan}",
                    body=(
                        ("The past-due renewal went through" + (" and the grace is over." if before["halted_at"] else "."))
                        if recovered else f"Renewed through {day(sub.current_period_end)}."
                    ),
                    club=club, subscription=sub,
                    data=_data(sub, amount_paise=amount, paid_count=paid, recovered=recovered or None,
                               current_period_end=sub.current_period_end),
                )
        return

    if event == "subscription.pending":
        await _failure(session, sub, club, name, before, reason=sub.last_failure_reason,
                       episode=f"{event}:{_now().isoformat()}", short_url=billing.short_url_from_payload(payload),
                       payment_id=None)
        return

    if event == "subscription.halted":
        # Razorpay's last retry failed: its own `payment.failed` said so, and
        # the grace mail says what happens next. No second failure mail.
        from app.api.subscriptions.service import in_grace

        grace_began = before["halted_at"] is None and sub.halted_at is not None
        # A grace an admin started while Razorpay was still retrying, now
        # lengthened to the full GRACE_DAYS from the halt.
        grace_moved = not grace_began and sub.grace_ends_at is not None and before["grace_ends_at"] != sub.grace_ends_at
        await _failure(session, sub, club, name, before, reason=sub.last_failure_reason,
                       episode=f"{event}:{_now().isoformat()}", short_url=None, payment_id=None,
                       alert_failure=not (grace_began or grace_moved), email_club=False)
        if grace_began or grace_moved:
            await notify(
                session, "grace_started",
                title=f"{name}: Razorpay stopped retrying — grace until {day(sub.grace_ends_at)}",
                body=(
                    f"{name} keeps {plan} until {day(sub.grace_ends_at)}, then moves to Free. "
                    "Retry the charge, extend the grace or cancel from Subscriptions."
                ),
                club=club, subscription=sub,
                data=_data(sub, halted_at=sub.halted_at, grace_ends_at=sub.grace_ends_at, reason=sub.last_failure_reason),
            )
        if in_grace(sub):
            # Once per grace (the ledger's episode is the grace's start).
            await email_grace_started(session, sub, name, short_url=billing.short_url_from_payload(payload))
        return

    if event == "subscription.paused":
        if was != SubscriptionStatus.paused and now_status == SubscriptionStatus.paused:
            await notify(session, "subscription_paused", title=f"{name} paused {plan}",
                         body="Paused at Razorpay: nothing is charged, and the club is on Free until it resumes.",
                         club=club, subscription=sub, data=_data(sub))
        return

    if event == "subscription.resumed":
        if was != now_status:
            await notify(session, "subscription_resumed", title=f"{name} resumed {plan}",
                         body="Razorpay is charging again and the plan is back on.",
                         club=club, subscription=sub, data=_data(sub, previous_status=_value(was)))
        return

    if event in ("subscription.cancelled", "subscription.completed"):
        completed = event == "subscription.completed"
        if not completed and before["cancel_requested_at"] is not None:
            return  # the club or an admin asked; that was the alert
        await notify(
            session, "subscription_cancelled",
            title=f"{name}'s {plan} subscription {'completed' if completed else 'was cancelled at Razorpay'}",
            body=(
                "Razorpay finished every billing cycle it was set up for."
                if completed else "Cancelled outside the app (Razorpay's dashboard, or Razorpay itself)."
            ) + (" Access runs to " + day(sub.current_period_end) + "." if is_entitled(sub) else " The club is on Free now."),
            club=club, subscription=sub, data=_data(sub, by="razorpay_completed" if completed else "razorpay"),
        )
        return


async def after_payment_failure(session, subscription: ClubSubscriptions, payload: dict, before: dict) -> None:
    """A `payment.failed` for one of the subscription's charges. Never raises."""
    try:
        async with session.begin_nested():
            club = await _club(session, subscription.club_id)
            name = club.name if club is not None else f"Club {subscription.club_id}"
            payment = billing.payment_from_payload(payload)
            await _failure(
                session, subscription, club, name, before, reason=subscription.last_failure_reason,
                episode=f"payment:{payment.get('id') or _now().isoformat()}", short_url=None,
                payment_id=payment.get("id"), amount=payment.get("amount"),
            )
    except Exception:  # noqa: BLE001
        logger.warning("billing notices for payment.failed on subscription %s failed", subscription.id, exc_info=True)


async def _failure(session, sub, club, name, before, *, reason, episode, short_url, payment_id, amount=None,
                   alert_failure: bool = True, email_club: bool = True) -> None:
    from app.api.subscriptions.service import is_entitled

    was = before["status"]
    plan = _plan_name(sub)
    checkout = was == SubscriptionStatus.pending
    if alert_failure:
        await notify(
            session, "payment_failed",
            title=f"Payment failed for {name} ({plan})",
            body=(reason or "Razorpay gave no reason.") + (
                " This was the first charge of a checkout; the club has nothing until it goes through." if checkout else ""
            ),
            club=club, subscription=sub,
            data=_data(sub, reason=reason, payment_id=payment_id, amount_paise=amount, checkout=checkout or None),
        )
    if sub.status == SubscriptionStatus.past_due and was != SubscriptionStatus.past_due:
        entitled = is_entitled(sub)
        await notify(
            session, "subscription_past_due",
            title=f"{name} is past due on {plan}",
            body=(
                f"Razorpay is retrying. {name} keeps {plan} meanwhile."
                if entitled else f"It was never paid for, so {name} is on Free until a charge goes through."
            ),
            club=club, subscription=sub, data=_data(sub, reason=reason),
        )
    if email_club and not checkout:
        # A club at checkout sees the failure on Razorpay's own page.
        await email_payment_failed(session, sub, name, episode=episode, reason=reason, short_url=short_url,
                                   entitled=is_entitled(sub))
