"""Plans, entitlements and the Razorpay subscription mirror.

The entitlement resolver is the keystone: every Pro-gated feature in the
Clubs app asks it whether a club may do something. The cases that matter are
the ones where getting it wrong is silent -- a club losing a feature it pays
for, or an unpaid club keeping one.
"""

from datetime import datetime, timezone

import pytest

from tests.conftest import auth_header


@pytest.fixture
def plan_payload():
    def _build(**overrides):
        payload = {
            "key": "growth",
            "name": "Growth",
            "tagline": "For clubs running several events a term",
            "price_monthly_paise": 149900,
            "price_yearly_paise": 1499000,
            "trial_days": 14,
            "visibility": "public",
            "sort_order": 1,
            "limits": {"events_per_month": 10, "committee_seats": 5},
            "features": {"tickets": True, "certificates": True, "merch": True},
        }
        payload.update(overrides)
        return payload

    return _build


async def make_plan(session, *, key="free", name="Free", monthly=0, yearly=0, **limits_features):
    """A plan straight into the database, for resolver tests."""
    from app.api.subscriptions.models import (
        PlanFeatures,
        PlanLimits,
        PlanVisibility,
        Plans,
    )

    plan = Plans(
        key=key,
        name=name,
        price_monthly_paise=monthly,
        price_yearly_paise=yearly,
        visibility=PlanVisibility.public,
    )
    session.add(plan)
    await session.flush()
    session.add(
        PlanLimits(
            plan_id=plan.id,
            **{k: v for k, v in limits_features.items() if not isinstance(v, bool)},
        )
    )
    session.add(
        PlanFeatures(
            plan_id=plan.id,
            **{k: v for k, v in limits_features.items() if isinstance(v, bool)},
        )
    )
    await session.flush()
    return plan


# ---------------------------------------------------------------------------
# Entitlements
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_club_with_no_subscription_gets_the_free_plan(
    session, make_club
):
    from app.api.subscriptions import service

    _, club = await make_club(name="Unsubscribed", email="unsub@example.com")
    await make_plan(session, key="free", name="Free", events_per_month=2, tickets=False)

    resolved = await service.resolve_entitlements(session, club.id)

    assert resolved.plan.key == "free"
    assert resolved.limit("events_per_month") == 2
    assert resolved.allows("tickets") is False
    assert resolved.configured is True


@pytest.mark.asyncio
async def test_with_no_free_plan_everything_is_permitted(session, make_club):
    """
    Introducing this domain must not lock existing clubs out.

    Before plans existed every club could do everything. If the platform has
    no free plan to fall back on, the resolver keeps that status quo and flags
    itself unconfigured -- a product that silently stops working is worse than
    one that is not yet gated.
    """
    from app.api.subscriptions import service

    _, club = await make_club(name="No Plans", email="noplans@example.com")

    resolved = await service.resolve_entitlements(session, club.id)

    assert resolved.configured is False
    assert resolved.allows("tickets") is True
    assert resolved.limit("events_per_month") is None


@pytest.mark.asyncio
async def test_a_subscribed_club_gets_its_own_plan(session, make_club):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    _, club = await make_club(name="Paying", email="paying@example.com")
    await make_plan(session, key="free", name="Free", events_per_month=2, tickets=False)
    growth = await make_plan(
        session, key="growth", name="Growth", monthly=149900, events_per_month=20, tickets=True
    )
    await service.assign_plan(
        session, club_id=club.id, plan=growth, cycle=BillingCycle.monthly
    )

    resolved = await service.resolve_entitlements(session, club.id)

    assert resolved.plan.key == "growth"
    assert resolved.limit("events_per_month") == 20
    assert resolved.allows("tickets") is True


@pytest.mark.asyncio
async def test_a_past_due_club_keeps_its_plan(session, make_club):
    """
    Razorpay retries a failed charge for days.

    Cutting a club off at the first failure would take a paying customer's
    event offline over an expired card, which is a worse outcome than
    carrying them while the retry runs.
    """
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle, SubscriptionStatus

    _, club = await make_club(name="Past Due", email="pastdue@example.com")
    await make_plan(session, key="free", name="Free", tickets=False)
    growth = await make_plan(session, key="growth", name="Growth", monthly=149900, tickets=True)
    await service.assign_plan(
        session,
        club_id=club.id,
        plan=growth,
        cycle=BillingCycle.monthly,
        status=SubscriptionStatus.past_due,
    )

    resolved = await service.resolve_entitlements(session, club.id)

    assert resolved.allows("tickets") is True


@pytest.mark.asyncio
async def test_a_cancelled_club_drops_to_free(session, make_club):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle, SubscriptionStatus

    _, club = await make_club(name="Cancelled", email="cancelled@example.com")
    await make_plan(session, key="free", name="Free", tickets=False)
    growth = await make_plan(session, key="growth", name="Growth", monthly=149900, tickets=True)
    await service.assign_plan(
        session,
        club_id=club.id,
        plan=growth,
        cycle=BillingCycle.monthly,
        status=SubscriptionStatus.cancelled,
    )

    resolved = await service.resolve_entitlements(session, club.id)

    assert resolved.plan.key == "free"
    assert resolved.allows("tickets") is False


@pytest.mark.asyncio
async def test_zero_and_unlimited_are_different(session, make_club):
    """
    NULL is unlimited; 0 is none allowed.

    A default of 0 on the limit columns would silently forbid everything on a
    plan that meant to allow it, and `within` has to tell the two apart.
    """
    from app.api.subscriptions import service

    _, club = await make_club(name="Limits", email="limits@example.com")
    await make_plan(session, key="free", name="Free", events_per_month=0)

    resolved = await service.resolve_entitlements(session, club.id)

    assert resolved.limit("events_per_month") == 0
    assert resolved.within("events_per_month", 0) is False
    # An unset limit is unlimited, not zero.
    assert resolved.limit("announcements_per_month") is None
    assert resolved.within("announcements_per_month", 10_000) is True


# ---------------------------------------------------------------------------
# MRR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_yearly_subscription_contributes_a_twelfth_to_mrr(session, make_club):
    """
    Summing yearly prices into MRR overstates it twelvefold.

    This is the most common way the number on the dashboard is wrong, and it
    is wrong in the flattering direction, so nobody questions it.
    """
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    _, club = await make_club(name="Yearly", email="yearly@example.com")
    plan = await make_plan(
        session, key="growth", name="Growth", monthly=149900, yearly=1499000
    )

    subscription = await service.assign_plan(
        session, club_id=club.id, plan=plan, cycle=BillingCycle.yearly
    )

    assert subscription.mrr_paise == 1499000 // 12
    assert subscription.mrr_paise < 149900


@pytest.mark.asyncio
async def test_totals_report_mrr_and_arr(session, make_club):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    plan = await make_plan(session, key="growth", name="Growth", monthly=100000)
    for i in range(3):
        _, club = await make_club(name=f"Club {i}", email=f"mrr{i}@example.com")
        await service.assign_plan(
            session, club_id=club.id, plan=plan, cycle=BillingCycle.monthly
        )

    totals = await service.mrr_totals(session)

    assert totals["mrr_paise"] == 300000
    assert totals["arr_paise"] == 300000 * 12
    assert totals["active_subscriptions"] == 3


# ---------------------------------------------------------------------------
# Plan catalogue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_read_a_plan(client, make_admin, mint_token, plan_payload):
    admin = await make_admin()
    token = await mint_token(admin)

    created = await client.post(
        "/api/v1/admin/plans", json=plan_payload(), headers=auth_header(token)
    )

    assert created.status_code == 200, created.text
    body = created.json()
    assert body["key"] == "growth"
    assert body["limits"]["events_per_month"] == 10
    # An unset limit comes back as null, meaning unlimited.
    assert body["limits"]["announcements_per_month"] is None
    assert "storage_gb" not in body["limits"]
    assert body["features"]["tickets"] is True
    assert body["features"]["sso"] is False
    # 34 flags in four groups, so the editor does not hardcode its own copy.
    assert sum(len(v) for v in body["feature_groups"].values()) == 34
    assert "whatsapp_own" in body["feature_groups"]["club"] and body["features"]["whatsapp_own"] is False


@pytest.mark.asyncio
async def test_a_yearly_price_above_twelve_months_is_rejected(
    client, make_admin, mint_token, plan_payload
):
    """Always a slipped decimal, and it would be charged before anyone looked."""
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.post(
        "/api/v1/admin/plans",
        json=plan_payload(price_monthly_paise=100000, price_yearly_paise=1300000),
        headers=auth_header(token),
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_a_duplicate_key_is_refused(client, make_admin, mint_token, plan_payload):
    admin = await make_admin()
    token = await mint_token(admin)
    await client.post("/api/v1/admin/plans", json=plan_payload(), headers=auth_header(token))

    again = await client.post(
        "/api/v1/admin/plans", json=plan_payload(), headers=auth_header(token)
    )

    assert again.status_code == 409, again.text


@pytest.mark.asyncio
async def test_creating_a_plan_needs_manage_money(
    client, make_admin, mint_token, plan_payload
):
    support = await make_admin(email="support@example.com", role="support")
    token = await mint_token(support)

    response = await client.post(
        "/api/v1/admin/plans", json=plan_payload(), headers=auth_header(token)
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_a_plan_with_subscribers_archives_rather_than_deletes(
    client, session, make_admin, make_club, mint_token
):
    """
    Deleting it would orphan its subscribers' entitlements and drop them to
    the free plan without anyone deciding that.
    """
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle, PlanVisibility, Plans

    admin = await make_admin()
    plan = await make_plan(session, key="growth", name="Growth", monthly=100000)
    _, club = await make_club(name="Subscriber", email="sub@example.com")
    await service.assign_plan(
        session, club_id=club.id, plan=plan, cycle=BillingCycle.monthly
    )
    token = await mint_token(admin)

    response = await client.post(
        f"/api/v1/admin/plans/{plan.id}/archive", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "archived"
    await session.refresh(plan)
    assert plan.visibility == PlanVisibility.legacy
    assert plan.is_deleted is False


@pytest.mark.asyncio
async def test_deleting_a_subscribed_plan_is_refused_not_quietly_archived(
    client, session, make_admin, make_club, mint_token
):
    """
    DELETE promises removal. Quietly archiving instead is how an admin ends up
    believing a plan is gone while clubs are still billed for it.
    """
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle

    admin = await make_admin()
    plan = await make_plan(session, key="growth", name="Growth", monthly=100000)
    _, club = await make_club(name="Subscriber", email="sub2@example.com")
    await service.assign_plan(
        session, club_id=club.id, plan=plan, cycle=BillingCycle.monthly
    )
    token = await mint_token(admin)

    response = await client.delete(
        f"/api/v1/admin/plans/{plan.id}", headers=auth_header(token)
    )

    assert response.status_code == 409, response.text
    assert "archive" in response.json()["message"].lower()


@pytest.mark.asyncio
async def test_an_unsubscribed_plan_deletes(client, session, make_admin, mint_token):
    admin = await make_admin()
    plan = await make_plan(session, key="unused", name="Unused", monthly=100000)
    token = await mint_token(admin)

    response = await client.post(
        f"/api/v1/admin/plans/{plan.id}/archive", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "deleted"


@pytest.mark.asyncio
async def test_a_duplicate_is_hidden_and_shares_no_razorpay_id(
    client, session, make_admin, mint_token
):
    """
    A copy that went straight onto the pricing page would be sold before it
    was finished -- and a shared Razorpay id would bill its subscribers
    against the other plan.
    """
    from app.api.subscriptions.models import Plans

    admin = await make_admin()
    plan = await make_plan(session, key="growth", name="Growth", monthly=100000, tickets=True)
    plan.razorpay_plan_id_monthly = "plan_originalXYZ"
    await session.flush()
    token = await mint_token(admin)

    response = await client.post(
        f"/api/v1/admin/plans/{plan.id}/duplicate", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["visibility"] == "hidden"
    assert body["key"] != "growth"
    assert body["features"]["tickets"] is True
    copy = await session.get(Plans, body["id"])
    assert copy.razorpay_plan_id_monthly is None


@pytest.mark.asyncio
async def test_a_paid_plan_is_not_billable_until_synced(
    client, make_admin, mint_token, plan_payload
):
    """The admin screen branches on this rather than letting someone publish a
    plan and discover at checkout that it cannot be charged."""
    admin = await make_admin()
    token = await mint_token(admin)

    created = await client.post(
        "/api/v1/admin/plans", json=plan_payload(), headers=auth_header(token)
    )

    assert created.json()["billable"] is False


@pytest.mark.asyncio
async def test_a_free_plan_is_always_billable(
    client, make_admin, mint_token, plan_payload
):
    admin = await make_admin()
    token = await mint_token(admin)

    created = await client.post(
        "/api/v1/admin/plans",
        json=plan_payload(key="free", name="Free", price_monthly_paise=0, price_yearly_paise=0),
        headers=auth_header(token),
    )

    assert created.json()["billable"] is True


@pytest.mark.asyncio
async def test_changing_the_price_clears_the_razorpay_ids(
    client, session, make_admin, mint_token, plan_payload
):
    """
    Razorpay plans are immutable, so the old id no longer describes this
    price. Clearing it makes the next subscribe create a fresh Razorpay plan
    while everyone already subscribed keeps the price they agreed to.
    """
    from app.api.subscriptions.models import Plans

    admin = await make_admin()
    plan = await make_plan(session, key="growth", name="Growth", monthly=100000)
    plan.razorpay_plan_id_monthly = "plan_oldprice"
    await session.flush()
    token = await mint_token(admin)

    response = await client.put(
        f"/api/v1/admin/plans/{plan.id}",
        json=plan_payload(key="growth", price_monthly_paise=200000, price_yearly_paise=0),
        headers=auth_header(token),
    )

    assert response.status_code == 200, response.text
    await session.refresh(plan)
    assert plan.razorpay_plan_id_monthly is None


# ---------------------------------------------------------------------------
# The Razorpay mirror
# ---------------------------------------------------------------------------


def webhook_payload(event: str, *, razorpay_id="sub_test123", club_id=None, current_end=None, error=None):
    entity = {"id": razorpay_id, "notes": {}}
    if club_id is not None:
        entity["notes"]["club_id"] = str(club_id)
    if current_end is not None:
        entity["current_end"] = current_end
    payload = {"subscription": {"entity": entity}}
    if error:
        payload["payment"] = {"entity": {"error_description": error}}
    return {"event": event, "entity": "event", "payload": payload}


@pytest.mark.asyncio
async def test_a_charge_marks_the_subscription_active(session, make_club):
    from app.api.subscriptions import billing, service
    from app.api.subscriptions.models import BillingCycle, SubscriptionStatus

    _, club = await make_club(name="Charged", email="charged@example.com")
    plan = await make_plan(session, key="growth", name="Growth", monthly=100000)
    subscription = await service.assign_plan(
        session,
        club_id=club.id,
        plan=plan,
        cycle=BillingCycle.monthly,
        status=SubscriptionStatus.past_due,
    )
    subscription.last_failure_reason = "card declined"

    period_end = int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp())
    changed = billing.apply_event(
        subscription,
        "subscription.charged",
        webhook_payload("subscription.charged", current_end=period_end),
    )

    assert changed is True
    assert subscription.status == SubscriptionStatus.active
    assert subscription.last_failure_reason is None
    assert subscription.current_period_end.year == 2026


@pytest.mark.asyncio
async def test_a_halt_records_why_it_failed(session, make_club):
    from app.api.subscriptions import billing, service
    from app.api.subscriptions.models import BillingCycle, SubscriptionStatus

    _, club = await make_club(name="Halted", email="halted@example.com")
    plan = await make_plan(session, key="growth", name="Growth", monthly=100000)
    subscription = await service.assign_plan(
        session, club_id=club.id, plan=plan, cycle=BillingCycle.monthly
    )

    billing.apply_event(
        subscription,
        "subscription.halted",
        webhook_payload("subscription.halted", error="Card expired"),
    )

    assert subscription.status == SubscriptionStatus.past_due
    assert subscription.last_failure_reason == "Card expired"


@pytest.mark.asyncio
async def test_a_cancellation_zeroes_mrr(session, make_club):
    """A cancelled subscription contributing to MRR is how the number drifts
    upward and never comes back down."""
    from app.api.subscriptions import billing, service
    from app.api.subscriptions.models import BillingCycle, SubscriptionStatus

    _, club = await make_club(name="Gone", email="gone@example.com")
    plan = await make_plan(session, key="growth", name="Growth", monthly=100000)
    subscription = await service.assign_plan(
        session, club_id=club.id, plan=plan, cycle=BillingCycle.monthly
    )
    assert subscription.mrr_paise == 100000

    billing.apply_event(
        subscription, "subscription.cancelled", webhook_payload("subscription.cancelled")
    )

    assert subscription.status == SubscriptionStatus.cancelled
    assert subscription.mrr_paise == 0


@pytest.mark.asyncio
async def test_an_unknown_event_changes_nothing(session, make_club):
    """Razorpay adds events. Reacting to one we do not understand is how a
    subscription ends up in a state nobody intended."""
    from app.api.subscriptions import billing, service
    from app.api.subscriptions.models import BillingCycle, SubscriptionStatus

    _, club = await make_club(name="Unknown", email="unknown@example.com")
    plan = await make_plan(session, key="growth", name="Growth", monthly=100000)
    subscription = await service.assign_plan(
        session, club_id=club.id, plan=plan, cycle=BillingCycle.monthly
    )

    changed = billing.apply_event(
        subscription,
        "subscription.some_future_event",
        webhook_payload("subscription.some_future_event"),
    )

    assert changed is False
    assert subscription.status == SubscriptionStatus.active


@pytest.mark.asyncio
async def test_the_club_id_note_survives_a_non_numeric_value(session):
    """Razorpay notes are free-form strings; a bad one must not 500 the
    webhook, because a 500 makes Razorpay retry it forever."""
    from app.api.subscriptions import billing

    payload = {"payload": {"subscription": {"entity": {"notes": {"club_id": "oops"}}}}}
    assert billing.club_id_from_payload(payload) is None
