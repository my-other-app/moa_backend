"""What the providers say became of a mail, recorded against the send; bounced addresses skipped afterwards."""
import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from app.api.deliveries import service as deliveries_service
from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.api.hooks import sns
from app.config import settings
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from app.core.email import email as email_mod
from tests.conftest import auth_header

CERT_URL = "https://sns.ap-south-1.amazonaws.com/SimpleNotificationService-abc123.pem"


@pytest.fixture
def amazon(monkeypatch):
    """A key pair standing in for Amazon's, and a fetch that hands out its certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1)).not_valid_after(datetime.now(timezone.utc) + timedelta(days=1)).sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        if url == CERT_URL:
            return pem
        return "ok"

    monkeypatch.setattr(sns, "fetch", fetch)
    sns._certs.clear()

    def envelope(kind: str, message: str, **more) -> dict:
        msg = {"Type": kind, "MessageId": str(uuid.uuid4()), "TopicArn": "arn:aws:sns:ap-south-1:1:ses", "Message": message, "Timestamp": datetime.now(timezone.utc).isoformat(), "SignatureVersion": "1", "SigningCertURL": CERT_URL, **more}
        text = sns.canonical(msg)
        msg["Signature"] = base64.b64encode(key.sign(text.encode(), padding.PKCS1v15(), hashes.SHA1())).decode()
        return msg

    return {"envelope": envelope, "fetched": fetched, "key": key}


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
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="hooks@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=5), duration=3.0)
    a = await make_user(email="ananya-h@example.com")
    reg = await make_registration(event=event, user=a, full_name="Ananya Iyer", email="ananya-h@example.com")
    d = Deliveries(id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email, status=DeliveryStatus.sent, provider_id="ses-abc", sent_at=datetime.now(timezone.utc))
    session.add(d)
    await session.commit()
    club_h = auth_header(await mint_token(owner))
    # A designed ticket, so a resend has something to render.
    st = ticket_starter("classic")
    saved = await client.put(f"/api/v1/events/{event.id}/designs/ticket/layout", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=club_h)
    assert saved.status_code == 200, saved.text
    return {"event": event, "reg": reg, "delivery": d, "club": club_h}


def ses_message(kind: str, message_id: str, address: str, **more) -> str:
    body = {"notificationType": kind, "mail": {"messageId": message_id, "timestamp": datetime.now(timezone.utc).isoformat(), "destination": [address]}}
    if kind == "Bounce":
        body["bounce"] = {"bounceType": more.get("bounce_type", "Permanent"), "bounceSubType": more.get("sub", "General"), "bouncedRecipients": [{"emailAddress": address, "diagnosticCode": more.get("diag", "smtp; 550 5.1.1 user unknown")}], "timestamp": datetime.now(timezone.utc).isoformat()}
    elif kind == "Delivery":
        body["delivery"] = {"recipients": [address], "timestamp": datetime.now(timezone.utc).isoformat()}
    elif kind == "Complaint":
        body["complaint"] = {"complainedRecipients": [{"emailAddress": address}], "complaintFeedbackType": "abuse", "timestamp": datetime.now(timezone.utc).isoformat()}
    return json.dumps(body)


async def test_a_bounce_lands_on_the_send_marks_the_address_and_later_sends_leave_it_alone(client, session, world, amazon, outbox, monkeypatch):
    monkeypatch.setattr(settings, "SES_WEBHOOK_TOKEN", "hooktoken", raising=False)
    reg, d = world["reg"], world["delivery"]
    # The first call subscribes: verified, then confirmed by fetching Amazon's URL.
    sub = amazon["envelope"]("SubscriptionConfirmation", "You have chosen to subscribe", SubscribeURL="https://sns.ap-south-1.amazonaws.com/?Action=ConfirmSubscription&Token=t1", Token="t1")
    r = await client.post("/api/v1/hooks/ses", params={"token": "hooktoken"}, content=json.dumps(sub), headers={"content-type": "text/plain"})
    assert r.status_code == 202 and r.json() == {"ok": True, "matched": 0}
    assert amazon["fetched"][-1].startswith("https://sns.ap-south-1.amazonaws.com/?Action=ConfirmSubscription")

    # A hard bounce: the send carries the verdict, the registration carries the mark.
    bounce = amazon["envelope"]("Notification", ses_message("Bounce", "ses-abc", reg.email))
    r = await client.post("/api/v1/hooks/ses", params={"token": "hooktoken"}, content=json.dumps(bounce), headers={"content-type": "text/plain"})
    assert r.status_code == 202 and r.json() == {"ok": True, "matched": 1}
    await session.refresh(d)
    await session.refresh(reg)
    assert d.outcome.value == "bounced" and d.reason == "smtp; 550 5.1.1 user unknown" and d.reason_code == "General" and d.outcome_at is not None
    assert reg.email_bounced_at is not None and reg.email_bounce_reason == "smtp; 550 5.1.1 user unknown"

    # A late "delivered" never undoes a bounce.
    late = amazon["envelope"]("Notification", ses_message("Delivery", "ses-abc", reg.email))
    assert (await client.post("/api/v1/hooks/ses", params={"token": "hooktoken"}, content=json.dumps(late), headers={"content-type": "text/plain"})).json() == {"ok": True, "matched": 0}
    await session.refresh(d)
    assert d.outcome.value == "bounced"

    # Sending again is skipped for the bounced address, with the reason; nothing goes to SES.
    h = world["club"]
    again = await client.post(f"/api/v1/events/{world['event'].id}/tickets/{reg.id}/send", headers=h)
    assert again.status_code == 200, again.text
    # The worker, driven here rather than waited for.
    await deliveries_service.deliver(session, d.id)
    await session.refresh(d)
    assert d.status is DeliveryStatus.skipped and "bounced" in (d.error or "") and outbox == []
    # The attendees list says so.
    rows = await client.get(f"/api/v1/events/registration/{world['event'].id}/list", headers=h)
    assert rows.status_code == 200, rows.text
    mine = next(x for x in rows.json()["items"] if x["id"] == str(reg.id))
    assert mine["email_bounced_at"] and "user unknown" in mine["email_bounce_reason"]

    # Correcting the address clears the mark, and the next send goes out.
    fixed = await client.patch(f"/api/v1/events/registration/{world['event'].id}/info/{reg.id}", json={"email": "ananya.iyer@example.com"}, headers=h)
    assert fixed.status_code == 200, fixed.text
    await session.refresh(reg)
    assert reg.email_bounced_at is None and reg.email_bounce_reason is None
    assert (await client.post(f"/api/v1/events/{world['event'].id}/tickets/{reg.id}/send", headers=h)).status_code == 200
    await deliveries_service.deliver(session, d.id)
    await session.refresh(d)
    assert d.status is DeliveryStatus.sent, d.error
    assert len(outbox) == 1 and outbox[0]["recipients"] == ["ananya.iyer@example.com"]


async def test_the_hook_trusts_only_amazons_signature_and_the_shared_token(client, world, amazon, monkeypatch):
    monkeypatch.setattr(settings, "SES_WEBHOOK_TOKEN", "hooktoken", raising=False)
    good = amazon["envelope"]("Notification", ses_message("Complaint", "ses-abc", world["reg"].email))
    assert (await client.post("/api/v1/hooks/ses", params={"token": "nope"}, content=json.dumps(good), headers={"content-type": "text/plain"})).json() == {"ok": False, "reason": "token"}
    forged = dict(good, Signature=base64.b64encode(b"not a signature").decode())
    assert (await client.post("/api/v1/hooks/ses", params={"token": "hooktoken"}, content=json.dumps(forged), headers={"content-type": "text/plain"})).json() == {"ok": False, "reason": "signature"}
    elsewhere = dict(good, SigningCertURL="https://evil.example.com/cert.pem")
    assert (await client.post("/api/v1/hooks/ses", params={"token": "hooktoken"}, content=json.dumps(elsewhere), headers={"content-type": "text/plain"})).json() == {"ok": False, "reason": "signature"}
    assert (await client.post("/api/v1/hooks/ses", params={"token": "hooktoken"}, content=b"not json", headers={"content-type": "text/plain"})).json()["ok"] is False
    # The real one lands, as a complaint.
    assert (await client.post("/api/v1/hooks/ses", params={"token": "hooktoken"}, content=json.dumps(good), headers={"content-type": "text/plain"})).json() == {"ok": True, "matched": 1}


async def test_whatsapp_subscribes_by_token_and_signs_its_statuses(client, world, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "verify-me", raising=False)
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "app-secret", raising=False)
    ok = await client.get("/api/v1/hooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    assert (await client.get("/api/v1/hooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"})).status_code == 403
    body = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.1", "status": "failed", "timestamp": "1760000000", "errors": [{"code": 131026}]}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert (await client.post("/api/v1/hooks/whatsapp", content=body, headers={"content-type": "application/json", "x-hub-signature-256": "sha256=bad"})).json() == {"ok": False, "reason": "signature"}
    r = await client.post("/api/v1/hooks/whatsapp", content=body, headers={"content-type": "application/json", "x-hub-signature-256": sig})
    # Nothing was ever sent on WhatsApp, so nothing matches; the call is still accepted.
    assert r.status_code == 202 and r.json() == {"ok": True, "matched": 0}


async def test_channels_say_whether_a_provider_is_behind_them(client, world):
    s = await client.get(f"/api/v1/events/{world['event'].id}/deliveries/summary", params={"kind": "ticket"}, headers=world["club"])
    assert s.status_code == 200, s.text
    by = {p["channel"]: p for p in s.json()["providers"]}
    assert by["whatsapp"]["configured"] is False and "No WhatsApp provider" in by["whatsapp"]["note"]
    assert by["email"]["name"] == "Amazon SES"
