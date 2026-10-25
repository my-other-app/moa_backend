"""Refunds beyond a whole ticket payment (app/api/admin_ops/refunds.py): part
of a ticket, one store order, the rest, and a plan subscription's charge.
Razorpay is a fake throughout; the ledger must agree with what was given back."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.api.admin_ops.models import PaymentRefunds
from app.api.audit.models import AdminAuditLogs
from app.api.ledger import book
from app.api.ledger.models import LedgerEntries as E
from app.api.payments import service as payments_service
from app.api.payments.models import PaymentLogs, PaymentStatus
from app.api.store.models import EventStoreItems, EventStoreOrderLines, EventStoreOrders
from tests.admin_ops_support import FakeRefunds, captured_payment, role_headers

API = "/api/v1"


@pytest.fixture
async def paid(session, make_user, make_club, make_event, make_registration):
    """A ₹700 payment: a ₹500 ticket and a ₹200 store order (two T-shirts)."""
    club_user, club = await make_club(name="Refund Club", email=f"refunds-{uuid.uuid4().hex[:6]}@example.com")
    event, reg, order, log = await captured_payment(
        session, make_user=make_user, make_event=make_event, make_registration=make_registration, club=club, club_user=club_user,
        amount=700, ticket=500,
    )
    item = EventStoreItems(club_id=club.id, event_id=event.id, name="T-shirt", price=Decimal("100"), stock=10, sold_count=2)
    session.add(item)
    await session.flush()
    store_order = EventStoreOrders(event_id=event.id, registration_id=reg.id, user_id=reg.user_id, status="paid",
                                   total=Decimal("200"), paid_at=datetime.now(timezone.utc))
    session.add(store_order)
    await session.flush()
    session.add(EventStoreOrderLines(order_id=store_order.id, item_id=item.id, item_name="T-shirt", qty=2,
                                     unit_price=Decimal("100"), line_total=Decimal("200")))
    await session.commit()
    return {"club": club, "event": event, "reg": reg, "order": order, "log": log, "item": item, "store_order": store_order}


async def _gateway_paise(session, payment_id: str) -> int:
    return int(await session.scalar(
        select(func.coalesce(func.sum(E.amount_paise), 0)).where(E.razorpay_payment_id == payment_id, E.account == book.GATEWAY)
    ))


async def _unbalanced(session) -> list:
    return (await session.execute(select(E.txn_key).group_by(E.txn_key).having(func.sum(E.amount_paise) != 0))).all()


async def test_part_of_a_ticket_goes_back_and_the_ticket_stands(client, session, make_admin, mint_token, paid, monkeypatch):
    h = await role_headers(make_admin, mint_token)
    fake = FakeRefunds()
    monkeypatch.setattr(payments_service.razorpay_client.payment, "refund", fake)
    log, reg = paid["log"], paid["reg"]

    detail = (await client.get(f"{API}/admin/refunds/payments/{log.id}", headers=h["viewer"])).json()
    assert detail["payment"]["refundable"] == 700.0 and detail["ticket"]["paid_amount"] == 500.0
    assert detail["store_orders"][0]["refundable"] is True

    body = {"kind": "ticket", "amount": "150", "reason": "Started an hour late"}
    for role in ("viewer", "support"):
        assert (await client.post(f"{API}/admin/refunds/payments/{log.id}", json=body, headers=h[role])).status_code == 403
    r = await client.post(f"{API}/admin/refunds/payments/{log.id}", json=body, headers=h["owner"])
    assert r.status_code == 200, r.text
    assert fake.calls == [(log.razorpay_payment_id, {"amount": 15000, "notes": {"reason": "Started an hour late", "by": "admin", "kind": "ticket"}})]
    assert r.json()["payment"]["refunded"] == 150.0 and r.json()["payment"]["refundable"] == 550.0
    assert r.json()["payment"]["status"] == "captured"

    await session.refresh(reg)
    assert reg.is_paid is True and reg.paid_amount == Decimal("350") and reg.actual_amount == Decimal("350")
    assert await _gateway_paise(session, log.razorpay_payment_id) == 70000 - 15000
    assert await _unbalanced(session) == []
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "payment.refund_part"))
    assert audit is not None and audit.details["kind"] == "ticket"


async def test_one_store_order_goes_back_and_its_pieces_return(client, session, make_admin, mint_token, paid, monkeypatch):
    h = await role_headers(make_admin, mint_token)
    monkeypatch.setattr(payments_service.razorpay_client.payment, "refund", FakeRefunds())
    log = paid["log"]
    r = await client.post(f"{API}/admin/refunds/payments/{log.id}",
                          json={"kind": "store_order", "store_order_id": str(paid["store_order"].id), "reason": "Out of sizes"},
                          headers=h["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["payment"]["refunded"] == 200.0
    await session.refresh(paid["store_order"])
    await session.refresh(paid["item"])
    await session.refresh(paid["reg"])
    assert paid["store_order"].status == "refunded" and paid["item"].sold_count == 0
    assert paid["reg"].is_paid is True and paid["reg"].paid_amount == Decimal("500")
    assert await _gateway_paise(session, log.razorpay_payment_id) == 50000
    assert await _unbalanced(session) == []
    again = await client.post(f"{API}/admin/refunds/payments/{log.id}",
                              json={"kind": "store_order", "store_order_id": str(paid["store_order"].id), "reason": "twice"},
                              headers=h["owner"])
    assert again.status_code == 409


async def test_the_rest_after_a_part_ends_the_ticket_and_the_ledger_balances(client, session, make_admin, mint_token, paid, monkeypatch):
    h = await role_headers(make_admin, mint_token)
    fake = FakeRefunds()
    monkeypatch.setattr(payments_service.razorpay_client.payment, "refund", fake)
    log, reg = paid["log"], paid["reg"]
    await client.post(f"{API}/admin/refunds/payments/{log.id}", json={"kind": "ticket", "amount": "100", "reason": "partial first"}, headers=h["owner"])
    r = await client.post(f"{API}/admin/refunds/payments/{log.id}", json={"kind": "rest", "reason": "cancelled"}, headers=h["owner"])
    assert r.status_code == 200, r.text
    assert fake.calls[-1][1]["amount"] == 60000
    assert r.json()["payment"]["status"] == "refunded" and r.json()["payment"]["refundable"] == 0
    await session.refresh(reg)
    assert reg.is_paid is False and reg.paid_amount == 0
    assert await _gateway_paise(session, log.razorpay_payment_id) == 0
    assert await _unbalanced(session) == []
    kinds = sorted((await session.scalars(select(PaymentRefunds.kind).where(PaymentRefunds.payment_log_id == log.id))).all())
    assert kinds == ["rest", "ticket"]


async def test_the_existing_full_refund_asks_razorpay_only_for_what_is_left(client, session, make_admin, mint_token, paid, monkeypatch):
    h = await role_headers(make_admin, mint_token)
    fake = FakeRefunds()
    monkeypatch.setattr(payments_service.razorpay_client.payment, "refund", fake)
    log = paid["log"]
    await client.post(f"{API}/admin/refunds/payments/{log.id}", json={"kind": "ticket", "amount": "50", "reason": "goodwill"}, headers=h["owner"])
    r = await client.post(f"{API}/payments/logs/{log.id}/refund", headers=h["owner"])
    assert r.status_code == 200, r.text
    assert fake.calls[-1] == (log.razorpay_payment_id, {"amount": 65000})
    assert await _gateway_paise(session, log.razorpay_payment_id) == 0
    assert await _unbalanced(session) == []


async def test_a_ticket_part_must_be_less_than_the_ticket_and_razorpay_refusing_changes_nothing(client, session, make_admin, mint_token, paid, monkeypatch):
    h = await role_headers(make_admin, mint_token)
    log = paid["log"]
    whole = await client.post(f"{API}/admin/refunds/payments/{log.id}", json={"kind": "ticket", "amount": "500", "reason": "whole"}, headers=h["owner"])
    assert whole.status_code == 422 and whole.json()["error_code"] == "AMOUNT_TOO_LARGE"
    monkeypatch.setattr(payments_service.razorpay_client.payment, "refund", FakeRefunds(fail=RuntimeError("BAD_REQUEST_ERROR")))
    refused = await client.post(f"{API}/admin/refunds/payments/{log.id}", json={"kind": "ticket", "amount": "10", "reason": "try"}, headers=h["owner"])
    assert refused.status_code == 502 and refused.json()["error_code"] == "REFUND_FAILED"
    assert await session.scalar(select(func.count()).select_from(PaymentRefunds)) == 0
    await session.refresh(paid["reg"])
    assert paid["reg"].paid_amount == Decimal("500")


async def test_a_refunded_payment_is_not_refunded_again(client, session, make_admin, mint_token, paid):
    h = await role_headers(make_admin, mint_token)
    log = await session.get(PaymentLogs, paid["log"].id)
    log.status = PaymentStatus.refunded
    await session.commit()
    r = await client.post(f"{API}/admin/refunds/payments/{log.id}", json={"kind": "rest", "reason": "again"}, headers=h["owner"])
    assert r.status_code == 409 and r.json()["error_code"] == "NOT_REFUNDABLE"


# ─── A plan subscription's charge ───────────────────────────────────────


@pytest.fixture
async def charge(session, make_club):
    from app.api.subscriptions.models import SubscriptionCharges

    _, club = await make_club(name="Subscriber", email=f"sub-{uuid.uuid4().hex[:6]}@example.com")
    row = SubscriptionCharges(club_id=club.id, amount_paise=99900, razorpay_payment_id=f"pay_sub{uuid.uuid4().hex[:10]}",
                              status="captured", refunded_paise=0, charged_at=datetime.now(timezone.utc))
    session.add(row)
    await session.commit()
    return row


async def test_a_subscription_charge_is_refunded_in_part_then_the_rest(client, session, make_admin, mint_token, charge, monkeypatch):
    from app.api.subscriptions import billing

    h = await role_headers(make_admin, mint_token)
    fake = FakeRefunds()
    monkeypatch.setattr(billing.razorpay_client.payment, "refund", fake)
    assert (await client.post(f"{API}/admin/refunds/subscription-charges/{charge.id}", json={"reason": "nope"}, headers=h["support"])).status_code == 403
    part = await client.post(f"{API}/admin/refunds/subscription-charges/{charge.id}", json={"amount": "199", "reason": "pro-rata"}, headers=h["owner"])
    assert part.status_code == 200, part.text
    assert part.json()["refunded_paise"] == 19900 and part.json()["status"] == "partially_refunded"
    rest = await client.post(f"{API}/admin/refunds/subscription-charges/{charge.id}", json={"reason": "cancelled"}, headers=h["owner"])
    assert rest.status_code == 200 and rest.json()["refunded_paise"] == 99900 and rest.json()["status"] == "refunded"
    assert [c[1]["amount"] for c in fake.calls] == [19900, 80000]
    assert await _gateway_paise(session, charge.razorpay_payment_id) == 0
    assert await _unbalanced(session) == []
    done = await client.post(f"{API}/admin/refunds/subscription-charges/{charge.id}", json={"reason": "third"}, headers=h["owner"])
    assert done.status_code == 409
    audit = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "subscription.refund"))).all()
    assert len(audit) == 2


async def test_the_webhook_after_an_admin_refund_changes_nothing(session, charge, monkeypatch):
    """refund.processed for the same payment, with Razorpay's running total, is the same figure."""
    from app.api.subscriptions import ledger as subscription_ledger

    charge.refunded_paise = 19900
    charge.status = "partially_refunded"
    await session.commit()
    data = {"payload": {"refund": {"entity": {"id": "rfnd_x", "payment_id": charge.razorpay_payment_id, "amount": 19900}},
                        "payment": {"entity": {"id": charge.razorpay_payment_id, "amount_refunded": 19900}}}}
    assert await subscription_ledger.apply_refund(session, data) is True
    await session.refresh(charge)
    assert charge.refunded_paise == 19900
