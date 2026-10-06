"""What the dashboard's headline figures count.

Every bug here renders as a plausible number rather than an error, which is
the worst kind: nobody checks a figure that looks reasonable. Two classes are
covered -- rows that should not be counted (soft-deleted clubs and events,
which every list endpoint filters), and revenue, which the endpoint returned
as a literal 0 for its entire life.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.conftest import auth_header


@pytest.fixture
async def payment_factory(session):
    """A payment log with the order it requires, since order_id is NOT NULL."""
    import itertools
    from app.api.payments.models import (
        OrderStatus,
        PaymentLogs,
        PaymentOrders,
        PaymentStatus,
    )

    counter = itertools.count(1)

    async def _make(amount, status=None, *, days_ago=0, deleted=False):
        n = next(counter)
        when = datetime.now(timezone.utc) - timedelta(days=days_ago)
        order = PaymentOrders(
            receipt=f"rcpt-{n}",
            razorpay_order_id=f"order_{n}",
            amount=Decimal(str(amount)),
            currency="INR",
            status=OrderStatus.created,
            source="test",
            payload={},
            created_at=when,
        )
        session.add(order)
        await session.flush()

        log = PaymentLogs(
            order_id=order.id,
            razorpay_payment_id=f"pay_{n}",
            amount=Decimal(str(amount)),
            status=status or PaymentStatus.captured,
            is_deleted=deleted,
            created_at=when,
        )
        session.add(log)
        await session.flush()
        return log

    return _make


async def analytics(client, token):
    response = await client.get("/api/v1/admin/analytics", headers=auth_header(token))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_a_deleted_club_is_not_counted(
    client, session, make_admin, make_club, mint_token
):
    """
    /clubs/admin/list filters is_deleted, so counting it here made the card
    and the table it links to disagree on the same screen.
    """
    admin = await make_admin()
    _, kept = await make_club(name="Kept", email="kept@example.com", is_verified=True)
    _, gone = await make_club(name="Gone", email="gone@example.com", is_verified=True)
    gone.is_deleted = True
    await session.flush()
    token = await mint_token(admin)

    assert (await analytics(client, token))["verified_clubs"] == 1


@pytest.mark.asyncio
async def test_a_deleted_event_is_not_counted(
    client, session, make_admin, make_club, make_event, mint_token
):
    admin = await make_admin()
    owner, club = await make_club(name="Host", email="host@example.com")
    await make_event(club=club, name="Kept", created_by=owner)
    gone = await make_event(club=club, name="Gone", created_by=owner)
    gone.is_deleted = True
    await session.flush()
    token = await mint_token(admin)

    assert (await analytics(client, token))["events_hosted"] == 1


@pytest.mark.asyncio
async def test_revenue_is_captured_less_refunded(
    client, make_admin, mint_token, payment_factory
):
    """It returned a hard-coded 0, so the card read no revenue regardless."""
    from app.api.payments.models import PaymentStatus

    admin = await make_admin()
    await payment_factory(1000)
    await payment_factory(500)
    await payment_factory(300, PaymentStatus.refunded)
    token = await mint_token(admin)

    assert (await analytics(client, token))["platform_revenue"] == pytest.approx(1200.0)


@pytest.mark.asyncio
async def test_failed_and_deleted_payments_are_not_revenue(
    client, make_admin, mint_token, payment_factory
):
    from app.api.payments.models import PaymentStatus

    admin = await make_admin()
    await payment_factory(1000)
    await payment_factory(9999, PaymentStatus.failed)
    await payment_factory(8888, deleted=True)
    token = await mint_token(admin)

    assert (await analytics(client, token))["platform_revenue"] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_revenue_delta_says_so_rather_than_inventing_a_percentage(
    client, make_admin, mint_token, payment_factory
):
    """
    Growth from no baseline is not "+100%". With nothing in the preceding
    window there is no percentage to report, and claiming one is worse than
    saying nothing.
    """
    admin = await make_admin()
    await payment_factory(1000, days_ago=1)
    token = await mint_token(admin)

    data = await analytics(client, token)
    assert data["platform_revenue_delta"] == "first revenue this month"


@pytest.mark.asyncio
async def test_revenue_delta_compares_against_the_preceding_month(
    client, make_admin, mint_token, payment_factory
):
    admin = await make_admin()
    await payment_factory(200, days_ago=45)   # previous window
    await payment_factory(300, days_ago=5)    # current window
    token = await mint_token(admin)

    data = await analytics(client, token)
    assert data["platform_revenue_delta"] == "+50% from last month"
    # Net is all-time, so it includes both.
    assert data["platform_revenue"] == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_no_revenue_at_all_says_so(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    data = await analytics(client, token)
    assert data["platform_revenue"] == 0
    assert data["platform_revenue_delta"] == "no revenue yet"


@pytest.mark.asyncio
async def test_trends_revenue_matches_the_payments_source(
    client, make_admin, mint_token, payment_factory
):
    """
    The chart summed what tickets were priced at; the payments page summed
    what cleared. Two numbers for "revenue" on one dashboard.
    """
    from app.api.payments.models import PaymentStatus

    admin = await make_admin()
    await payment_factory(400, days_ago=2)
    await payment_factory(600, days_ago=3)
    await payment_factory(5000, PaymentStatus.failed, days_ago=2)
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/admin/analytics/trends?days=30", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["totals"]["revenue"] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_trends_ignores_deleted_events(
    client, session, make_admin, make_club, make_event, mint_token
):
    admin = await make_admin()
    owner, club = await make_club(name="T", email="trendclub@example.com")
    await make_event(club=club, name="Kept", created_by=owner)
    gone = await make_event(club=club, name="Gone", created_by=owner)
    gone.is_deleted = True
    await session.flush()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/admin/analytics/trends?days=30", headers=auth_header(token)
    )

    assert response.json()["totals"]["events"] == 1
