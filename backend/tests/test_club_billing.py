"""Club billing: nothing is granted until Razorpay confirms it, one trial per
club, no second checkout on top of a live one, cancelling, the webhooks, and
the sign-up and sign-in paths the self-serve flow needs.

Razorpay is LIVE in production: every call here goes to a fake, and a test
that reached the real client would fail on its missing credentials.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth_header

API = "/api/v1"


# ─── Fakes and helpers ───────────────────────────────────────────────────


class _Resource:
    def __init__(self, fake, name):
        self._fake, self._name = fake, name

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self._fake.calls.append((self._name, method, args))
            handler = self._fake.handlers.get((self._name, method))
            if handler is None:
                raise AssertionError(f"unexpected Razorpay call {self._name}.{method}{args}")
            return handler(*args)

        return call


class FakeRazorpay:
    """Records every call; answers the ones a test expects."""

    def __init__(self):
        self.calls = []
        self.created = 0
        self.handlers = {
            ("plan", "create"): lambda data: {"id": f"plan_{data['period']}_{len(self.calls)}"},
            ("subscription", "create"): self._create,
            ("subscription", "cancel"): lambda sub_id, data: {"id": sub_id, "status": "cancelled"},
        }

    def _create(self, data):
        self.created += 1
        return {"id": f"sub_new{self.created}", "short_url": f"https://rzp.io/i/new{self.created}", "status": "created"}

    def __getattr__(self, name):
        return _Resource(self, name)

    def of(self, resource, method):
        return [args for r, m, args in self.calls if (r, m) == (resource, method)]


@pytest.fixture
def razorpay(monkeypatch):
    from app.api.payments import service as payments
    from app.api.subscriptions import billing

    fake = FakeRazorpay()
    monkeypatch.setattr(billing, "razorpay_client", fake)
    monkeypatch.setattr(payments, "razorpay_client", fake)
    return fake


async def make_plan(session, *, key, monthly=0, yearly=0, trial=0, visibility="public", contact_sales=False,
                    deleted=False, limits=None, features=()):
    from app.api.subscriptions.models import PlanFeatures, PlanLimits, PlanVisibility, Plans

    plan = Plans(key=key, name=key.title(), price_monthly_paise=monthly, price_yearly_paise=yearly,
                 trial_days=trial, visibility=PlanVisibility(visibility), contact_sales=contact_sales)
    if deleted:
        plan.soft_delete()
    session.add(plan)
    await session.flush()
    session.add(PlanLimits(plan_id=plan.id, **(limits or {})))
    session.add(PlanFeatures(plan_id=plan.id, **{f: True for f in features}))
    await session.flush()
    return plan


@pytest.fixture
async def shop(session, make_club, mint_token):
    """A club owner, and the plans a pricing page would show."""
    owner, club = await make_club(name="Billing Club", email="billing-owner@example.com")
    free = await make_plan(session, key="free", limits={"events_per_month": 2})
    growth = await make_plan(session, key="growth", monthly=69900, yearly=699000, trial=14,
                             limits={"events_per_month": 15}, features=("tickets", "quiz"))
    pro = await make_plan(session, key="pro", monthly=149900, yearly=1499000, trial=14,
                          limits={"events_per_month": None}, features=("tickets", "quiz", "merch"))
    await session.commit()
    return dict(owner=owner, club=club, free=free, growth=growth, pro=pro, h=auth_header(await mint_token(owner)))


async def _row(session, club_id):
    """The club's row as the database has it now."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.api.subscriptions.models import ClubSubscriptions

    return await session.scalar(
        select(ClubSubscriptions)
        .where(ClubSubscriptions.club_id == club_id, ClubSubscriptions.is_deleted == False)  # noqa: E712
        .options(selectinload(ClubSubscriptions.plan))
        .execution_options(populate_existing=True)
    )


async def _plan_key(session, club_id):
    from app.api.subscriptions import service

    await _row(session, club_id)
    return (await service.resolve_entitlements(session, club_id)).plan.key


async def _start(client, shop, **body):
    return await client.post(f"{API}/clubs/subscription/start", json=body, headers=shop["h"])


async def _webhook(session, event, sub_id, *, club_id=None, notes=None, current_end=None, plan_id=None,
                   payment=None, event_id=None):
    from app.api.payments.service import handle_razorpay_webhook

    entity = {"id": sub_id, "notes": dict(notes or {})}
    if club_id is not None:
        entity["notes"]["club_id"] = str(club_id)
    if current_end is not None:
        entity["current_end"] = int(current_end.timestamp())
    if plan_id is not None:
        entity["plan_id"] = plan_id
    payload = {"subscription": {"entity": entity}}
    if payment is not None:
        payload["payment"] = {"entity": payment}
    data = {"event": event, "entity": "event", "payload": payload}
    await handle_razorpay_webhook(session, data, event_id=event_id or f"evt_{event}_{sub_id}_{datetime.now().timestamp()}", signature="sig")


# ─── 1. Buying by key ────────────────────────────────────────────────────


async def test_the_pricing_page_gives_each_plan_its_id(client, shop):
    plans = (await client.get(f"{API}/plans")).json()["plans"]
    assert {p["key"]: p["id"] for p in plans}["growth"] == shop["growth"].id


async def test_a_checkout_can_name_the_plan_by_key_or_by_id(client, session, shop, razorpay):
    by_key = await _start(client, shop, plan_key="Growth", cycle="yearly")
    assert by_key.status_code == 200, by_key.text
    body = by_key.json()
    assert body["plan_key"] == "growth" and body["cycle"] == "yearly" and body["status"] == "pending"
    assert body["checkout_url"] == "https://rzp.io/i/new1" and body["razorpay_status"] == "created"
    assert body["trial_days"] == 14

    # The pending one was never authorised, so a second checkout replaces it.
    by_id = await _start(client, shop, plan_id=shop["pro"].id)
    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["plan_key"] == "pro"

    neither = await _start(client, shop, cycle="monthly")
    assert neither.status_code == 422


# ─── 2. Nothing before Razorpay confirms ─────────────────────────────────


async def test_opening_checkout_grants_nothing(client, session, shop, razorpay):
    started = await _start(client, shop, plan_key="growth")
    assert started.status_code == 200, started.text

    mine = (await client.get(f"{API}/clubs/subscription", headers=shop["h"])).json()
    assert (mine["status"], mine["entitled"], mine["plan"]["key"]) == ("pending", False, "growth")
    assert mine["checkout_expires_at"] is not None

    ent = (await client.get(f"{API}/clubs/entitlements", headers=shop["h"])).json()
    assert ent["plan_key"] == "free" and ent["subscription_status"] == "pending"
    assert ent["features"]["tickets"] is False
    # And the checkout link dies when the pending falls back.
    created = razorpay.of("subscription", "create")[0][0]
    assert created["expire_by"] == int(datetime.fromisoformat(started.json()["expires_at"]).timestamp())
    assert "start_at" in created  # the plan's trial, for a club that never had one


async def test_authenticated_starts_the_trial_and_a_charge_makes_it_active(client, session, shop, razorpay):
    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]

    await _webhook(session, "subscription.authenticated", sub_id)
    row = await _row(session, shop["club"].id)
    assert row.status.value == "trial" and row.trial_started_at is not None
    assert row.current_period_end == row.trial_ends_at
    assert await _plan_key(session, shop["club"].id) == "growth"

    await _webhook(session, "subscription.charged", sub_id, current_end=datetime.now(timezone.utc) + timedelta(days=30))
    row = await _row(session, shop["club"].id)
    assert row.status.value == "active" and row.activated_at is not None and row.mrr_paise == 69900
    assert row.previous_state is None and row.pending_since is None


async def test_without_a_trial_authenticated_waits_for_the_charge(client, session, shop, razorpay):
    from app.api.subscriptions.models import ClubSubscriptions

    # The club had its trial already.
    session.add(ClubSubscriptions(club_id=shop["club"].id, plan_id=shop["growth"].id, status="cancelled",
                                  trial_started_at=datetime.now(timezone.utc) - timedelta(days=90)))
    await session.commit()
    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]

    await _webhook(session, "subscription.authenticated", sub_id)
    row = await _row(session, shop["club"].id)
    assert row.status.value == "pending" and row.authenticated_at is not None
    assert await _plan_key(session, shop["club"].id) == "free"

    await _webhook(session, "subscription.activated", sub_id, current_end=datetime.now(timezone.utc) + timedelta(days=30))
    assert await _plan_key(session, shop["club"].id) == "growth"


async def test_a_plan_the_club_had_keeps_going_while_a_checkout_is_pending(client, session, shop, razorpay, make_admin, mint_token):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    await service.assign_plan(session, club_id=shop["club"].id, plan=shop["growth"], cycle=BillingCycle.monthly)
    await session.commit()

    started = await _start(client, shop, plan_key="pro")
    assert started.status_code == 200, started.text
    ent = (await client.get(f"{API}/clubs/entitlements", headers=shop["h"])).json()
    assert (ent["plan_key"], ent["status"], ent["subscription_status"]) == ("growth", "active", "pending")

    # A day on, unpaid: it falls back to the Growth the admin granted.
    row = await _row(session, shop["club"].id)
    row.pending_since = datetime.now(timezone.utc) - timedelta(hours=25)
    await session.commit()
    mine = (await client.get(f"{API}/clubs/subscription", headers=shop["h"])).json()
    assert (mine["plan"]["key"], mine["status"], mine["entitled"]) == ("growth", "active", True)
    assert mine["razorpay_subscription_id"] is None and mine["pending_since"] is None


async def test_an_unpaid_checkout_with_nothing_before_it_falls_back_to_free(client, session, shop, razorpay):
    await _start(client, shop, plan_key="growth")
    row = await _row(session, shop["club"].id)
    row.pending_since = datetime.now(timezone.utc) - timedelta(hours=25)
    await session.commit()

    ent = (await client.get(f"{API}/clubs/entitlements", headers=shop["h"])).json()
    assert ent["plan_key"] == "free" and ent["subscription_status"] == "cancelled"


async def test_past_due_keeps_the_plan_only_for_a_subscription_once_paid(session, shop):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import ClubSubscriptions, SubscriptionStatus

    row = ClubSubscriptions(club_id=shop["club"].id, plan_id=shop["growth"].id, status=SubscriptionStatus.past_due,
                            razorpay_subscription_id="sub_never_paid")
    session.add(row)
    await session.flush()
    assert await _plan_key(session, shop["club"].id) == "free"

    row = await _row(session, shop["club"].id)
    row.activated_at = datetime.now(timezone.utc) - timedelta(days=40)
    await session.flush()
    assert await _plan_key(session, shop["club"].id) == "growth"
    # An old-style trial written before Razorpay confirmed anything grants nothing.
    row.status, row.razorpay_subscription_id = SubscriptionStatus.trial, "sub_old_trial"
    await session.flush()
    assert service.is_entitled(row) is False


async def test_an_admin_grant_needs_no_razorpay(session, shop):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle, SubscriptionStatus

    for status in (SubscriptionStatus.trial, SubscriptionStatus.past_due, SubscriptionStatus.active):
        row = await service.assign_plan(session, club_id=shop["club"].id, plan=shop["pro"], cycle=BillingCycle.monthly, status=status)
        assert service.is_entitled(row), status


# ─── 3. One trial per club ───────────────────────────────────────────────


async def test_a_club_that_had_a_trial_gets_none_the_second_time(client, session, shop, razorpay):
    first = await _start(client, shop, plan_key="growth")
    await _webhook(session, "subscription.authenticated", first.json()["razorpay_subscription_id"])
    await _webhook(session, "subscription.cancelled", first.json()["razorpay_subscription_id"])
    row = await _row(session, shop["club"].id)
    assert row.status.value == "cancelled" and row.trial_started_at is not None

    again = await _start(client, shop, plan_key="pro")
    assert again.status_code == 200, again.text
    assert again.json()["trial_days"] == 0
    assert "start_at" not in razorpay.of("subscription", "create")[-1][0]


# ─── 4. No double billing ────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["trial", "active", "past_due", "paused"])
async def test_a_live_subscription_refuses_a_second_checkout(client, session, shop, razorpay, status):
    from app.api.subscriptions.models import ClubSubscriptions

    session.add(ClubSubscriptions(club_id=shop["club"].id, plan_id=shop["growth"].id, status=status,
                                  razorpay_subscription_id="sub_live", authenticated_at=datetime.now(timezone.utc)))
    await session.commit()

    response = await _start(client, shop, plan_key="pro")

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "SUBSCRIPTION_EXISTS"
    assert razorpay.of("subscription", "create") == []


async def test_an_authorised_pending_checkout_is_not_replaced(client, session, shop, razorpay):
    sub_id = (await _start(client, shop, plan_key="growth", cycle="monthly")).json()["razorpay_subscription_id"]
    row = await _row(session, shop["club"].id)
    row.trial_ends_at = None  # authorised, waiting on the first charge
    await session.commit()
    await _webhook(session, "subscription.authenticated", sub_id)

    response = await _start(client, shop, plan_key="pro")
    assert response.status_code == 409 and response.json()["error_code"] == "SUBSCRIPTION_EXISTS"


async def test_replacing_an_unpaid_checkout_closes_it_at_razorpay_first(client, session, shop, razorpay):
    old = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]

    new = await _start(client, shop, plan_key="pro")

    assert new.status_code == 200, new.text
    assert razorpay.of("subscription", "cancel") == [(old, {"cancel_at_cycle_end": 0})]
    row = await _row(session, shop["club"].id)
    assert row.razorpay_subscription_id == new.json()["razorpay_subscription_id"] and row.plan.key == "pro"

    # Razorpay then reports the old one cancelled: the new checkout is untouched.
    await _webhook(session, "subscription.cancelled", old, club_id=shop["club"].id)
    row = await _row(session, shop["club"].id)
    assert row.status.value == "pending" and row.plan.key == "pro"


async def test_a_checkout_razorpay_will_not_close_blocks_a_new_one(client, session, shop, razorpay):
    old = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]

    def refuse(sub_id, data):
        raise RuntimeError("Razorpay unavailable")

    razorpay.handlers[("subscription", "cancel")] = refuse
    razorpay.handlers[("subscription", "fetch")] = lambda sub_id: {"id": sub_id, "status": "created"}
    blocked = await _start(client, shop, plan_key="pro")
    assert blocked.status_code == 502 and blocked.json()["error_code"] == "CHECKOUT_NOT_CLOSED"
    assert razorpay.created == 1
    assert (await _row(session, shop["club"].id)).razorpay_subscription_id == old

    # Already expired at Razorpay: nothing can charge, so go ahead.
    razorpay.handlers[("subscription", "fetch")] = lambda sub_id: {"id": sub_id, "status": "expired"}
    assert (await _start(client, shop, plan_key="pro")).status_code == 200


# ─── 5. Cancelling ───────────────────────────────────────────────────────


async def _active(session, shop, *, plan="growth", status="active", days_left=20):
    from app.api.subscriptions.models import ClubSubscriptions

    now = datetime.now(timezone.utc)
    session.add(ClubSubscriptions(
        club_id=shop["club"].id, plan_id=shop[plan].id, status=status, razorpay_subscription_id="sub_paid",
        authenticated_at=now - timedelta(days=40), activated_at=now - timedelta(days=40),
        trial_started_at=now - timedelta(days=40) if status == "trial" else None,
        current_period_end=now + timedelta(days=days_left), mrr_paise=69900,
    ))
    await session.commit()


async def test_cancelling_runs_to_the_end_of_the_paid_period(client, session, shop, razorpay):
    await _active(session, shop)

    cancelled = await client.post(f"{API}/clubs/subscription/cancel", headers=shop["h"])

    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert (body["status"], body["cancel_at_period_end"], body["entitled"]) == ("active", True, True)
    assert razorpay.of("subscription", "cancel") == [("sub_paid", {"cancel_at_cycle_end": 1})]
    mine = (await client.get(f"{API}/clubs/subscription", headers=shop["h"])).json()
    assert mine["cancel_at_period_end"] is True and mine["cancel_requested_at"] is not None

    # Asking again does not ask Razorpay again.
    assert (await client.post(f"{API}/clubs/subscription/cancel", headers=shop["h"])).status_code == 200
    assert len(razorpay.of("subscription", "cancel")) == 1

    # Razorpay's cancelled arriving early leaves the paid days; at the end, Free.
    await _webhook(session, "subscription.cancelled", "sub_paid")
    assert await _plan_key(session, shop["club"].id) == "growth"
    row = await _row(session, shop["club"].id)
    row.current_period_end = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    assert await _plan_key(session, shop["club"].id) == "free"


async def test_cancelling_a_trial_stops_the_charge_now_and_keeps_the_trial(client, session, shop, razorpay):
    await _active(session, shop, status="trial", days_left=5)

    await client.post(f"{API}/clubs/subscription/cancel", headers=shop["h"])

    assert razorpay.of("subscription", "cancel") == [("sub_paid", {"cancel_at_cycle_end": 0})]
    await _webhook(session, "subscription.cancelled", "sub_paid")
    assert await _plan_key(session, shop["club"].id) == "growth"


async def test_cancelling_a_pending_checkout_goes_back_to_what_was_there(client, session, shop, razorpay):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    await service.assign_plan(session, club_id=shop["club"].id, plan=shop["growth"], cycle=BillingCycle.monthly)
    await session.commit()
    sub_id = (await _start(client, shop, plan_key="pro")).json()["razorpay_subscription_id"]

    response = await client.post(f"{API}/clubs/subscription/cancel", headers=shop["h"])

    assert response.status_code == 200, response.text
    assert (response.json()["plan"]["key"], response.json()["status"]) == ("growth", "active")
    assert razorpay.of("subscription", "cancel") == [(sub_id, {"cancel_at_cycle_end": 0})]
    # Razorpay's cancelled for it then changes nothing.
    await _webhook(session, "subscription.cancelled", sub_id, club_id=shop["club"].id)
    assert await _plan_key(session, shop["club"].id) == "growth"


async def test_nothing_to_cancel(client, session, shop, razorpay):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    none_yet = await client.post(f"{API}/clubs/subscription/cancel", headers=shop["h"])
    assert none_yet.status_code == 409 and none_yet.json()["error_code"] == "NO_SUBSCRIPTION"
    await service.assign_plan(session, club_id=shop["club"].id, plan=shop["growth"], cycle=BillingCycle.monthly)
    await session.commit()
    granted = await client.post(f"{API}/clubs/subscription/cancel", headers=shop["h"])
    assert granted.status_code == 409
    assert razorpay.calls == []


@pytest.mark.parametrize("role", ["admin", "editor", "door"])
def test_only_the_owner_starts_or_cancels(role):
    from app.api.club_access import policy

    assert policy.allows(role, "POST", "/api/v1/clubs/subscription/cancel") is False
    assert policy.allows(role, "POST", "/api/v1/clubs/subscription/start") is False


# ─── 6. Webhooks ─────────────────────────────────────────────────────────


async def test_paused_and_resumed(session, shop):
    await _active(session, shop)

    await _webhook(session, "subscription.paused", "sub_paid")
    row = await _row(session, shop["club"].id)
    assert (row.status.value, row.mrr_paise) == ("paused", 0)
    assert await _plan_key(session, shop["club"].id) == "free"

    await _webhook(session, "subscription.resumed", "sub_paid")
    row = await _row(session, shop["club"].id)
    assert (row.status.value, row.mrr_paise) == ("active", 69900)


async def test_updated_moves_to_the_plan_razorpay_now_bills(session, shop):
    shop["pro"].razorpay_plan_id_yearly = "plan_pro_yearly"
    await session.commit()
    await _active(session, shop)

    await _webhook(session, "subscription.updated", "sub_paid", plan_id="plan_pro_yearly")
    row = await _row(session, shop["club"].id)
    assert (row.plan.key, row.cycle.value, row.mrr_paise) == ("pro", "yearly", 1499000 // 12)

    await _webhook(session, "subscription.updated", "sub_paid", plan_id="plan_nobody_knows")
    assert (await _row(session, shop["club"].id)).plan.key == "pro"


async def test_a_failed_charge_says_why(session, shop, razorpay):
    from app.api.payments.service import handle_razorpay_webhook

    await _active(session, shop)
    data = {"event": "payment.failed", "entity": "event", "payload": {"payment": {"entity": {
        "id": "pay_1", "subscription_id": "sub_paid", "error_description": "Card declined",
    }}}}
    await handle_razorpay_webhook(session, data, event_id="evt_pf_1", signature="sig")
    row = await _row(session, shop["club"].id)
    assert (row.status.value, row.last_failure_reason) == ("past_due", "Card declined")
    # Paid before, so it keeps the plan while Razorpay retries.
    assert await _plan_key(session, shop["club"].id) == "growth"

    # Found through the invoice when the payment does not name it.
    razorpay.handlers[("invoice", "fetch")] = lambda invoice_id: {"id": invoice_id, "subscription_id": "sub_paid"}
    data = {"event": "payment.failed", "entity": "event", "payload": {"payment": {"entity": {
        "id": "pay_2", "invoice_id": "inv_9", "error_reason": "payment_timed_out",
    }}}}
    await handle_razorpay_webhook(session, data, event_id="evt_pf_2", signature="sig")
    assert (await _row(session, shop["club"].id)).last_failure_reason == "payment_timed_out"


async def test_a_failed_first_charge_leaves_a_checkout_pending(client, session, shop, razorpay):
    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]
    await _webhook(session, "subscription.halted", sub_id, payment={"error_description": "Insufficient funds"})
    row = await _row(session, shop["club"].id)
    assert (row.status.value, row.last_failure_reason) == ("pending", "Insufficient funds")


async def test_a_stray_cancellation_never_ends_what_the_club_has(session, shop):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    await service.assign_plan(session, club_id=shop["club"].id, plan=shop["growth"], cycle=BillingCycle.monthly)
    await session.commit()

    await _webhook(session, "subscription.cancelled", "sub_somebody_elses", club_id=shop["club"].id)

    assert await _plan_key(session, shop["club"].id) == "growth"


async def test_a_late_payment_for_a_fallen_back_checkout_activates_what_was_paid_for(client, session, shop, razorpay):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    await service.assign_plan(session, club_id=shop["club"].id, plan=shop["growth"], cycle=BillingCycle.monthly)
    await session.commit()
    sub_id = (await _start(client, shop, plan_key="pro", cycle="yearly")).json()["razorpay_subscription_id"]
    row = await _row(session, shop["club"].id)
    row.pending_since = datetime.now(timezone.utc) - timedelta(hours=25)
    await session.commit()
    await client.get(f"{API}/clubs/subscription", headers=shop["h"])  # falls back to Growth

    await _webhook(session, "subscription.charged", sub_id, club_id=shop["club"].id,
                   notes={"plan_key": "pro", "cycle": "yearly"}, current_end=datetime.now(timezone.utc) + timedelta(days=365))

    row = await _row(session, shop["club"].id)
    assert (row.plan.key, row.cycle.value, row.status.value, row.razorpay_subscription_id) == ("pro", "yearly", "active", sub_id)


# ─── 7. Which plans can be bought ────────────────────────────────────────


@pytest.mark.parametrize("key,code,error", [
    ("bespoke", 404, "PLAN_NOT_AVAILABLE"),
    ("withdrawn", 404, "PLAN_NOT_AVAILABLE"),
    ("gone", 404, "PLAN_NOT_AVAILABLE"),
    ("nothing_by_this_name", 404, "PLAN_NOT_AVAILABLE"),
    ("enterprise", 422, "CONTACT_SALES"),
    ("free", 422, "FREE_PLAN"),
])
async def test_what_cannot_be_bought(client, session, shop, razorpay, key, code, error):
    await make_plan(session, key="bespoke", monthly=99900, visibility="hidden")
    await make_plan(session, key="withdrawn", monthly=99900, visibility="legacy")
    await make_plan(session, key="gone", monthly=99900, deleted=True)
    await make_plan(session, key="enterprise", monthly=999900, contact_sales=True)
    await session.commit()

    response = await _start(client, shop, plan_key=key)

    assert response.status_code == code, response.text
    assert response.json()["error_code"] == error
    assert razorpay.calls == []


async def test_a_hidden_plan_by_id_is_not_available_either(client, session, shop, razorpay):
    bespoke = await make_plan(session, key="bespoke", monthly=99900, visibility="hidden")
    await session.commit()
    response = await _start(client, shop, plan_id=bespoke.id)
    assert response.status_code == 404 and response.json()["error_code"] == "PLAN_NOT_AVAILABLE"


# ─── 8. Usage ────────────────────────────────────────────────────────────


async def test_entitlements_count_this_months_usage(client, session, shop, make_event, make_user, make_registration):
    from app.api.club_access.models import ClubMembers
    from app.api.notifications.models import Notifications
    from app.api.subscriptions.service import month_start

    start = month_start()
    owner, club = shop["owner"], shop["club"]
    this_month = await make_event(club=club, created_by=owner, name="Now")
    this_month.created_at = start + timedelta(hours=1)
    second = await make_event(club=club, created_by=owner, name="Also now")
    second.created_at = start + timedelta(hours=2)
    last_month = await make_event(club=club, created_by=owner, name="Before")
    last_month.created_at = start - timedelta(days=3)
    deleted = await make_event(club=club, created_by=owner, name="Deleted")
    deleted.created_at = start + timedelta(hours=3)
    deleted.soft_delete()

    people = [await make_user(email=f"usage{i}@example.com") for i in range(3)]
    for person in people:
        await make_registration(event=this_month, user=person)
    early = await make_registration(event=last_month, user=people[0])
    early.created_at = start - timedelta(days=2)

    for status in ("active", "invited", "removed"):
        session.add(ClubMembers(club_id=club.id, email=f"{status}-seat@example.com", role="editor", status=status))

    sent_at = start + timedelta(hours=5)
    for person in people:  # one announcement, to three phones
        session.add(Notifications(user_id=person.id, title="Doors open", description="Hall B", type="club_announcement",
                                  event_id=this_month.id, created_at=sent_at, updated_at=sent_at))
    session.add(Notifications(user_id=people[0].id, title="Starting", description="Now", type="club_announcement",
                              event_id=this_month.id, created_at=sent_at + timedelta(hours=1), updated_at=sent_at))
    session.add(Notifications(user_id=people[0].id, title="Live", description="Q1", type="event_live_update",
                              event_id=this_month.id, created_at=sent_at, updated_at=sent_at))
    await session.commit()

    usage = (await client.get(f"{API}/clubs/entitlements", headers=shop["h"])).json()["usage"]

    assert usage == {"events_per_month": 2, "registrations_per_month": 3, "committee_seats": 2, "announcements_per_month": 2}


# ─── 9. Applying, and being approved ─────────────────────────────────────


async def test_an_application_keeps_everything_it_was_given(client, session, fake_s3):
    from sqlalchemy import select

    from app.api.clubs.models import Clubs, ClubSocials

    response = await client.post(f"{API}/clubs/create", data={
        "email": "keeps@example.com", "password": "Password123!", "name": "Keeps All",
        "location_name": "Hall B", "location_link": "https://www.google.com/maps/@12.9716,77.5946,15z",
        "contact_phone": "+91 98765 43210", "contact_email": "committee@keeps.example.com",
        "instagram": "https://instagram.com/keeps", "linkedin": "", "youtube": "https://youtube.com/@keeps",
        "website": "https://keeps.example.com",
    })

    assert response.status_code == 200, response.text
    club = await session.get(Clubs, response.json()["id"])
    await session.refresh(club)
    assert (club.location_link, club.contact_phone, club.contact_email) == (
        "https://www.google.com/maps/@12.9716,77.5946,15z", "+91 98765 43210", "committee@keeps.example.com")
    assert (club.lat, club.lng, club.geo_source) == (12.9716, 77.5946, "link")
    socials = await session.scalar(select(ClubSocials).where(ClubSocials.club_id == club.id))
    assert (socials.instagram, socials.linkedin, socials.youtube, socials.website) == (
        "https://instagram.com/keeps", None, "https://youtube.com/@keeps", "https://keeps.example.com")


async def test_an_application_with_no_links_makes_no_socials_row(client, session):
    from sqlalchemy import func, select

    from app.api.clubs.models import ClubSocials

    response = await client.post(f"{API}/clubs/create", data={"email": "bare@example.com", "password": "Password123!", "name": "Bare Club"})
    assert response.status_code == 200, response.text
    assert await session.scalar(select(func.count(ClubSocials.id)).where(ClubSocials.club_id == response.json()["id"])) == 0


async def test_approval_emails_the_club_once(client, session, make_admin, mint_token, monkeypatch):
    from app.core.email import email as email_mod

    from app.core import after_commit

    sent = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    applied = await client.post(f"{API}/clubs/create", data={"email": "approve-me@example.com", "password": "Password123!", "name": "Approve Me"})
    club_id = applied.json()["id"]
    header = auth_header(await mint_token(await make_admin()))
    # The application itself alerts the admins (test_billing_alerts.py);
    # this test is about the club's own mail.
    await after_commit.drain()
    sent.clear()

    assert (await client.post(f"{API}/clubs/{club_id}/approve", headers=header)).status_code == 200
    assert [m["recipients"] for m in sent] == [["approve-me@example.com"]]
    assert "approved" in sent[0]["subject"] and "/login" in sent[0]["text"]

    assert (await client.post(f"{API}/clubs/{club_id}/approve", headers=header)).status_code == 200
    assert len(sent) == 1


async def test_an_approval_email_that_fails_does_not_fail_the_approval(client, make_admin, mint_token, monkeypatch):
    from app.core.email import email as email_mod

    def broken(**kw):
        raise RuntimeError("mail down")

    monkeypatch.setattr(email_mod, "send_email", broken)
    club_id = (await client.post(f"{API}/clubs/create", data={"email": "mail-down@example.com", "password": "Password123!", "name": "Mail Down"})).json()["id"]
    response = await client.post(f"{API}/clubs/{club_id}/approve", headers=auth_header(await mint_token(await make_admin())))
    assert response.status_code == 200 and response.json()["is_verified"] is True


# ─── 10. Interests, before signing in ────────────────────────────────────


async def test_the_interest_list_is_public(client, session):
    from app.api.interests.models import InterestCategory, Interests

    category = InterestCategory(name="Making")
    session.add(category)
    await session.flush()
    session.add(Interests(name="Robotics", category_id=category.id))
    await session.commit()

    response = await client.get(f"{API}/interests/list")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "public, max-age=300"
    [making] = [c for c in response.json() if c["name"] == "Making"]
    assert set(making) == {"id", "name", "icon", "icon_type", "interests"}
    assert [set(i) for i in making["interests"]] == [{"id", "name", "icon", "icon_type"}]


# ─── 11. Signing in to the clubs dashboard ───────────────────────────────


async def _twin_accounts(session, make_club, *, verified=True):
    from app.api.users.models import UserTypes, Users
    from app.core.auth.authentication import get_password_hash

    person = Users(full_name="Twin Person", username="twin_person", email="twin@example.com",
                   password=get_password_hash("App-pass-1"), user_type=UserTypes.app_user)
    session.add(person)
    await session.flush()
    owner, club = await make_club(name="Twin Club", email="twin@example.com", is_verified=verified)
    owner.password = get_password_hash("Club-pass-1")
    await session.commit()
    return person, owner


def _who(response) -> int:
    from app.core.auth.jwt import decode_jwt_token

    return decode_jwt_token(response.json()["access_token"])["user_id"]


async def _sign_in(client, password, scope=None):
    form = {"username": "twin@example.com", "password": password}
    if scope:
        form["scope"] = scope
    return await client.post(f"{API}/auth/token", data=form)


async def test_the_club_scope_signs_in_the_club_account(client, session, make_club):
    person, owner = await _twin_accounts(session, make_club)

    club = await _sign_in(client, "Club-pass-1", scope="club")
    assert club.status_code == 200, club.text
    assert _who(club) == owner.id

    # The person's own password on the dashboard is still the person.
    mine = await _sign_in(client, "App-pass-1", scope="club")
    assert mine.status_code == 200 and _who(mine) == person.id


async def test_without_the_scope_nothing_changes(client, session, make_club, monkeypatch):
    from app.core.auth import authentication

    person, owner = await _twin_accounts(session, make_club)

    async def never(*args, **kwargs):
        raise AssertionError("the app's sign-in must not look for a club account")

    monkeypatch.setattr(authentication, "authenticate_club_account", never)
    app = await _sign_in(client, "App-pass-1")
    assert app.status_code == 200 and _who(app) == person.id


async def test_a_club_waiting_for_approval_is_told_so(client, session, make_club):
    await _twin_accounts(session, make_club, verified=False)

    waiting = await _sign_in(client, "Club-pass-1", scope="club")

    assert waiting.status_code == 403, waiting.text
    assert waiting.json()["error_code"] == "CLUB_NOT_VERIFIED"
    # A wrong password learns nothing about the club.
    wrong = await _sign_in(client, "Not-it-1", scope="club")
    assert wrong.status_code == 401


async def test_an_admin_cannot_hand_out_razorpays_states(client, session, shop, make_admin, mint_token):
    header = auth_header(await mint_token(await make_admin()))
    for status in ("pending", "paused"):
        response = await client.put(
            f"{API}/admin/subscriptions/club/{shop['club'].id}/plan",
            json={"plan_id": shop["growth"].id, "cycle": "yearly", "status": status},
            headers=header,
        )
        assert response.status_code == 422, response.text
    granted = await client.put(
        f"{API}/admin/subscriptions/club/{shop['club'].id}/plan",
        json={"plan_id": shop["growth"].id, "cycle": "yearly", "status": "trial"},
        headers=header,
    )
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert (body["status"], body["cycle"], body["entitled"]) == ("trial", "yearly", True)
