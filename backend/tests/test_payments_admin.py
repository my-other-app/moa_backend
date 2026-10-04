"""The payments admin surface.

`PaymentLogs` has held every transaction since the module was written, and
there was no endpoint to list them. The only payment route an admin had was
the refund, which takes a payment id they had no way to look up -- so a
platform that takes money could not show a transaction, investigate a failed
one, or find the payment behind "I paid and got nothing".
"""

import uuid

import pytest

from tests.conftest import auth_header


@pytest.fixture
async def make_payment(session):
    from app.api.payments.models import (
        OrderStatus,
        PaymentLogs,
        PaymentOrders,
        PaymentStatus,
    )

    async def _factory(*, user=None, amount=250, status=PaymentStatus.captured):
        suffix = uuid.uuid4().hex[:10]
        order = PaymentOrders(
            receipt=f"rcpt_{suffix}",
            razorpay_order_id=f"order_{suffix}",
            amount=amount,
            currency="INR",
            status=OrderStatus.paid,
            user_id=user.id if user else None,
            source="test",
            payload={},
        )
        session.add(order)
        await session.flush()

        log = PaymentLogs(
            order_id=order.id,
            razorpay_payment_id=f"pay_{suffix}",
            status=status,
            amount=amount,
            payment_method="upi",
        )
        session.add(log)
        await session.flush()
        return order, log

    return _factory


@pytest.mark.asyncio
async def test_payments_can_be_listed(client, make_admin, make_user, make_payment, mint_token):
    admin = await make_admin()
    payer = await make_user(email="payer@example.com")
    await make_payment(user=payer, amount=500)
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/payments/admin/list", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["amount"] == 500
    assert row["user"]["email"] == "payer@example.com"
    assert row["receipt"]


@pytest.mark.asyncio
async def test_a_captured_payment_is_marked_refundable(
    client, make_admin, make_payment, mint_token
):
    """The service refuses anything else, so the UI should not offer it."""
    from app.api.payments.models import PaymentStatus

    admin = await make_admin()
    await make_payment(status=PaymentStatus.captured)
    await make_payment(status=PaymentStatus.failed)
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/payments/admin/list", headers=auth_header(token)
    )

    flags = {r["status"]: r["is_refundable"] for r in response.json()["items"]}
    assert flags["captured"] is True
    assert flags["failed"] is False


@pytest.mark.asyncio
async def test_payments_filter_by_status(
    client, make_admin, make_payment, mint_token
):
    from app.api.payments.models import PaymentStatus

    admin = await make_admin()
    await make_payment(status=PaymentStatus.captured)
    await make_payment(status=PaymentStatus.failed)
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/payments/admin/list?status=failed", headers=auth_header(token)
    )

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_payments_can_be_searched_by_payment_id(
    client, make_admin, make_payment, mint_token
):
    """Finding one transaction is the whole reason support opens this page."""
    admin = await make_admin()
    _, target = await make_payment()
    await make_payment()
    token = await mint_token(admin)

    response = await client.get(
        f"/api/v1/payments/admin/list?search={target.razorpay_payment_id}",
        headers=auth_header(token),
    )

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["razorpay_payment_id"] == target.razorpay_payment_id


@pytest.mark.asyncio
async def test_payments_can_be_scoped_to_one_user(
    client, make_admin, make_user, make_payment, mint_token
):
    admin = await make_admin()
    mine = await make_user(email="mine@example.com")
    theirs = await make_user(email="theirs@example.com")
    await make_payment(user=mine)
    await make_payment(user=theirs)
    token = await mint_token(admin)

    response = await client.get(
        f"/api/v1/payments/admin/list?user_id={mine.id}", headers=auth_header(token)
    )

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["user"]["email"] == "mine@example.com"


@pytest.mark.asyncio
async def test_totals_report_net_of_refunds(
    client, make_admin, make_payment, mint_token
):
    """Gross alone overstates what the platform kept."""
    from app.api.payments.models import PaymentStatus

    admin = await make_admin()
    await make_payment(amount=1000, status=PaymentStatus.captured)
    await make_payment(amount=300, status=PaymentStatus.refunded)
    await make_payment(amount=50, status=PaymentStatus.failed)
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/payments/admin/totals", headers=auth_header(token)
    )

    body = response.json()
    assert body["captured_total"] == 1000
    assert body["refunded_total"] == 300
    assert body["net_total"] == 700
    assert body["failed_count"] == 1


@pytest.mark.asyncio
async def test_the_payments_list_is_admin_only(client, make_user, mint_token):
    nosy = await make_user(email="nosypay@example.com")
    token = await mint_token(nosy)

    for path in ("/api/v1/payments/admin/list", "/api/v1/payments/admin/totals"):
        response = await client.get(path, headers=auth_header(token))
        assert response.status_code in (401, 403), f"{path} -> {response.status_code}"
