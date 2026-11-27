"""The admin WhatsApp inbox for MyOtherApp's own number (app/api/platform_whatsapp).

What people write to the platform's number arrives on the platform's own
webhook (/hooks/whatsapp), signed with its app secret; it is kept, labelled
with the event it is probably about (the latest automatic send to that
number), and answered by an admin: free text inside the 24-hour window, an
approved template outside it, nothing after STOP. Meta is faked."""
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.deliveries.models import Deliveries, DeliveryAttempts, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.api.platform_whatsapp.models import PlatformWaConversations, PlatformWaMessages
from app.api.users.models import AdminRoles
from app.config import settings
from app.core import whatsapp
from tests.conftest import auth_header

API = "/api/v1"
INBOX = f"{API}/admin/whatsapp/inbox"
HOOK = f"{API}/hooks/whatsapp"
SECRET = "platform-app-secret"
NUMBER_ID = "1029384756"
GUEST = "919876543210"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def meta(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", SECRET, raising=False)
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "test-token", raising=False)
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", NUMBER_ID, raising=False)
    sent: list = []

    def answer(request: httpx.Request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.out{len(sent)}"}]})

    monkeypatch.setattr(whatsapp, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    return sent


@pytest.fixture
async def world(session, make_club, make_event, make_registration, make_user, make_admin, mint_token):
    club_user, club = await make_club(name="GDG Cloud Trivandrum", email="gdg-pwa@example.com")
    event = await make_event(club=club, name="DevFest x CCD 2026", created_by=club_user)
    guest = await make_user(email="guest-pwa@example.com")
    reg = await make_registration(event=event, user=guest, phone="+91 98765 43210")
    delivery = Deliveries(event_id=event.id, registration_id=reg.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.whatsapp,
                          status=DeliveryStatus.sent, provider_id="wamid.ticket1", via="platform")
    session.add(delivery)
    await session.flush()
    session.add(DeliveryAttempts(provider="whatsapp", provider_id="wamid.ticket1", delivery_id=delivery.id, address=GUEST,
                                 via="platform", created_at=NOW - timedelta(days=2)))
    support = await make_admin(email="support-pwa@example.com", role=AdminRoles.support)
    support.full_name = "Sita Support"
    viewer = await make_admin(email="viewer-pwa@example.com", role=AdminRoles.viewer)
    await session.commit()
    return SimpleNamespace(
        club=club, event=event, club_h=auth_header(await mint_token(club_user)), guest_h=auth_header(await mint_token(guest)),
        h=auth_header(await mint_token(support)), viewer_h=auth_header(await mint_token(viewer)), support=support,
    )


def inbound(text="Where is the venue?", *, wamid=None, at=None, number=NUMBER_ID, frm=GUEST, name="Asha", kind="text", extra=None):
    message = {"from": frm, "id": wamid or f"wamid.in{uuid.uuid4().hex[:8]}", "timestamp": str(int((at or NOW).timestamp())),
               "type": kind}
    if kind == "text":
        message["text"] = {"body": text}
    message.update(extra or {})
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": number},
        "contacts": [{"wa_id": frm, "profile": {"name": name}}], "messages": [message]}}]}]}


async def hook(client, payload, secret=SECRET):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return await client.post(HOOK, content=body, headers={"content-type": "application/json", "x-hub-signature-256": sig})


async def conv_of(session, wa_id=GUEST):
    return await session.scalar(select(PlatformWaConversations).where(PlatformWaConversations.wa_id == wa_id)
                                .execution_options(populate_existing=True))


async def test_an_inbound_message_is_kept_and_labelled_with_the_event(client, session, world, meta):
    r = await hook(client, inbound())
    assert r.status_code == 202 and r.json()["inbox"]["messages"] == 1
    # The same delivery again keeps nothing twice.
    conv = await conv_of(session)
    assert conv.profile_name == "Asha" and conv.unread_count == 1
    assert conv.about_event_id == world.event.id and conv.about_club_id == world.club.id

    listing = (await client.get(INBOX, headers=world.h)).json()
    assert listing["unread_conversations"] == 1 and listing["items"][0]["phone"] == f"+{GUEST}"
    assert listing["items"][0]["about"] == {"event_id": world.event.id, "event_name": "DevFest x CCD 2026",
                                            "club_id": world.club.id, "club_name": "GDG Cloud Trivandrum"}
    assert listing["items"][0]["can_send"]["text"] is True

    t = (await client.get(f"{INBOX}/{conv.id}", headers=world.h)).json()
    first, second = t["items"]
    # The automatic ticket send, from the delivery log, labelled.
    assert first["automatic"] and first["direction"] == "out"
    assert (first["label"]["event_name"], first["label"]["club_name"], first["label"]["kind_label"]) == (
        "DevFest x CCD 2026", "GDG Cloud Trivandrum", "Ticket")
    assert second["direction"] == "in" and second["body"] == "Where is the venue?"
    assert second["probably_about"]["event_id"] == world.event.id

    # Filters.
    assert (await client.get(INBOX, params={"club_id": world.club.id + 999}, headers=world.h)).json()["total"] == 0
    assert (await client.get(INBOX, params={"event_id": world.event.id}, headers=world.h)).json()["total"] == 1
    assert (await client.get(INBOX, params={"q": "asha"}, headers=world.h)).json()["total"] == 1
    assert (await client.get(INBOX, params={"q": "98765"}, headers=world.h)).json()["total"] == 1
    assert (await client.get(INBOX, params={"q": "nobody"}, headers=world.h)).json()["total"] == 0

    r = await client.post(f"{INBOX}/{conv.id}/read", headers=world.h)
    assert r.status_code == 200 and r.json()["unread_conversations"] == 0
    assert (await client.get(f"{INBOX}/unread", headers=world.h)).json() == {"unread_conversations": 0, "unread_messages": 0}


async def test_media_keeps_its_metadata_and_a_repeat_is_kept_once(client, session, world, meta):
    payload = inbound(kind="image", wamid="wamid.photo", extra={"image": {"id": "media1", "mime_type": "image/jpeg", "caption": "my ticket"}})
    await hook(client, payload)
    await hook(client, payload)
    rows = (await session.scalars(select(PlatformWaMessages))).all()
    assert len(rows) == 1 and rows[0].media_id == "media1" and rows[0].media_mime == "image/jpeg" and rows[0].body == "my ticket"


async def test_a_bad_signature_keeps_nothing(client, session, world, meta):
    r = await hook(client, inbound(), secret="someone-else")
    assert r.json() == {"ok": False, "reason": "signature"}
    assert await conv_of(session) is None


async def test_a_club_numbers_messages_are_never_kept_here(client, session, world, meta):
    r = await hook(client, inbound(number="555000111"))
    assert r.status_code == 202 and r.json()["inbox"]["messages"] == 0
    assert await conv_of(session) is None
    assert (await client.get(INBOX, headers=world.h)).json()["total"] == 0


async def test_text_inside_the_window_and_who_sent_it(client, session, world, meta):
    await hook(client, inbound())
    conv = await conv_of(session)
    r = await client.post(f"{INBOX}/{conv.id}/messages", json={"mode": "text", "text": "Hall B, 9 am."}, headers=world.h)
    assert r.status_code == 200, r.text
    out = r.json()["message"]
    assert out["status"] == "sent" and out["sent_by"] == "Sita Support"
    assert out["label"]["event_name"] == "DevFest x CCD 2026" and out["label"]["kind_label"] == "Manual reply"
    assert meta[-1]["type"] == "text" and meta[-1]["to"] == GUEST and meta[-1]["text"]["body"] == "Hall B, 9 am."
    row = await session.scalar(select(PlatformWaMessages).where(PlatformWaMessages.direction == "out"))
    assert row.sent_by_user_id == world.support.id
    log = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "platform_whatsapp.reply"))
    assert log is not None and log.actor_id == world.support.id and "Hall B" not in json.dumps(log.details)

    # Meta's later status moves it forward.
    status = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": NUMBER_ID}, "statuses": [{"id": row.wamid, "status": "read", "timestamp": "1"}]}}]}]}
    await hook(client, status)
    await session.refresh(row)
    assert row.status == "read"


async def test_outside_the_window_text_is_refused_and_a_template_goes(client, session, world, meta):
    await hook(client, inbound(at=NOW - timedelta(hours=30)))
    conv = await conv_of(session)
    r = await client.post(f"{INBOX}/{conv.id}/messages", json={"mode": "text", "text": "hi"}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "WINDOW_CLOSED"
    assert meta == []

    templates = (await client.get(f"{INBOX}/templates", headers=world.h)).json()["templates"]
    assert {t["name"] for t in templates} == {"event_reminder", "payment_pending", "event_update"}

    bad = await client.post(f"{INBOX}/{conv.id}/messages", json={"mode": "template", "template": "ticket_confirmed"}, headers=world.h)
    assert bad.status_code == 422 and bad.json()["error_code"] == "UNKNOWN_TEMPLATE"
    short = await client.post(f"{INBOX}/{conv.id}/messages", json={"mode": "template", "template": "event_update",
                                                                    "params": {"name": "Asha"}}, headers=world.h)
    assert short.status_code == 422 and short.json()["error_code"] == "MISSING_PARAMS"

    r = await client.post(f"{INBOX}/{conv.id}/messages", json={"mode": "template", "template": "event_update", "params": {
        "name": "Asha", "club": "GDG Cloud Trivandrum", "event": "DevFest x CCD 2026", "update": "Doors open at 8"}}, headers=world.h)
    assert r.status_code == 200, r.text
    assert meta[-1]["type"] == "template" and meta[-1]["template"]["name"] == "event_update"
    assert r.json()["can_send"]["text"] is False and r.json()["can_send"]["template"] is True


async def test_stop_is_honoured_until_start(client, session, world, meta):
    await hook(client, inbound("STOP"))
    conv = await conv_of(session)
    assert conv.opted_out_at is not None
    r = await client.post(f"{INBOX}/{conv.id}/messages", json={"mode": "text", "text": "ok"}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "OPTED_OUT"
    r = await client.post(f"{INBOX}/{conv.id}/messages", json={"mode": "template", "template": "event_update", "params": {
        "name": "A", "club": "C", "event": "E", "update": "U"}}, headers=world.h)
    assert r.status_code == 409 and meta == []
    await hook(client, inbound("start"))
    assert (await conv_of(session)).opted_out_at is None
    assert (await client.post(f"{INBOX}/{conv.id}/messages", json={"mode": "text", "text": "ok"}, headers=world.h)).status_code == 200


async def test_only_admins_with_the_inbox_see_it(client, session, world, meta):
    await hook(client, inbound())
    conv = await conv_of(session)
    for h in (world.club_h, world.guest_h, world.viewer_h, {}):
        assert (await client.get(INBOX, headers=h)).status_code in (401, 403)
        assert (await client.get(f"{INBOX}/{conv.id}", headers=h)).status_code in (401, 403)
        r = await client.post(f"{INBOX}/{conv.id}/messages", json={"mode": "text", "text": "x"}, headers=h)
        assert r.status_code in (401, 403)
    assert meta == []
