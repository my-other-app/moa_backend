"""A club giving a guest's money back from the event's Payments: all or part
of a payment, through the Razorpay account that took it, with a reason; the
guest told once; the ledger right; only the people who handle money."""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.audit.models import AdminAuditLogs
from app.api.club_ops import refunds as refund_service
from app.api.club_ops.models import ClubRefunds
from app.api.ledger import book
from app.api.ledger.models import LedgerEntries
from app.api.payments import service as payments
from app.api.payments.models import PaymentStatus
from app.api.security import service as security
from app.api.users.models import AdminRoles
from app.core.email import email as email_mod
from app.core.rate_limit import limiter
from tests.club_ops_helpers import FakeRazorpay, acting_as, captured, member_header
from tests.conftest import auth_header


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)
    mails: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: mails.append(kw))
    rz = FakeRazorpay()
    monkeypatch.setattr(payments, "razorpay_client", rz)
    owner, club = await make_club(name=f"Money Club {uuid.uuid4().hex[:4]}", email=f"money{uuid.uuid4().hex[:6]}@example.com")
    rival_owner, rival = await make_club(name=f"Rival Club {uuid.uuid4().hex[:4]}", email=f"rival{uuid.uuid4().hex[:6]}@example.com")
    event = await make_event(club=club, created_by=owner, name="Paid Night", has_fee=True, reg_fee=100.0)
    pay = await captured(session, make_user, make_registration, event, amount=100.0)
    rz.holds(pay.log)
    return SimpleNamespace(
        owner=owner, club=club, event=event, pay=pay, rz=rz, mails=mails,
        h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)),
    )


def _url(w, path=""):
    return f"/api/v1/events/{w.event.id}/payments{path}"


async def _refund(client, w, h=None, **body):
    body.setdefault("reason", "Asked for it back")
    return await client.post(_url(w, f"/{w.pay.log.id}/refund"), json=body, headers=h or w.h)


async def _ledger_refunded(session, payment_id) -> int:
    return -int(await session.scalar(
        select(func.coalesce(func.sum(LedgerEntries.amount_paise), 0)).where(
            LedgerEntries.razorpay_payment_id == payment_id, LedgerEntries.account == book.GATEWAY, LedgerEntries.kind == book.REFUND,
        )
    ) or 0)


async def test_the_events_payments_are_listed_with_guest_amount_method_and_date(client, world):
    r = await client.get(_url(world), headers=world.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"] == {"payments": 1, "captured": 100.0, "refunded": 0.0}
    item = body["items"][0]
    assert item["id"] == str(world.pay.log.id) and item["guest_name"] == world.pay.reg.full_name
    assert item["guest_email"] == world.pay.reg.email and item["amount"] == 100.0 and item["refundable"] == 100.0
    assert item["method"] == "upi" and item["status"] == "captured" and item["gateway"] == "platform" and item["paid_at"]


async def test_a_full_refund_goes_through_razorpay_unpays_the_ticket_and_tells_the_guest_once(client, session, world):
    r = await _refund(client, world, reason="Can't make it")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["refund"]["status"] == "succeeded" and body["refund"]["amount"] == 100.0 and body["refund"]["source"] == "club"
    assert body["payment"]["status"] == "refunded" and body["payment"]["refundable"] == 0.0
    # Razorpay was asked for exactly the amount, on the platform's account.
    assert world.rz.refunds == [(world.pay.log.razorpay_payment_id, 10000)]
    await session.refresh(world.pay.log)
    await session.refresh(world.pay.reg)
    assert world.pay.log.status == PaymentStatus.refunded
    assert world.pay.reg.is_paid is False and float(world.pay.reg.paid_amount) == 0
    assert await _ledger_refunded(session, world.pay.log.razorpay_payment_id) == 10000
    told = [m for m in world.mails if world.pay.reg.email in m["recipients"]]
    assert len(told) == 1 and "₹100 refunded" in told[0]["subject"] and "Can't make it" in told[0]["text"]
    # Telling again does nothing: once.
    assert await refund_service.tell_guest(session, uuid.UUID(body["refund"]["id"])) is False
    assert len([m for m in world.mails if world.pay.reg.email in m["recipients"]]) == 1
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club.refund"))
    assert audit is not None and audit.details["reason"] == "Can't make it"


async def test_a_part_refund_keeps_the_ticket_and_the_rest_can_follow_without_counting_twice(client, session, world):
    first = await _refund(client, world, amount=40, reason="Came late")
    assert first.status_code == 200, first.text
    assert first.json()["payment"]["status"] == "partially_refunded"
    assert first.json()["payment"]["refunded"] == 40.0 and first.json()["payment"]["refundable"] == 60.0
    await session.refresh(world.pay.log)
    await session.refresh(world.pay.reg)
    assert world.pay.log.status == PaymentStatus.captured
    assert world.pay.reg.is_paid is True and float(world.pay.reg.paid_amount) == 60.0
    assert await _ledger_refunded(session, world.pay.log.razorpay_payment_id) == 4000

    too_much = await _refund(client, world, amount=61)
    assert too_much.status_code == 422 and too_much.json()["error_code"] == "AMOUNT_TOO_HIGH"

    rest = await _refund(client, world, reason="Event moved")
    assert rest.status_code == 200, rest.text
    assert rest.json()["refund"]["amount"] == 60.0 and rest.json()["payment"]["status"] == "refunded"
    assert world.rz.refunds == [(world.pay.log.razorpay_payment_id, 4000), (world.pay.log.razorpay_payment_id, 6000)]
    await session.refresh(world.pay.reg)
    assert world.pay.reg.is_paid is False
    # The ledger gave back 100 in all, not 160.
    assert await _ledger_refunded(session, world.pay.log.razorpay_payment_id) == 10000

    nothing = await _refund(client, world)
    assert nothing.status_code == 409 and nothing.json()["error_code"] == "NOT_REFUNDABLE"


async def test_the_same_dialog_twice_refunds_once(client, world):
    ref = str(uuid.uuid4())
    a = await _refund(client, world, amount=10, client_ref=ref)
    b = await _refund(client, world, amount=10, client_ref=ref)
    assert a.status_code == b.status_code == 200
    assert a.json()["refund"]["id"] == b.json()["refund"]["id"]
    assert len(world.rz.refunds) == 1


async def test_a_refund_razorpay_refuses_is_kept_as_failed_and_retried(client, session, world):
    world.rz.fail_with = RuntimeError("The amount must be at least INR 1.00")
    r = await _refund(client, world, reason="Duplicate booking")
    assert r.status_code == 200, r.text
    refund = r.json()["refund"]
    assert refund["status"] == "failed" and "Razorpay didn't take the refund" in refund["error"]
    assert r.json()["payment"]["refundable"] == 100.0  # nothing is held by a failed one
    await session.refresh(world.pay.reg)
    assert world.pay.reg.is_paid is True
    assert not [m for m in world.mails if world.pay.reg.email in m["recipients"]]

    world.rz.fail_with = None
    again = await client.post(f"/api/v1/events/{world.event.id}/refunds/{refund['id']}/retry", headers=world.h)
    assert again.status_code == 200, again.text
    assert again.json()["refund"]["status"] == "succeeded" and again.json()["refund"]["attempts"] == 2
    assert len(world.rz.refunds) == 1
    # Retrying one that went through does nothing more.
    done = await client.post(f"/api/v1/events/{world.event.id}/refunds/{refund['id']}/retry", headers=world.h)
    assert done.json()["refund"]["status"] == "succeeded" and len(world.rz.refunds) == 1


async def test_an_answer_lost_on_the_way_back_is_found_at_razorpay_not_refunded_twice(client, session, world):
    world.rz.lose_answer = True
    r = await _refund(client, world)
    assert r.status_code == 200, r.text
    assert r.json()["refund"]["status"] == "succeeded"
    assert len(world.rz.refunds) == 1
    await session.refresh(world.pay.log)
    assert world.pay.log.status == PaymentStatus.refunded


async def test_a_payment_on_the_clubs_own_razorpay_is_refunded_there(client, session, world, make_user, make_registration, monkeypatch):
    from app.api.payment_gateways import service as gateways
    from app.api.payment_gateways.models import ClubPaymentGateways

    gateway = ClubPaymentGateways(club_id=world.club.id, mode="keys", key_id="rzp_live_ClubOwn0001", key_secret_enc="x", status="active",
                                  webhook_token=uuid.uuid4().hex, webhook_secret_enc="x")
    session.add(gateway)
    await session.commit()
    own = FakeRazorpay()
    monkeypatch.setattr(gateways, "client_for", lambda g: own)
    pay = await captured(session, make_user, make_registration, world.event, amount=250.0, gateway_id=gateway.id)
    own.holds(pay.log)

    listed = (await client.get(_url(world), headers=world.h)).json()["items"]
    assert {i["gateway"] for i in listed} == {"platform", "club"}
    r = await client.post(_url(world, f"/{pay.log.id}/refund"), json={"reason": "Club's own account"}, headers=world.h)
    assert r.status_code == 200, r.text
    assert r.json()["refund"]["status"] == "succeeded"
    assert own.refunds == [(pay.log.razorpay_payment_id, 25000)] and world.rz.refunds == []


async def test_only_owners_and_admins_of_the_club_handle_refunds(client, session, world, make_admin):
    editor = await member_header(client, session, world.club, "editor")
    door = await member_header(client, session, world.club, "door")
    admin_member = await member_header(client, session, world.club, "admin")
    for h in (editor, door):
        assert (await client.get(_url(world), headers=h)).status_code == 403
        assert (await _refund(client, world, h=h)).status_code == 403
    assert (await client.get(_url(world), headers=world.rival_h)).status_code == 403
    assert (await _refund(client, world, h=world.rival_h)).status_code == 403
    assert world.rz.refunds == []

    r = await _refund(client, world, h=admin_member, amount=5, reason="By the club's admin")
    assert r.status_code == 200 and r.json()["refund"]["by"] == "Admin Member"
    row = await session.scalar(select(ClubRefunds).where(ClubRefunds.id == uuid.UUID(r.json()["refund"]["id"])))
    assert row.member_id is not None


async def test_myotherapp_admins_need_the_money_role_signed_in_or_acting_as_the_club(client, session, world, make_admin, mint_token):
    viewer = await make_admin(email=f"viewer{uuid.uuid4().hex[:6]}@example.com", role=AdminRoles.viewer)
    owner_admin = await make_admin(email=f"boss{uuid.uuid4().hex[:6]}@example.com")
    await session.commit()
    refused = await _refund(client, world, h=auth_header(await mint_token(viewer)))
    assert refused.status_code == 403 and refused.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"

    as_club = await acting_as(session, world.owner, owner_admin)
    r = await _refund(client, world, h=as_club, amount=7, reason="Support refund")
    assert r.status_code == 200, r.text
    assert r.json()["refund"]["by"] == "MyOtherApp support"
    row = await session.scalar(select(ClubRefunds).where(ClubRefunds.id == uuid.UUID(r.json()["refund"]["id"])))
    assert row.impersonated_by == owner_admin.id
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club.refund"))
    assert audit.details["impersonated_by"] == owner_admin.id


async def test_a_payment_that_did_not_go_through_cannot_be_refunded(client, session, world):
    world.pay.log.status = PaymentStatus.failed
    await session.commit()
    r = await _refund(client, world)
    assert r.status_code == 409 and r.json()["error_code"] == "NOT_REFUNDABLE"
    other = await client.post(_url(world, f"/{uuid.uuid4()}/refund"), json={"reason": "nope"}, headers=world.h)
    assert other.status_code == 404
