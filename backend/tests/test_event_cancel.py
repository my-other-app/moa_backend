"""Cancelling an event people registered for: a reason, a confirm step with
the counts, then once -- the status, no new sign-ups, every guest told (the
event's Messages, the app, WhatsApp for those who asked), every online
payment refunded in full through the account that took it, failures listed
and retried."""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.audit.models import AdminAuditLogs
from app.api.closeout.models import EventStatus
from app.api.club_ops import cancel as cancel_service
from app.api.club_ops.models import ClubRefunds, EventCancellations, GuestNotices
from app.api.emails.models import MessageDeliveries, Messages
from app.api.payments import service as payments
from app.api.payments.models import PaymentStatus
from app.api.security import service as security
from app.core import whatsapp
from app.core.email import email as email_mod
from app.core.rate_limit import limiter
from tests.club_ops_helpers import FakeRazorpay, acting_as, captured, member_header
from tests.conftest import auth_header

REASON = "The hall flooded overnight"


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
    owner, club = await make_club(name=f"Cancel Club {uuid.uuid4().hex[:4]}", email=f"cancel{uuid.uuid4().hex[:6]}@example.com")
    from datetime import datetime, timedelta, timezone

    event = await make_event(club=club, created_by=owner, name="Spring Fest", has_fee=True, reg_fee=100.0,
                             event_datetime=datetime.now(timezone.utc) + timedelta(days=5))
    paid = [await captured(session, make_user, make_registration, event, amount=a) for a in (100.0, 150.0)]
    for p in paid:
        rz.holds(p.log)
    free_guest = await make_user(email=f"free{uuid.uuid4().hex[:6]}@example.com")
    unpaid = await make_registration(event=event, user=free_guest, whatsapp_opt_in=True, phone="9876543210")
    door_guest = await make_user(email=f"door{uuid.uuid4().hex[:6]}@example.com")
    cash = await make_registration(event=event, user=door_guest, is_paid=True, paid_amount=100.0, actual_amount=100.0)
    await session.commit()
    return SimpleNamespace(owner=owner, club=club, event=event, paid=paid, unpaid=unpaid, cash=cash, rz=rz, mails=mails,
                           h=auth_header(await mint_token(owner)))


def _url(w, path=""):
    return f"/api/v1/events/{w.event.id}/cancel{path}"


async def _cancel(client, w, h=None, **body):
    body.setdefault("reason", REASON)
    return await client.post(_url(w), json=body, headers=h or w.h)


async def test_the_confirm_step_shows_who_is_told_and_what_goes_back(client, world):
    r = await client.get(_url(world), headers=world.h)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["cancelled"] is False and s["can_cancel"] is True and s["status"] == "published"
    assert s["registrations"] == 4
    assert s["paid_online"] == 2 and s["refund_total"] == 250.0
    assert s["paid_at_door"] == 1 and s["paid_at_door_total"] == 100.0
    assert s["refunds"]["total"] == 0 and s["failures"] == []


async def test_cancelling_tells_everyone_refunds_every_online_payment_and_is_audited(client, session, world, monkeypatch):
    sent: list[tuple] = []

    async def fake_send(number, template, document=None, meter=None, sender=None):
        sent.append((number, template.name, meter))
        return {"MessageId": "wamid.1", "Via": "platform"}

    monkeypatch.setattr(whatsapp, "configured", lambda: True)
    monkeypatch.setattr(whatsapp, "send", fake_send)

    r = await _cancel(client, world, expected_registrations=4, expected_refund_total=250)
    assert r.status_code == 200, r.text
    answered = r.json()
    # The answer comes before the refunds run: they are asked for, one job each.
    assert answered["cancelled"] is True and answered["status"] == "cancelled" and answered["reason"] == REASON
    assert answered["refunds"]["total"] == 2 and answered["refund_total"] == 250.0
    s = (await client.get(_url(world), headers=world.h)).json()
    assert s["refunds"] == {"total": 2, "succeeded": 2, "pending": 0, "failed": 0, "refunded_amount": 250.0}

    await session.refresh(world.event)
    assert world.event.lifecycle is EventStatus.cancelled and world.event.status_clock is False
    # Each payment, in full, on the account that took it.
    assert sorted(world.rz.refunds) == sorted([(p.log.razorpay_payment_id, int(p.log.amount * 100)) for p in world.paid])
    for p in world.paid:
        await session.refresh(p.log)
        await session.refresh(p.reg)
        assert p.log.status == PaymentStatus.refunded and p.reg.is_paid is False

    # The email went through the event's Messages, to every guest.
    c = await session.scalar(select(EventCancellations).where(EventCancellations.event_id == world.event.id))
    msg = await session.get(Messages, c.message_id)
    assert msg is not None and msg.subject == "Cancelled: Spring Fest" and REASON in msg.body
    assert await session.scalar(select(func.count()).select_from(MessageDeliveries).where(MessageDeliveries.message_id == msg.id)) == 4
    # The app push was claimed once; WhatsApp went to the guest who asked for it, metered.
    assert c.push_at is not None
    assert [(n, name) for n, name, _ in sent] == [("9876543210", "event_update")]
    assert sent[0][2]["kind"] == "update" and sent[0][2]["club_id"] == world.club.id
    assert s["notice"]["whatsapp_sent"] == 1 and s["notice"]["email_message_id"] == str(msg.id)
    # Each paid guest also hears their refund went through.
    for p in world.paid:
        assert any(p.reg.email in m["recipients"] and "refunded" in m["subject"] for m in world.mails)
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "event.cancel"))
    assert audit is not None and audit.details["reason"] == REASON and audit.details["refunds"] == 2


async def test_cancelling_twice_does_everything_once(client, session, world):
    first = await _cancel(client, world)
    assert first.status_code == 200
    refunds_before = list(world.rz.refunds)
    again = await _cancel(client, world, reason="Another reason entirely")
    assert again.status_code == 200 and again.json()["reason"] == REASON
    # The follow-up run again (a retried job) sends nothing new.
    await cancel_service.follow_up(session, world.event.id)
    assert world.rz.refunds == refunds_before
    assert await session.scalar(select(func.count()).select_from(Messages).where(Messages.event_id == world.event.id)) == 1
    assert await session.scalar(select(func.count()).select_from(ClubRefunds).where(ClubRefunds.event_id == world.event.id)) == 2
    assert await session.scalar(select(func.count()).select_from(EventCancellations).where(EventCancellations.event_id == world.event.id)) == 1


async def test_a_cancelled_event_takes_no_one_new_and_says_so_publicly(client, session, world, make_user, mint_token):
    assert (await _cancel(client, world)).status_code == 200
    guest = await make_user(email=f"late{uuid.uuid4().hex[:6]}@example.com")
    gh = auth_header(await mint_token(guest))
    r = await client.post(f"/api/v1/events/registration/{world.event.id}/register",
                          json={"full_name": "Late Comer", "email": guest.email, "phone": "9123456780"}, headers=gh)
    assert r.status_code == 400, r.text
    assert r.json()["error_code"] == "REGISTRATIONS_CLOSED" and r.json()["errors"]["closed_reason"] == "cancelled"

    pay = await client.post("/api/v1/payments/orders", params={"checkout": 2}, headers=auth_header(await mint_token(world.paid[0].payer)),
                            json={"source": "event_registration", "payload": {"event_id": world.event.id, "event_registration_id": str(world.paid[0].reg.id)}})
    assert pay.status_code == 409 and pay.json()["error_code"] == "EVENT_CANCELLED"

    info = (await client.get(f"/api/v1/events/info/{world.event.id}")).json()
    assert info["status"] == "cancelled" and info["cancellation"]["reason"] == REASON
    page = await client.get(f"/api/v1/discover/events/{world.event.slug}")
    assert page.status_code == 200, page.text
    assert page.json()["event"]["status"] == "cancelled" and page.json()["event"]["registration_open"] is False
    assert page.json()["event"]["cancellation"]["reason"] == REASON
    listed = (await client.get("/api/v1/discover/events", params={"q": "Spring Fest"})).json()
    assert world.event.id not in [e["id"] for e in listed.get("items", listed.get("events", []))]


async def test_a_cancelled_event_stays_cancelled_and_locked(client, session, world):
    assert (await _cancel(client, world)).status_code == 200
    back = await client.patch(f"/api/v1/events/{world.event.id}/closeout/settings", json={"status": "published"}, headers=world.h)
    assert back.status_code == 409 and back.json()["error_code"] == "EVENT_CANCELLED"
    settings = (await client.get(f"/api/v1/events/{world.event.id}/closeout/settings", headers=world.h)).json()
    assert settings["status"] == "cancelled" and "cancelled" in [s["key"] for s in settings["statuses"]]
    # Deleting is still refused while it has registrations.
    gone = await client.delete(f"/api/v1/events/delete/{world.event.id}", headers=world.h)
    assert gone.status_code >= 400


async def test_numbers_that_changed_since_the_confirm_step_stop_the_cancel(client, session, world):
    r = await _cancel(client, world, expected_registrations=3, expected_refund_total=250)
    assert r.status_code == 409 and r.json()["error_code"] == "CANCEL_PREVIEW_STALE"
    assert r.json()["errors"]["registrations"] == 4
    await session.refresh(world.event)
    assert world.event.lifecycle is EventStatus.published and world.rz.refunds == []


async def test_a_refund_razorpay_refuses_is_listed_and_retried(client, session, world):
    world.rz.fail_with = RuntimeError("Razorpay is having a moment")
    r = await _cancel(client, world)
    assert r.status_code == 200
    s = (await client.get(_url(world), headers=world.h)).json()
    assert s["refunds"]["failed"] == 2 and len(s["failures"]) == 2
    # Each payment counted once, failed or not.
    assert s["paid_online"] == 2 and s["refund_total"] == 250.0
    assert {f["gateway"] for f in s["failures"]} == {"platform"} and s["failures"][0]["error"]
    world.rz.fail_with = None
    again = await client.post(_url(world, "/retry"), headers=world.h)
    assert again.status_code == 200, again.text
    assert again.json()["refunds"]["succeeded"] == 2 and again.json()["failures"] == []
    assert len(world.rz.refunds) == 2


async def test_a_part_refunded_payment_only_gives_back_what_is_left(client, session, world):
    part = await client.post(f"/api/v1/events/{world.event.id}/payments/{world.paid[0].log.id}/refund",
                             json={"amount": 30, "reason": "Discount"}, headers=world.h)
    assert part.status_code == 200
    preview = (await client.get(_url(world), headers=world.h)).json()
    assert preview["refund_total"] == 220.0
    r = await _cancel(client, world, expected_refund_total=220)
    assert r.status_code == 200, r.text
    assert sorted(p for _, p in world.rz.refunds) == [3000, 7000, 15000]


async def test_only_owners_and_admins_cancel_and_not_a_finished_event(client, session, world, make_admin):
    editor = await member_header(client, session, world.club, "editor")
    door = await member_header(client, session, world.club, "door")
    for h in (editor, door):
        assert (await client.get(_url(world), headers=h)).status_code == 403
        assert (await _cancel(client, world, h=h)).status_code == 403
    me = (await client.get("/api/v1/clubs/access/me", headers=editor)).json()
    assert me["can"]["cancel_events"] is False and me["can"]["contacts"] is True
    owner_admin = await make_admin(email=f"boss{uuid.uuid4().hex[:6]}@example.com")
    await session.commit()
    as_club = await acting_as(session, world.owner, owner_admin)
    r = await _cancel(client, world, h=as_club)
    assert r.status_code == 200 and r.json()["cancelled_by"] == "MyOtherApp support"
    c = await session.scalar(select(EventCancellations).where(EventCancellations.event_id == world.event.id))
    assert c.impersonated_by == owner_admin.id


async def test_a_finished_event_cannot_be_cancelled(client, session, world):
    world.event.lifecycle = EventStatus.closed
    await session.commit()
    r = await _cancel(client, world)
    assert r.status_code == 409 and r.json()["error_code"] == "CANNOT_CANCEL"
    s = (await client.get(_url(world), headers=world.h)).json()
    assert s["can_cancel"] is False and s["blocked_reason"]


async def test_whatsapp_goes_once_per_guest_even_when_the_follow_up_runs_again(client, session, world, monkeypatch):
    sent = []

    async def fake_send(number, template, document=None, meter=None, sender=None):
        sent.append(number)
        return {"MessageId": f"wamid.{len(sent)}", "Via": "platform"}

    monkeypatch.setattr(whatsapp, "configured", lambda: True)
    monkeypatch.setattr(whatsapp, "send", fake_send)
    assert (await _cancel(client, world)).status_code == 200
    await cancel_service.follow_up(session, world.event.id)
    assert sent == ["9876543210"]
    rows = (await session.scalars(select(GuestNotices).where(GuestNotices.event_id == world.event.id))).all()
    assert [(r.channel, r.status) for r in rows] == [("whatsapp", "sent")]
