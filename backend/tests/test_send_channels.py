"""Email, WhatsApp or both: the event's choice per kind, a send's own choice,
and the rule that holds for both -- whoever WhatsApp can't reach gets the email.

World (tests/test_whatsapp.py): asked (opted in, a mobile number), silent
(not opted in), no_phone (opted in, no number), landline (opted in, a number
WhatsApp can't reach).
"""
import hashlib
import hmac
import json

import httpx
from sqlalchemy import select

from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.config import settings
from tests.conftest import auth_header
from tests.test_door_privacy import _member_header
from tests.test_whatsapp import NOW, _rows, background, meta, outbox, world  # noqa: F401 - fixtures

E, W = DeliveryChannel.email, DeliveryChannel.whatsapp


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}{path}"


async def _set(client, world, **modes):
    body = {"ticket": "both", "reminder": "both", "certificate": "both", **modes}
    r = await client.put(_url(world, "/delivery-channels"), json=body, headers=world["club"])
    assert r.status_code == 200, r.text
    return r.json()


def _mails_to(outbox, key):
    return [m for m in outbox if m["recipients"] == [f"wa-{key}@example.com"]]


# ─── The setting ─────────────────────────────────────────────────────────


async def test_an_event_starts_on_both_and_says_whether_whatsapp_is_there(client, world):
    r = await client.get(_url(world, "/delivery-channels"), headers=world["club"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k: body[k] for k in ("ticket", "reminder", "certificate")} == {"ticket": "both", "reminder": "both", "certificate": "both"}
    assert body["whatsapp_available"] is False and body["whatsapp_note"]


async def test_the_setting_saves_and_reads_back(client, world, meta):
    saved = await _set(client, world, ticket="whatsapp", certificate="email")
    assert (saved["ticket"], saved["reminder"], saved["certificate"]) == ("whatsapp", "both", "email")
    assert saved["whatsapp_available"] is True and saved["whatsapp_note"] is None
    again = (await client.get(_url(world, "/delivery-channels"), headers=world["club"])).json()
    assert again == saved
    bad = await client.put(_url(world, "/delivery-channels"), json={"ticket": "sms"}, headers=world["club"])
    assert bad.status_code == 422


async def test_another_club_and_door_staff_cannot_change_it(client, session, world, make_club, mint_token):
    other_owner, _ = await make_club(name="Elsewhere", email="elsewhere@example.com")
    await session.commit()
    other = auth_header(await mint_token(other_owner))
    assert (await client.get(_url(world, "/delivery-channels"), headers=other)).status_code in (403, 404)
    assert (await client.put(_url(world, "/delivery-channels"), json={"ticket": "email"}, headers=other)).status_code in (403, 404)

    event = world["event"]
    await session.refresh(event, ["club"])
    from app.api.users.models import Users

    owner = await session.get(Users, event.club.user_id)
    door = await _member_header(session, owner, event.club, "door")
    assert (await client.put(_url(world, "/delivery-channels"), json={"ticket": "email"}, headers=door)).status_code == 403
    await session.refresh(event)
    assert event.delivery_channels is None


# ─── Each mode ───────────────────────────────────────────────────────────


async def test_both_is_todays_behaviour(client, session, world, meta, outbox, background):
    r = await client.post(_url(world, "/tickets/send"), json={}, headers=world["club"])
    assert r.json()["channels"] == ["email", "whatsapp"]
    assert len(outbox) == 4 and len(meta["messages"]) == 1


async def test_email_sends_no_whatsapp(client, session, world, meta, outbox, background):
    await _set(client, world, ticket="email")
    r = await client.post(_url(world, "/tickets/send"), json={}, headers=world["club"])
    assert r.json()["channels"] == ["email"]
    assert len(outbox) == 4 and meta["messages"] == []
    assert set(await _rows(session, world["asked"])) == {E}


async def test_whatsapp_emails_only_whoever_whatsapp_cannot_reach(client, session, world, meta, outbox, background):
    await _set(client, world, ticket="whatsapp")
    r = await client.post(_url(world, "/tickets/send"), json={}, headers=world["club"])
    assert r.status_code == 200, r.text

    # Ananya: WhatsApp alone.
    assert len(meta["messages"]) == 1 and _mails_to(outbox, "asked") == []
    assert set(await _rows(session, world["asked"])) == {W}
    # Not opted in, or no number: the email, with why on the row.
    silent = (await _rows(session, world["silent"]))[E]
    assert silent.status is DeliveryStatus.sent and silent.fallback_code == deliveries.EMAIL_NO_OPT_IN[0]
    no_phone = (await _rows(session, world["no_phone"]))[E]
    assert no_phone.fallback_code == deliveries.EMAIL_NO_NUMBER[0]
    # A number WhatsApp can't use: skipped there, and the email went instead.
    landline = await _rows(session, world["landline"])
    assert landline[W].status is DeliveryStatus.skipped
    assert landline[E].status is DeliveryStatus.sent and landline[E].fallback_code == deliveries.EMAIL_WA_FAILED
    assert "WhatsApp couldn't reach them" in landline[E].fallback_reason
    assert len(outbox) == 3

    # The log says which channel was used, and why.
    log = (await client.get(_url(world, "/deliveries"), params={"channel": "email"}, headers=world["club"])).json()
    row = next(i for i in log["items"] if i["registration_id"] == str(world["landline"].id))
    assert row["channel"] == "email" and row["fallback_code"] == deliveries.EMAIL_WA_FAILED


async def test_whatsapp_without_whatsapp_set_up_is_the_email_for_everyone(client, session, world, outbox, background):
    await _set(client, world, ticket="whatsapp")
    r = await client.post(_url(world, "/tickets/send"), json={}, headers=world["club"])
    assert r.json()["channels"] == ["email"]
    assert len(outbox) == 4
    assert (await _rows(session, world["asked"]))[E].fallback_code == deliveries.EMAIL_NO_WHATSAPP[0]


async def test_a_number_meta_refuses_gets_the_email(client, session, world, meta, outbox, background):
    meta["answer"] = lambda body: httpx.Response(400, json={"error": {"code": 131026, "message": "Message undeliverable"}})
    await _set(client, world, ticket="whatsapp")
    await client.post(_url(world, "/tickets/send"), json={}, headers=world["club"])
    rows = await _rows(session, world["asked"])
    assert rows[W].status is DeliveryStatus.skipped
    assert rows[E].status is DeliveryStatus.sent and len(_mails_to(outbox, "asked")) == 1


async def test_a_later_bounce_from_meta_sends_the_email(client, session, world, meta, outbox, background, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "app-secret", raising=False)
    await _set(client, world, ticket="whatsapp")
    await client.post(_url(world, "/tickets/send"), json={}, headers=world["club"])
    wa = (await _rows(session, world["asked"]))[W]
    assert wa.status is DeliveryStatus.sent and wa.email_instead is True and _mails_to(outbox, "asked") == []

    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {"statuses": [
        {"id": wa.provider_id, "status": "failed", "timestamp": str(int(NOW.timestamp())),
         "errors": [{"code": 131026, "title": "Message undeliverable"}]},
    ]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    r = await client.post("/api/v1/hooks/whatsapp", content=body, headers={"content-type": "application/json", "x-hub-signature-256": sig})
    assert r.status_code in (200, 202), r.text
    email = (await _rows(session, world["asked"])).get(E)
    if email is not None and email.status is DeliveryStatus.pending:
        await deliveries.deliver(session, email.id)
        email = (await _rows(session, world["asked"]))[E]
    # Meta's 131026 is a permanent failure: the email goes in its place.
    assert email is not None and email.status is DeliveryStatus.sent and email.fallback_code == deliveries.EMAIL_WA_FAILED


async def test_both_mode_never_falls_back_twice(client, session, world, meta, outbox, background):
    # Both: the landline guest already has the email; the WhatsApp skip adds nothing.
    await client.post(_url(world, "/tickets/send"), json={}, headers=world["club"])
    assert len(_mails_to(outbox, "landline")) == 1
    assert (await _rows(session, world["landline"]))[E].fallback_code is None


# ─── Manual sends ────────────────────────────────────────────────────────


async def test_a_send_can_choose_its_own_channels(client, session, world, meta, outbox, background):
    await _set(client, world, ticket="email")
    # The event says email; this send says both.
    r = await client.post(_url(world, "/tickets/send"), json={"channels": "both"}, headers=world["club"])
    assert r.json()["channels"] == ["email", "whatsapp"] and len(meta["messages"]) == 1
    bad = await client.post(_url(world, "/tickets/send"), json={"channels": "sms"}, headers=world["club"])
    assert bad.status_code == 422


async def test_one_guest_by_email_alone_or_whatsapp(client, session, world, meta, outbox, background):
    one = _url(world, f"/tickets/{world['asked'].id}/send")
    # No body: the event's setting (both).
    r = await client.post(one, headers=world["club"])
    assert r.status_code == 200 and r.json()["channels"] == ["email", "whatsapp"]
    r = await client.post(one, json={"channels": "email"}, headers=world["club"])
    assert r.json()["channels"] == ["email"] and len(meta["messages"]) == 1
    assert len(_mails_to(outbox, "asked")) == 2

    # Someone who didn't opt in, sent "by WhatsApp": the email still goes.
    r = await client.post(_url(world, f"/tickets/{world['silent'].id}/send"), json={"channels": "whatsapp"}, headers=world["club"])
    assert r.status_code == 200, r.text
    assert len(_mails_to(outbox, "silent")) == 1
    assert (await _rows(session, world["silent"]))[E].fallback_code == deliveries.EMAIL_NO_OPT_IN[0]


async def test_certificates_follow_the_setting_and_the_override(client, session, world, meta, outbox, background):
    for key in ("asked", "silent"):
        world[key].is_attended = True
        world[key].certificate_issued_at = NOW
    await session.commit()
    from app.core.layout.model import TicketLayoutFields, dump
    from tests.legacy_layouts import certificate_starter

    st = certificate_starter("classic")
    r = await client.put(_url(world, "/designs/certificate/layout"),
                         json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=world["club"])
    assert r.status_code == 200, r.text
    await _set(client, world, certificate="whatsapp")
    r = await client.post(_url(world, "/certificates/send"), json={"only": "unsent"}, headers=world["club"])
    assert r.status_code == 200, r.text
    assert set(await _rows(session, world["asked"], DeliveryKind.certificate)) == {W}
    assert set(await _rows(session, world["silent"], DeliveryKind.certificate)) == {E}

    r = await client.post(_url(world, f"/certificates/{world['asked'].id}/send"), json={"channels": "email"}, headers=world["club"])
    assert r.status_code == 200 and r.json()["channels"] == ["email"]
    assert (await _rows(session, world["asked"], DeliveryKind.certificate))[E].status is DeliveryStatus.sent


async def test_reminders_on_email_alone_queue_no_whatsapp(client, session, world, meta):
    from datetime import timedelta

    from app.api.closeout.models import EventStatus

    event = world["event"]
    event.event_datetime = deliveries.pipeline.now() + timedelta(hours=12)
    event.lifecycle = EventStatus.published
    asked = world["asked"]
    asked.created_at = deliveries.pipeline.now() - timedelta(days=3)
    await session.commit()
    await _set(client, world, reminder="email")
    assert await deliveries.queue_reminders(session) == 0
    await _set(client, world, reminder="both")
    assert await deliveries.queue_reminders(session) == 1
    rows = (await session.scalars(select(Deliveries).where(Deliveries.kind == DeliveryKind.reminder))).all()
    assert [r.registration_id for r in rows] == [asked.id]
