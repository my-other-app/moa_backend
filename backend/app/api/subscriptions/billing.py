"""Razorpay recurring billing.

Separate from `service.py` on purpose: everything that can move money lives
here, so an admin changing a plan dropdown cannot charge a card as a side
effect of a dropdown change. `service.assign_plan` grants a plan;
`start_subscription` takes money, and only this module does.

Razorpay's model:

- a *plan* is a price and a period, created once and reused
- a *subscription* links a customer to a plan and is authorised by the club
  through a hosted checkout URL
- charges then arrive as webhooks; we never poll

We mirror Razorpay's state rather than own it. Where the two disagree,
Razorpay is right -- it is the one that moved the money.
"""

import logging

from app.api.payments.service import razorpay_client
from app.api.subscriptions.models import (
    BillingCycle,
    ClubSubscriptions,
    Plans,
)
from app.api.subscriptions.service import mrr_for, price_for
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: Razorpay wants a finite number of billing cycles. Ten years of monthly
#: charges is effectively "until cancelled" without being unbounded, which
#: their API rejects.
TOTAL_COUNT = {BillingCycle.monthly: 120, BillingCycle.yearly: 10}

_PERIOD = {BillingCycle.monthly: "monthly", BillingCycle.yearly: "yearly"}


def _razorpay_plan_field(cycle: BillingCycle) -> str:
    return (
        "razorpay_plan_id_yearly"
        if cycle == BillingCycle.yearly
        else "razorpay_plan_id_monthly"
    )


def sync_plan(plan: Plans, cycle: BillingCycle) -> str:
    """
    Ensure a Razorpay plan exists for this plan and cycle, and return its id.

    Razorpay plans are immutable: the price cannot be edited once created. So
    changing a price here creates a *new* Razorpay plan and leaves the old one
    serving the subscribers already on it, which is the behaviour you want --
    existing subscribers keep the price they agreed to.
    """
    existing = getattr(plan, _razorpay_plan_field(cycle))
    if existing:
        return existing

    amount = price_for(plan, cycle)
    if amount <= 0:
        raise CustomHTTPException(
            status_code=422,
            message="A free plan has no Razorpay plan; subscribe to it directly.",
        )

    created = razorpay_client.plan.create(
        {
            "period": _PERIOD[cycle],
            "interval": 1,
            "item": {
                "name": f"{plan.name} ({cycle.value})",
                "amount": amount,
                "currency": "INR",
                "description": plan.tagline or plan.name,
            },
            # The key, not the id: ids differ between environments, the key
            # is what the webhook handler matches on.
            "notes": {"plan_key": plan.key, "cycle": cycle.value},
        }
    )
    setattr(plan, _razorpay_plan_field(cycle), created["id"])
    return created["id"]


def start_subscription(
    *,
    plan: Plans,
    cycle: BillingCycle,
    club_id: int,
    club_name: str,
    notify_email: str | None,
    notify_phone: str | None,
    trial_days: int | None = None,
    expire_by: int | None = None,
) -> dict:
    """
    Create a Razorpay subscription and return the checkout URL.

    This does not charge anything by itself: it creates an authorisation the
    club completes at `short_url`. The first charge arrives as a webhook once
    they have.

    `trial_days` is what the caller decided the club may have (a club gets one
    trial, ever); None means the plan's own. `expire_by` (unix seconds) is when
    the checkout link stops working, matching when a pending checkout falls
    back, so a club cannot pay for a checkout we have already given up on.
    """
    if trial_days is None:
        trial_days = plan.trial_days
    razorpay_plan_id = sync_plan(plan, cycle)

    payload = {
        "plan_id": razorpay_plan_id,
        "total_count": TOTAL_COUNT[cycle],
        "customer_notify": 1,
        "notes": {
            # How the webhook finds its way back to our row. Razorpay echoes
            # notes on every event for the subscription.
            "club_id": str(club_id),
            "club_name": club_name,
            "plan_key": plan.key,
            "cycle": cycle.value,
        },
    }
    if trial_days:
        payload["start_at"] = _trial_start(trial_days)
    if expire_by:
        payload["expire_by"] = expire_by

    notify = {}
    if notify_email:
        notify["email"] = notify_email
    if notify_phone:
        notify["contact"] = notify_phone
    if notify:
        payload["notify_info"] = notify

    subscription = razorpay_client.subscription.create(payload)
    return {
        "razorpay_subscription_id": subscription["id"],
        "checkout_url": subscription.get("short_url"),
        "status": subscription.get("status"),
    }


def _trial_start(trial_days: int) -> int:
    """Unix timestamp at which billing begins, i.e. when the trial ends."""
    from datetime import datetime, timedelta, timezone

    return int((datetime.now(timezone.utc) + timedelta(days=trial_days)).timestamp())


def cancel_subscription(subscription_id: str, *, at_period_end: bool = True) -> None:
    """
    Stop a subscription at Razorpay.

    Defaults to the end of the paid period: a club that cancels mid-month has
    paid for that month and keeps it. Cancelling immediately would be taking
    away something already bought.
    """
    razorpay_client.subscription.cancel(
        subscription_id, {"cancel_at_cycle_end": 1 if at_period_end else 0}
    )


#: Razorpay states in which a subscription can no longer charge anyone.
_FINISHED = {"cancelled", "completed", "expired"}


def close_unpaid_checkout(subscription_id: str) -> None:
    """
    Make sure a checkout nobody paid for can never be paid: cancel it now.

    If Razorpay refuses because it is already over (expired, cancelled), that
    is the outcome wanted. Anything else is raised as a 502: going on to open
    a second checkout while the first could still be paid is how a club ends
    up billed twice.
    """
    try:
        cancel_subscription(subscription_id, at_period_end=False)
        return
    except Exception:
        logger.info("Razorpay refused to cancel %s; checking whether it is over", subscription_id, exc_info=True)
    try:
        state = (razorpay_client.subscription.fetch(subscription_id) or {}).get("status")
    except Exception:
        state = None
    if state in _FINISHED:
        return
    raise CustomHTTPException(
        status_code=502,
        message="Razorpay did not confirm the earlier checkout is closed. Try again in a minute.",
        error_code="CHECKOUT_NOT_CLOSED",
    )


def stop_now(subscription_id: str) -> str:
    """
    Cancel a Razorpay subscription at once: the club is about to be given a
    plan for nothing, and must never be billed while it has it.

    Returns "cancelled", or "already_over" when Razorpay refused because it
    had finished with the subscription anyway (cancelled, completed,
    expired) -- the outcome wanted. Anything else is a 502 RAZORPAY_ERROR and
    the caller grants nothing: a comp on top of a subscription that can still
    charge is exactly what this is here to prevent.
    """
    try:
        cancel_subscription(subscription_id, at_period_end=False)
        return "cancelled"
    except Exception as exc:  # noqa: BLE001
        refused = str(exc)[:200]
        logger.info("Razorpay refused to cancel %s; checking whether it is over", subscription_id, exc_info=True)
    try:
        state = (razorpay_client.subscription.fetch(subscription_id) or {}).get("status")
    except Exception:  # noqa: BLE001
        state = None
    if state in _FINISHED:
        return "already_over"
    raise CustomHTTPException(
        status_code=502,
        message=f"Razorpay did not cancel the subscription, so no plan was given: {refused}",
        error_code="RAZORPAY_ERROR",
    )


def retry_charge(subscription_id: str) -> None:
    """
    Ask Razorpay to retry a failed charge now.

    Razorpay retries on its own schedule; this is the "try again, I have
    updated the card" button. It is the only call here an admin triggers
    directly, which is why it sits behind `manage_money`.
    """
    # `pending_update` is not a retry; the documented way to re-attempt a
    # halted subscription is to resume it, which re-queues the charge.
    razorpay_client.subscription.resume(
        subscription_id, {"resume_at": "now"}
    )


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

#: Events we act on. Anything else is logged and ignored rather than guessed
#: at -- Razorpay adds events, and reacting to one we do not understand is
#: how a subscription ends up in a state nobody intended.
HANDLED_EVENTS = {
    "subscription.authenticated",
    "subscription.activated",
    "subscription.charged",
    "subscription.pending",
    "subscription.halted",
    "subscription.paused",
    "subscription.resumed",
    "subscription.updated",
    "subscription.cancelled",
    "subscription.completed",
}

#: Events that confirm the club authorised or paid for the subscription
#: Razorpay is talking about -- the ones a late, unmatched delivery may adopt.
CONFIRMING_EVENTS = {
    "subscription.authenticated",
    "subscription.activated",
    "subscription.charged",
}


def club_id_from_payload(payload: dict) -> int | None:
    """Our club id, from the notes Razorpay echoes back."""
    entity = (
        payload.get("payload", {}).get("subscription", {}).get("entity", {})
    )
    raw = (entity.get("notes") or {}).get("club_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        logger.warning("Razorpay subscription note club_id was not an int: %r", raw)
        return None


def subscription_id_from_payload(payload: dict) -> str | None:
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    return entity.get("id")


def period_end_from_payload(payload: dict):
    """When the paid period ends, as an aware datetime, or None."""
    from datetime import datetime, timezone

    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    end = entity.get("current_end")
    if not end:
        return None
    try:
        return datetime.fromtimestamp(int(end), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def failure_reason_from_payload(payload: dict) -> str | None:
    payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
    return payment.get("error_description") or payment.get("error_reason")


def short_url_from_payload(payload: dict) -> str | None:
    """Razorpay's hosted page for the subscription, where a club can update
    how it pays. On subscription events; absent on payment ones."""
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {}) or {}
    url = entity.get("short_url")
    return url if isinstance(url, str) and url.startswith("https://") else None


def paid_count_from_payload(payload: dict) -> int | None:
    """How many charges Razorpay has taken on the subscription, if it says."""
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {}) or {}
    try:
        return int(entity["paid_count"]) if entity.get("paid_count") is not None else None
    except (TypeError, ValueError):
        return None


def payment_from_payload(payload: dict) -> dict:
    """The payment entity on a charged or failed event: id, amount (paise), method."""
    return payload.get("payload", {}).get("payment", {}).get("entity", {}) or {}


def razorpay_plan_id_from_payload(payload: dict) -> str | None:
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    return entity.get("plan_id")


def notes_from_payload(payload: dict) -> dict:
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    return entity.get("notes") or {}


def apply_event(
    subscription: ClubSubscriptions,
    event: str,
    payload: dict,
    *,
    new_plan: Plans | None = None,
    new_cycle: BillingCycle | None = None,
    now=None,
) -> bool:
    """
    Move our row to match the event. Returns whether anything changed.

    The state machine (service.is_entitled says what each state grants):

    - pending --authenticated--> trial, when a trial was granted at checkout;
      otherwise stays pending (authorised, waiting for the first charge)
    - pending | trial | past_due | paused --activated / charged--> active,
      clearing any grace
    - active | trial --pending / halted--> past_due (pending stays pending);
      halted on one that was paid for starts its grace (service.start_grace)
    - any --paused--> paused;  paused | past_due --resumed--> active, clearing
      any grace
    - pending --cancelled / completed--> back to what the club had before
    - anything else --cancelled / completed--> cancelled
    - updated: the plan or cycle changed at Razorpay (`new_plan`, resolved by
      the caller from Razorpay's plan id)

    Imported lazily by the webhook handler so this module stays free of
    session handling -- it decides *what* the new state is; the caller owns
    the transaction.
    """
    from datetime import datetime, timezone

    from app.api.subscriptions import service
    from app.api.subscriptions.models import SubscriptionStatus

    now = now or datetime.now(timezone.utc)
    status = subscription.status

    if event == "subscription.authenticated":
        subscription.authenticated_at = subscription.authenticated_at or now
        if status == SubscriptionStatus.pending and subscription.trial_ends_at and subscription.trial_ends_at > now:
            subscription.status = SubscriptionStatus.trial
            subscription.trial_started_at = subscription.trial_started_at or now
            subscription.current_period_end = subscription.trial_ends_at
            service.checkout_confirmed(subscription)
        return True

    if event in ("subscription.activated", "subscription.charged"):
        period_end = period_end_from_payload(payload)
        service.mark_charged(subscription, period_end=period_end)
        service.clear_grace(subscription)
        # A plan change at Razorpay's end (an upgrade mid-cycle) is reflected
        # in MRR here, where the plan row is to hand.
        if subscription.plan:
            subscription.mrr_paise = mrr_for(subscription.plan, subscription.cycle)
        return True

    if event in ("subscription.pending", "subscription.halted"):
        service.mark_failed(subscription, reason=failure_reason_from_payload(payload))
        if event == "subscription.halted":
            # Razorpay has stopped retrying. A club that paid keeps its plan
            # for GRACE_DAYS more; one that never paid had nothing to keep.
            service.start_grace(subscription, now)
        return True

    if event == "subscription.paused":
        subscription.status = SubscriptionStatus.paused
        subscription.mrr_paise = 0
        return True

    if event == "subscription.resumed":
        if status in (SubscriptionStatus.paused, SubscriptionStatus.past_due):
            subscription.status = SubscriptionStatus.active
            subscription.last_failure_reason = None
            subscription.activated_at = subscription.activated_at or now
            service.clear_grace(subscription)
            if subscription.plan:
                subscription.mrr_paise = mrr_for(subscription.plan, subscription.cycle)
            return True
        return False

    if event == "subscription.updated":
        if new_plan is None:
            logger.info("Razorpay subscription.updated for a plan we do not know; ignored")
            return False
        subscription.plan_id = new_plan.id
        subscription.plan = new_plan
        if new_cycle is not None:
            subscription.cycle = new_cycle
        if subscription.status == SubscriptionStatus.active:
            subscription.mrr_paise = mrr_for(new_plan, subscription.cycle)
        return True

    if event in ("subscription.cancelled", "subscription.completed"):
        if status == SubscriptionStatus.pending:
            # A checkout that was never paid for ending: the club goes back
            # to what it had.
            service.revert_checkout(subscription)
        else:
            service.mark_cancelled(subscription)
        return True

    logger.info("Ignoring unhandled Razorpay subscription event %s", event)
    return False


def apply_payment_failure(subscription: ClubSubscriptions, payload: dict) -> bool:
    """A `payment.failed` for one of this subscription's charges: say why,
    and a running subscription is now past due. Razorpay retries on its own."""
    from app.api.subscriptions import service

    service.mark_failed(subscription, reason=failure_reason_from_payload(payload))
    return True


def subscription_id_for_payment(payload: dict) -> str | None:
    """The Razorpay subscription a payment event belongs to, if any: on the
    payment itself, alongside it, or on the invoice it paid."""
    payment = payload.get("payload", {}).get("payment", {}).get("entity", {}) or {}
    found = payment.get("subscription_id") or (
        payload.get("payload", {}).get("subscription", {}).get("entity", {}) or {}
    ).get("id")
    if found:
        return found
    invoice_id = payment.get("invoice_id")
    if not invoice_id:
        return None
    try:
        return (razorpay_client.invoice.fetch(invoice_id) or {}).get("subscription_id")
    except Exception:
        logger.warning("Could not read Razorpay invoice %s for a failed payment", invoice_id, exc_info=True)
        return None
