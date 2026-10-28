"""SES through its SMTP interface (MAIL_PROVIDER=ses_smtp, app/core/email/ses_smtp.py).

A fake SES SMTP server runs on 127.0.0.1 with a real TLS certificate made
for the test, so smtplib, STARTTLS and the certificate check are the real
ones; nothing leaves the machine and no credential is real."""
from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import logging
import re
import socket
import ssl
import threading
import uuid
from email import message_from_bytes

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryOutcome, DeliveryStatus
from app.api.hooks import service as hooks_service
from app.config import settings
from app.core import providers
from app.core.email import email as email_mod
from app.core.email import pace, ses_smtp, transport
from app.core.email.email import send_email as REAL_SEND_EMAIL
from app.core.mail import suppression

USER = "smtp-user-for-tests"
PASSWORD = "smtp-password-for-tests-0123"


# ─── A fake SES SMTP endpoint ────────────────────────────────────────────


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    """A self-signed certificate for localhost, and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    folder = tmp_path_factory.mktemp("smtp-tls")
    certfile, keyfile = folder / "cert.pem", folder / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(certfile), str(keyfile)


class FakeSES:
    """Speaks enough of SES's SMTP interface: EHLO, STARTTLS, AUTH PLAIN and
    LOGIN (only once encrypted, unless told otherwise), MAIL, RCPT, DATA,
    RSET, NOOP, QUIT. `script[stage]` replaces the next reply at a stage
    (MAIL, RCPT, DATA, END, AUTH); END is the reply to the end of DATA."""

    def __init__(self, certfile: str, keyfile: str):
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(certfile, keyfile)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.lock = threading.Lock()
        self.connections = 0
        #: (connection number, encrypted?, command verb) for every command received.
        self.commands: list[tuple[int, bool, str]] = []
        self.messages: list[dict] = []
        self.script: dict[str, list[str]] = {}
        self.offer_starttls = True
        self.auth_before_tls = False
        #: Drop the connection, saying nothing, right after each mail is taken.
        self.drop_after_mail = False
        #: Answer the next MAIL on every open connection with SES's idle timeout, then close.
        self.time_out_idle = False
        self.closed = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def scripted(self, stage: str, default: str) -> str:
        with self.lock:
            queue = self.script.get(stage)
            return queue.pop(0) if queue else default

    def _serve(self):
        while not self.closed:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            if self.closed:
                conn.close()
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        with self.lock:
            self.connections += 1
            number = self.connections
        encrypted = False
        f = conn.makefile("rwb")

        def say(*lines: str):
            f.write(b"".join(line.encode() + b"\r\n" for line in lines))
            f.flush()

        def read() -> str:
            line = f.readline()
            if not line:
                raise EOFError
            return line.decode().rstrip("\r\n")

        try:
            say("220 email-smtp.ap-south-1.amazonaws.com ESMTP SimpleEmailService-test")
            sender, recipients = None, []
            while True:
                line = read()
                verb = line.split(" ", 1)[0].upper()
                with self.lock:
                    self.commands.append((number, encrypted, verb))
                if verb == "EHLO":
                    caps = ["250-email-smtp.amazonaws.com", "250-8BITMIME", "250-SIZE 10485760"]
                    if not encrypted and self.offer_starttls:
                        caps.append("250-STARTTLS")
                    if encrypted or self.auth_before_tls:
                        caps.append("250-AUTH PLAIN LOGIN")
                    say(*caps, "250 Ok")
                elif verb == "STARTTLS":
                    say("220 Ready to start TLS")
                    f.close()
                    conn = self.tls.wrap_socket(conn, server_side=True)
                    f = conn.makefile("rwb")
                    encrypted = True
                elif verb == "AUTH":
                    parts = line.split(" ")
                    if parts[1].upper() == "PLAIN":
                        _, user, password = base64.b64decode(parts[2]).decode().split("\0")
                    else:
                        user = base64.b64decode(parts[2]).decode() if len(parts) > 2 else ""
                        if not user:
                            say("334 VXNlcm5hbWU6")
                            user = base64.b64decode(read()).decode()
                        say("334 UGFzc3dvcmQ6")
                        password = base64.b64decode(read()).decode()
                    ok = user == USER and password == PASSWORD
                    say(self.scripted("AUTH", "235 Authentication successful." if ok else "535 Authentication Credentials Invalid"))
                elif verb == "MAIL":
                    if self.time_out_idle:
                        say("451 4.4.2 Timeout waiting for data from client.")
                        return
                    reply = self.scripted("MAIL", "250 Ok")
                    if reply.startswith("250"):
                        sender, recipients = re.search(r"<(.*)>", line).group(1), []
                    say(reply)
                elif verb == "RCPT":
                    reply = self.scripted("RCPT", "250 Ok")
                    if reply.startswith("250"):
                        recipients.append(re.search(r"<(.*)>", line).group(1))
                    say(reply)
                elif verb == "DATA":
                    reply = self.scripted("DATA", "354 End data with <CR><LF>.<CR><LF>")
                    say(reply)
                    if not reply.startswith("354"):
                        continue
                    chunks = []
                    while True:
                        raw = f.readline()
                        if raw == b".\r\n":
                            break
                        chunks.append(raw[1:] if raw.startswith(b"..") else raw)
                    reply = self.scripted("END", f"250 Ok 0109019a{uuid.uuid4().hex[:8]}-{uuid.uuid4()}-000000")
                    if reply.startswith("250"):
                        with self.lock:
                            self.messages.append({"connection": number, "sender": sender, "recipients": list(recipients),
                                                  "data": b"".join(chunks), "id": reply.split()[2] if len(reply.split()) > 2 else None})
                    sender, recipients = None, []
                    say(reply)
                    if reply.startswith("421"):
                        return
                    if self.drop_after_mail and reply.startswith("250"):
                        return
                elif verb == "RSET":
                    sender, recipients = None, []
                    say("250 Ok")
                elif verb == "NOOP":
                    say("250 Ok")
                elif verb == "QUIT":
                    say("221 Bye")
                    return
                else:
                    say("502 Command not implemented")
        except (EOFError, OSError, ValueError):
            return
        finally:
            try:
                f.close()
                conn.close()
            except OSError:
                pass

    def verbs(self, connection: int | None = None) -> list[str]:
        return [v for n, _, v in self.commands if connection is None or n == connection]

    def close(self):
        """Stop listening, for good. On Linux, close() alone does not: the
        thread blocked in accept() keeps the socket listening and takes the
        next connection, so a "closed" server still answered (macOS wakes
        accept() on close and never showed it). shutdown() wakes it on Linux;
        macOS refuses shutdown() on a listening socket, where close() is enough."""
        self.closed = True
        try:
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()
        self.thread.join(timeout=5)


@pytest.fixture
def ses(certificate, monkeypatch):
    """The fake endpoint, and the settings and trust that point at it."""
    server = FakeSES(*certificate)
    transport.reset_for_tests()
    trusted = ssl.create_default_context(cafile=certificate[0])
    monkeypatch.setattr(ses_smtp, "_tls_context", lambda: trusted)
    monkeypatch.setattr(email_mod, "send_email", REAL_SEND_EMAIL)
    monkeypatch.setattr(suppression, "lookup_sync", lambda addresses: {})
    for name, value in {
        "MAIL_PROVIDER": "ses_smtp", "SMTP_HOST": "localhost", "SMTP_PORT": server.port, "SMTP_USERNAME": USER,
        "SMTP_PASSWORD": PASSWORD, "SMTP_TIMEOUT": 5.0, "SES_ACCESS_KEY": "", "SES_SECRET_KEY": "",
        "SES_DEFAULT_SENDER": "tickets@myotherapp.in", "MAIL_FROM_NAME": "MyOtherApp",
    }.items():
        monkeypatch.setattr(settings, name, value)
    yield server
    transport.reset_for_tests()
    server.close()


def _mail(**more) -> transport.Mail:
    return transport.Mail(**{"to": ["ananya@example.com"], "subject": "Your ticket", "text": "See you there", "html": "<p>See you there</p>", **more})


# ─── STARTTLS, the message, the id ───────────────────────────────────────


# First in the file: run on its own, a module must reach the database from an
# async test before any sync test runs (tests/test_mail_transport.py fails the
# same way alone on master); in the whole suite the order does not matter.
async def test_ses_notices_match_a_mail_sent_over_smtp(ses, session, make_club, make_event, make_user, make_registration):
    """The id comes back where the API's MessageId did, so the SES hook lands
    a bounce on the send that carried it, exactly as before."""
    owner, club = await make_club(name="SMTP Club", email="smtp-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5))
    reg = await make_registration(event=event, user=await make_user(email="ananya-smtp@example.com"), full_name="Ananya", email="ananya-smtp@example.com")
    out = await pace.blocking(email_mod.send_email, recipients=["ananya-smtp@example.com"], subject="Ticket", text="x", html="<p>x</p>")
    assert out["Provider"] == "ses" and out["MessageId"] == ses.messages[0]["id"]
    session.add(Deliveries(id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email,
                           status=DeliveryStatus.sent, provider_id=out["MessageId"], sent_at=dt.datetime.now(dt.timezone.utc)))
    await session.commit()

    bounce = {
        "notificationType": "Bounce",
        "mail": {"messageId": out["MessageId"], "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "destination": ["ananya-smtp@example.com"]},
        "bounce": {"bounceType": "Permanent", "bounceSubType": "General", "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "bouncedRecipients": [{"emailAddress": "ananya-smtp@example.com", "diagnosticCode": "smtp; 550 5.1.1 user unknown"}]},
    }
    assert await hooks_service.ses(session, json.dumps(bounce)) == {"matched": 1}
    row = await session.scalar(select(Deliveries).where(Deliveries.provider_id == out["MessageId"]).execution_options(populate_existing=True))
    assert row.outcome == DeliveryOutcome.bounced


def test_starttls_comes_before_auth_and_the_message_goes_as_the_api_would_send_it(ses, monkeypatch):
    built: list = []
    real = transport.ses_envelope
    monkeypatch.setattr(transport, "ses_envelope", lambda mail: built.append(real(mail)) or built[-1])
    mail = _mail(
        cc=["rohan@example.com"], bcc=["desk@example.com"], reply_to="lead@circuit.org", from_name="Circuit Society",
        attachments=[transport.Attachment("qr.png", b"\x89PNG-qr", content_id="qr"), transport.Attachment("ticket.pdf", b"%PDF-1.7 ticket")],
        headers={"In-Reply-To": "<a@b>"},
    )
    out = transport.deliver(mail)

    assert out["Provider"] == "ses" and re.fullmatch(r"0109019a[0-9a-f]{8}-[0-9a-f-]{36}-000000", out["MessageId"])
    # Encrypted before the credentials, and never a credential in the clear.
    verbs = [(tls, v) for _, tls, v in ses.commands]
    assert verbs[:4] == [(False, "EHLO"), (False, "STARTTLS"), (True, "EHLO"), (True, "AUTH")]
    assert all(tls for tls, v in verbs if v in ("AUTH", "MAIL", "RCPT", "DATA"))
    sent = ses.messages[0]
    # The envelope: the bare sender, and every To, Cc and Bcc, as the API's Destinations.
    source, destinations, raw = built[0]
    assert source == "Circuit Society <tickets@myotherapp.in>"
    assert sent["sender"] == "tickets@myotherapp.in"
    assert sent["recipients"] == destinations == ["ananya@example.com", "rohan@example.com", "desk@example.com"]
    # The raw MIME, byte for byte, with SMTP's CRLF line ends.
    assert sent["data"] == raw.replace("\n", "\r\n").encode()
    received = message_from_bytes(sent["data"])
    assert received["From"] == "Circuit Society <tickets@myotherapp.in>" and received["Cc"] == "rohan@example.com"
    assert received["Reply-To"] == "lead@circuit.org" and received["In-Reply-To"] == "<a@b>"
    assert received["Bcc"] is None
    files = {p.get_filename(): p.get_payload(decode=True) for p in received.walk() if p.get_filename()}
    assert files == {"qr.png": b"\x89PNG-qr", "ticket.pdf": b"%PDF-1.7 ticket"}
    assert out["MessageId"] == sent["id"]


def test_the_same_mime_as_the_api_path(ses, monkeypatch):
    """Built once for each door, the two differ only in the multipart boundaries (random)."""
    api: list = []

    class FakeBoto:
        def send_raw_email(self, Source, Destinations, RawMessage):  # noqa: N803 - boto3's names
            api.append((Source, Destinations, RawMessage["Data"]))
            return {"MessageId": "api-1"}

    monkeypatch.setattr(transport, "_new_ses_client", lambda *a: FakeBoto())
    mail = _mail(bcc=["desk@example.com"], attachments=[transport.Attachment("ticket.pdf", b"%PDF-1.7")])
    transport.deliver(mail)
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses")
    monkeypatch.setattr(settings, "SES_ACCESS_KEY", "AKIAFAKEFORTESTS0000")
    monkeypatch.setattr(settings, "SES_SECRET_KEY", "fake-secret-for-tests")
    assert transport.deliver(mail) == {"MessageId": "api-1", "Provider": "ses"}

    def same(text: str) -> str:
        return re.sub(r"={15}\d+==", "BOUNDARY", text.replace("\r\n", "\n"))

    assert same(ses.messages[0]["data"].decode()) == same(api[0][2])
    assert ses.messages[0]["recipients"] == api[0][1]


@pytest.mark.parametrize("reply, found", [
    ("Ok 0109019a1b2c3d4e-5f6a7b8c-9d0e-4f1a-8b2c-3d4e5f6a7b8c-000000", "0109019a1b2c3d4e-5f6a7b8c-9d0e-4f1a-8b2c-3d4e5f6a7b8c-000000"),
    (b"Ok <0000014a2e0d6d06-0b8e6c9c-6c7f-4a1f-9e27-a1ac04e0e3c6-000000>", "0000014a2e0d6d06-0b8e6c9c-6c7f-4a1f-9e27-a1ac04e0e3c6-000000"),
    ("2.0.0 Ok abc-123", "abc-123"),
    ("Ok", None),
    ("Queued", None),
])
def test_the_message_id_is_read_from_250_ok(reply, found):
    assert ses_smtp.message_id(reply) == found


# ─── Connections ─────────────────────────────────────────────────────────


def test_one_connection_carries_the_mails_of_a_thread(ses):
    for i in range(3):
        transport.deliver(_mail(subject=f"#{i}"))
    assert ses.connections == 1 and len(ses.messages) == 3
    assert ses.verbs().count("AUTH") == 1


def test_a_dropped_connection_is_replaced_and_the_mail_sent_once(ses):
    ses.drop_after_mail = True
    first = transport.deliver(_mail(subject="1"))
    # SES hung up after the first; the second finds it gone at MAIL, before
    # anything of the mail was taken, and goes on a new connection.
    second = transport.deliver(_mail(subject="2"))
    assert ses.connections == 2 and [m["connection"] for m in ses.messages] == [1, 2]
    assert first["MessageId"] != second["MessageId"] and len(ses.messages) == 2


def test_ses_idle_timeout_reply_is_a_dropped_connection_too(ses, monkeypatch):
    transport.deliver(_mail(subject="1"))
    ses.time_out_idle = True
    # The new connection must not be timed out as well.
    monkeypatch.setattr(ses_smtp, "_open", _open_then(lambda: setattr(ses, "time_out_idle", False)))
    transport.deliver(_mail(subject="2"))
    assert ses.connections == 2 and len(ses.messages) == 2


def _open_then(after):
    real = ses_smtp._open

    def opened(key):
        after()
        return real(key)

    return opened


def test_an_idle_old_or_busy_connection_is_replaced(ses, monkeypatch):
    monkeypatch.setattr(ses_smtp, "MAX_MESSAGES", 2)
    for i in range(3):
        transport.deliver(_mail(subject=f"#{i}"))
    assert ses.connections == 2 and "QUIT" in ses.verbs(1)
    monkeypatch.setattr(ses_smtp, "IDLE_SECONDS", -1.0)
    transport.deliver(_mail(subject="idle"))
    assert ses.connections == 3


def test_each_thread_has_its_own_connection(ses):
    errors: list = []

    def burst(n):
        try:
            for i in range(3):
                transport.deliver(_mail(subject=f"{n}-{i}"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=burst, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(ses.messages) == 12
    by_connection: dict = {}
    for m in ses.messages:
        by_connection.setdefault(m["connection"], []).append(m)
    assert ses.connections == 4 and all(len(v) == 3 for v in by_connection.values())


# ─── What SES says ───────────────────────────────────────────────────────


def test_throttling_is_retried_a_temporary_failure_deferred_and_a_rejection_final(ses):
    ses.script["MAIL"] = ["454 Throttling failure: Maximum sending rate exceeded."]
    with pytest.raises(transport.MailThrottled):
        transport.deliver(_mail())
    ses.script["END"] = ["451 Temporary service failure"]
    with pytest.raises(transport.MailDeferred) as deferred:
        transport.deliver(_mail())
    assert not isinstance(deferred.value, transport.MailThrottled)
    ses.script["END"] = ["554 Message rejected: Email address is not verified. The following identities failed the check in region AP-SOUTH-1: ananya@example.com"]
    with pytest.raises(ses_smtp.SmtpRejected) as rejected:
        transport.deliver(_mail())
    assert not isinstance(rejected.value, transport.MailDeferred) and not isinstance(rejected.value, ses_smtp.SmtpConfigError)
    assert rejected.value.response["Error"]["Code"] == "MessageRejected" and "not verified" in str(rejected.value)
    ses.script["RCPT"] = ["553 <nobody@@example.com> address unknown"]
    with pytest.raises(ses_smtp.SmtpRejected):
        transport.deliver(_mail(to=["nobody@@example.com"]))
    assert "RSET" in ses.verbs()
    # Every refusal left the connection usable: still one, and the next mail goes.
    assert transport.deliver(_mail())["MessageId"] and ses.connections == 1 and len(ses.messages) == 1


async def test_the_pipeline_slows_down_and_tries_a_throttled_mail_again(ses):
    from app.api.deliveries import pipeline

    pace.reset_for_tests()
    ses.script["MAIL"] = ["454 Throttling failure: Maximum sending rate exceeded."]
    out = await pipeline.send_with_backoff(email_mod.send_email, recipients=["ananya@example.com"], subject="x", text="x", html="<p>x</p>")
    assert out["MessageId"] == ses.messages[0]["id"] and len(ses.messages) == 1
    assert pace.bucket().rate < pace.bucket().ceiling
    pace.reset_for_tests()


def test_bad_credentials_are_a_configuration_error_said_once_and_not_hammered(ses, monkeypatch, caplog):
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "the-wrong-password-9876")
    with caplog.at_level(logging.WARNING, logger="app.core.email.ses_smtp"):
        with pytest.raises(ses_smtp.SmtpConfigError) as refused:
            transport.deliver(_mail())
        assert not isinstance(refused.value, transport.MailDeferred)
        assert refused.value.smtp_code == 535 and "APP_SMTP_PASSWORD" in str(refused.value)
        assert "the-wrong-password-9876" not in str(refused.value)
        tried = ses.connections
        # The next mails fail the same way without asking SES again.
        for _ in range(3):
            with pytest.raises(ses_smtp.SmtpConfigError):
                transport.deliver(_mail())
        assert ses.connections == tried
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "535" in errors[0].getMessage()
    assert all("the-wrong-password-9876" not in r.getMessage() for r in caplog.records)
    # Fixed credentials are tried at once.
    monkeypatch.setattr(settings, "SMTP_PASSWORD", PASSWORD)
    assert transport.deliver(_mail())["MessageId"]


def test_a_temporary_authentication_failure_is_tried_again_later(ses):
    # smtplib tries PLAIN, then LOGIN: SES answers both the same way.
    ses.script["AUTH"] = ["454 Temporary authentication failure"] * 2
    with pytest.raises(transport.MailDeferred):
        transport.deliver(_mail())
    assert transport.deliver(_mail())["MessageId"]


def test_access_denied_is_a_configuration_error(ses):
    ses.script["END"] = ["554 Access denied: User arn:aws:iam::1:user/ses-smtp is not authorized to perform ses:SendRawEmail on resource x"]
    with pytest.raises(ses_smtp.SmtpConfigError):
        transport.deliver(_mail())


def test_no_starttls_means_no_credentials(ses):
    ses.offer_starttls = False
    ses.auth_before_tls = True  # a server (or something in between) offering AUTH in the clear
    with pytest.raises(ses_smtp.SmtpConfigError) as refused:
        transport.deliver(_mail())
    assert "STARTTLS" in str(refused.value)
    assert "AUTH" not in ses.verbs() and ses.messages == []


def test_a_certificate_that_does_not_verify_means_no_credentials(ses, monkeypatch):
    monkeypatch.setattr(ses_smtp, "_tls_context", ssl.create_default_context)  # the system's roots: our test certificate isn't one
    with pytest.raises(ses_smtp.SmtpConfigError) as refused:
        transport.deliver(_mail())
    assert "certificate" in str(refused.value)
    assert "AUTH" not in ses.verbs() and ses.messages == []


def test_an_unreachable_endpoint_is_tried_again_later(ses, monkeypatch):
    ses.close()
    assert not ses.thread.is_alive()
    with pytest.raises(transport.MailDeferred) as deferred:
        transport.deliver(_mail())
    assert "could not be reached" in str(deferred.value)
    # Nothing answered: no connection was taken and no mail went.
    assert ses.connections == 0 and ses.messages == []


# ─── The other providers are as they were ────────────────────────────────


def test_resend_falls_back_through_smtp_only_without_api_keys(ses, monkeypatch):
    class Refused:
        status_code = 422
        text = "{}"

        def json(self):
            return {"name": "validation_error", "message": "Invalid `to` field"}

    api: list = []

    class FakeBoto:
        def send_raw_email(self, Source, Destinations, RawMessage):  # noqa: N803 - boto3's names
            api.append(Destinations)
            return {"MessageId": "api-1"}

    monkeypatch.setattr(transport, "_post_resend", lambda body, headers: Refused())
    monkeypatch.setattr(transport, "_new_ses_client", lambda *a: FakeBoto())
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "resend")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    # No API keys: SES's SMTP interface catches what Resend refused.
    assert transport.deliver(_mail())["Provider"] == "ses" and len(ses.messages) == 1 and api == []
    # With them, the API, as before.
    monkeypatch.setattr(settings, "SES_ACCESS_KEY", "AKIAFAKEFORTESTS0000")
    monkeypatch.setattr(settings, "SES_SECRET_KEY", "fake-secret-for-tests")
    assert transport.deliver(_mail()) == {"MessageId": "api-1", "Provider": "ses"} and len(ses.messages) == 1


def test_ses_never_touches_smtp(ses, monkeypatch):
    class FakeBoto:
        def send_raw_email(self, Source, Destinations, RawMessage):  # noqa: N803 - boto3's names
            return {"MessageId": "api-1"}

    monkeypatch.setattr(transport, "_new_ses_client", lambda *a: FakeBoto())
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses")
    assert transport.deliver(_mail()) == {"MessageId": "api-1", "Provider": "ses"}
    assert ses.connections == 0


def test_the_channel_says_ses_over_smtp(monkeypatch):
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses_smtp")
    monkeypatch.setattr(settings, "SMTP_HOST", "email-smtp.ap-south-1.amazonaws.com")
    monkeypatch.setattr(settings, "SMTP_USERNAME", USER)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", PASSWORD)
    p = providers.email_provider()
    assert p.name == "Amazon SES (SMTP)" and p.configured and "SES hook" in p.note
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    assert providers.email_provider().configured is False
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses")
    assert providers.email_provider().name == "Amazon SES"
