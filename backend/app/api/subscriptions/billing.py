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
) -> dict:
    """
    Create a Razorpay subscription and return the checkout URL.

    This does not charge anything by itself: it creates an authorisation the
    club completes at `short_url`. The first charge arrives as a webhook once
    they have.
    """
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
    if plan.trial_days:
        payload["start_at"] = _trial_start(plan.trial_days)

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
    "subscription.activated",
    "subscription.charged",
    "subscription.pending",
    "subscription.halted",
    "subscription.cancelled",
    "subscription.completed",
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


def apply_event(subscription: ClubSubscriptions, event: str, payload: dict) -> bool:
    """
    Move our row to match the event. Returns whether anything changed.

    Imported lazily by the webhook handler so this module stays free of
    session handling -- it decides *what* the new state is; the caller owns
    the transaction.
    """
    from app.api.subscriptions import service

    if event in ("subscription.activated", "subscription.charged"):
        period_end = period_end_from_payload(payload)
        service.mark_charged(subscription, period_end=period_end)
        # A plan change at Razorpay's end (an upgrade mid-cycle) is reflected
        # in MRR here, where the plan row is to hand.
        if subscription.plan:
            subscription.mrr_paise = mrr_for(subscription.plan, subscription.cycle)
        return True

    if event in ("subscription.pending", "subscription.halted"):
        service.mark_failed(subscription, reason=failure_reason_from_payload(payload))
        return True

    if event in ("subscription.cancelled", "subscription.completed"):
        service.mark_cancelled(subscription)
        return True

    logger.info("Ignoring unhandled Razorpay subscription event %s", event)
    return False
