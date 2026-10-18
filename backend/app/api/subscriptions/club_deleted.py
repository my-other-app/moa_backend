"""A deleted club stops paying.

A club is soft-deleted by an admin (delete or reject), or with its account
(an admin deleting a club's sign-in deletes the club: users.service). Its
Razorpay subscription used to carry on charging a club nobody could sign in
to. Now, in the same request:

- the subscription is cancelled at Razorpay at once -- best effort: a refusal
  or an outage is logged and said in the alert, and the deletion still goes
  ahead (an admin removing a club is not stopped by Razorpay being down);
- the row here is marked cancelled, whatever Razorpay said;
- the admins hear of it like any other cancellation: `subscription_cancelled`,
  with data.by = "club_deleted" and what Razorpay answered.

Only a subscription Razorpay could still charge is touched (pending,
trial, active, past due, paused, with a Razorpay id). A plan an admin gave
for nothing, or by hand, has nothing at Razorpay and is left as it is.

Restoring the club does NOT bring the subscription back: Razorpay cannot
un-cancel one, and the club comes back on Free and subscribes again if it
wants to (clubs.service.restore_club).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


async def end_for_deleted_club(session, club) -> Optional[str]:
    """Cancel the deleted club's live Razorpay subscription. Returns what
    happened -- "cancelled", "already_over" (Razorpay had finished with it),
    "razorpay_failed" (cancelled here only) -- or None when there was nothing
    to cancel. Does not commit: the caller's deletion and this go together."""
    from app.api.admin_alerts import notify
    from app.api.subscriptions import billing, service

    row = await service.get_club_subscription(session, club.id, for_update=True)
    if row is None or not row.razorpay_subscription_id or row.status not in service.LIVE_BILLED_STATUSES:
        return None
    razorpay_id = row.razorpay_subscription_id
    was = row.status.value
    reason = None
    try:
        outcome = await billing.stop_now(razorpay_id)
    except Exception as exc:  # noqa: BLE001
        outcome = "razorpay_failed"
        reason = (getattr(exc, "message", None) or str(exc) or type(exc).__name__)[:200]
        logger.warning("club %s deleted: Razorpay did not cancel subscription %s: %s", club.id, razorpay_id, reason)

    now = datetime.now(timezone.utc)
    row.cancel_at_period_end = False
    row.cancel_requested_at = row.cancel_requested_at or now
    row.pending_since = None
    row.previous_state = None
    service.mark_cancelled(row)

    plan_name = row.plan.name if row.plan else "its plan"
    await notify(
        session, "subscription_cancelled",
        title=f"{club.name} was deleted, so its {plan_name} subscription is cancelled",
        body=(
            "Razorpay did not confirm the cancellation. Check the subscription in the Razorpay dashboard and cancel it there."
            if outcome == "razorpay_failed"
            else "Cancelled at Razorpay; nothing more will be charged. Restoring the club does not restore the subscription."
        ),
        club=club, subscription=row, dedupe=False,
        data={"by": "club_deleted", "razorpay": outcome, "razorpay_subscription_id": razorpay_id,
              "previous_status": was, **({"reason": reason} if reason else {})},
    )
    return outcome
