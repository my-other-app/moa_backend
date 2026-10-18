"""Where a written mail goes: SES by default, Resend first when chosen with SES
behind it on any refusal, the day's quota respected, and Resend's own word on
what became of each mail landing on the send it belongs to."""
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryOutcome, DeliveryStatus
from app.api.hooks import svix
from app.config import settings
from app.core.email import email as email_mod
from app.core.email import transport
from app.core.email.email import send_email as REAL_SEND_EMAIL
from app.core.mail import suppression  # captured before the suite's autouse stub replaces it

SECRET = "whsec_" + base64.b64encode(b"a-test-signing-secret-32-bytes!!").decode()


class FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def wire(monkeypatch):
    """Resend over HTTP and SES over boto3, both recorded, neither reached."""
    transport.reset_for_tests()
    calls = {"resend": [], "ses": [], "clients": 0}
    replies: list = []
    ses_replies: list = []

    def post(body, headers):
        calls["resend"].append({"url": transport.RESEND_URL, "json": body, "headers": headers})
        r = replies.pop(0) if replies else FakeResponse(200, {"id": f"re_{len(calls['resend'])}"})
        if isinstance(r, Exception):
            raise r
        return r

    class FakeSES:
        def send_raw_email(self, Source, Destinations, RawMessage):  # noqa: N803 - boto3's names
            if ses_replies:
                raise ses_replies.pop(0)
            calls["ses"].append({"source": Source, "destinations": Destinations, "raw": RawMessage["Data"]})
            return {"MessageId": f"ses-{len(calls['ses'])}"}

    def new_client(*_a):
        calls["clients"] += 1
        return FakeSES()

    monkeypatch.setattr(email_mod, "send_email", REAL_SEND_EMAIL)
    monkeypatch.setattr(transport, "_post_resend", post)
    monkeypatch.setattr(transport, "_new_ses_client", new_client)
    # The suppression list is the database's; these tests are about the wire.
    monkeypatch.setattr(suppression, "lookup_sync", lambda addresses: {})
    calls["ses_replies"] = ses_replies
    monkeypatch.setattr(settings, "SES_DEFAULT_SENDER", "tickets@myotherapp.in")
    monkeypatch.setattr(settings, "MAIL_FROM_ADDRESS", "info@myotherapp.in")
    monkeypatch.setattr(settings, "MAIL_FROM_NAME", "MyOtherApp")
    yield calls, replies
    transport.reset_for_tests()


def _resend_on(monkeypatch):
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "resend")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")


def test_ses_alone_by_default_with_every_recipient_named(wire, monkeypatch):
    calls, _ = wire
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses")
    out = email_mod.send_email_with_attachment(recipients="ananya@example.com", bcc_recipients=["desk@example.com"], subject="Your receipt", body_text="Thanks", attachment_bytes=b"%PDF-1", attachment_filename="receipt.pdf")
    assert out == {"MessageId": "ses-1", "Provider": "ses"} and calls["resend"] == []
    sent = calls["ses"][0]
    # The old code sent an empty destination list unless there was a bcc; every recipient is named now.
    assert sent["destinations"] == ["ananya@example.com", "desk@example.com"]
    assert sent["source"] == "MyOtherApp <tickets@myotherapp.in>" and 'filename="receipt.pdf"' in sent["raw"]


def test_resend_first_with_files_pictures_and_the_club_name(wire, monkeypatch):
    calls, _ = wire
    _resend_on(monkeypatch)
    out = email_mod.send_email(
        recipients=["ananya@example.com"], subject="Your ticket", text="See you there", html='<img src="cid:qr">',
        attachments=[{"filename": "qr.png", "content": b"\x89PNG", "content_id": "qr"}, {"filename": "ticket.pdf", "content": b"%PDF-1"}],
        sender_name="Circuit Society",
    )
    assert out == {"MessageId": "re_1", "Provider": "resend"} and calls["ses"] == []
    body = calls["resend"][0]["json"]
    assert calls["resend"][0]["headers"]["Authorization"] == "Bearer re_test_key"
    assert body["from"] == "Circuit Society <info@myotherapp.in>" and body["to"] == ["ananya@example.com"] and body["html"] == '<img src="cid:qr">'
    assert body["attachments"][0] == {"filename": "qr.png", "content": base64.b64encode(b"\x89PNG").decode(), "content_id": "qr"}
    assert "content_id" not in body["attachments"][1]


def test_a_sender_off_the_verified_domain_becomes_the_reply_to(wire, monkeypatch):
    calls, _ = wire
    _resend_on(monkeypatch)
    email_mod.send_email(recipients=["a@example.com"], subject="Hi", text="x", html="<p>x</p>", sender="lead@circuitsociety.org", sender_name="Circuit Society")
    email_mod.send_email(recipients=["a@example.com"], subject="Hi", text="x", html="<p>x</p>", sender="hello@circuit.myotherapp.in", sender_name="Circuit Society")
    first, second = (c["json"] for c in calls["resend"])
    assert first["from"] == "Circuit Society <info@myotherapp.in>" and first["reply_to"] == "lead@circuitsociety.org"
    assert second["from"] == "Circuit Society <hello@circuit.myotherapp.in>" and "reply_to" not in second


def test_the_days_quota_sends_through_ses_and_leaves_resend_alone_until_tomorrow(wire, monkeypatch):
    calls, replies = wire
    _resend_on(monkeypatch)
    replies.append(FakeResponse(429, {"name": "daily_quota_exceeded", "message": "You have reached your daily email sending quota."}))
    first = email_mod.send_email(recipients=["a@example.com"], subject="1", text="x", html="<p>x</p>")
    second = email_mod.send_email(recipients=["b@example.com"], subject="2", text="x", html="<p>x</p>")
    assert first["Provider"] == "ses" and second["Provider"] == "ses"
    assert len(calls["resend"]) == 1 and len(calls["ses"]) == 2  # Resend was asked once, not twice
    # Another refusal — a bad address, a burst over the rate — falls back once and does not pause Resend.
    transport.reset_for_tests()
    replies.append(FakeResponse(429, {"name": "rate_limit_exceeded", "message": "Too many requests"}))
    assert email_mod.send_email(recipients=["c@example.com"], subject="3", text="x", html="<p>x</p>")["Provider"] == "ses"
    assert email_mod.send_email(recipients=["d@example.com"], subject="4", text="x", html="<p>x</p>")["Provider"] == "resend"
    # So does Resend being unreachable.
    replies.append(httpx.ConnectError("no route"))
    assert email_mod.send_email(recipients=["e@example.com"], subject="5", text="x", html="<p>x</p>")["Provider"] == "ses"


def test_resend_is_skipped_without_a_key(wire, monkeypatch):
    calls, _ = wire
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "resend")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    assert email_mod.send_email(recipients=["a@example.com"], subject="x", text="x", html="x")["Provider"] == "ses" and calls["resend"] == []


def _signed(body: bytes, secret: str = SECRET, at: int | None = None) -> dict:
    msg_id, ts = "msg_" + uuid.uuid4().hex[:10], str(at if at is not None else int(time.time()))
    key = base64.b64decode(secret.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}", "content-type": "application/json"}


def test_the_signature_check_refuses_a_wrong_secret_a_changed_body_and_an_old_capture():
    body = b'{"type":"email.delivered"}'
    h = _signed(body)
    assert svix.verify(SECRET, h["svix-id"], h["svix-timestamp"], h["svix-signature"], body)
    other = "whsec_" + base64.b64encode(b"another-secret-entirely-32-bytes").decode()
    assert not svix.verify(other, h["svix-id"], h["svix-timestamp"], h["svix-signature"], body)
    assert not svix.verify(SECRET, h["svix-id"], h["svix-timestamp"], h["svix-signature"], body + b" ")
    old = _signed(body, at=int(time.time()) - 600)
    assert not svix.verify(SECRET, old["svix-id"], old["svix-timestamp"], old["svix-signature"], body)
    assert not svix.verify("", h["svix-id"], h["svix-timestamp"], h["svix-signature"], body)


async def test_resends_word_lands_on_the_send_and_a_bounce_marks_the_address(client, session, make_club, make_event, make_user, make_registration, monkeypatch):
    monkeypatch.setattr(settings, "RESEND_WEBHOOK_SECRET", SECRET)
    owner, club = await make_club(name="Hooks Club", email="resend-hooks@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=5))
    other = await make_registration(event=event, user=await make_user(email="rohan-r@example.com"), full_name="Rohan", email="rohan-r@example.com")
    reg = await make_registration(event=event, user=await make_user(email="ananya-r@example.com"), full_name="Ananya", email="ananya-r@example.com")
    for pid, r in (("re_delivered", other), ("re_bounced", reg)):
        session.add(Deliveries(id=uuid.uuid4(), event_id=event.id, registration_id=r.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email, status=DeliveryStatus.sent, provider_id=pid, sent_at=datetime.now(timezone.utc)))
    await session.commit()

    async def post(payload: dict, headers: dict | None = None):
        raw = json.dumps(payload).encode()
        return (await client.post("/api/v1/hooks/resend", content=raw, headers=headers or _signed(raw))).json()

    assert await post({"type": "email.delivered", "created_at": datetime.now(timezone.utc).isoformat(), "data": {"email_id": "re_delivered"}}) == {"ok": True, "matched": 1}
    assert await post({"type": "email.opened", "created_at": datetime.now(timezone.utc).isoformat(), "data": {"email_id": "re_delivered"}}) == {"ok": True, "matched": 1}
    bounced = {"type": "email.bounced", "created_at": datetime.now(timezone.utc).isoformat(), "data": {"email_id": "re_bounced", "bounce": {"type": "Permanent", "subType": "General", "message": "550 5.1.1 The email account does not exist"}}}
    assert await post(bounced) == {"ok": True, "matched": 1}
    # A late "delivered" never undoes a bounce; an event a desk does not act on is a no-op; an unsigned call is refused.
    assert await post({"type": "email.delivered", "data": {"email_id": "re_bounced"}}) == {"ok": True, "matched": 0}
    assert await post({"type": "email.clicked", "data": {"email_id": "re_delivered"}}) == {"ok": True, "matched": 0}
    assert await post({"type": "email.delivered", "data": {"email_id": "re_delivered"}}, headers={"content-type": "application/json"}) == {"ok": False, "reason": "signature"}

    rows = {d.provider_id: d for d in (await session.execute(Deliveries.__table__.select())).all()}
    await session.refresh(reg)
    assert rows["re_delivered"].outcome == DeliveryOutcome.read
    assert rows["re_bounced"].outcome == DeliveryOutcome.bounced and "does not exist" in rows["re_bounced"].reason
    assert reg.email_bounced_at is not None and "does not exist" in reg.email_bounce_reason


# ─── One client, the right fallback, the warning (email integrity) ─────────


def test_a_resend_timeout_is_tried_again_later_and_never_sent_again_through_ses(wire, monkeypatch):
    """Resend may have taken a mail it did not answer for: SES now would be
    the guest's second copy. A refusal (4xx) or no connection at all still
    falls back, as it is certain nothing was taken."""
    calls, replies = wire
    _resend_on(monkeypatch)
    replies.append(httpx.ReadTimeout("no answer"))
    with pytest.raises(transport.MailDeferred):
        email_mod.send_email(recipients=["a@example.com"], subject="1", text="x", html="<p>x</p>", idempotency_key="delivery-1")
    replies.append(FakeResponse(502, {"name": "internal_server_error", "message": "upstream"}))
    with pytest.raises(transport.MailDeferred):
        email_mod.send_email(recipients=["a@example.com"], subject="1", text="x", html="<p>x</p>", idempotency_key="delivery-1")
    assert calls["ses"] == []
    # The retry carries the same key, so Resend sends it once whichever try it took.
    assert [c["headers"]["Idempotency-Key"] for c in calls["resend"]] == ["delivery-1", "delivery-1"]
    replies.append(FakeResponse(422, {"name": "validation_error", "message": "Invalid `to` field"}))
    assert email_mod.send_email(recipients=["a@example.com"], subject="2", text="x", html="<p>x</p>")["Provider"] == "ses"


def test_ses_throttling_and_silence_are_tried_again_and_a_rejection_is_final(wire, monkeypatch):
    from botocore.exceptions import ClientError, ReadTimeoutError

    calls, _ = wire
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses")

    def refusal(code, status=400):
        return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "SendRawEmail")

    calls["ses_replies"].extend([refusal("Throttling"), ReadTimeoutError(endpoint_url="https://email.ap-south-1.amazonaws.com"), refusal("MessageRejected")])
    with pytest.raises(transport.MailThrottled):
        email_mod.send_email(recipients=["a@example.com"], subject="1", text="x", html="x")
    with pytest.raises(transport.MailDeferred):
        email_mod.send_email(recipients=["a@example.com"], subject="1", text="x", html="x")
    with pytest.raises(ClientError) as rejected:
        email_mod.send_email(recipients=["a@example.com"], subject="1", text="x", html="x")
    assert not isinstance(rejected.value, transport.MailDeferred)


def test_one_ses_client_serves_every_send_on_every_thread(monkeypatch):
    """A client a mail meant a TLS handshake to ap-south-1 a mail, and
    building clients from boto3's default session on many threads at once
    is not safe. One client, built once, serves them all."""
    import threading

    import boto3

    transport.reset_for_tests()
    built: list = []

    class FakeSES:
        def send_raw_email(self, Source, Destinations, RawMessage):  # noqa: N803 - boto3's names
            return {"MessageId": "ses-x"}

    def client(self, service_name, *a, **k):
        built.append((service_name, self.region_name, (k.get("config") or None) and k["config"].region_name))
        return FakeSES()

    monkeypatch.setattr(boto3.session.Session, "client", client)
    monkeypatch.setattr(email_mod, "send_email", REAL_SEND_EMAIL)
    monkeypatch.setattr(suppression, "lookup_sync", lambda addresses: {})
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses")

    def burst():
        for _ in range(5):
            email_mod.send_email(recipients=["a@example.com"], subject="x", text="x", html="x")

    threads = [threading.Thread(target=burst) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1 and built[0][0] == "ses"
    # Its region is a setting; ap-south-1 unless told otherwise.
    assert built[0][1] == "ap-south-1"
    monkeypatch.setattr(settings, "SES_REGION", "eu-west-1")
    email_mod.send_email(recipients=["a@example.com"], subject="x", text="x", html="x")
    assert len(built) == 2 and built[1][1] == "eu-west-1"
    transport.reset_for_tests()


def test_the_overhead_of_a_send_drops_with_one_client(monkeypatch, capsys):
    """Measured with SES stubbed out, so no network: what is left of a send
    is the work on our side. A client per mail rebuilt the whole botocore
    client each time (and, live, its TLS connection to ap-south-1); one
    client pays that once."""
    import time

    import boto3
    from botocore.stub import Stubber

    transport.reset_for_tests()
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses")
    monkeypatch.setattr(suppression, "lookup_sync", lambda addresses: {})
    n = 20
    mail = transport.Mail(to=["a@example.com"], subject="x", text="x", html="<p>x</p>")
    ok = {"MessageId": "ses-1"}

    # Before: what the old _send_ses did for every mail.
    started = time.perf_counter()
    for _ in range(n):
        client = boto3.client("ses", region_name="ap-south-1", aws_access_key_id="k", aws_secret_access_key="s")
        with Stubber(client) as stub:
            stub.add_response("send_raw_email", ok)
            client.send_raw_email(Source="a@example.com", Destinations=["a@example.com"], RawMessage={"Data": "x"})
    per_send_before = (time.perf_counter() - started) / n

    # After: the transport's one client.
    shared = transport._ses()
    with Stubber(shared) as stub:
        for _ in range(n):
            stub.add_response("send_raw_email", ok)
        started = time.perf_counter()
        for _ in range(n):
            transport._send_ses(mail)
        per_send_after = (time.perf_counter() - started) / n
    with capsys.disabled():
        print(f"\n  SES per-send overhead, stubbed: {per_send_before * 1000:.1f} ms with a client a mail, {per_send_after * 1000:.1f} ms with one")
    assert per_send_after * 2 < per_send_before
    transport.reset_for_tests()


def test_account_mail_to_a_suppressed_address_goes_with_a_warning(wire, monkeypatch, caplog):
    """A password reset is asked for this minute; holding it back would lock
    the person out. It goes -- and the log says the address is suppressed."""
    calls, _ = wire
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses")
    monkeypatch.setattr(suppression, "lookup_sync", lambda addresses: {"gone@example.com": "bounce"} if "gone@example.com" in addresses else {})
    with caplog.at_level("WARNING", logger="app.core.mail.suppression"):
        out = email_mod.send_email(recipients=["gone@example.com"], subject="Reset your password", text="x", html="x")
    assert out["Provider"] == "ses" and len(calls["ses"]) == 1
    assert any("suppressed" in r.getMessage() and "g***@example.com" in r.getMessage() for r in caplog.records)
    # A sender that already looked (the event pipelines) is not looked up again.
    looked: list = []
    monkeypatch.setattr(suppression, "lookup_sync", lambda addresses: looked.append(addresses) or {})
    email_mod.send_email(recipients=["b@example.com"], subject="x", text="x", html="x", check_suppression=False)
    assert looked == []
