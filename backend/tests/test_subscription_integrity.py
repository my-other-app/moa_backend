"""Club subscriptions against Razorpay's real behaviour: events out of order,
a paid period that ends with no word from Razorpay, the price a subscriber
actually pays, a double-clicked checkout, and a charge on a subscription no
club holds. Every Razorpay call goes to the fake in test_club_billing."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import event as sa_event
from sqlalchemy import select

from app.api.admin_alerts.models import AdminNotifications
from tests.test_club_billing import (  # noqa: F401 (fixtures)
    _row,
    _start,
    razorpay,
    shop,
)

NOW = datetime.now(timezone.utc)
_seq = {"n": 0}


async def _event(session, name, sub_id, *, at, status=None, current_end=None, payment=None, club_id=None):
    """A Razorpay subscription webhook made at `at` (its created_at)."""
    from app.api.payments.service import handle_razorpay_webhook

    _seq["n"] += 1
    entity = {"id": sub_id, "notes": {}}
    if club_id is not None:
        entity["notes"]["club_id"] = str(club_id)
    if status is not None:
        entity["status"] = status
    if current_end is not None:
        entity["current_end"] = int(current_end.timestamp())
    payload = {"subscription": {"entity": entity}}
    if payment is not None:
        payload["payment"] = {"entity": payment}
    data = {"event": name, "entity": "event", "created_at": int(at.timestamp()), "payload": payload}
    await handle_razorpay_webhook(session, data, event_id=f"evt_order_{_seq['n']}", signature="sig")


async def _charges(session, club_id):
    from app.api.subscriptions.models import SubscriptionCharges

    return list((await session.scalars(select(SubscriptionCharges).where(SubscriptionCharges.club_id == club_id))).all())


async def test_a_late_pending_after_a_charge_is_not_applied(client, session, shop, razorpay):
    """Razorpay does not deliver in order: a `pending` from before the
    charge, arriving after it, made a paying club past due."""
    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]
    await _event(session, "subscription.charged", sub_id, at=NOW, current_end=NOW + timedelta(days=30),
                 payment={"id": "pay_ord_1", "amount": 69900})
    await _event(session, "subscription.pending", sub_id, at=NOW - timedelta(minutes=5))
    row = await _row(session, shop["club"].id)
    assert row.status.value == "active" and row.last_event_at == NOW.replace(microsecond=0)


async def test_a_late_charge_after_cancelling_is_recorded_but_does_not_reactivate(client, session, shop, razorpay):
    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]
    await _event(session, "subscription.charged", sub_id, at=NOW - timedelta(hours=2), current_end=NOW + timedelta(days=30),
                 payment={"id": "pay_ord_2", "amount": 69900})
    await _event(session, "subscription.cancelled", sub_id, at=NOW)
    await _event(session, "subscription.charged", sub_id, at=NOW - timedelta(hours=1), current_end=NOW + timedelta(days=60),
                 payment={"id": "pay_ord_3", "amount": 69900})
    row = await _row(session, shop["club"].id)
    assert row.status.value == "cancelled"
    # The money moved whatever the order: both charges are in the ledger.
    assert sorted(c.razorpay_payment_id for c in await _charges(session, shop["club"].id)) == ["pay_ord_2", "pay_ord_3"]


async def test_the_subscriptions_own_status_wins_over_the_events_name(client, session, shop, razorpay):
    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]
    await _event(session, "subscription.charged", sub_id, at=NOW - timedelta(minutes=10), current_end=NOW + timedelta(days=30),
                 payment={"id": "pay_ord_4", "amount": 69900})
    # A `pending` whose subscription Razorpay already holds as active again.
    await _event(session, "subscription.pending", sub_id, at=NOW, status="active")
    assert (await _row(session, shop["club"].id)).status.value == "active"
    # A `charged` on a subscription Razorpay holds as cancelled.
    await _event(session, "subscription.charged", sub_id, at=NOW + timedelta(minutes=1), status="cancelled",
                 payment={"id": "pay_ord_5", "amount": 69900})
    assert (await _row(session, shop["club"].id)).status.value == "cancelled"


async def test_active_lapses_three_days_after_the_paid_period_with_no_renewal(client, session, shop, razorpay):
    """`active` granted the plan with no look at the period: a renewal
    webhook lost, and the club kept its plan for ever."""
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]
    await _event(session, "subscription.charged", sub_id, at=NOW, current_end=NOW + timedelta(days=30),
                 payment={"id": "pay_ord_6", "amount": 69900})
    row = await _row(session, shop["club"].id)
    assert service.is_entitled(row, NOW + timedelta(days=32)) is True
    assert service.is_entitled(row, NOW + timedelta(days=33, minutes=1)) is False
    assert service.paying(row, NOW + timedelta(days=33, minutes=1)) is False

    # An admin's grant stands on the admin's word, whatever date is on it.
    await service.assign_plan(session, club_id=shop["club"].id, plan=shop["growth"], cycle=BillingCycle.monthly, granted_by=shop["owner"].id)
    await session.commit()
    row = await _row(session, shop["club"].id)
    assert service.is_entitled(row, NOW + timedelta(days=90)) is True


async def test_mrr_is_what_the_subscriber_pays_not_the_plans_price_today(client, session, shop, razorpay):
    """A charge recomputed MRR from the plan: after an admin raised the
    price, every existing subscriber was counted at the new one, though
    Razorpay keeps charging them the old."""
    from app.api.subscriptions import service

    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]
    assert (await _row(session, shop["club"].id)).price_paise == 69900
    shop["growth"].price_monthly_paise = 99900
    await session.commit()
    await _event(session, "subscription.charged", sub_id, at=NOW, current_end=NOW + timedelta(days=30),
                 payment={"id": "pay_ord_7", "amount": 69900})
    row = await _row(session, shop["club"].id)
    assert row.mrr_paise == 69900
    assert (await service.mrr_totals(session))["mrr_paise"] == 69900


async def test_a_double_clicked_checkout_takes_the_clubs_lock_around_razorpay(client, session, shop, razorpay):
    """Two clicks both read the row before either wrote it and opened two
    Razorpay subscriptions; the one not kept could still charge the club."""
    seen: list[str] = []
    conn = await session.connection()

    def before(_conn, _cursor, statement, *_args):
        seen.append(statement)

    sa_event.listen(conn.sync_connection, "before_cursor_execute", before)
    created_after = []
    original = razorpay.handlers[("subscription", "create")]

    def create(data):
        created_after.append(len(seen))
        return original(data)

    razorpay.handlers[("subscription", "create")] = create
    try:
        assert (await _start(client, shop, plan_key="growth")).status_code == 200
    finally:
        sa_event.remove(conn.sync_connection, "before_cursor_execute", before)
    before_create = seen[: created_after[0]]
    assert any("pg_advisory_xact_lock" in s for s in before_create)
    assert any("FROM club_subscriptions" in s and "FOR UPDATE" in s for s in before_create)
    # The second click replaces the first, cancelled at Razorpay: never two live.
    second = await _start(client, shop, plan_key="growth")
    assert second.status_code == 200
    assert razorpay.of("subscription", "cancel")[0][0] == "sub_new1"


async def test_a_charge_on_a_subscription_no_club_holds_is_cancelled_and_reported(client, session, shop, razorpay):
    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]
    await _event(session, "subscription.charged", sub_id, at=NOW, current_end=NOW + timedelta(days=30),
                 payment={"id": "pay_ord_8", "amount": 69900})
    # Another subscription, the club's notes on it, charged while the club pays for sub_id.
    await _event(session, "subscription.charged", "sub_orphan", at=NOW, club_id=shop["club"].id,
                 payment={"id": "pay_orphan", "amount": 69900})
    assert ("sub_orphan", {"cancel_at_cycle_end": 0}) in razorpay.of("subscription", "cancel")
    alerts = list((await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == "subscription_orphan_charged"))).all())
    assert len(alerts) == 1
    assert alerts[0].club_id == shop["club"].id and alerts[0].data["payment_id"] == "pay_orphan" and alerts[0].data["cancelled"] is True
    # What the club has is untouched.
    row = await _row(session, shop["club"].id)
    assert (row.razorpay_subscription_id, row.status.value) == (sub_id, "active")
