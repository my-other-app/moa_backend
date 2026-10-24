"""Tickets, certificates and the day-before reminder by WhatsApp.

Meta is never reached: `meta` stands in for the Cloud API (the media upload
and the messages endpoint) and records what was sent, and every other test
finds it unreachable (conftest). What is checked is what Meta would be
handed -- the approved template, its values in order, the document, the URL
button -- and what the delivery rows say afterwards.
"""
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryAttempts, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.api.events.models import EventRegistrationsLink
from app.config import settings
from app.core import jobs, providers, whatsapp
from app.core.email import email as email_mod
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import certificate_starter, ticket_starter
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


# ─── Fakes ───────────────────────────────────────────────────────────────


def _fake_meta(state: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        state["calls"].append(request)
        path = request.url.path
        if path.endswith("/media"):
            state["uploads"].append(request.content)
            return httpx.Response(200, json={"id": f"media-{len(state['uploads'])}"})
        if path.endswith("/messages"):
            body = json.loads(request.content)
            state["messages"].append(body)
            if state["answer"] is not None:
                return state["answer"](body)
            n = len(state["messages"])
            return httpx.Response(200, json={
                "messaging_product": "whatsapp",
                "contacts": [{"input": body["to"], "wa_id": body["to"]}],
                "messages": [{"id": f"wamid.TEST{n}"}],
            })
        return httpx.Response(404, json={"error": {"message": "unknown path", "code": 100}})

    return handler


@pytest.fixture
def meta(monkeypatch):
    """WhatsApp configured, and a stand-in for Meta's API. `answer` set to a
    function of the message body replaces the "accepted" reply."""
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "test-token", raising=False)
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "1029384756", raising=False)
    monkeypatch.setattr(settings, "WHATSAPP_GRAPH_VERSION", "v23.0", raising=False)
    state = {"calls": [], "uploads": [], "messages": [], "answer": None}
    monkeypatch.setattr(whatsapp, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(_fake_meta(state))))
    return state


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []

    def send(**kwargs):
        sent.append(kwargs)
        return {"MessageId": f"ses-{len(sent)}"}

    monkeypatch.setattr(email_mod, "send_email_with_attachment", send)
    monkeypatch.setattr(email_mod, "send_email", send)
    return sent


@pytest.fixture
def background(monkeypatch, session):
    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(deliveries, "session_scope", scope)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="wa-club@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 13, 0, tzinfo=timezone.utc), duration=3.0,
        location_name="Innovation Hall",
    )
    regs = {}
    for key, name, phone, opt_in in (
        ("asked", "Ananya Iyer", "98765 43210", True),
        ("silent", "Bala Krishnan", "9876543211", False),
        ("no_phone", "Chitra Nair", None, True),
        ("landline", "Dev Menon", "2233 4455", True),
    ):
        u = await make_user(email=f"wa-{key}@example.com")
        regs[key] = await make_registration(
            event=event, user=u, full_name=name, email=u.email, phone=phone,
            whatsapp_opt_in=opt_in, whatsapp_opt_in_at=NOW if opt_in else None,
        )
    await session.commit()
    headers = auth_header(await mint_token(owner))
    st = ticket_starter("classic")
    r = await client.put(
        f"/api/v1/events/{event.id}/designs/ticket/layout",
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return {"event": event, "club": headers, **regs}


async def _rows(session, reg, kind=DeliveryKind.ticket) -> dict:
    rows = await session.scalars(
        select(Deliveries).where(Deliveries.registration_id == reg.id, Deliveries.kind == kind).execution_options(populate_existing=True)
    )
    return {d.channel: d for d in rows}


def _send_tickets(client, world, **body):
    return client.post(f"/api/v1/events/{world['event'].id}/tickets/send", json=body, headers=world["club"])


# ─── Sending ─────────────────────────────────────────────────────────────
#
# First in the module: run alone, a module whose first test is not async
# leaves its client tests on another event loop than the suite's session.


async def test_a_send_uploads_the_document_then_sends_the_template(session, meta):
    t = whatsapp.ticket_confirmed(name="Ananya", event="Build Night", when="Fri", ticket_id="MOA-1")
    out = await whatsapp.send("9876543210", t, document=(b"%PDF-1.4 ticket", "ticket-MOA-1.pdf"))
    assert out == {"MessageId": "wamid.TEST1", "Provider": "whatsapp", "Address": "+919876543210", "Via": "platform"}
    upload, message = meta["calls"]
    assert str(upload.url) == "https://graph.facebook.com/v23.0/1029384756/media"
    assert upload.headers["authorization"] == "Bearer test-token"
    assert b'name="messaging_product"' in upload.content and b"application/pdf" in upload.content and b"%PDF-1.4 ticket" in upload.content
    assert str(message.url) == "https://graph.facebook.com/v23.0/1029384756/messages"
    sent = meta["messages"][0]
    assert sent["to"] == "919876543210"
    assert sent["template"]["components"][0]["parameters"][0]["document"] == {"id": "media-1", "filename": "ticket-MOA-1.pdf"}


async def test_trouble_reaching_meta_is_retried_but_a_lost_answer_is_not(session, monkeypatch, meta):
    t = whatsapp.event_reminder(name="A", event="B", when="C", venue="D", ticket_id="MOA-1")

    def failing(exc):
        def handler(request):
            raise exc("boom", request=request)
        monkeypatch.setattr(whatsapp, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    failing(httpx.ConnectError)
    with pytest.raises(whatsapp.Deferred):
        await whatsapp.send("9876543210", t)
    # The request went and no answer came: it may have been sent, so it is not tried again.
    failing(httpx.ReadTimeout)
    with pytest.raises(whatsapp.Unconfirmed):
        await whatsapp.send("9876543210", t)


async def test_nothing_is_sent_while_whatsapp_is_not_configured(session, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "", raising=False)
    assert not whatsapp.configured()
    with pytest.raises(whatsapp.Undeliverable):
        await whatsapp.send("9876543210", whatsapp.event_reminder(name="A", event="B", when="C", venue="D", ticket_id="MOA-1"))


# ─── The templates, exactly ──────────────────────────────────────────────


def test_ticket_confirmed_is_the_approved_template_with_its_values_in_order():
    t = whatsapp.ticket_confirmed(name="Ananya  Iyer", event="Build Night", when="Fri 20 Nov 2026, 6:30 PM", ticket_id="MOA-ABCDE-12345")
    body = whatsapp.payload("+919876543210", t, media_id="media-1", filename="ticket-MOA-ABCDE-12345.pdf")
    assert body == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "919876543210",
        "type": "template",
        "template": {
            "name": "ticket_confirmed",
            "language": {"code": "en"},
            "components": [
                {"type": "header", "parameters": [{"type": "document", "document": {"id": "media-1", "filename": "ticket-MOA-ABCDE-12345.pdf"}}]},
                {"type": "body", "parameters": [
                    {"type": "text", "text": "Ananya"},
                    {"type": "text", "text": "Build Night"},
                    {"type": "text", "text": "Fri 20 Nov 2026, 6:30 PM"},
                    {"type": "text", "text": "MOA-ABCDE-12345"},
                ]},
                {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "MOA-ABCDE-12345"}]},
            ],
        },
    }


def test_every_template_carries_the_values_its_wording_numbers():
    reminder = whatsapp.event_reminder(name="Bala", event="Build Night", when="Fri 20 Nov 2026, 6:30 PM", venue=None, ticket_id="MOA-1")
    assert (reminder.name, reminder.body, reminder.button, reminder.document) == (
        "event_reminder", ("Bala", "Build Night", "Fri 20 Nov 2026, 6:30 PM", "online"), "MOA-1", False,
    )
    cert = whatsapp.certificate_ready(name="Sita Raman", event="Build Night", club="Yukthi", number="CERT-AB2CD-EF3GH")
    assert (cert.name, cert.body, cert.button, cert.document) == (
        "certificate_ready", ("Sita", "Build Night", "Yukthi", "CERT-AB2CD-EF3GH"), "CERT-AB2CD-EF3GH", True,
    )
    pay = whatsapp.payment_pending(name="Dev", event="Build Night", amount=whatsapp.rupees_text("1499.5"), slug="build-night-x1")
    assert (pay.name, pay.body, pay.button) == ("payment_pending", ("Dev", "Build Night", "1,499.50"), "build-night-x1")
    update = whatsapp.event_update(name="Dev", club="Yukthi", event="Build Night", update="The hall moved to\nBlock B")
    assert (update.name, update.body, update.button) == ("event_update", ("Dev", "Yukthi", "Build Night", "The hall moved to Block B"), None)
    # No header component without a document; no button component without one.
    components = whatsapp.payload("+15551234567", update)["template"]["components"]
    assert [c["type"] for c in components] == ["body"]


def test_values_are_cleaned_as_meta_requires():
    t = whatsapp.ticket_confirmed(name="", event="A\tvery\n\nlong     name " + "x" * 300, when=" ", ticket_id="MOA-1")
    first, event, when, _ = t.body
    assert first == "there" and when == "the day"
    assert "\n" not in event and "\t" not in event and "  " not in event
    assert len(event) <= 200 and event.endswith("…")
    with pytest.raises(ValueError):
        whatsapp.payload("+919876543210", t)  # a document template without its document


def test_rupees_are_written_the_indian_way():
    assert [whatsapp.rupees_text(v) for v in ("0", "499", "1499", "1499.5", "123456", "12345678.25")] == [
        "0", "499", "1,499", "1,499.50", "1,23,456", "1,23,45,678.25",
    ]


@pytest.mark.parametrize("raw, expected", [
    ("9876543210", "+919876543210"),
    ("98765 43210", "+919876543210"),
    ("098765-43210", "+919876543210"),
    ("919876543210", "+919876543210"),
    ("+91 98765 43210", "+919876543210"),
    ("0091 9876543210", "+919876543210"),
    ("(+1) 650-555-1234", "+16505551234"),
    ("+44 7700 900123", "+447700900123"),
])
def test_numbers_become_e164_with_india_by_default(raw, expected):
    assert whatsapp.normalise(raw) == (expected, None)


@pytest.mark.parametrize("raw", [None, "", "   ", "12345", "5876543210", "+91 5876543210", "+91987654321", "98765O4321", "+0123456789", "2233 4455", "+1234567890123456"])
def test_numbers_that_cannot_be_reached_are_refused_with_a_reason(raw):
    number, why = whatsapp.normalise(raw)
    assert number is None and why


# ─── What Meta answers ───────────────────────────────────────────────────


def _err(code, status=400, message="x"):
    return whatsapp.refusal(status, {"error": {"code": code, "message": message}})


def test_meta_errors_become_skipped_failed_or_retried():
    dead = _err(131026)
    assert isinstance(dead, whatsapp.Undeliverable) and dead.permanent and dead.code == "131026"
    test_list = _err(131030)
    assert isinstance(test_list, whatsapp.Undeliverable) and not test_list.permanent
    for code in (130429, 131048, 131056, 80007, 131000):
        assert isinstance(_err(code), whatsapp.Deferred), code
    assert isinstance(whatsapp.refusal(503, {}), whatsapp.Deferred)
    assert isinstance(whatsapp.refusal(429, {}), whatsapp.Deferred)
    for code in (131047, 132000, 132001, 132012, 132015, 190, 10):
        err = _err(code)
        assert isinstance(err, whatsapp.Refused) and f"({code})" in err.reason, code
    assert "No approved template" in _err(132001).reason
    unknown = _err(999999, message="Something new")
    assert isinstance(unknown, whatsapp.Refused) and unknown.reason == "WhatsApp refused it: Something new (999999)"


def test_the_providers_list_says_truthfully_whether_whatsapp_is_on(monkeypatch):
    by = lambda: {p["channel"]: p for p in providers.providers()}  # noqa: E731
    assert by()["whatsapp"]["configured"] is False
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "t", raising=False)
    assert by()["whatsapp"]["configured"] is False  # the token alone sends nothing
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "1", raising=False)
    assert by()["whatsapp"]["configured"] is True and "no app secret" in by()["whatsapp"]["note"]
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "s", raising=False)
    assert "statuses arrive" in by()["whatsapp"]["note"]


# ─── Tickets ─────────────────────────────────────────────────────────────


async def test_unconfigured_the_channel_is_email_alone(client, session, world, outbox, background):
    r = await _send_tickets(client, world)
    assert r.status_code == 200, r.text
    assert r.json()["channels"] == ["email"]
    assert set(await _rows(session, world["asked"])) == {DeliveryChannel.email}


async def test_a_ticket_goes_by_whatsapp_to_whoever_asked_and_by_email_to_everyone(client, session, world, meta, outbox, background):
    r = await _send_tickets(client, world)
    assert r.status_code == 200, r.text
    assert r.json()["channels"] == ["email", "whatsapp"]
    assert len(outbox) == 4

    # Only Ananya asked and has a mobile number: one upload, one template.
    assert len(meta["messages"]) == 1 and len(meta["uploads"]) == 1
    sent = meta["messages"][0]
    assert sent["to"] == "919876543210" and sent["template"]["name"] == "ticket_confirmed"
    values = [p["text"] for p in sent["template"]["components"][1]["parameters"]]
    assert values == ["Ananya", "Build Night", deliveries._when(world["event"]), world["asked"].ticket_id]
    assert sent["template"]["components"][2]["parameters"][0]["text"] == world["asked"].ticket_id
    assert b"%PDF" in meta["uploads"][0]

    rows = await _rows(session, world["asked"])
    wa = rows[DeliveryChannel.whatsapp]
    assert (wa.status, wa.provider_id) == (DeliveryStatus.sent, "wamid.TEST1")
    attempt = await session.scalar(select(DeliveryAttempts).where(DeliveryAttempts.provider_id == "wamid.TEST1"))
    assert (attempt.provider, attempt.address, attempt.delivery_id) == ("whatsapp", "+919876543210", wa.id)
    # Nobody who did not ask has a WhatsApp row at all.
    assert set(await _rows(session, world["silent"])) == {DeliveryChannel.email}
    # Asked, but with no number, or one WhatsApp cannot reach: no row, or skipped with why.
    assert set(await _rows(session, world["no_phone"])) == {DeliveryChannel.email}
    landline = (await _rows(session, world["landline"]))[DeliveryChannel.whatsapp]
    assert landline.status is DeliveryStatus.skipped and landline.error.startswith("no WhatsApp number")

    # The attendee list carries both, email first.
    rows = (await client.get(f"/api/v1/events/registration/{world['event'].id}/list", headers=world["club"])).json()["items"]
    mine = next(row for row in rows if row["id"] == str(world["asked"].id))
    assert [d["channel"] for d in mine["ticket_deliveries"]] == ["email", "whatsapp"]
    summary = (await client.get(f"/api/v1/events/{world['event'].id}/deliveries/summary", headers=world["club"])).json()
    by = {c["channel"]: c for c in summary["channels"]}
    # Its audience is the two who asked with a number, not everyone.
    assert (by["whatsapp"]["sent"], by["whatsapp"]["skipped"], by["whatsapp"]["unsent"]) == (1, 1, 0)


async def test_whatsapp_is_never_sent_twice_even_when_the_email_is(client, session, world, meta, outbox, background):
    await _send_tickets(client, world)
    await _send_tickets(client, world)
    await _send_tickets(client, world, only="all")
    one = await client.post(f"/api/v1/events/{world['event'].id}/tickets/{world['asked'].id}/send", headers=world["club"])
    assert one.status_code == 200, one.text
    # The email went three more times (the button means it); WhatsApp once.
    assert len([m for m in outbox if m["recipients"] == ["wa-asked@example.com"]]) == 3
    assert len(meta["messages"]) == 1
    rows = await session.scalars(select(Deliveries).where(Deliveries.registration_id == world["asked"].id))
    assert sorted(d.channel.value for d in rows) == ["email", "whatsapp"]


async def test_a_ticket_sent_again_from_the_public_form_is_mail_alone(client, session, world, meta, outbox, background):
    """Anyone can type an address into the form and have its ticket sent
    again; that never sets off a WhatsApp message, even one that failed."""
    await _send_tickets(client, world)
    rows = await _rows(session, world["asked"])
    rows[DeliveryChannel.whatsapp].status = DeliveryStatus.failed
    rows[DeliveryChannel.email].sent_at = NOW - timedelta(hours=1)
    await session.commit()
    sent = len(outbox)
    assert await deliveries.resend_document(session, world["event"], world["asked"], DeliveryKind.ticket, inline=True)
    assert len(outbox) == sent + 1 and len(meta["messages"]) == 1
    assert (await _rows(session, world["asked"]))[DeliveryChannel.whatsapp].status is DeliveryStatus.failed


async def test_a_bounced_number_a_demo_account_and_a_withdrawn_consent_get_nothing(client, session, world, meta, outbox, background, make_user, make_registration):
    asked = world["asked"]
    asked.wa_bounced_at, asked.wa_bounce_reason = NOW, "WhatsApp says this number cannot receive messages (131026)"
    demo_user = await make_user(email="guest-1@demo.myotherapp.in")
    demo = await make_registration(event=world["event"], user=demo_user, full_name="Demo Guest", email=demo_user.email, phone="9876543299", whatsapp_opt_in=True)
    await session.commit()

    await _send_tickets(client, world)
    assert meta["messages"] == []
    assert (await _rows(session, asked))[DeliveryChannel.whatsapp].error == providers.WA_BOUNCED
    assert (await _rows(session, demo))[DeliveryChannel.whatsapp].error == providers.DEMO_ACCOUNT

    # A row made while they had asked, read after they stopped: skipped, with why.
    asked.wa_bounced_at = None
    asked.whatsapp_opt_in = False
    await session.commit()
    await session.refresh(asked)
    assert deliveries.whatsapp_skip(asked) == providers.NO_WHATSAPP_OPT_IN


async def test_meta_refusals_land_on_the_row_as_skipped_failed_or_retried(client, session, world, meta, outbox, background):
    asked = world["asked"]
    meta["answer"] = lambda body: httpx.Response(400, json={"error": {"code": 131026, "message": "Message undeliverable"}})
    await _send_tickets(client, world)
    wa = (await _rows(session, asked))[DeliveryChannel.whatsapp]
    await session.refresh(asked)
    assert wa.status is DeliveryStatus.skipped and "(131026)" in wa.error
    # Noted, not yet marked: a first 131026 can be an old app or terms not
    # accepted; a second marks the number (tests/test_club_whatsapp.py).
    assert asked.wa_bounced_at is None and "131026" in asked.wa_bounce_reason
    assert asked.wa_bounce_reason.startswith(whatsapp.FIRST_STRIKE)

    # Correcting the number clears the mark.
    fixed = await client.patch(f"/api/v1/events/registration/{world['event'].id}/info/{asked.id}", json={"phone": "9876500001"}, headers=world["club"])
    assert fixed.status_code == 200, fixed.text
    await session.refresh(asked)
    assert asked.wa_bounced_at is None and asked.wa_bounce_reason is None

    meta["answer"] = lambda body: httpx.Response(400, json={"error": {"code": 132001, "message": "Template name does not exist in the translation"}})
    await _send_tickets(client, world, only="failed")
    wa = (await _rows(session, asked))[DeliveryChannel.whatsapp]
    assert wa.status is DeliveryStatus.failed and wa.error == "No approved template by that name in this language (132001)"

    meta["answer"] = lambda body: httpx.Response(400, json={"error": {"code": 130429, "message": "Rate limit hit"}})
    await _send_tickets(client, world, only="failed")
    wa = (await _rows(session, asked))[DeliveryChannel.whatsapp]
    assert wa.status is DeliveryStatus.pending and wa.next_attempt_at is not None and "will try again" in wa.error


async def test_the_status_webhook_updates_the_whatsapp_send(client, session, world, meta, outbox, background, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "app-secret", raising=False)
    await _send_tickets(client, world)
    wa = (await _rows(session, world["asked"]))[DeliveryChannel.whatsapp]

    def post(statuses):
        body = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {"statuses": statuses}}]}]}).encode()
        sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        return client.post("/api/v1/hooks/whatsapp", content=body, headers={"content-type": "application/json", "x-hub-signature-256": sig})

    r = await post([{"id": wa.provider_id, "status": "delivered", "timestamp": str(int(NOW.timestamp()))}])
    assert r.json() == {"ok": True, "matched": 1}
    wa = (await _rows(session, world["asked"]))[DeliveryChannel.whatsapp]
    assert wa.outcome.value == "delivered"
    r = await post([{"id": wa.provider_id, "status": "read", "timestamp": str(int(NOW.timestamp()))}])
    wa = (await _rows(session, world["asked"]))[DeliveryChannel.whatsapp]
    assert wa.outcome.value == "read"
    log = (await client.get(f"/api/v1/events/{world['event'].id}/deliveries", params={"channel": "whatsapp"}, headers=world["club"])).json()
    row = next(i for i in log["items"] if i["registration_id"] == str(world["asked"].id))
    assert (row["status"], row["outcome"], row["address"]) == ("sent", "read", "98765 43210")


# ─── Certificates ────────────────────────────────────────────────────────


async def test_a_certificate_goes_with_its_own_number_never_the_ticket_id(client, session, world, meta, outbox, background):
    asked = world["asked"]
    asked.is_attended = True
    asked.certificate_issued_at = NOW
    await session.commit()
    st = certificate_starter("classic")
    r = await client.put(
        f"/api/v1/events/{world['event'].id}/designs/certificate/layout",
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])),
        headers=world["club"],
    )
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/v1/events/{world['event'].id}/certificates/send", json={"only": "unsent"}, headers=world["club"])
    assert r.status_code == 200, r.text
    sent = meta["messages"][0]
    assert sent["template"]["name"] == "certificate_ready"
    values = [p["text"] for p in sent["template"]["components"][1]["parameters"]]
    number = asked.certificate_no
    assert number and number.startswith("CERT-")
    assert values == ["Ananya", "Build Night", "Yukthi", number]
    assert sent["template"]["components"][2]["parameters"][0]["text"] == number
    assert asked.ticket_id not in json.dumps(sent)
    wa = (await _rows(session, asked, DeliveryKind.certificate))[DeliveryChannel.whatsapp]
    assert wa.status is DeliveryStatus.sent


# ─── The day-before reminder ─────────────────────────────────────────────


@pytest.fixture
async def tomorrow(session, make_club, make_event, make_user, make_registration):
    """An event starting in twenty hours, and its guests from a week back."""
    owner, club = await make_club(name="Reminder Club", email="wa-remind@example.com")
    starts = NOW + timedelta(hours=20)
    event = await make_event(club=club, created_by=owner, name="Demo Day", event_datetime=starts, location_name="Hall 2")
    week_ago = NOW - timedelta(days=7)
    regs = {}
    for key, opt_in, extra in (
        ("asked", True, {}),
        ("silent", False, {}),
        ("late", True, {"created_at": NOW - timedelta(hours=2)}),  # registered within the day
        ("revoked", True, {"ticket_revoked_at": NOW}),
    ):
        u = await make_user(email=f"remind-{key}@example.com")
        regs[key] = await make_registration(
            event=event, user=u, full_name=f"{key.title()} Guest", email=u.email, phone="9876512345",
            whatsapp_opt_in=opt_in, **{"created_at": week_ago, **extra},
        )
    await session.commit()
    return {"event": event, **regs}


async def test_the_reminder_goes_once_to_whoever_asked(session, tomorrow, meta, background):
    queued = await deliveries.queue_reminders(session)
    assert queued == 1
    await jobs.settle()

    assert len(meta["messages"]) == 1
    sent = meta["messages"][0]
    assert sent["template"]["name"] == "event_reminder"
    assert [p["text"] for p in sent["template"]["components"][0]["parameters"]] == [
        "Asked", "Demo Day", deliveries._when(tomorrow["event"]), "Hall 2",
    ]
    assert sent["template"]["components"][1]["parameters"][0]["text"] == tomorrow["asked"].ticket_id
    row = (await _rows(session, tomorrow["asked"], DeliveryKind.reminder))[DeliveryChannel.whatsapp]
    assert row.status is DeliveryStatus.sent and row.provider_id == "wamid.TEST1"

    # Every later minute finds nothing to do; nobody gets a second.
    assert await deliveries.queue_reminders(session) == 0
    assert await deliveries.queue_reminders(session, NOW + timedelta(hours=5)) == 0
    await jobs.settle()
    assert len(meta["messages"]) == 1
    for key in ("silent", "late", "revoked"):
        assert await _rows(session, tomorrow[key], DeliveryKind.reminder) == {}, key


async def test_the_reminder_waits_for_its_day_and_never_comes_at_the_last_minute(session, tomorrow, meta, background):
    starts = tomorrow["event"].event_datetime
    # Two days out: not yet.
    assert await deliveries.queue_reminders(session, starts - timedelta(hours=48)) == 0
    # Forty minutes before the start (a sweep that was down): too late.
    assert await deliveries.queue_reminders(session, starts - timedelta(minutes=40)) == 0
    assert meta["messages"] == []


async def test_no_reminder_without_whatsapp_or_on_an_unpaid_ticket(session, tomorrow, monkeypatch, meta, background):
    tomorrow["event"].has_fee = True
    await session.commit()
    # Paid event, nobody has paid: no ticket, no reminder.
    assert await deliveries.queue_reminders(session) == 0
    tomorrow["event"].has_fee = False
    await session.commit()
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "", raising=False)
    assert await deliveries.queue_reminders(session) == 0
    assert meta["messages"] == []


async def test_the_minute_sweep_queues_the_reminders(session, tomorrow, meta, background):
    from app.api.emails import service as emails

    assert await emails.whatsapp_reminders(session) == 1
    await jobs.settle()
    assert len(meta["messages"]) == 1


# ─── The opt-in, through the registration API ────────────────────────────


async def _open_event(session, make_club, make_event):
    owner, club = await make_club(name="Open Club", email="wa-open@example.com")
    event = await make_event(club=club, created_by=owner, name="Rust Meetup", event_datetime=NOW + timedelta(days=5))
    await session.commit()
    return event


async def _registered(session, registration_id) -> EventRegistrationsLink:
    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == registration_id).execution_options(populate_existing=True)
    )


async def test_the_tick_on_the_form_is_kept_and_unticked_is_the_default(client, session, make_club, make_event):
    event = await _open_event(session, make_club, make_event)
    url = f"/api/v1/events/registration/{event.id}/register"

    yes = await client.post(url, json={"full_name": "Asha Menon", "email": "asha-wa@example.com", "phone": "98765 00000", "whatsapp_opt_in": True})
    assert yes.status_code == 200, yes.text
    assert yes.json()["whatsapp_opt_in"] is True
    reg = await _registered(session, yes.json()["event_registration_id"])
    assert reg.whatsapp_opt_in is True and reg.whatsapp_opt_in_at is not None

    unsaid = await client.post(url, json={"full_name": "Ravi Kumar", "email": "ravi-wa@example.com", "phone": "9876500001"})
    assert unsaid.status_code == 200, unsaid.text
    assert unsaid.json()["whatsapp_opt_in"] is False
    reg = await _registered(session, unsaid.json()["event_registration_id"])
    assert reg.whatsapp_opt_in is False and reg.whatsapp_opt_in_at is None

    # Ticked with no number WhatsApp can reach: said now, not taken and wasted.
    bad = await client.post(url, json={"full_name": "Meera Das", "email": "meera-wa@example.com", "phone": "12345", "whatsapp_opt_in": True})
    assert bad.status_code == 422, bad.text
    assert bad.json()["error_code"] == "WHATSAPP_NUMBER" and "phone" in bad.json()["errors"]
    none = await client.post(url, json={"full_name": "Meera Das", "email": "meera-wa@example.com", "whatsapp_opt_in": True})
    assert none.status_code == 422


async def test_a_signed_in_registration_takes_the_tick_too(client, session, make_club, make_event, make_user, mint_token):
    event = await _open_event(session, make_club, make_event)
    user = await make_user(email="signed-wa@example.com")
    r = await client.post(
        f"/api/v1/events/registration/{event.id}/register",
        json={"full_name": "Signed User", "email": "signed-wa@example.com", "phone": "+91 98765 11111", "whatsapp_opt_in": True},
        headers=auth_header(await mint_token(user)),
    )
    assert r.status_code == 200, r.text
    reg = await _registered(session, r.json()["event_registration_id"])
    assert reg.whatsapp_opt_in is True and reg.user_id == user.id and reg.phone == "+91 98765 11111"


async def test_existing_registrations_are_never_opted_in_by_anyone_but_the_guest(session, make_club, make_event, make_user, make_registration):
    event = await _open_event(session, make_club, make_event)
    u = await make_user(email="old-wa@example.com")
    reg = await make_registration(event=event, user=u, phone="9876543210")
    await session.commit()
    await session.refresh(reg)
    assert reg.whatsapp_opt_in is False and reg.whatsapp_opt_in_at is None


# ─── The test-send script ────────────────────────────────────────────────


def test_the_test_script_will_not_run_without_a_number(capsys):
    from scripts import whatsapp_test

    with pytest.raises(SystemExit) as stopped:
        whatsapp_test.main(["--template", "ticket_confirmed"])
    assert stopped.value.code == 2 and "--to" in capsys.readouterr().err


async def test_the_test_script_sends_one_sample_template(session, meta):
    import io

    from scripts import whatsapp_test

    out, err = io.StringIO(), io.StringIO()
    assert await whatsapp_test.run("98765 43210", "certificate_ready", out=out, err=err) == 0
    assert json.loads(out.getvalue()) == {"sent": True, "template": "certificate_ready", "to": "+919876543210", "message_id": "wamid.TEST1"}
    assert len(meta["messages"]) == 1 and b"%PDF" in meta["uploads"][0]
    assert [p["text"] for p in meta["messages"][0]["template"]["components"][1]["parameters"]][3] == "CERT-TEST0-00001"
    # A number it cannot reach, or no keys: nothing goes, and it says why.
    assert await whatsapp_test.run("12345", "event_reminder", out=out, err=err) == 2
    assert len(meta["messages"]) == 1


async def test_the_test_script_refuses_while_unconfigured(session):
    import io

    from scripts import whatsapp_test

    out, err = io.StringIO(), io.StringIO()
    assert await whatsapp_test.run("9876543210", "event_reminder", out=out, err=err) == 2
    assert "not configured" in err.getvalue() and out.getvalue() == ""
