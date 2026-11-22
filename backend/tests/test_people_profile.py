"""The guest profile: GET /events/{event_id}/people/{ref}
(feature-register/guest-profile/CONTRACT.md §1).

One person on one event, everything about them, and a time-ordered log read
from the tables that already hold each fact and from the audit log.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event as sa_event

from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
START = NOW + timedelta(days=3)
FORM = [
    {"key": "college", "label": "College", "field_type": "text", "required": True},
    {"key": "shirt", "label": "Shirt size", "field_type": "text", "required": True},
]


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="People Club", email="people-club@example.com")
    event = await make_event(club=club, created_by=owner, name="People Night", event_datetime=START, duration=4.0, additional_details=[dict(q) for q in FORM])
    user = await make_user(email="ananya.iyer@gmail.com")
    reg = await make_registration(
        event=event, user=user, full_name="Ananya Iyer", email="ananya.iyer@gmail.com", phone="9876544321",
        additional_details={"college": "CET"}, created_at=ago(days=2), admission_no="A1",
    )
    other_owner, other_club = await make_club(name="Other Club", email="people-other@example.com")
    await session.commit()
    return dict(
        owner=owner, club=club, event=event, user=user, reg=reg, h=auth_header(await mint_token(owner)),
        other_h=auth_header(await mint_token(other_owner)), other_club=other_club, other_owner=other_owner,
    )


def _url(world, ref=None, event_id=None):
    return f"{API}/events/{event_id or world['event'].id}/people/{ref or world['reg'].id}"


async def _get(client, world, ref=None, headers=None, **kw):
    return await client.get(_url(world, ref, **kw), headers=headers or world["h"])


def kinds(body) -> list[str]:
    return [r["kind"] for r in body["timeline"]]


def row(body, kind):
    found = [r for r in body["timeline"] if r["kind"] == kind]
    assert found, f"no {kind} in {kinds(body)}"
    return found[0]


async def _member_header(session, world, role: str) -> dict:
    from app.api.auth.schemas import AuthTokenData
    from app.api.club_access.models import ClubMembers
    from app.core.auth.jwt import create_access_token

    member = ClubMembers(club_id=world["club"].id, email=f"people-{role}@example.com", full_name=role.title(), role=role, status="active")
    session.add(member)
    await session.commit()
    await session.refresh(world["owner"])
    token = create_access_token(data=AuthTokenData(user_id=world["owner"].id, token_type="access_token", token_version=world["owner"].token_version, member_id=member.id).model_dump())
    return auth_header(token)


# ─── The shape ────────────────────────────────────────────────────────────


async def test_a_registration_comes_back_in_the_contracts_shape(client, world):
    r = await _get(client, world)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "kind", "registration_id", "request_id", "person", "status", "answers", "ticket", "payment", "check_in", "prints", "merch",
        "deliveries", "timeline", "timeline_truncated", "actions",
    }
    assert body["kind"] == "registration" and body["registration_id"] == str(world["reg"].id) and body["request_id"] is None
    assert set(body["person"]) == {"full_name", "email", "phone", "institution", "role", "photo_url", "account"}
    assert body["person"]["full_name"] == "Ananya Iyer" and body["person"]["email"] == "ananya.iyer@gmail.com"
    assert body["person"]["phone"] == "9876544321" and body["person"]["role"] == "attendee"
    assert body["person"]["account"] == {"kind": "app", "user_id": world["user"].id}
    assert body["status"] == {
        "stage": "confirmed", "checked_in": False, "card_printed": False, "certificate": "none", "paid": None, "ticket_revoked": False,
        "email_bounced": False,
    }
    assert body["answers"] == [
        {"key": "college", "label": "College", "value": "CET", "missing": False},
        {"key": "shirt", "label": "Shirt size", "value": None, "missing": True},
    ]
    assert set(body["ticket"]) == {"code", "type_name", "admission_no", "pdf_url", "issued_at", "revoked_at"}
    assert body["ticket"]["code"] == world["reg"].ticket_id and body["ticket"]["admission_no"] == "A1"
    assert body["ticket"]["pdf_url"] == f"/api/v1/events/tickets/{world['reg'].ticket_id}/pdf"
    assert body["payment"] is None and body["check_in"] is None and body["prints"] == [] and body["merch"] == []
    assert body["timeline_truncated"] is False
    assert set(body["actions"]) == {
        "can_print", "can_send_to_station", "printers_online", "can_resend_ticket", "can_check_in", "can_undo_check_in", "can_edit",
        "can_confirm", "can_decline", "can_ask_missing", "can_remove",
    }
    assert [r["kind"] for r in body["timeline"]][:2] == ["ticket_issued", "registered"]
    for line in body["timeline"]:
        assert set(line) == {"at", "kind", "title", "detail", "by", "tone"} and line["tone"] in ("default", "success", "warning", "danger", "info")


async def test_the_timeline_is_newest_first(client, session, world):
    from app.api.merch.models import MerchCollections, MerchItems

    item = MerchItems(event_id=world["event"].id, name="Tee", stock=5)
    session.add(item)
    await session.flush()
    session.add(MerchCollections(registration_id=world["reg"].id, item_id=item.id, collected_at=ago(hours=1)))
    await session.commit()
    body = (await _get(client, world)).json()
    times = [r["at"] for r in body["timeline"]]
    assert times == sorted(times, reverse=True) and body["timeline"][0]["kind"] == "merch_collected"


# ─── The ref, both ways ───────────────────────────────────────────────────


async def test_the_ref_is_a_registration_id_a_ticket_code_or_a_request_id(client, session, world):
    from app.api.approvals.models import RegistrationRequests

    req = RegistrationRequests(
        event_id=world["event"].id, full_name="Dev Menon", email="dev.menon@gmail.com", token=uuid.uuid4().hex,
        answers={"college": "NIT"}, submitted_at=ago(hours=5),
    )
    session.add(req)
    await session.commit()
    by_id = (await _get(client, world)).json()
    by_code = (await _get(client, world, ref=world["reg"].ticket_id)).json()
    assert by_code["registration_id"] == by_id["registration_id"]
    asked = (await _get(client, world, ref=req.id)).json()
    assert asked["kind"] == "request" and asked["registration_id"] is None and asked["request_id"] == str(req.id)
    assert asked["person"]["full_name"] == "Dev Menon" and asked["status"]["stage"] == "under_review"
    assert asked["ticket"] is None and asked["check_in"] is None and asked["prints"] == []
    assert asked["answers"][0] == {"key": "college", "label": "College", "value": "NIT", "missing": False}
    assert [r["kind"] for r in asked["timeline"]] == ["request_submitted"]
    assert asked["timeline"][0]["by"] == "the guest"


async def test_an_unknown_ref_or_another_events_person_is_404(client, session, world, make_event):
    assert (await _get(client, world, ref=uuid.uuid4())).status_code == 404
    assert (await _get(client, world, ref="not-a-ref")).status_code == 404
    elsewhere = await make_event(club=world["club"], created_by=world["owner"], name="Elsewhere", event_datetime=START)
    await session.commit()
    # The registration of one event, asked for under another.
    assert (await _get(client, world, event_id=elsewhere.id)).status_code == 404


# ─── Request to registration: one history ──────────────────────────────────


async def test_a_request_that_became_a_registration_shows_one_continuous_history(client, session, world, make_event):
    from app.api.approvals.models import RegistrationRequests
    from app.api.emails.models import ReviewResendRecipients, ReviewResends

    reg = world["reg"]
    declined = RegistrationRequests(
        event_id=world["event"].id, user_id=reg.user_id, full_name="Ananya Iyer", email=reg.email, token=uuid.uuid4().hex, status="declined",
        submitted_at=ago(days=6), decided_at=ago(days=5, hours=20), decided_by_name="Priya", decline_note="Full for now",
    )
    approved = RegistrationRequests(
        event_id=world["event"].id, user_id=reg.user_id, full_name="Ananya Iyer", email=reg.email, token=uuid.uuid4().hex, status="approved",
        submitted_at=ago(days=3), decided_at=ago(days=2, hours=1), decided_by_name="Rahul", registration_id=reg.id, answers={"college": "CET"},
    )
    session.add_all([declined, approved])
    await session.flush()
    resend = ReviewResends(event_id=world["event"].id, stage="REQUEST_APPROVED", client_ref=uuid.uuid4(), subject="You are in", design={}, created_by_name="Rahul", created_at=ago(days=2))
    session.add(resend)
    await session.flush()
    session.add(ReviewResendRecipients(resend_id=resend.id, request_id=approved.id, status="sent", sent_at=ago(days=2)))
    await session.commit()

    via_reg = (await _get(client, world)).json()
    via_req = (await _get(client, world, ref=approved.id)).json()
    older = (await _get(client, world, ref=declined.id)).json()
    # Whichever way in, the same person, the same log.
    for body in (via_reg, via_req, older):
        assert body["kind"] == "registration" and body["registration_id"] == str(reg.id)
        assert kinds(body).count("request_submitted") == 2
        assert {"request_declined", "request_confirmed", "registered", "review_email"} <= set(kinds(body))
    assert via_reg["request_id"] == via_req["request_id"] == str(approved.id) and older["request_id"] == str(declined.id)
    assert via_reg["timeline"] == via_req["timeline"] == older["timeline"]
    assert row(via_reg, "request_declined")["detail"] == "Full for now" and row(via_reg, "request_declined")["by"] == "Priya"
    assert row(via_reg, "request_confirmed")["by"] == "Rahul"
    assert "after the club confirmed the request" in row(via_reg, "registered")["detail"]
    assert row(via_reg, "review_email")["title"] == "Emailed: Confirmed"
    assert [r["at"] for r in via_reg["timeline"]] == sorted((r["at"] for r in via_reg["timeline"]), reverse=True)


async def test_an_unconfirmed_request_for_someone_who_registered_another_way_joins_their_registration(client, session, world):
    from app.api.approvals.models import RegistrationRequests

    req = RegistrationRequests(
        event_id=world["event"].id, full_name="Ananya", email="ANANYA.IYER@gmail.com", token=uuid.uuid4().hex, status="closed",
        closed_reason="registered_directly", submitted_at=ago(days=4),
    )
    session.add(req)
    await session.commit()
    body = (await _get(client, world, ref=req.id)).json()
    assert body["kind"] == "registration" and body["registration_id"] == str(world["reg"].id)
    assert row(body, "request_closed")["detail"] == "They registered another way"


# ─── One test per source of timeline rows ──────────────────────────────────


async def test_request_rows_submitted_confirmed_declined_withdrawn_lapsed_and_closed(client, session, world):
    from app.api.approvals.models import RegistrationRequests

    def make(status, **kw):
        r = RegistrationRequests(
            event_id=world["event"].id, full_name=f"Person {status}", email=f"{status}@example.com", token=uuid.uuid4().hex, status=status,
            submitted_at=ago(days=2), **kw,
        )
        session.add(r)
        return r

    declined = make("declined", decided_at=ago(days=1), decided_by_name="Priya", decline_note="Not this time")
    approved = make("approved", decided_at=ago(days=1), decided_by_name="Rahul", amount=500)
    lapsed = make("lapsed", decided_at=ago(days=1), decided_by_name="Rahul", amount=500, updated_at=ago(hours=3))
    withdrawn = make("withdrawn", updated_at=ago(hours=2))
    closed = make("closed", closed_reason="event_ended", updated_at=ago(hours=1))
    await session.commit()
    bodies = {s: (await _get(client, world, ref=r.id)).json() for s, r in dict(declined=declined, approved=approved, lapsed=lapsed, withdrawn=withdrawn, closed=closed).items()}
    assert bodies["declined"]["status"]["stage"] == "declined"
    assert (row(bodies["declined"], "request_declined")["title"], row(bodies["declined"], "request_declined")["detail"]) == ("Declined", "Not this time")
    assert bodies["approved"]["status"]["stage"] == "confirmed" and bodies["approved"]["status"]["paid"] is False
    assert row(bodies["approved"], "request_confirmed")["detail"] == "Ticket ₹500, held until paid"
    assert bodies["approved"]["payment"]["status"] == "unpaid" and bodies["approved"]["payment"]["amount"] == 500
    assert {"request_confirmed", "request_lapsed"} <= set(kinds(bodies["lapsed"])) and bodies["lapsed"]["status"]["stage"] == "cancelled"
    assert row(bodies["withdrawn"], "request_withdrawn")["by"] == "the guest"
    assert row(bodies["closed"], "request_closed")["detail"] == "The event ended before the request was answered"


async def test_the_registered_row_says_where_it_came_from_and_the_ticket_is_issued(client, session, world):
    from app.api.links.models import TrackingLinks

    link = TrackingLinks(club_id=world["club"].id, event_id=world["event"].id, name="Freshers fair QR", code="FRQR1", utm_source="poster", utm_medium="qr")
    session.add(link)
    await session.flush()
    world["reg"].tracking_link_id = link.id
    await session.commit()
    body = (await _get(client, world)).json()
    registered = row(body, "registered")
    assert "Through the link “Freshers fair QR”" in registered["detail"] and "with an app account" in registered["detail"] and registered["by"] == "the guest"
    assert row(body, "ticket_issued")["title"] == "Ticket issued"


async def test_the_check_in_is_in_the_section_the_status_and_the_log(client, session, world):
    from app.api.checkins.models import CheckIns, CheckInVia, Doors

    door = Doors(event_id=world["event"].id, name="Main gate")
    session.add(door)
    await session.flush()
    session.add(CheckIns(event_id=world["event"].id, registration_id=world["reg"].id, door_id=door.id, door_label="Main gate", checked_in_by=world["owner"].id, via=CheckInVia.desk, checked_in_at=ago(minutes=30)))
    world["reg"].is_attended = True
    await session.commit()
    body = (await _get(client, world)).json()
    assert body["status"]["checked_in"] is True
    assert body["check_in"]["door"] == "Main gate" and body["check_in"]["method"] == "By hand" and body["check_in"]["via"] == "desk"
    assert body["check_in"]["by"] == world["owner"].full_name
    line = row(body, "checked_in")
    assert (line["title"], line["detail"], line["tone"]) == ("Checked in at Main gate", "By hand", "success")
    assert body["actions"]["can_undo_check_in"] is True and body["actions"]["can_check_in"] is False


async def test_a_phone_or_kiosk_check_in_is_by_the_guest_or_the_kiosk(client, session, world):
    from app.api.checkins.models import CheckIns, CheckInVia

    session.add(CheckIns(event_id=world["event"].id, registration_id=world["reg"].id, door_label="Lobby", via=CheckInVia.phone, checked_in_at=ago(minutes=5)))
    await session.commit()
    body = (await _get(client, world)).json()
    assert body["check_in"]["by"] == "the guest" and body["check_in"]["method"] == "Phone self check-in"
    assert row(body, "checked_in")["by"] == "the guest"


async def _station(session, world, name="Desk printer", door="Desk"):
    from app.api.kiosk.models import KioskDevices

    device = KioskDevices(event_id=world["event"].id, name=name, door_label=door, token_hash=uuid.uuid4().hex + uuid.uuid4().hex, token_prefix="kiosk_ab", expires_at=NOW + timedelta(days=1), role="printer")
    session.add(device)
    await session.flush()
    return device


async def test_print_queue_steps_and_print_jobs_are_one_log_without_double_counting(client, session, world):
    from app.api.idcards.models import PrintJobs, PrintSource
    from app.api.kiosk.models import CardPrintRequests

    printer = await _station(session, world)
    reg = world["reg"]
    printed = CardPrintRequests(
        event_id=world["event"].id, registration_id=reg.id, source="dashboard", requested_by_user_id=world["owner"].id, printer_id=printer.id,
        status="printed", attempts=1, claimed_by=printer.id, claimed_at=ago(minutes=50), printed_at=ago(minutes=49), created_at=ago(minutes=51), updated_at=ago(minutes=49),
    )
    failed = CardPrintRequests(
        event_id=world["event"].id, registration_id=reg.id, source="kiosk", status="failed", attempts=3, error="Out of paper",
        claimed_by=printer.id, claimed_at=ago(minutes=20), created_at=ago(minutes=22), updated_at=ago(minutes=19),
    )
    session.add_all([printed, failed])
    # The job the station's own print wrote, and a card the organiser printed on their computer.
    session.add(PrintJobs(event_id=world["event"].id, registration_id=reg.id, source=PrintSource.manual, door_label="Desk", cards=1, created_at=ago(minutes=49, seconds=30)))
    session.add(PrintJobs(event_id=world["event"].id, registration_id=reg.id, source=PrintSource.manual, door_label=None, requested_by=world["owner"].id, cards=1, created_at=ago(minutes=5)))
    reg.badge_printed_at = ago(minutes=49)
    await session.commit()
    body = (await _get(client, world)).json()
    assert [(p["status"], p["source"]) for p in body["prints"]] == [("printed", "dashboard"), ("failed", "kiosk"), ("printed", "dashboard")]
    station = body["prints"][2]
    assert station["printer"] == "Desk printer" and station["door"] == "Desk" and station["by"] == world["owner"].full_name
    assert body["prints"][1]["error"] == "Out of paper" and body["prints"][1]["printer"] == "Desk printer"
    assert body["prints"][0]["printer"] is None and body["prints"][0]["by"] == world["owner"].full_name
    # Queue requests carry their id (Retry acts on it); a card printed another way has none.
    assert station["id"] and body["prints"][1]["id"] and body["prints"][0]["id"] is None
    assert body["status"]["card_printed"] is True
    assert kinds(body).count("print_queued") == 2 and kinds(body).count("print_printed") == 2
    assert {"print_printing", "print_failed"} <= set(kinds(body))
    assert row(body, "print_failed")["detail"] == "Out of paper"
    assert "Printing on Desk printer" in {r["title"] for r in body["timeline"]}


async def test_a_queued_card_and_a_cancelled_one_are_in_the_log(client, session, world):
    from app.api.kiosk.models import CardPrintRequests

    session.add(CardPrintRequests(event_id=world["event"].id, registration_id=world["reg"].id, source="door", status="cancelled", created_at=ago(minutes=10), updated_at=ago(minutes=9)))
    await session.commit()
    body = (await _get(client, world)).json()
    assert [p["status"] for p in body["prints"]] == ["cancelled"] and body["status"]["card_printed"] is False
    assert {"print_queued", "print_cancelled"} <= set(kinds(body))
    assert row(body, "print_queued")["title"] == "Card queued from the door"


async def test_merch_collected_is_in_the_section_and_the_log(client, session, world):
    from app.api.merch.models import MerchCollections, MerchItems, MerchVariants

    item = MerchItems(event_id=world["event"].id, name="DevFest tee")
    session.add(item)
    await session.flush()
    variant = MerchVariants(item_id=item.id, label="M", stock=10)
    session.add(variant)
    await session.flush()
    session.add(MerchCollections(registration_id=world["reg"].id, item_id=item.id, variant_id=variant.id, door_label="Swag desk", collected_by=world["owner"].id, collected_at=ago(minutes=15)))
    await session.commit()
    body = (await _get(client, world)).json()
    assert [(m["item"], m["variant"], m["by"]) for m in body["merch"]] == [("DevFest tee", "M", world["owner"].full_name)]
    line = row(body, "merch_collected")
    assert (line["title"], line["detail"]) == ("Collected DevFest tee (M)", "at Swag desk")


async def test_payment_started_paid_failed_and_refunded(client, session, world):
    from app.api.admin_ops.models import PaymentRefunds
    from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus

    reg = world["reg"]
    reg.is_paid, reg.paid_amount, reg.actual_amount, reg.payment_receipt = True, 500, 500, "rcpt_people_1"
    world["event"].has_fee = True
    order = PaymentOrders(receipt="rcpt_people_1", razorpay_order_id="order_people_1", amount=500, status=OrderStatus.paid, source="event", payload={}, created_at=ago(days=1, minutes=2))
    session.add(order)
    await session.flush()
    bad = PaymentLogs(order_id=order.id, razorpay_payment_id="pay_bad", status=PaymentStatus.failed, amount=500, payment_method="card", created_at=ago(days=1, minutes=1))
    good = PaymentLogs(order_id=order.id, razorpay_payment_id="pay_good", status=PaymentStatus.captured, amount=500, payment_method="upi", created_at=ago(days=1))
    session.add_all([bad, good])
    await session.flush()
    session.add(PaymentRefunds(payment_log_id=good.id, razorpay_payment_id="pay_good", kind="ticket", registration_id=reg.id, amount=200, reason="Asked nicely", created_by=world["owner"].id, created_at=ago(hours=2)))
    await session.commit()
    body = (await _get(client, world)).json()
    assert body["status"]["paid"] is True
    assert body["payment"] == {"amount": 500.0, "currency": "INR", "method": "upi", "status": "partly refunded", "paid_at": body["payment"]["paid_at"], "receipt": "rcpt_people_1"}
    assert {"payment_started", "payment_paid", "payment_failed", "payment_refunded"} <= set(kinds(body))
    assert row(body, "payment_paid")["title"] == "Paid ₹500" and row(body, "payment_failed")["tone"] == "danger"
    refund = row(body, "payment_refunded")
    assert (refund["title"], refund["detail"], refund["by"]) == ("Refunded ₹200", "Asked nicely", world["owner"].full_name)


async def test_a_ticket_sent_again_and_what_the_provider_said(client, session, world):
    from app.api.deliveries.models import DeliveryAttempts, DeliveryChannel, DeliveryKind, DeliveryOutcome, DeliveryStatus, Deliveries

    d = Deliveries(
        event_id=world["event"].id, registration_id=world["reg"].id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email, status=DeliveryStatus.sent,
        sent_at=ago(hours=1), outcome=DeliveryOutcome.bounced, reason="Mailbox full",
    )
    session.add(d)
    await session.flush()
    session.add(DeliveryAttempts(provider="resend", provider_id="msg-1", delivery_id=d.id, created_at=ago(hours=5), outcome=DeliveryOutcome.delivered, outcome_at=ago(hours=5) + timedelta(seconds=9)))
    session.add(DeliveryAttempts(provider="resend", provider_id="msg-2", delivery_id=d.id, created_at=ago(hours=1), outcome=DeliveryOutcome.bounced, outcome_at=ago(hours=1) + timedelta(seconds=30), reason="Mailbox full"))
    await session.commit()
    body = (await _get(client, world)).json()
    titles = [r["title"] for r in reversed(body["timeline"]) if r["kind"].startswith("ticket_") and r["kind"] != "ticket_issued"]
    assert titles == ["Ticket sent by email", "Ticket was delivered", "Ticket sent again by email", "Ticket bounced"]
    assert row(body, "ticket_bounced")["tone"] == "danger" and row(body, "ticket_bounced")["detail"] == "Mailbox full"
    assert body["deliveries"] == [{"what": "ticket", "channel": "email", "status": "sent", "outcome": "bounced", "at": body["deliveries"][0]["at"], "reason": "Mailbox full"}]


async def test_a_certificate_issued_sent_and_taken_back(client, session, world):
    from app.api.deliveries.models import DeliveryChannel, DeliveryKind, DeliveryStatus, Deliveries

    reg = world["reg"]
    reg.certificate_issued_at, reg.certificate_revoked_at = ago(hours=4), ago(hours=1)
    session.add(Deliveries(event_id=world["event"].id, registration_id=reg.id, kind=DeliveryKind.certificate, channel=DeliveryChannel.email, status=DeliveryStatus.sent, sent_at=ago(hours=3)))
    await session.commit()
    body = (await _get(client, world)).json()
    assert body["status"]["certificate"] == "revoked"
    assert {"certificate_issued", "certificate_sent", "certificate_revoked"} <= set(kinds(body))
    assert row(body, "certificate_sent")["title"] == "Certificate sent by email"


async def test_a_revoked_ticket_and_a_bounced_address_are_in_the_log(client, session, world):
    reg = world["reg"]
    reg.ticket_revoked_at, reg.email_bounced_at, reg.email_bounce_reason = ago(hours=2), ago(hours=3), "Mailbox does not exist"
    await session.commit()
    body = (await _get(client, world)).json()
    assert body["status"]["ticket_revoked"] is True and body["ticket"]["pdf_url"] is None and body["ticket"]["revoked_at"] is not None
    assert body["status"]["email_bounced"] is True
    assert row(body, "ticket_revoked")["tone"] == "danger" and row(body, "email_bounced")["detail"] == "Mailbox does not exist"
    assert body["actions"]["can_print"] is False and body["actions"]["can_resend_ticket"] is False and body["actions"]["can_check_in"] is False


async def test_a_ticket_given_back_and_a_removal(client, session, world, make_registration, make_user):
    from app.api.ticket_cancel.models import TicketCancellations

    reg = world["reg"]
    reg.soft_delete()
    reg.deleted_at = ago(hours=1)
    session.add(TicketCancellations(registration_id=reg.id, event_id=world["event"].id, user_id=reg.user_id, full_name=reg.full_name, email=reg.email, ticket_id=reg.ticket_id, reason="Exam clash", cancelled_at=ago(hours=1)))
    gone = await make_registration(event=world["event"], user=await make_user(email="gone@example.com"), full_name="Gone Away", email="gone@example.com")
    gone.soft_delete()
    gone.deleted_at = ago(minutes=30)
    await session.commit()
    back = (await _get(client, world)).json()
    assert back["status"]["stage"] == "cancelled" and (row(back, "ticket_given_back")["detail"], row(back, "ticket_given_back")["by"]) == ("Exam clash", "the guest")
    assert back["ticket"]["pdf_url"] is None and back["actions"]["can_remove"] is False and back["actions"]["can_edit"] is False
    removed = (await _get(client, world, ref=gone.id)).json()
    assert removed["status"]["stage"] == "removed" and row(removed, "removed")["title"] == "Taken off the list"


async def test_asking_for_missing_answers_and_the_answer(client, session, world):
    from app.api.missing_answers.models import AnswerAskPeople, AnswerAskSends, AnswerAsks

    ask = AnswerAsks(event_id=world["event"].id, created_at=ago(hours=6), created_by_name="Rahul", questions=[{"key": "shirt", "label": "Shirt size"}], channels=["email"], message="Please add it")
    session.add(ask)
    await session.flush()
    session.add(AnswerAskPeople(ask_id=ask.id, ref_kind="registration", ref_id=world["reg"].id, event_id=world["event"].id, added_at=ago(hours=6), opened_at=ago(hours=5), answered_at=ago(hours=4)))
    session.add(AnswerAskSends(ask_id=ask.id, ref_kind="registration", ref_id=world["reg"].id, channel="email", round=0, status="sent", sent_at=ago(hours=6)))
    session.add(AnswerAskSends(ask_id=ask.id, ref_kind="registration", ref_id=world["reg"].id, channel="email", round=1, status="sent", sent_at=ago(hours=3)))
    await session.commit()
    body = (await _get(client, world)).json()
    asked = row(body, "answers_asked")
    assert (asked["title"], asked["detail"], asked["by"]) == ("Asked for answers", "Shirt size", "Rahul")
    assert {"answers_opened", "answers_answered", "answers_ask_sent"} <= set(kinds(body))
    assert sorted(r["title"] for r in body["timeline"] if r["kind"] == "answers_ask_sent") == ["Ask sent by email", "Reminder 1 sent by email"]
    assert [d["what"] for d in body["deliveries"]].count("answers_ask") == 2
    assert body["actions"]["can_ask_missing"] is True


async def test_waiting_list_offer_and_claim(client, session, world):
    from app.api.ticketing import types as types_mod
    from app.api.ticketing.models import CLAIMED, WaitlistEntries

    ttype = (await types_mod.ensure(session, world["event"]))[0]
    session.add(WaitlistEntries(
        event_id=world["event"].id, ticket_type_id=ttype.id, full_name="Ananya Iyer", email="ananya.iyer@gmail.com", status=CLAIMED, registration_id=world["reg"].id,
        created_at=ago(days=3), offered_at=ago(days=2, hours=2), offer_expires_at=ago(days=1, hours=2), updated_at=ago(days=2),
    ))
    await session.commit()
    body = (await _get(client, world)).json()
    assert [r["kind"] for r in reversed(body["timeline"]) if r["kind"].startswith("waitlist")] == ["waitlist_joined", "waitlist_offered", "waitlist_claimed"]


async def test_feedback_and_a_rating(client, session, world):
    from app.api.events.models import EventRatingsLink
    from app.api.live.models import FeedbackResponses, FeedbackSends, FeedbackSurveys

    survey = FeedbackSurveys(event_id=world["event"].id)
    session.add(survey)
    await session.flush()
    session.add(FeedbackSends(survey_id=survey.id, registration_id=world["reg"].id, sent_at=ago(hours=2), channel="email", delivered_at=ago(hours=2)))
    session.add(FeedbackResponses(survey_id=survey.id, registration_id=world["reg"].id, answers={}, submitted_at=ago(hours=1)))
    session.add(EventRatingsLink(event_id=world["event"].id, user_id=world["user"].id, rating=4.5, review="Loved it", created_at=ago(minutes=50)))
    await session.commit()
    body = (await _get(client, world)).json()
    assert {"feedback_asked", "feedback_given", "rating_given"} <= set(kinds(body))
    assert row(body, "rating_given")["title"] == "Rated the event 4.5 out of 5" and row(body, "rating_given")["detail"] == "Loved it"


async def test_broadcast_messages_and_notices(client, session, world):
    from app.api.club_ops.models import GuestNotices
    from app.api.deliveries.models import DeliveryChannel, DeliveryStatus
    from app.api.emails.models import MessageDeliveries, MessageKind, Messages

    msg = Messages(event_id=world["event"].id, kind=MessageKind.BROADCAST, subject="Doors open at 6", body="x", created_by=world["owner"].id)
    session.add(msg)
    await session.flush()
    session.add(MessageDeliveries(message_id=msg.id, registration_id=world["reg"].id, channel=DeliveryChannel.email, status=DeliveryStatus.sent, sent_at=ago(hours=2)))
    session.add(GuestNotices(ref="refund:7", event_id=world["event"].id, registration_id=world["reg"].id, channel="email", status="sent"))
    await session.commit()
    body = (await _get(client, world)).json()
    assert row(body, "message_sent")["title"] == "Message sent: “Doors open at 6”" and row(body, "message_sent")["by"] == world["owner"].full_name
    assert row(body, "notice_sent")["title"] == "Told about a refund"
    assert sorted(d["what"] for d in body["deliveries"]) == ["message", "notice"]


async def test_the_audit_log_rows_about_this_person_are_in_the_log(client, session, world):
    from app.api.audit.models import AdminAuditLogs

    reg = world["reg"]
    session.add(AdminAuditLogs(action="ticket_link.resend", entity_type="registration", entity_id=str(reg.id), summary="A fresh ticket link was asked for MOA-X: sent", created_at=ago(hours=1)))
    session.add(AdminAuditLogs(actor_id=world["owner"].id, actor_name="Rahul", action="registration.note", entity_type="registration", entity_id=str(reg.id), summary="Moved to the VIP list", created_at=ago(minutes=30)))
    session.add(AdminAuditLogs(action="ticket_link.resend", entity_type="registration", entity_id=str(uuid.uuid4()), summary="Somebody else's", created_at=ago(minutes=10)))
    await session.commit()
    body = (await _get(client, world)).json()
    audit = [r for r in body["timeline"] if r["kind"] == "audit"]
    assert [(r["title"], r["by"]) for r in audit] == [("Moved to the VIP list", "Rahul"), ("A fresh ticket link was asked for MOA-X: sent", "the guest")]


async def test_a_long_log_is_capped_at_200_rows_and_says_so(client, session, world):
    from app.api.audit.models import AdminAuditLogs

    for i in range(230):
        session.add(AdminAuditLogs(action="x.y", entity_type="registration", entity_id=str(world["reg"].id), summary=f"Note {i}", created_at=ago(minutes=i + 1)))
    await session.commit()
    body = (await _get(client, world)).json()
    assert len(body["timeline"]) == 200 and body["timeline_truncated"] is True
    # The newest 200 of everything: the oldest rows, the registration itself among them, are cut.
    assert "registered" not in kinds(body) and body["timeline"][0]["title"] == "Note 0"
    few = (await _get(client, world, ref=world["reg"].id)).json()
    assert few["timeline_truncated"] is True


# ─── Rights and masking ────────────────────────────────────────────────────


async def test_another_club_is_refused_and_no_login_is_401(client, world):
    assert (await _get(client, world, headers=world["other_h"])).status_code == 403
    assert (await client.get(_url(world))).status_code == 401


async def test_the_organiser_sees_everything_a_door_member_sees_it_masked(client, session, world):
    door_h = await _member_header(session, world, "door")
    whole = (await _get(client, world)).json()
    masked = (await _get(client, world, headers=door_h)).json()
    assert set(masked) == set(whole)
    assert masked["person"]["email"] == "a•••@gmail.com" and masked["person"]["phone"] == "•••• 4321" and whole["person"]["phone"] == "9876544321"
    assert masked["person"]["full_name"] == "Ananya Iyer" and masked["ticket"]["admission_no"] == "A1"
    assert masked["ticket"]["code"].startswith("••••") and masked["ticket"]["code"] != whole["ticket"]["code"]
    assert masked["ticket"]["pdf_url"] is None and whole["ticket"]["pdf_url"] is not None
    assert masked["actions"]["can_edit"] is False and masked["actions"]["can_remove"] is False and masked["actions"]["can_print"] is False
    assert masked["actions"]["can_confirm"] is False and masked["actions"]["can_decline"] is False
    # Door staff may check people in and send a ticket again, as their own role allows.
    assert masked["actions"]["can_check_in"] is True
    for line in masked["timeline"]:
        assert world["reg"].ticket_id not in (line["detail"] or "") + line["title"]


async def test_a_bounce_reason_with_an_address_is_masked_for_door_staff(client, session, world):
    world["reg"].email_bounced_at, world["reg"].email_bounce_reason = ago(hours=1), "550 ananya.iyer@gmail.com does not exist"
    await session.commit()
    door_h = await _member_header(session, world, "door")
    assert row((await _get(client, world, headers=door_h)).json(), "email_bounced")["detail"] == "550 a•••@gmail.com does not exist"
    assert "ananya.iyer@gmail.com" in row((await _get(client, world)).json(), "email_bounced")["detail"]


async def test_an_editor_may_edit_but_the_receipt_is_the_clubs_money_alone(client, session, world):
    from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus

    reg = world["reg"]
    reg.is_paid, reg.paid_amount, reg.actual_amount, reg.payment_receipt = True, 300, 300, "rcpt_people_2"
    order = PaymentOrders(receipt="rcpt_people_2", razorpay_order_id="order_people_2", amount=300, status=OrderStatus.paid, source="event", payload={})
    session.add(order)
    await session.flush()
    session.add(PaymentLogs(order_id=order.id, razorpay_payment_id="pay_people_2", status=PaymentStatus.captured, amount=300, payment_method="upi"))
    await session.commit()
    editor_h = await _member_header(session, world, "editor")
    editor = (await _get(client, world, headers=editor_h)).json()
    owner = (await _get(client, world)).json()
    assert editor["actions"]["can_edit"] is True and editor["actions"]["can_remove"] is True and editor["actions"]["can_print"] is True
    assert editor["payment"]["status"] == "paid" and editor["payment"]["receipt"] is None and owner["payment"]["receipt"] == "rcpt_people_2"
    assert editor["person"]["email"] == "ananya.iyer@gmail.com"


async def test_a_platform_admin_reads_it_but_decides_nothing(client, session, world, make_admin, mint_token):
    from app.api.approvals.models import RegistrationRequests

    req = RegistrationRequests(event_id=world["event"].id, full_name="Dev Menon", email="dev.menon@gmail.com", token=uuid.uuid4().hex, submitted_at=ago(hours=1))
    session.add(req)
    admin = await make_admin(email="people-admin@example.com")
    await session.commit()
    headers = auth_header(await mint_token(admin))
    as_admin = await _get(client, world, ref=req.id, headers=headers)
    assert as_admin.status_code == 200
    assert as_admin.json()["actions"]["can_confirm"] is False and as_admin.json()["actions"]["can_decline"] is False and as_admin.json()["actions"]["can_edit"] is True
    as_club = (await _get(client, world, ref=req.id)).json()
    assert as_club["actions"]["can_confirm"] is True and as_club["actions"]["can_decline"] is True


async def test_a_cohost_reads_it_as_the_host_and_a_stranger_does_not(client, session, world, make_club, mint_token):
    from app.api.cohosts.models import ACCEPTED, EventCohosts

    co_owner, co_club = await make_club(name="Co Club", email="people-co@example.com")
    session.add(EventCohosts(event_id=world["event"].id, club_id=co_club.id, status=ACCEPTED, invited_at=NOW, expires_at=START, responded_at=NOW))
    await session.commit()
    co_h = auth_header(await mint_token(co_owner))
    as_co = await _get(client, world, headers=co_h)
    assert as_co.status_code == 200
    assert as_co.json() == (await _get(client, world)).json()
    assert (await _get(client, world, headers=world["other_h"])).status_code == 403


async def test_the_actions_follow_the_state_of_the_request_and_the_printers(client, session, world):
    from app.api.approvals.models import RegistrationRequests

    pending = RegistrationRequests(event_id=world["event"].id, full_name="Dev Menon", email="dev.menon@gmail.com", token=uuid.uuid4().hex, submitted_at=ago(hours=1))
    declined = RegistrationRequests(event_id=world["event"].id, full_name="Gita Rao", email="gita@example.com", token=uuid.uuid4().hex, status="declined", submitted_at=ago(hours=2), decided_at=ago(hours=1))
    session.add_all([pending, declined])
    await session.commit()
    p = (await _get(client, world, ref=pending.id)).json()["actions"]
    assert p["can_confirm"] and p["can_decline"] and p["can_edit"] and p["can_ask_missing"]
    assert not (p["can_print"] or p["can_check_in"] or p["can_resend_ticket"] or p["can_remove"] or p["can_undo_check_in"])
    d = (await _get(client, world, ref=declined.id)).json()["actions"]
    assert not (d["can_confirm"] or d["can_decline"]) and d["can_edit"]

    quiet = (await _get(client, world)).json()["actions"]
    assert quiet["can_print"] and quiet["can_send_to_station"] is False and quiet["printers_online"] == 0
    printer = await _station(session, world)
    printer.last_seen_at = datetime.now(timezone.utc)
    await session.commit()
    online = (await _get(client, world)).json()["actions"]
    assert online["printers_online"] == 1 and online["can_send_to_station"] is True and online["can_check_in"] and online["can_resend_ticket"] and online["can_remove"]


# ─── Performance: one round of queries per source, however much there is ────


def _counter(session):
    seen: list[str] = []
    conn = session.bind.sync_connection

    def count(_conn, _cursor, statement, *_):
        seen.append(statement)

    sa_event.listen(conn, "before_cursor_execute", count)
    return seen, lambda: sa_event.remove(conn, "before_cursor_execute", count)


async def _fill(session, world, reg, n: int, tag: str):
    """`n` of every kind of row for one person: the more, the more a per-row query would show."""
    from app.api.admin_ops.models import PaymentRefunds
    from app.api.approvals.models import RegistrationRequests
    from app.api.audit.models import AdminAuditLogs
    from app.api.checkins.models import CheckIns, CheckInVia
    from app.api.deliveries.models import DeliveryAttempts, DeliveryChannel, DeliveryKind, DeliveryOutcome, DeliveryStatus, Deliveries
    from app.api.emails.models import ReviewResendRecipients, ReviewResends
    from app.api.idcards.models import PrintJobs, PrintSource
    from app.api.kiosk.models import CardPrintRequests
    from app.api.merch.models import MerchCollections, MerchItems
    from app.api.missing_answers.models import AnswerAskPeople, AnswerAskSends, AnswerAsks
    from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus

    eid = world["event"].id
    printer = await _station(session, world, name=f"Printer {tag}", door=f"Door {tag}")
    reg.payment_receipt = f"rcpt_{tag}"
    session.add(CheckIns(event_id=eid, registration_id=reg.id, door_label="Gate", checked_in_by=world["owner"].id, via=CheckInVia.gate, checked_in_at=ago(hours=9)))
    delivery = Deliveries(event_id=eid, registration_id=reg.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email, status=DeliveryStatus.sent, sent_at=ago(hours=8))
    order = PaymentOrders(receipt=f"rcpt_{tag}", razorpay_order_id=f"order_{tag}", amount=100, status=OrderStatus.paid, source="event", payload={})
    resend = ReviewResends(event_id=eid, stage="REQUEST_RECEIVED", client_ref=uuid.uuid4(), subject="s", design={})
    ask = AnswerAsks(event_id=eid, created_at=ago(hours=7), created_by_name="Rahul", questions=[{"key": "shirt", "label": "Shirt"}], channels=["email"], message="m")
    session.add_all([delivery, order, resend, ask])
    await session.flush()
    first_request = None
    for i in range(n):
        req = RegistrationRequests(event_id=eid, user_id=reg.user_id, full_name=reg.full_name, email=reg.email, token=uuid.uuid4().hex, status="declined", submitted_at=ago(days=10 + i), decided_at=ago(days=9 + i), decided_by_id=world["owner"].id)
        session.add(req)
        await session.flush()
        first_request = first_request or req
        item = MerchItems(event_id=eid, name=f"Item {tag}{i}")
        log = PaymentLogs(order_id=order.id, razorpay_payment_id=f"pay_{tag}{i}", status=PaymentStatus.captured, amount=100, payment_method="upi")
        session.add_all([item, log])
        await session.flush()
        session.add_all([
            MerchCollections(registration_id=reg.id, item_id=item.id, collected_by=world["owner"].id, collected_at=ago(minutes=i + 1)),
            PrintJobs(event_id=eid, registration_id=reg.id, source=PrintSource.manual, requested_by=world["owner"].id, cards=1, created_at=ago(hours=1, minutes=i)),
            CardPrintRequests(event_id=eid, registration_id=reg.id, source="door", status="failed", printer_id=printer.id, claimed_by=printer.id, claimed_at=ago(hours=2, minutes=i), created_at=ago(hours=2, minutes=i + 1), updated_at=ago(hours=2, minutes=i)),
            DeliveryAttempts(provider="resend", provider_id=f"p-{tag}-{i}", delivery_id=delivery.id, created_at=ago(hours=6, minutes=i), outcome=DeliveryOutcome.delivered, outcome_at=ago(hours=6, minutes=i)),
            AdminAuditLogs(actor_id=world["owner"].id, action="x.y", entity_type="registration", entity_id=str(reg.id), summary=f"Note {tag}{i}", created_at=ago(minutes=i + 1)),
            PaymentRefunds(payment_log_id=log.id, razorpay_payment_id=f"pay_{tag}{i}", kind="ticket", registration_id=reg.id, amount=1, reason="r", created_by=world["owner"].id),
            ReviewResendRecipients(resend_id=resend.id, request_id=req.id, status="sent", sent_at=ago(days=9 + i)),
            AnswerAskPeople(ask_id=ask.id, ref_kind="registration", ref_id=reg.id, event_id=eid, added_at=ago(hours=7)) if i == 0 else AdminAuditLogs(action="x.z", entity_type="other", entity_id="0"),
            AnswerAskSends(ask_id=ask.id, ref_kind="registration", ref_id=reg.id, channel="email", round=0, status="sent", sent_at=ago(hours=7)) if i == 0 else AdminAuditLogs(action="x.z", entity_type="other", entity_id="0"),
        ])
    await session.commit()


async def test_a_person_with_hundreds_of_events_costs_the_same_queries_as_one_with_a_few(client, session, world, make_registration, make_user):
    few_reg = await make_registration(event=world["event"], user=await make_user(email="few@example.com"), full_name="Few Rows", email="few@example.com")
    many_reg = await make_registration(event=world["event"], user=await make_user(email="many@example.com"), full_name="Many Rows", email="many@example.com")
    await session.commit()
    await _fill(session, world, few_reg, 1, "few")
    await _fill(session, world, many_reg, 40, "many")
    # Warm anything the first request of a process loads, then count.
    await _get(client, world, ref=few_reg.id)

    async def statements(ref):
        seen, stop = _counter(session)
        r = await _get(client, world, ref=ref)
        stop()
        assert r.status_code == 200, r.text
        return len(seen), r.json()

    n_few, few = await statements(few_reg.id)
    n_many, many = await statements(many_reg.id)
    assert n_few == n_many, (n_few, n_many)
    assert n_many <= 35
    # And the many really is many: every source has more rows, and nothing was dropped silently.
    assert len(many["timeline"]) == 200 and many["timeline_truncated"] is True and len(few["timeline"]) < 40
    assert len(many["merch"]) == 40 and len(many["prints"]) >= 40
