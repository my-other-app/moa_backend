"""Razorpay disputes (app/api/admin_ops/disputes.py), from the platform
webhook to the admin's list, with alerts and notes."""
import time
import uuid

import pytest
from sqlalchemy import select

from app.api.admin_alerts.models import AdminNotifications
from app.api.admin_ops.models import PaymentDisputes
from app.api.payments.service import handle_razorpay_webhook
from tests.admin_ops_support import captured_payment, role_headers

API = "/api/v1"


def _event(name: str, *, dispute_id: str, payment_id: str, status: str, amount: int = 50000, respond_in: int = 5 * 86400) -> dict:
    return {
        "entity": "event",
        "event": name,
        "contains": ["payment", "dispute"],
        "payload": {
            "payment": {"entity": {"id": payment_id, "amount": amount}},
            "dispute": {"entity": {
                "id": dispute_id, "entity": "dispute", "payment_id": payment_id, "amount": amount, "currency": "INR",
                "amount_deducted": 0, "reason_code": "chargeback", "reason_description": "Cardholder says not received",
                "respond_by": int(time.time()) + respond_in, "status": status, "phase": "chargeback", "created_at": int(time.time()),
            }},
        },
    }


@pytest.fixture
async def ticket(session, make_user, make_club, make_event, make_registration):
    club_user, club = await make_club(name="Disputed Club", email=f"disputed-{uuid.uuid4().hex[:6]}@example.com")
    event, reg, order, log = await captured_payment(session, make_user=make_user, make_event=make_event,
                                                    make_registration=make_registration, club=club, club_user=club_user, amount=500)
    return {"club": club, "event": event, "log": log}


async def test_a_new_dispute_is_recorded_linked_and_alerted(client, session, make_admin, mint_token, ticket):
    pid = ticket["log"].razorpay_payment_id
    await handle_razorpay_webhook(session, _event("payment.dispute.created", dispute_id="disp_A", payment_id=pid, status="open"),
                                  event_id="evt_disp_1", signature="sig")
    row = await session.scalar(select(PaymentDisputes).where(PaymentDisputes.razorpay_dispute_id == "disp_A"))
    assert row.status == "open" and row.amount_paise == 50000 and row.club_id == ticket["club"].id
    assert row.payment_log_id == ticket["log"].id and row.event_id == ticket["event"].id and row.respond_by is not None
    alert = await session.scalar(select(AdminNotifications).where(AdminNotifications.kind == "payment_disputed"))
    assert alert is not None and alert.club_id == ticket["club"].id and alert.data["dispute_id"] == "disp_A"

    h = await role_headers(make_admin, mint_token)
    listed = await client.get(f"{API}/admin/disputes", headers=h["viewer"])
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["razorpay_dispute_id"] == "disp_A" and item["amount"] == 500.0 and item["kind"] == "ticket"
    assert item["club"]["name"] == "Disputed Club" and item["is_open"] is True and listed.json()["open"] == 1


async def test_the_same_delivery_twice_is_one_dispute_and_a_lost_one_alerts_again(client, session, ticket):
    pid = ticket["log"].razorpay_payment_id
    created = _event("payment.dispute.created", dispute_id="disp_B", payment_id=pid, status="open")
    await handle_razorpay_webhook(session, created, event_id="evt_b1", signature="sig")
    await handle_razorpay_webhook(session, created, event_id="evt_b1", signature="sig")
    await handle_razorpay_webhook(session, _event("payment.dispute.under_review", dispute_id="disp_B", payment_id=pid, status="under_review"),
                                  event_id="evt_b2", signature="sig")
    await handle_razorpay_webhook(session, _event("payment.dispute.lost", dispute_id="disp_B", payment_id=pid, status="lost"),
                                  event_id="evt_b3", signature="sig")
    rows = (await session.scalars(select(PaymentDisputes).where(PaymentDisputes.razorpay_dispute_id == "disp_B"))).all()
    assert len(rows) == 1 and rows[0].status == "lost" and rows[0].closed_at is not None
    assert rows[0].last_event == "payment.dispute.lost"
    alerts = (await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == "payment_disputed"))).all()
    assert sorted(a.data["status"] for a in alerts) == ["lost", "open"]


async def test_won_and_closed_leave_the_open_list(client, session, make_admin, mint_token, ticket):
    pid = ticket["log"].razorpay_payment_id
    await handle_razorpay_webhook(session, _event("payment.dispute.created", dispute_id="disp_C", payment_id=pid, status="open"),
                                  event_id="evt_c1", signature="sig")
    await handle_razorpay_webhook(session, _event("payment.dispute.won", dispute_id="disp_C", payment_id=pid, status="won"),
                                  event_id="evt_c2", signature="sig")
    h = await role_headers(make_admin, mint_token)
    assert (await client.get(f"{API}/admin/disputes", headers=h["viewer"])).json()["items"] == []
    closed = (await client.get(f"{API}/admin/disputes", params={"status": "closed"}, headers=h["viewer"])).json()
    assert closed["items"][0]["status"] == "won"
    found = (await client.get(f"{API}/admin/disputes", params={"status": "all", "q": pid[-6:]}, headers=h["viewer"])).json()
    assert found["total"] == 1


async def test_a_dispute_on_a_payment_we_do_not_know_is_kept_as_unknown(session):
    await handle_razorpay_webhook(session, _event("payment.dispute.created", dispute_id="disp_D", payment_id="pay_elsewhere", status="open"),
                                  event_id="evt_d1", signature="sig")
    row = await session.scalar(select(PaymentDisputes).where(PaymentDisputes.razorpay_dispute_id == "disp_D"))
    assert row is not None and row.payment_log_id is None and row.club_id is None


async def test_notes_are_for_moderators_and_audited(client, session, make_admin, mint_token, ticket):
    pid = ticket["log"].razorpay_payment_id
    await handle_razorpay_webhook(session, _event("payment.dispute.created", dispute_id="disp_E", payment_id=pid, status="open"),
                                  event_id="evt_e1", signature="sig")
    row = await session.scalar(select(PaymentDisputes).where(PaymentDisputes.razorpay_dispute_id == "disp_E"))
    h = await role_headers(make_admin, mint_token)
    assert (await client.post(f"{API}/admin/disputes/{row.id}/notes", json={"text": "hi"}, headers=h["viewer"])).status_code == 403
    r = await client.post(f"{API}/admin/disputes/{row.id}/notes", json={"text": "Sent the ticket scan as evidence"}, headers=h["support"])
    assert r.status_code == 201, r.text
    assert r.json()["notes"][0]["text"] == "Sent the ticket scan as evidence"
    one = (await client.get(f"{API}/admin/disputes/{row.id}", headers=h["viewer"])).json()
    assert len(one["notes"]) == 1


async def test_a_dispute_without_an_id_is_refused_for_good(session):
    from app.api.payments.models import RazorpayWebhookLogs

    await handle_razorpay_webhook(session, {"event": "payment.dispute.created", "payload": {"dispute": {"entity": {}}}},
                                  event_id="evt_bad", signature="sig")
    log = await session.scalar(select(RazorpayWebhookLogs).where(RazorpayWebhookLogs.event_id == "evt_bad"))
    assert log.processed_at is not None and log.last_error
