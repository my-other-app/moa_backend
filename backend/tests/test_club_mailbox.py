"""A club's mailbox, <handle>@mail.myotherapp.in (app/api/club_mailbox).

AWS is never reached: SNS's envelopes are signed here with a certificate of
the test's own (served where SNS would serve it), the inbound bucket is a
dict, the media bucket a dict, and SES's send is a fake client that keeps
the raw MIME it was handed. What is checked is what the club, its team, the
admins and the other side of a mail would see.

The plans are production's: club_plans' seed, then club_mailbox's grant
(Pro and Enterprise have `mailbox`).
"""
import base64
import importlib.util
import json
import logging
import pathlib
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from types import SimpleNamespace

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.api.audit.models import AdminAuditLogs
from app.api.auth.schemas import AuthTokenData
from app.api.club_access import policy as access_policy
from app.api.club_access.models import ClubMembers
from app.api.club_alerts.models import ClubAlerts
from app.api.club_mailbox import credits, export, handles, live, outbound, parse, providers, sanitise, store, sweep, threads
from app.api.club_mailbox import router as mailbox_router
from app.api.club_mailbox.models import (
    ClubMailAttachments,
    ClubMailboxAddresses,
    ClubMailboxes,
    ClubMailMessages,
    ClubMailThreads,
)
from app.api.hooks import sns
from app.api.message_quota import service as quota
from app.api.message_quota.models import MessageUsage
from app.api.subscriptions import gates, service as subscriptions
from app.api.subscriptions.models import BillingCycle, Plans
from app.config import settings
from app.core import config_checks, redact
from app.core.auth.jwt import create_access_token
from app.core.email import email as email_mod
from app.core.email import transport
from app.core.mail import suppression
from tests.conftest import auth_header
from tests.test_plan_enforcement import impersonating

API = "/api/v1"
BASE = f"{API}/clubs/mailbox"
HOOK = f"{API}/hooks/mailbox/ses"
NOW = datetime.now(timezone.utc)
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)
DOMAIN = "mail.myotherapp.in"
BUCKET = "moa-mailbox-inbound"
TOPIC = "arn:aws:sns:ap-south-1:123456789012:moa-mailbox-inbound"
CERT_URL = "https://sns.ap-south-1.amazonaws.com/SimpleNotificationService-test.pem"
SUBSCRIBE_URL = "https://sns.ap-south-1.amazonaws.com/?Action=ConfirmSubscription&TopicArn=x&Token=abc"


# ─── Amazon, faked ───────────────────────────────────────────────────────


def _certificate():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30)).sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM).decode()


SIGNING_KEY, CERT_PEM = _certificate()
OTHER_KEY, _ = _certificate()


def envelope(message, *, kind="Notification", topic=TOPIC, key=SIGNING_KEY, cert_url=CERT_URL) -> dict:
    msg = {
        "Type": kind, "MessageId": str(uuid.uuid4()), "TopicArn": topic,
        "Message": message if isinstance(message, str) else json.dumps(message),
        "Timestamp": NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "SignatureVersion": "2", "SigningCertURL": cert_url,
    }
    if kind == "SubscriptionConfirmation":
        msg.update(SubscribeURL=SUBSCRIBE_URL, Token="abc")
    text = sns.canonical(msg)
    msg["Signature"] = base64.b64encode(key.sign(text.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    return msg


def ses_notice(key: str, recipients, *, spam="PASS", virus="PASS", dmarc="PASS", dmarc_policy=None, bucket=BUCKET) -> dict:
    receipt = {
        "timestamp": NOW.isoformat(), "processingTimeMillis": 300, "recipients": list(recipients),
        "spamVerdict": {"status": spam}, "virusVerdict": {"status": virus}, "spfVerdict": {"status": "PASS"},
        "dkimVerdict": {"status": "PASS"}, "dmarcVerdict": {"status": dmarc},
        "action": {"type": "S3", "topicArn": TOPIC, "bucketName": bucket, "objectKeyPrefix": "", "objectKey": key},
    }
    if dmarc_policy:
        receipt["dmarcPolicy"] = dmarc_policy
    return {
        "notificationType": "Received",
        "mail": {"timestamp": NOW.isoformat(), "source": "ananya@example.org", "messageId": key, "destination": list(recipients),
                 "headersTruncated": False, "headers": [], "commonHeaders": {}},
        "receipt": receipt,
    }


class Amazon:
    """SNS's certificate host and subscription URL, the inbound bucket, the
    media bucket and SES's sending, all in memory."""

    def __init__(self):
        self.fetched: list[str] = []
        self.bucket: dict[str, bytes] = {}
        self.read: list[str] = []
        self.discarded: list[str] = []
        self.unreachable = False
        self.files: dict[str, bytes] = {}
        self.sent: list[dict] = []
        self.send_error = None

    def fetch(self, url: str) -> str:
        self.fetched.append(url)
        return CERT_PEM if url == CERT_URL else ""

    def read_raw(self, received):
        self.read.append(received.key)
        if self.unreachable:
            raise providers.Unreadable("EndpointConnectionError")
        data = self.bucket[received.key]
        if len(data) > int(settings.MAILBOX_INBOUND_MAX_MB) * 1024 * 1024:
            raise providers.TooLarge("big")
        return data

    def send_raw_email(self, Source, Destinations, RawMessage):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"source": Source, "destinations": list(Destinations), "raw": RawMessage["Data"]})
        return {"MessageId": f"0109019a-{len(self.sent):04d}-000000"}

    def mime(self, n: int = -1):
        return BytesParser(policy=policy.default).parsebytes(self.sent[n]["raw"].encode())


@pytest.fixture
def aws(monkeypatch):
    fake = Amazon()
    monkeypatch.setattr(sns, "fetch", fake.fetch)
    sns._certs.clear()
    monkeypatch.setattr(providers, "read", fake.read_raw)
    monkeypatch.setattr(providers, "discard", lambda received: fake.discarded.append(received.key))
    monkeypatch.setattr(store, "put", lambda key, data, ctype: fake.files.__setitem__(key, data))
    monkeypatch.setattr(store, "get", lambda key: fake.files.get(key))

    def delete(keys):
        keys = list(keys)
        for k in keys:
            fake.files.pop(k, None)
        return len(keys)

    monkeypatch.setattr(store, "delete", delete)
    monkeypatch.setattr(transport, "_ses", lambda: fake)
    monkeypatch.setattr(settings, "MAIL_PROVIDER", "ses")
    yield fake
    sns._certs.clear()


@pytest.fixture(autouse=True)
def _mailbox_on(monkeypatch):
    monkeypatch.setattr(settings, "CLUB_MAILBOX", True)
    monkeypatch.setattr(settings, "MAILBOX_DOMAIN", DOMAIN)
    monkeypatch.setattr(settings, "MAILBOX_INBOUND_BUCKET", BUCKET)
    monkeypatch.setattr(settings, "MAILBOX_SNS_TOPIC_ARN", TOPIC)
    monkeypatch.setattr(settings, "SES_REGION", "ap-south-1")
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    monkeypatch.setattr(settings, "LIVE_SSE", False)
    gates.reset()
    yield
    gates.reset()


@pytest.fixture
def owner_mails(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kwargs: sent.append(kwargs))
    return sent


@pytest.fixture
def changes(monkeypatch):
    told: list[tuple] = []

    async def changed(club_id, thread_id, kind):
        told.append((club_id, thread_id, kind))

    monkeypatch.setattr(live, "changed", changed)
    return told


def _migration(name):
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def plans(session):
    club_plans, mine = _migration("club_plans"), _migration("club_mailbox")
    connection = await session.connection()

    def seed(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            club_plans.seed_plans()
            mine.grant()

    await connection.run_sync(seed)
    rows = (await session.execute(select(Plans).options(selectinload(Plans.limits), selectinload(Plans.features)))).scalars().all()
    return {p.key: p for p in rows}


@pytest_asyncio.fixture
async def world(session, make_club, mint_token, plans):
    owner, club = await make_club(name="Yukthi", email="yukthi@example.com")
    other_owner, other_club = await make_club(name="Robotics Guild", email="robotics@example.com")
    small_owner, small_club = await make_club(name="Chess Circle", email="chess@example.com")
    await subscriptions.assign_plan(session, club_id=club.id, plan=plans["pro"], cycle=BillingCycle.monthly)
    await subscriptions.assign_plan(session, club_id=other_club.id, plan=plans["enterprise"], cycle=BillingCycle.monthly)
    await subscriptions.assign_plan(session, club_id=small_club.id, plan=plans["growth"], cycle=BillingCycle.monthly)
    await session.commit()
    return SimpleNamespace(
        owner=owner, club=club, other_owner=other_owner, other_club=other_club, small_owner=small_owner, small_club=small_club,
        plans=plans, h=auth_header(await mint_token(owner)), other_h=auth_header(await mint_token(other_owner)),
        small_h=auth_header(await mint_token(small_owner)),
    )


async def member(session, w, role, *, mail=False) -> dict:
    row = ClubMembers(club_id=w.club.id, email=f"{role}-{uuid.uuid4().hex[:6]}@example.com", full_name=f"Pat {role.title()}",
                      role=role, status="active", mail=mail)
    session.add(row)
    await session.commit()
    await session.refresh(w.owner)
    token = create_access_token(
        data=AuthTokenData(user_id=w.owner.id, token_type="access_token", token_version=w.owner.token_version, member_id=row.id).model_dump()
    )
    return auth_header(token)


async def set_up(client, w, handle="yukthi", headers=None):
    r = await client.put(f"{BASE}/address", json={"handle": handle}, headers=headers or w.h)
    assert r.status_code == 200, r.text
    return r.json()


def mime(*, subject="Is parking free?", sender=("Ananya Iyer", "ananya@example.org"), to=f"yukthi@{DOMAIN}", text="Is parking free?",
         html=None, message_id=None, in_reply_to=None, references=None, attachments=(), cc=None, date=None) -> bytes:
    msg = EmailMessage()
    msg["From"] = f'"{sender[0]}" <{sender[1]}>' if sender[0] else sender[1]
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or f"<{uuid.uuid4().hex}@example.org>"
    msg["Date"] = (date or NOW).strftime("%a, %d %b %Y %H:%M:%S +0000")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for name, ctype, content, cid in attachments:
        main, sub = ctype.split("/")
        if cid:
            msg.add_attachment(content, maintype=main, subtype=sub, filename=name, cid=f"<{cid}>", disposition="inline")
        else:
            msg.add_attachment(content, maintype=main, subtype=sub, filename=name)
    return msg.as_bytes()


async def deliver(client, aws, raw: bytes, recipients=(f"yukthi@{DOMAIN}",), *, key=None, **verdicts):
    key = key or uuid.uuid4().hex
    aws.bucket[key] = raw
    return await client.post(HOOK, json=envelope(ses_notice(key, recipients, **verdicts)))


async def threads_of(session, club_id):
    return (await session.scalars(
        select(ClubMailThreads).where(ClubMailThreads.club_id == club_id).order_by(ClubMailThreads.id)
        .execution_options(populate_existing=True)
    )).all()


async def messages_of(session, club_id):
    return (await session.scalars(
        select(ClubMailMessages).where(ClubMailMessages.club_id == club_id).order_by(ClubMailMessages.id)
        .execution_options(populate_existing=True)
    )).all()


async def send(client, w, headers=None, files=None, **fields):
    data = {"mode": "new", "to": "ananya@example.org", "subject": "Parking", "text": "Yes, parking is free.", **fields}
    return await client.post(f"{BASE}/messages", data=data, files=files or [], headers=headers or w.h)


# ─── The plan, the switch ────────────────────────────────────────────────


async def test_the_flag_is_on_pro_and_enterprise_alone(world):
    flags = {key: plan.features.mailbox for key, plan in world.plans.items()}
    assert {k for k, v in flags.items() if v} == {"pro", "enterprise"}
    assert gates.feature_message("mailbox", None) == "A club mailbox is not included in your plan."
    assert "mailbox" in gates.CLUB_WIDE_FEATURES


async def test_other_plans_see_the_lock_and_what_to_upgrade_to(client, session, world, aws):
    r = await client.put(f"{BASE}/address", json={"handle": "chess"}, headers=world.small_h)
    assert r.status_code == 403
    body = r.json()
    assert body["error_code"] == "PLAN_FEATURE_LOCKED" and body["errors"]["feature"] == "mailbox"
    assert body["errors"]["required_plan"] == "pro"
    page = (await client.get(BASE, headers=world.small_h)).json()
    assert page["plan_allows"] is False and page["upgrade"]["required_plan"] == "pro" and page["set_up"] is False
    assert await session.scalar(select(func.count()).select_from(ClubMailboxes)) == 0
    for h in (world.h, world.other_h):
        assert (await client.get(BASE, headers=h)).json()["plan_allows"] is True


async def test_off_the_section_is_hidden_and_mail_is_dropped(client, session, world, aws, monkeypatch):
    await set_up(client, world)
    monkeypatch.setattr(settings, "CLUB_MAILBOX", False)
    page = (await client.get(BASE, headers=world.h)).json()
    assert page["available"] is False and page["can_mail"] is False
    for method, path in [("GET", "/summary"), ("GET", "/threads"), ("PUT", "/address")]:
        r = await client.request(method, f"{BASE}{path}", json={"handle": "other"} if method == "PUT" else None, headers=world.h)
        assert r.status_code == 404 and r.json()["error_code"] == "MAILBOX_OFF", (path, r.text)
    r = await deliver(client, aws, mime())
    assert r.status_code == 200 and r.json()["dropped"] == {"off": 1}
    assert aws.read == [] and await messages_of(session, world.club.id) == []


# ─── The address ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("handle, code", [
    ("ab", "HANDLE_INVALID"), ("a" * 31, "HANDLE_INVALID"), ("yuk thi", "HANDLE_INVALID"), (".yukthi", "HANDLE_INVALID"),
    ("yukthi-", "HANDLE_INVALID"), ("yuk..thi", "HANDLE_INVALID"), ("yuk_thi", "HANDLE_INVALID"), ("yükthi", "HANDLE_INVALID"),
    ("admin", "HANDLE_RESERVED"), ("postmaster", "HANDLE_RESERVED"), ("abuse", "HANDLE_RESERVED"), ("support", "HANDLE_RESERVED"),
    ("info", "HANDLE_RESERVED"), ("noreply", "HANDLE_RESERVED"), ("no-reply", "HANDLE_RESERVED"), ("billing", "HANDLE_RESERVED"),
    ("security", "HANDLE_RESERVED"), ("Post.Master", "HANDLE_RESERVED"),
])
async def test_a_handle_that_cant_be_anyones_is_refused(client, session, world, aws, handle, code):
    r = await client.put(f"{BASE}/address", json={"handle": handle}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == code, r.text
    check = (await client.get(f"{BASE}/handle-check", params={"handle": handle}, headers=world.h)).json()
    assert check["available"] is False and check["code"] == code


async def test_setting_up_lowers_the_handle_and_audits_it(client, session, world, aws):
    page = await set_up(client, world, "Yukthi.Club")
    assert page["address"] == f"yukthi.club@{DOMAIN}" and page["handle"] == "yukthi.club" and page["set_up"] is True
    assert page["retention_months"] == 12 and page["can_manage"] and page["can_mail"] and page["is_owner"]
    log = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_mailbox.address"))
    assert log is not None and log.details["handle"] == "yukthi.club"


async def test_a_handle_is_unique_across_clubs(client, session, world, aws):
    await set_up(client, world, "yukthi")
    r = await client.put(f"{BASE}/address", json={"handle": "yukthi"}, headers=world.other_h)
    assert r.status_code == 409 and r.json()["error_code"] == "HANDLE_TAKEN"
    check = (await client.get(f"{BASE}/handle-check", params={"handle": "YUKTHI"}, headers=world.other_h)).json()
    assert check == {"handle": "yukthi", "address": f"yukthi@{DOMAIN}", "available": False, "code": "HANDLE_TAKEN",
                     "message": "That address is taken. Pick another."}
    mine = (await client.get(f"{BASE}/handle-check", params={"handle": "yukthi"}, headers=world.h)).json()
    assert mine["available"] is True


async def test_the_address_changes_once_a_month_and_the_old_one_forwards_90_days(client, session, world, aws):
    await set_up(client, world, "yukthi")
    page = await set_up(client, world, "yukthi.club")
    assert page["address"] == f"yukthi.club@{DOMAIN}"
    assert [p["address"] for p in page["previous"]] == [f"yukthi@{DOMAIN}"]
    assert page["next_change_at"] is not None
    r = await client.put(f"{BASE}/address", json={"handle": "yukthi.again"}, headers=world.h)
    assert r.status_code == 429 and r.json()["error_code"] == "HANDLE_CHANGED_RECENTLY"
    assert r.json()["errors"]["next_change_at"]
    # The old address still reaches the mailbox, and no other club can take it.
    r = await deliver(client, aws, mime(to=f"yukthi@{DOMAIN}"), [f"yukthi@{DOMAIN}"])
    assert r.json()["kept"] == 1
    [msg] = await messages_of(session, world.club.id)
    assert msg.delivered_to == f"yukthi@{DOMAIN}"
    r = await client.put(f"{BASE}/address", json={"handle": "yukthi"}, headers=world.other_h)
    assert r.status_code == 409
    # A month on, it may change again; 90 days on, the old one is let go.
    await session.execute(update(ClubMailboxes).where(ClubMailboxes.club_id == world.club.id)
                          .values(handle_changed_at=NOW - timedelta(days=31)))
    await session.execute(update(ClubMailboxAddresses).where(ClubMailboxAddresses.handle == "yukthi")
                          .values(forward_until=NOW - timedelta(minutes=1)))
    await session.commit()
    r = await deliver(client, aws, mime(to=f"yukthi@{DOMAIN}"), [f"yukthi@{DOMAIN}"])
    assert r.json()["kept"] == 0 and r.json()["dropped"] == {"unknown": 1}
    assert (await sweep.sweep(session))["released"] == 1
    other = await set_up(client, world, "yukthi", headers=world.other_h)
    assert other["address"] == f"yukthi@{DOMAIN}"


async def test_a_clubs_own_old_handle_comes_back_to_it(client, session, world, aws):
    await set_up(client, world, "yukthi")
    await set_up(client, world, "yukthi.club")
    await session.execute(update(ClubMailboxes).where(ClubMailboxes.club_id == world.club.id)
                          .values(handle_changed_at=NOW - timedelta(days=31)))
    await session.commit()
    page = await set_up(client, world, "yukthi")
    assert page["address"] == f"yukthi@{DOMAIN}" and [p["address"] for p in page["previous"]] == [f"yukthi.club@{DOMAIN}"]


def test_the_handle_rules():
    assert handles.problem("yukthi") is None and handles.problem("a.b-c") is None and handles.problem("abc") is None
    assert handles.problem("abuse")[0] == "HANDLE_RESERVED" and handles.problem("a-.b")[0] == "HANDLE_INVALID"
    assert handles.next_change_at(None) is None


async def test_only_our_domain_and_a_handle_resolve(session, world):
    assert await handles.resolve(session, f"yukthi@{DOMAIN}", DOMAIN) is None
    assert await handles.resolve(session, "yukthi@example.org", DOMAIN) is None
    assert await handles.resolve(session, f"admin@{DOMAIN}", DOMAIN) is None


def test_the_s3_reader_reads_our_bucket_within_its_limit(monkeypatch):
    class Body:
        def __init__(self, data):
            self.data = data

        def read(self, n=-1):
            return self.data if n < 0 else self.data[:n]

        def close(self):
            pass

    class S3:
        asked: list = []
        size = 10

        def get_object(self, Bucket, Key):
            self.asked.append((Bucket, Key))
            if Key == "gone":
                raise RuntimeError("NoSuchKey")
            return {"ContentLength": self.size, "Body": Body(b"x" * self.size)}

    fake = S3()
    monkeypatch.setattr(providers, "_s3", lambda: fake)
    monkeypatch.setattr(settings, "MAILBOX_INBOUND_BUCKET", BUCKET)
    received = providers.ses_received(ses_notice("abc/123", [f"Yukthi@{DOMAIN}"], spam="FAIL"))
    assert received.recipients == [f"yukthi@{DOMAIN}"] and received.spam and not received.virus
    assert providers.read(received) == b"x" * 10 and fake.asked == [(BUCKET, "abc/123")]
    fake.size = 41 * 1024 * 1024
    with pytest.raises(providers.TooLarge):
        providers.read(received)
    with pytest.raises(providers.Unreadable):
        providers.read(providers.Received(provider="ses", provider_message_id="g", recipients=[], bucket=BUCKET, key="gone"))
    for bad in ({"notificationType": "Bounce"}, {**ses_notice("../x", []), }, {**ses_notice("k", [], bucket="other")}):
        with pytest.raises(providers.NotOurs):
            providers.ses_received(bad)


# ─── The webhook: SNS ────────────────────────────────────────────────────


async def test_the_subscription_is_confirmed_by_fetching_its_url(client, session, world, aws):
    r = await client.post(HOOK, json=envelope("You have chosen to subscribe", kind="SubscriptionConfirmation"))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert aws.fetched == [CERT_URL, SUBSCRIBE_URL]


async def test_an_envelope_amazon_didnt_sign_is_refused_unread(client, session, world, aws):
    await set_up(client, world)
    aws.bucket["k1"] = mime()
    bad = envelope(ses_notice("k1", [f"yukthi@{DOMAIN}"]), key=OTHER_KEY)
    r = await client.post(HOOK, json=bad)
    assert r.status_code == 403 and r.json()["reason"] == "signature"
    tampered = envelope(ses_notice("k1", [f"yukthi@{DOMAIN}"]))
    tampered["Message"] = tampered["Message"].replace("PASS", "FAIL", 1)
    assert (await client.post(HOOK, json=tampered)).status_code == 403
    elsewhere = envelope(ses_notice("k1", [f"yukthi@{DOMAIN}"]), cert_url="https://evil.example.com/cert.pem")
    assert (await client.post(HOOK, json=elsewhere)).status_code == 403
    other_topic = envelope(ses_notice("k1", [f"yukthi@{DOMAIN}"]), topic="arn:aws:sns:ap-south-1:123456789012:someone-else")
    r = await client.post(HOOK, json=other_topic)
    assert r.status_code == 403 and r.json()["reason"] == "topic"
    unconfirmed = envelope("x", kind="SubscriptionConfirmation", topic="arn:aws:sns:ap-south-1:1:other")
    assert (await client.post(HOOK, json=unconfirmed)).status_code == 403
    assert SUBSCRIBE_URL not in aws.fetched
    assert aws.read == [] and await messages_of(session, world.club.id) == []


async def test_a_body_larger_than_sns_sends_is_refused(client, session, world, aws):
    r = await client.post(HOOK, content=b"{" + b" " * (400 * 1024) + b"}", headers={"content-type": "application/json"})
    assert r.status_code == 413


async def test_a_notification_about_another_bucket_is_not_read(client, session, world, aws):
    await set_up(client, world)
    aws.bucket["k2"] = mime()
    r = await client.post(HOOK, json=envelope(ses_notice("k2", [f"yukthi@{DOMAIN}"], bucket="someone-elses-bucket")))
    assert r.status_code == 200 and r.json()["reason"] == "not_ours"
    assert aws.read == []


# ─── Receiving ───────────────────────────────────────────────────────────


async def test_a_mail_is_read_from_the_bucket_parsed_and_kept(client, session, world, aws, changes):
    await set_up(client, world)
    logo = b"\x89PNG\r\n\x1a\nfake-logo"
    raw = mime(
        subject="=?utf-8?b?" + base64.b64encode("കാർ പാർക്കിംഗ് — free?".encode()).decode() + "?=",
        sender=("അനന്യ Iyer", "Ananya@Example.org"), cc="bala@example.org",
        text="Is parking free?\n> quoted before", html='<p onclick="x()">Is <b>parking</b> free?<script>alert(1)</script>'
        '<img src="https://tracker.example/p.gif"><img src="cid:logo1"></p>',
        attachments=[("മാപ്പ്.pdf", "application/pdf", b"%PDF-1.4 map", None), ("logo.png", "image/png", logo, "logo1")],
    )
    r = await deliver(client, aws, raw, key="ses-in-1")
    assert r.status_code == 200 and r.json()["kept"] == 1, r.text
    assert aws.read == ["ses-in-1"] and aws.discarded == ["ses-in-1"]
    [thread] = await threads_of(session, world.club.id)
    assert thread.folder == "inbox" and thread.unread_count == 1 and thread.subject == "കാർ പാർക്കിംഗ് — free?"
    assert thread.participants == ["ananya@example.org", "bala@example.org"] and thread.has_attachments
    [msg] = await messages_of(session, world.club.id)
    assert msg.from_address == "ananya@example.org" and msg.from_name == "അനന്യ Iyer"
    assert msg.provider == "ses" and msg.provider_message_id == "ses-in-1" and msg.verdicts["spam"] == "PASS"
    assert "script" not in msg.html_body and "onclick" not in msg.html_body and "<b>parking</b>" in msg.html_body
    assert msg.has_remote_images is True and msg.snippet == "Is parking free?"
    files = (await session.scalars(select(ClubMailAttachments).where(ClubMailAttachments.message_id == msg.id)
                                   .order_by(ClubMailAttachments.id))).all()
    assert [(a.filename, a.content_type, a.inline, a.content_id) for a in files] == [
        ("മാപ്പ്.pdf", "application/pdf", False, None), ("logo.png", "image/png", True, "logo1"),
    ]
    assert all(a.storage_key.startswith(f"mailbox/{world.club.id}/{msg.id}/") for a in files)
    assert aws.files[files[1].storage_key] == logo
    assert (world.club.id, thread.id, "message") in changes
    detail = (await client.get(f"{BASE}/threads/{thread.id}", headers=world.h)).json()
    assert detail["messages"][0]["subject"] == "കാർ പാർക്കിംഗ് — free?"
    assert [a["url"] for a in detail["messages"][0]["attachments"]] == [f"{API}/clubs/mailbox/attachments/{a.id}" for a in files]


def test_parsing_survives_what_senders_get_wrong():
    raw = (b"From: =?unknown-8bit?q?Bad=E9?= <x@y.org>\r\nTo: yukthi@" + DOMAIN.encode() + b"\r\nSubject: Caf\xe9\r\n"
           b"Content-Type: text/plain; charset=klingon\r\n\r\nCaf\xe9 au lait\r\n")
    parsed = parse.parse(raw)
    assert parsed.from_address == "x@y.org" and "Caf" in parsed.text
    html_only = EmailMessage()
    html_only["From"] = "a@b.org"
    html_only.set_content("<p>Hello <i>there</i></p>", subtype="html")
    parsed = parse.parse(html_only.as_bytes())
    assert parsed.text == "Hello there" and "<i>there</i>" in parsed.html


def test_html_is_cleaned_of_everything_that_runs():
    dirty = ('<a href="javascript:alert(1)">x</a><a href="https://ok.example" target="_self">ok</a><form><input></form>'
             '<iframe src="https://x"></iframe><img src="data:image/svg+xml;base64,PHN2Zz4=" onerror="x()"><style>p{}</style>')
    clean = sanitise.clean(dirty)
    assert "javascript" not in clean and "<form" not in clean and "<iframe" not in clean and "onerror" not in clean
    assert 'target="_blank"' in clean and "noopener" in clean and "svg" not in clean
    assert not sanitise.has_remote_images('<img src="cid:a">') and sanitise.has_remote_images('<td background="https://x/y.png">')


async def test_spam_goes_to_spam_and_raises_no_notice(client, session, world, aws):
    await set_up(client, world)
    await deliver(client, aws, mime(subject="WIN A PRIZE"), spam="FAIL")
    await deliver(client, aws, mime(subject="Pay now", sender=("", "x@spoof.example")), dmarc="FAIL", dmarc_policy="reject")
    folders = [t.folder for t in await threads_of(session, world.club.id)]
    assert folders == ["spam", "spam"]
    alerts = (await session.scalars(select(ClubAlerts).where(ClubAlerts.club_id == world.club.id))).all()
    assert [a for a in alerts if a.kind == "mail_message"] == []
    assert (await client.get(f"{BASE}/summary", headers=world.h)).json()["unread_total"] == 0
    listed = (await client.get(f"{BASE}/threads", params={"folder": "spam"}, headers=world.h)).json()
    assert listed["total"] == 2 and listed["counts"]["spam_unread"] == 2


async def test_a_virus_is_dropped_unread(client, session, world, aws, caplog):
    await set_up(client, world)
    with caplog.at_level(logging.INFO):
        r = await deliver(client, aws, mime(subject="invoice.exe"), virus="FAIL")
    assert r.status_code == 200 and r.json()["dropped"] == {"virus": 1}
    assert aws.read == [] and await messages_of(session, world.club.id) == []
    assert "invoice" not in caplog.text


async def test_mail_for_no_ones_address_is_dropped_and_never_bounced(client, session, world, aws, caplog):
    await set_up(client, world)
    with caplog.at_level(logging.INFO):
        r = await deliver(client, aws, mime(to=f"nobody@{DOMAIN}"), [f"nobody@{DOMAIN}", f"postmaster@{DOMAIN}"])
    assert r.status_code == 200 and r.json() == {"ok": True, "kept": 0, "dropped": {"unknown": 2}, "duplicate": 0}
    assert aws.sent == [] and aws.read == []
    assert "nobody@" not in caplog.text and "n***@mail.myotherapp.in" in caplog.text


async def test_the_same_mail_delivered_twice_is_kept_once(client, session, world, aws):
    await set_up(client, world)
    raw = mime()
    await deliver(client, aws, raw, key="dup-1")
    r = await deliver(client, aws, raw, key="dup-1")
    assert r.json()["duplicate"] == 1 and len(await messages_of(session, world.club.id)) == 1


async def test_a_bucket_that_cant_be_read_is_answered_503_and_comes_again(client, session, world, aws):
    await set_up(client, world)
    aws.unreachable = True
    r = await deliver(client, aws, mime(), key="later-1")
    assert r.status_code == 503 and r.json()["reason"] == "unreadable" and aws.discarded == []
    aws.unreachable = False
    r = await client.post(HOOK, json=envelope(ses_notice("later-1", [f"yukthi@{DOMAIN}"])))
    assert r.status_code == 200 and r.json()["kept"] == 1


async def test_a_mail_to_two_clubs_is_kept_by_each_apart(client, session, world, aws):
    await set_up(client, world, "yukthi")
    await set_up(client, world, "robotics", headers=world.other_h)
    r = await deliver(client, aws, mime(to=f"yukthi@{DOMAIN}, robotics@{DOMAIN}"), [f"yukthi@{DOMAIN}", f"robotics@{DOMAIN}"])
    assert r.json()["kept"] == 2 and aws.read == [aws.read[0]]
    mine, theirs = await messages_of(session, world.club.id), await messages_of(session, world.other_club.id)
    assert len(mine) == 1 and len(theirs) == 1 and mine[0].thread_id != theirs[0].thread_id


async def test_plus_addressing_reaches_the_club(client, session, world, aws):
    await set_up(client, world)
    r = await deliver(client, aws, mime(to=f"yukthi+fest@{DOMAIN}"), [f"yukthi+fest@{DOMAIN}"])
    assert r.json()["kept"] == 1


# ─── Threading ───────────────────────────────────────────────────────────


async def test_a_reply_to_what_the_club_sent_joins_its_thread_by_headers(client, session, world, aws):
    await set_up(client, world)
    sent = (await send(client, world)).json()
    assert sent["status"] == "sent"
    [out] = await messages_of(session, world.club.id)
    assert out.alt_message_id == "<0109019a-0001-000000@ap-south-1.amazonses.com>"
    # Their client quotes the Message-ID SES gave it, with another subject.
    await deliver(client, aws, mime(subject="Something else", in_reply_to=out.alt_message_id, references=out.alt_message_id))
    [thread] = await threads_of(session, world.club.id)
    assert thread.message_count == 2 and thread.folder == "inbox"
    # References alone, our own Message-ID in it, also joins.
    await deliver(client, aws, mime(subject="New words", references=f"<x@y> {out.message_id}"))
    assert len(await threads_of(session, world.club.id)) == 1


async def test_without_headers_the_subject_and_the_sender_join_within_30_days(client, session, world, aws):
    await set_up(client, world)
    await deliver(client, aws, mime(subject="Parking at the fest"))
    await deliver(client, aws, mime(subject="RE: Fwd: Re[2]: parking AT the fest"))
    assert len(await threads_of(session, world.club.id)) == 1
    # Another sender with the same subject: a thread of their own.
    await deliver(client, aws, mime(subject="Re: Parking at the fest", sender=("Bala", "bala@example.org")))
    assert len(await threads_of(session, world.club.id)) == 2
    # 31 days on, a new thread.
    await session.execute(update(ClubMailThreads).values(last_message_at=NOW - timedelta(days=31)))
    await session.commit()
    await deliver(client, aws, mime(subject="Re: Parking at the fest"))
    assert len(await threads_of(session, world.club.id)) == 3


async def test_spam_never_joins_a_clean_thread(client, session, world, aws):
    await set_up(client, world)
    first = mime(subject="Tickets", message_id="<first@example.org>")
    await deliver(client, aws, first)
    await deliver(client, aws, mime(subject="Re: Tickets", in_reply_to="<first@example.org>"), spam="FAIL")
    assert sorted(t.folder for t in await threads_of(session, world.club.id)) == ["inbox", "spam"]


def test_subject_keys():
    assert threads.subject_key("Re: FW: Aw: [club] Re[3]: Parking?") == "parking?"
    assert threads.subject_key("回复：停车") == "停车"
    assert threads.subject_key("Recipe ideas") == "recipe ideas"


# ─── Sending ─────────────────────────────────────────────────────────────


async def test_a_mail_goes_from_the_clubs_own_address_with_its_threading_headers(client, session, world, aws):
    await set_up(client, world)
    await client.put(f"{BASE}/signature", json={"signature": "Team Yukthi"}, headers=world.h)
    await client.patch(f"{BASE}/settings", json={"display_name": "Yukthi Fest"}, headers=world.h)
    await deliver(client, aws, mime(subject="Parking?", message_id="<q1@example.org>", references="<q0@example.org>",
                                    cc=f"bala@example.org, yukthi@{DOMAIN}"))
    [inbound] = await messages_of(session, world.club.id)
    r = await client.post(f"{BASE}/messages", data={
        "mode": "reply_all", "message_id": str(inbound.id), "text": "Yes, it is free.", "from": "ceo@bigcorp.com",
        "bcc": "secret@example.org",
    }, files=[("files", ("plan.pdf", b"%PDF-1.4 plan", "application/pdf"))], headers=world.h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "sent" and out["to"] == ["ananya@example.org"] and out["cc"] == ["bala@example.org"]
    assert out["subject"] == "Re: Parking?" and out["bcc"] == ["secret@example.org"]
    sent = aws.sent[-1]
    assert sent["source"] == f'Yukthi Fest <yukthi@{DOMAIN}>'
    assert sorted(sent["destinations"]) == ["ananya@example.org", "bala@example.org", "secret@example.org"]
    m = aws.mime()
    assert m["From"] == f"Yukthi Fest <yukthi@{DOMAIN}>" and "bigcorp" not in sent["raw"]
    assert m["To"] == "ananya@example.org" and m["Cc"] == "bala@example.org" and m["Bcc"] is None
    assert m["In-Reply-To"] == "<q1@example.org>" and m["References"] == "<q0@example.org> <q1@example.org>"
    assert m["Message-ID"].endswith(f"@{DOMAIN}>")
    plain = m.get_body(preferencelist=("plain",)).get_content()
    assert plain.startswith("Yes, it is free.\n\n-- \nTeam Yukthi") and "> Parking?" not in plain and "wrote:" in plain
    assert m.get_body(preferencelist=("html",)) is not None
    assert [p.get_filename() for p in m.iter_attachments()] == ["plan.pdf"]
    [thread] = await threads_of(session, world.club.id)
    assert thread.message_count == 2 and thread.last_outbound_at is not None


async def test_new_mail_starts_a_thread_in_sent_and_counts_one_email_credit(client, session, world, aws):
    await set_up(client, world)
    # Pro's 20,000 emails a month (migration message_quotas; club_plans' seed alone leaves it unlimited).
    world.plans["pro"].limits.emails_per_month = 20_000
    await session.commit()
    gates.reset()
    r = await send(client, world, to="Ananya <ananya@example.org>, b@example.org", cc="c@example.org")
    assert r.status_code == 200 and r.json()["status"] == "sent", r.text
    [thread] = await threads_of(session, world.club.id)
    assert thread.folder == "sent"
    inbox = (await client.get(f"{BASE}/threads", headers=world.h)).json()
    sent = (await client.get(f"{BASE}/threads", params={"folder": "sent"}, headers=world.h)).json()
    assert inbox["total"] == 0 and sent["total"] == 1
    usage = await session.scalar(select(MessageUsage).where(MessageUsage.club_id == world.club.id, MessageUsage.channel == "email"))
    assert usage.used == 1 and usage.kinds == {"mailbox": 1}
    page = (await client.get(BASE, headers=world.h)).json()
    assert page["credits_left"] == 19_999
    log = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_mailbox.send"))
    assert log.details["recipients"] == 3 and "ananya" not in json.dumps(log.details) and "Parking" not in (log.summary or "")


async def test_out_of_credits_the_send_is_refused_with_buy_more(client, session, world, aws, monkeypatch):
    await set_up(client, world)

    async def held(session, club_id, channel, kind, **kw):
        return None, "Not sent: this month's emails are used up."

    monkeypatch.setattr(quota, "check", held)
    r = await send(client, world)
    assert r.status_code == 402 and r.json()["error_code"] == "NO_EMAIL_CREDITS"
    assert "Buy more email credits" in r.json()["message"]
    assert aws.sent == [] and await messages_of(session, world.club.id) == []


async def test_the_prepaid_credits_are_used_when_they_exist(client, session, world, aws, monkeypatch):
    await set_up(client, world)
    left = {"n": 1}

    async def remaining(session, club_id, channel):
        assert channel == "email"
        return left["n"]

    monkeypatch.setattr(credits, "_credits_remaining", lambda: remaining)
    assert (await client.get(BASE, headers=world.h)).json()["credits_left"] == 1
    assert (await send(client, world)).status_code == 200
    left["n"] = 0
    r = await send(client, world)
    assert r.status_code == 402 and r.json()["error_code"] == "NO_EMAIL_CREDITS"


async def test_the_clubs_send_rate_is_limited(client, session, world, aws, monkeypatch):
    await set_up(client, world)
    monkeypatch.setattr(settings, "MAILBOX_SEND_PER_HOUR", 2)
    assert (await send(client, world)).status_code == 200
    assert (await send(client, world)).status_code == 200
    r = await send(client, world)
    assert r.status_code == 429 and r.json()["error_code"] == "MAILBOX_RATE_LIMITED"
    assert len(aws.sent) == 2


async def test_suppressed_addresses_are_refused_before_anything_goes(client, session, world, aws):
    await set_up(client, world)
    await suppression.add(session, "gone@example.org", suppression.BOUNCE, "550 no such user")
    await session.commit()
    r = await send(client, world, to="ananya@example.org, Gone@Example.org")
    assert r.status_code == 422 and r.json()["error_code"] == "RECIPIENT_SUPPRESSED"
    assert r.json()["errors"]["addresses"] == ["gone@example.org"]
    r = await send(client, world, to="someone@demo.myotherapp.in")
    assert r.status_code == 422 and r.json()["error_code"] == "RECIPIENT_SUPPRESSED"
    assert aws.sent == []


@pytest.mark.parametrize("fields, code", [
    ({"to": ""}, "NO_RECIPIENTS"), ({"to": "not-an-address"}, "INVALID_ADDRESS"), ({"text": "   "}, "EMPTY_MAIL"),
    ({"to": ",".join(f"p{i}@example.org" for i in range(51))}, "TOO_MANY_RECIPIENTS"), ({"mode": "reply"}, "NO_ORIGINAL"),
])
async def test_a_mail_that_cant_go_is_refused_in_words(client, session, world, aws, fields, code):
    await set_up(client, world)
    r = await send(client, world, **fields)
    assert r.status_code == 422 and r.json()["error_code"] == code, r.text


async def test_files_past_10_mb_together_are_refused(client, session, world, aws):
    await set_up(client, world)
    big = b"x" * (6 * 1024 * 1024)
    r = await send(client, world, files=[("files", ("a.bin", big, "application/octet-stream")), ("files", ("b.bin", big, "application/octet-stream"))])
    assert r.status_code == 413 and r.json()["error_code"] == "ATTACHMENTS_TOO_LARGE"
    assert aws.sent == []


async def test_a_refused_send_is_kept_failed_and_can_go_again(client, session, world, aws):
    from botocore.exceptions import ClientError

    await set_up(client, world)
    aws.send_error = ClientError({"Error": {"Code": "MessageRejected", "Message": "Email address is not verified."}}, "SendRawEmail")
    r = await send(client, world)
    assert r.status_code == 200 and r.json()["status"] == "failed" and "MessageRejected" in r.json()["error"]
    usage = await session.scalar(select(MessageUsage).where(MessageUsage.club_id == world.club.id))
    assert usage is None
    aws.send_error = None
    again = await client.post(f"{BASE}/messages/{r.json()['id']}/retry", headers=world.h)
    assert again.status_code == 200 and again.json()["status"] == "sent"
    r2 = await client.post(f"{BASE}/messages/{r.json()['id']}/retry", headers=world.h)
    assert r2.status_code == 409 and r2.json()["error_code"] == "NOT_FAILED"


async def test_a_forward_carries_the_originals_files(client, session, world, aws):
    await set_up(client, world)
    await deliver(client, aws, mime(attachments=[("map.pdf", "application/pdf", b"%PDF map", None)]))
    [inbound] = await messages_of(session, world.club.id)
    [file] = (await session.scalars(select(ClubMailAttachments).where(ClubMailAttachments.message_id == inbound.id))).all()
    r = await client.post(f"{BASE}/messages", data={
        "mode": "forward", "message_id": str(inbound.id), "to": "venue@example.org", "text": "FYI",
        "forward_attachments": str(file.id),
    }, headers=world.h)
    assert r.status_code == 200 and r.json()["subject"] == "Fwd: Is parking free?"
    m = aws.mime()
    assert [p.get_filename() for p in m.iter_attachments()] == ["map.pdf"]
    assert "Forwarded message" in m.get_body(preferencelist=("plain",)).get_content()
    assert len(await threads_of(session, world.club.id)) == 2


async def test_a_bounce_marks_the_mail_and_the_address(client, session, world, aws):
    from app.api.hooks import service as hooks
    from app.api.hooks.verdicts import Verdict
    from app.api.deliveries.models import DeliveryOutcome

    await set_up(client, world)
    sent = (await send(client, world, to="dead@example.org")).json()
    [msg] = await messages_of(session, world.club.id)
    await hooks.record(session, msg.provider_message_id, "email",
                       Verdict(outcome=DeliveryOutcome.bounced, reason="550 mailbox unavailable", code="550"), NOW,
                       address="dead@example.org")
    msg = (await messages_of(session, world.club.id))[0]
    assert sent["status"] == "sent" and msg.status == "bounced" and "550" in msg.error
    assert "dead@example.org" in await suppression.reasons(session, ["dead@example.org"])


def test_the_transport_carries_cc_and_headers():
    mail = transport.Mail(to=["a@x.org"], cc=["b@x.org"], subject="S", text="t", headers={"In-Reply-To": "<1@x>", "References": "<0@x> <1@x>"})
    m = transport.to_mime(mail, "Club <c@mail.myotherapp.in>")
    assert m["Cc"] == "b@x.org" and m["In-Reply-To"] == "<1@x>" and m["References"] == "<0@x> <1@x>"
    only_cc = transport._without_suppressed_domains(transport.Mail(to=["x@demo.myotherapp.in"], cc=["b@x.org"], subject="S"))
    assert only_cc.to == ["b@x.org"] and only_cc.cc == []
    assert outbound.ses_message_id("abc", "us-east-1") == "<abc@email.amazonses.com>"
    assert outbound.ses_message_id("abc", "ap-south-1") == "<abc@ap-south-1.amazonses.com>"


# ─── Reading ─────────────────────────────────────────────────────────────


async def test_search_finds_subject_sender_and_body_and_the_term_isnt_logged(client, session, world, aws):
    await set_up(client, world)
    await deliver(client, aws, mime(subject="Stall booking", text="We need a table"))
    await deliver(client, aws, mime(subject="Hello", sender=("Meera Nair", "meera@example.org"), text="About the 100% discount"))
    for q, found in [("stall", 1), ("meera", 1), ("TABLE", 1), ("100%", 1), ("nothing-like-it", 0), ("_", 0)]:
        r = (await client.get(f"{BASE}/threads", params={"q": q}, headers=world.h)).json()
        assert r["total"] == found, q
    assert redact.path(f"{BASE}/threads?folder=inbox&q=meera") == f"{BASE}/threads?folder=inbox&q=[Filtered]"
    assert redact.is_sensitive(f"{BASE}/messages") and redact.is_sensitive(HOOK)


async def test_reading_moving_and_deleting_a_thread(client, session, world, aws, changes):
    await set_up(client, world)
    await deliver(client, aws, mime(attachments=[("a.pdf", "application/pdf", b"%PDF", None)]))
    [thread] = await threads_of(session, world.club.id)
    assert (await client.get(f"{BASE}/summary", headers=world.h)).json()["unread_total"] == 1
    assert (await client.post(f"{BASE}/threads/{thread.id}/read", headers=world.h)).json()["unread_count"] == 0
    assert (await client.post(f"{BASE}/threads/{thread.id}/unread", headers=world.h)).json()["unread_count"] == 1
    assert (await client.patch(f"{BASE}/threads/{thread.id}", json={"folder": "archive"}, headers=world.h)).json()["folder"] == "archive"
    # An answer brings it back to the Inbox.
    await deliver(client, aws, mime(subject="Re: Is parking free?"))
    assert (await threads_of(session, world.club.id))[0].folder == "inbox"
    assert (await client.patch(f"{BASE}/threads/{thread.id}", json={"folder": "spam"}, headers=world.h)).json()["folder"] == "spam"
    assert (await client.patch(f"{BASE}/threads/{thread.id}", json={"folder": "inbox"}, headers=world.h)).json()["folder"] == "inbox"
    assert (await client.patch(f"{BASE}/threads/{thread.id}", json={"folder": "sent"}, headers=world.h)).status_code == 422
    assert aws.files
    r = await client.delete(f"{BASE}/threads/{thread.id}", headers=world.h)
    assert r.status_code == 200 and await threads_of(session, world.club.id) == [] and aws.files == {}
    assert (await client.get(f"{BASE}/threads/{thread.id}", headers=world.h)).status_code == 404


@pytest.mark.parametrize("ctype, name, served, disposition", [
    ("image/png", "logo.png", "image/png", "inline"),
    ("text/html", "page.html", "application/octet-stream", "attachment"),
    ("image/svg+xml", "x.svg", "application/octet-stream", "attachment"),
    ("application/pdf", "map.pdf", "application/pdf", "attachment"),
    ("application/x-msdownload", "run.exe", "application/octet-stream", "attachment"),
])
async def test_the_attachment_proxy_runs_nothing_on_our_origin(client, session, world, aws, ctype, name, served, disposition):
    await set_up(client, world)
    main, sub = ctype.split("/")
    await deliver(client, aws, mime(attachments=[(name, ctype, b"<script>alert(1)</script>", None)]))
    [file] = (await session.scalars(select(ClubMailAttachments))).all()
    r = await client.get(f"{BASE}/attachments/{file.id}", headers=world.h)
    assert r.status_code == 200 and r.content == b"<script>alert(1)</script>"
    assert r.headers["content-type"].split(";")[0] == served
    assert r.headers["content-disposition"].startswith(disposition)
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert r.headers["x-content-type-options"] == "nosniff" and "no-store" in r.headers["cache-control"]


def test_a_non_ascii_filename_is_served_in_ascii_and_in_utf8():
    media_type, headers = mailbox_router.media_headers("application/pdf", "മാപ്പ് plan.pdf")
    assert media_type == "application/pdf"
    assert headers["Content-Disposition"] == (
        "attachment; filename=\"plan.pdf\"; filename*=UTF-8''%E0%B4%AE%E0%B4%BE%E0%B4%AA%E0%B5%8D%E0%B4%AA%E0%B5%8D%20plan.pdf"
    )


async def test_the_bell_tells_only_those_with_the_mail(client, session, world, aws, make_admin):
    await set_up(client, world)
    editor = await member(session, world, "editor")
    helper = await member(session, world, "door", mail=True)
    await deliver(client, aws, mime(subject="Very private words"))
    [thread] = await threads_of(session, world.club.id)
    for h in (world.h, helper):
        items = [a for a in (await client.get(f"{API}/clubs/alerts", headers=h)).json()["items"] if a["kind"] == "mail_message"]
        assert [(a["title"], a["link"]) for a in items] == [("Ananya Iyer: 1 unread email", f"/mail?t={thread.id}")]
    assert (await client.get(f"{API}/clubs/alerts", headers=editor)).json()["items"] == []
    admin_h = await impersonating(session, make_admin, world)
    assert (await client.get(f"{API}/clubs/alerts", headers=admin_h)).json()["items"] == []
    assert "private" not in json.dumps((await client.get(f"{API}/clubs/alerts", headers=world.h)).json())
    await client.post(f"{BASE}/threads/{thread.id}/read", headers=world.h)
    assert [a for a in (await client.get(f"{API}/clubs/alerts", headers=world.h)).json()["items"] if a["kind"] == "mail_message"] == []


async def test_the_stream_is_off_without_live_sse(client, session, world, aws):
    await set_up(client, world)
    r = await client.get(f"{BASE}/stream", headers=world.h)
    assert r.status_code == 404 and r.json()["error_code"] == "MAILBOX_STREAM_OFF"


# ─── Who may ─────────────────────────────────────────────────────────────


async def test_the_mail_permission_decides_whatever_the_role(client, session, world, aws):
    await set_up(client, world)
    await deliver(client, aws, mime())
    [thread] = await threads_of(session, world.club.id)
    editor = await member(session, world, "editor")
    admin_role = await member(session, world, "admin")
    door_with = await member(session, world, "door", mail=True)
    for h in (editor, admin_role):
        for path in ("/summary", "/threads", f"/threads/{thread.id}"):
            r = await client.get(f"{BASE}{path}", headers=h)
            assert r.status_code == 403 and r.json()["error_code"] == "MAILBOX_PERMISSION", (path, r.text)
        assert (await send(client, world, headers=h)).status_code == 403
    assert (await client.get(f"{BASE}/threads/{thread.id}", headers=door_with)).status_code == 200
    assert (await send(client, world, headers=door_with)).json()["sent_by"] == "Pat Door"
    # The address: the owner and admins only.
    r = await client.patch(f"{BASE}/settings", json={"retention_months": 6}, headers=door_with)
    assert r.status_code == 403
    assert (await client.patch(f"{BASE}/settings", json={"retention_months": 6}, headers=admin_role)).status_code == 200
    assert (await client.get(BASE, headers=editor)).status_code == 403
    page = (await client.get(BASE, headers=admin_role)).json()
    assert page["can_manage"] is True and page["can_mail"] is False and page["signature"] is None
    assert access_policy.can("admin")["mailbox_manage"] is True and access_policy.can("editor")["mailbox_manage"] is False
    me = (await client.get(f"{API}/clubs/access/me", headers=door_with)).json()
    assert me["can"]["mail_inbox"] is True


async def test_only_the_owner_gives_the_mail_permission_and_never_an_admin_acting_as_the_club(client, session, world, aws, make_admin):
    row = ClubMembers(club_id=world.club.id, email="new@example.com", full_name="New", role="editor", status="active")
    session.add(row)
    await session.commit()
    r = await client.put(f"{API}/clubs/access/{row.id}/mail", json={"allowed": True}, headers=world.h)
    assert r.status_code == 200 and r.json()["mail"] is True
    admin_h = await impersonating(session, make_admin, world)
    r = await client.put(f"{API}/clubs/access/{row.id}/mail", json={"allowed": True}, headers=admin_h)
    assert r.status_code == 403 and r.json()["error_code"] == "MAILBOX_PRIVATE"
    assert await session.scalar(select(func.count()).select_from(AdminAuditLogs).where(AdminAuditLogs.action == "club_mailbox.permission")) == 1


async def test_an_admin_acting_as_the_club_reaches_no_mail(client, session, world, aws, make_admin):
    await set_up(client, world)
    await deliver(client, aws, mime(attachments=[("a.pdf", "application/pdf", b"%PDF", None)]))
    [thread] = await threads_of(session, world.club.id)
    [msg] = await messages_of(session, world.club.id)
    [file] = (await session.scalars(select(ClubMailAttachments))).all()
    h = await impersonating(session, make_admin, world)
    routes = [
        ("GET", "/summary", None), ("GET", "/threads", None), ("GET", "/threads?folder=spam", None), ("GET", f"/threads/{thread.id}", None),
        ("POST", f"/threads/{thread.id}/read", None), ("POST", f"/threads/{thread.id}/unread", None),
        ("PATCH", f"/threads/{thread.id}", {"folder": "archive"}), ("DELETE", f"/threads/{thread.id}", None),
        ("POST", f"/messages/{msg.id}/retry", None), ("GET", f"/attachments/{file.id}", None), ("GET", "/stream", None),
        ("GET", "/export", None), ("DELETE", "/mail?confirm=DELETE", None), ("PUT", "/signature", {"signature": "x"}),
    ]
    for method, path, body in routes:
        r = await client.request(method, f"{BASE}{path}", json=body, headers=h)
        assert r.status_code == 403 and r.json()["error_code"] == "MAILBOX_PRIVATE", (method, path, r.text)
    r = await send(client, world, headers=h)
    assert r.status_code == 403 and r.json()["error_code"] == "MAILBOX_PRIVATE"
    page = (await client.get(BASE, headers=h)).json()
    assert page["impersonated"] is True and page["can_mail"] is False and page["signature"] is None and page["address"]
    assert aws.sent == [] and len(await threads_of(session, world.club.id)) == 1


async def test_another_clubs_mail_is_no_such_mail(client, session, world, aws):
    await set_up(client, world, "yukthi")
    await set_up(client, world, "robotics", headers=world.other_h)
    await deliver(client, aws, mime(subject="Ours alone", attachments=[("a.pdf", "application/pdf", b"%PDF", None)]))
    [thread] = await threads_of(session, world.club.id)
    [msg] = await messages_of(session, world.club.id)
    [file] = (await session.scalars(select(ClubMailAttachments))).all()
    h = world.other_h
    for method, path in [("GET", f"/threads/{thread.id}"), ("POST", f"/threads/{thread.id}/read"), ("DELETE", f"/threads/{thread.id}"),
                         ("GET", f"/attachments/{file.id}"), ("POST", f"/messages/{msg.id}/retry")]:
        assert (await client.request(method, f"{BASE}{path}", headers=h)).status_code == 404, path
    r = await client.post(f"{BASE}/messages", data={"mode": "reply", "message_id": str(msg.id), "text": "hi"}, headers=h)
    assert r.status_code == 404
    r = await client.post(f"{BASE}/messages", data={"mode": "new", "to": "x@example.org", "text": "hi",
                                                     "forward_attachments": str(file.id)}, headers=h)
    assert r.status_code == 200 and list(aws.mime().iter_attachments()) == []
    assert (await client.get(f"{BASE}/threads", params={"q": "Ours alone"}, headers=h)).json()["total"] == 0


async def test_the_admin_card_has_counts_never_mail(client, session, world, aws, make_admin, mint_token):
    await set_up(client, world)
    await deliver(client, aws, mime(subject="Secret plans"))
    await deliver(client, aws, mime(subject="Prize"), spam="FAIL")
    await send(client, world)
    admin = await make_admin(email="card-admin@example.com")
    h = auth_header(await mint_token(admin))
    r = await client.get(f"{API}/admin/club-mailbox/{world.club.id}", headers=h)
    assert r.status_code == 200
    card = r.json()
    assert card["address"] == f"yukthi@{DOMAIN}" and card["state"] == "open" and card["plan_allows"] is True
    assert (card["received"], card["sent"], card["spam"], card["threads"]) == (2, 1, 1, 3)
    text = json.dumps(card)
    assert "Secret" not in text and "ananya" not in text.lower()
    assert (await client.get(f"{API}/admin/club-mailbox/{world.club.id}", headers=world.h)).status_code in (401, 403)


# ─── The plan lapsing, retention ─────────────────────────────────────────


async def test_leaving_pro_closes_the_mailbox_readable_30_days_then_export_or_delete(client, session, world, aws, owner_mails):
    await set_up(client, world)
    await deliver(client, aws, mime(subject="Before", attachments=[("a.pdf", "application/pdf", b"%PDF-1.4", None)]))
    [thread] = await threads_of(session, world.club.id)
    await subscriptions.assign_plan(session, club_id=world.club.id, plan=world.plans["free"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    done = await sweep.sweep(session)
    assert done["lapses"] == {"lapsed": 1}
    from app.core import after_commit

    await after_commit.drain()
    assert [m["subject"] for m in owner_mails] == ["Your plan no longer includes the club mailbox"]
    bell = (await client.get(f"{API}/clubs/alerts", headers=world.h)).json()["items"]
    assert any(a["kind"] == "mailbox_lapsed" for a in bell)
    page = (await client.get(BASE, headers=world.h)).json()
    assert page["state"] == "lapsed" and page["readable_until"] and page["upgrade"]["required_plan"] == "pro"
    # Nothing new in, nothing out; what is there is readable.
    r = await deliver(client, aws, mime(subject="After"))
    assert r.json()["dropped"] == {"closed": 1}
    assert (await session.scalar(select(ClubMailboxes.dropped_count).execution_options(populate_existing=True))) == 1
    r = await send(client, world)
    assert r.status_code == 403 and r.json()["error_code"] == "PLAN_FEATURE_LOCKED"
    assert (await client.get(f"{BASE}/threads/{thread.id}", headers=world.h)).status_code == 200
    # 30 days on: closed to reading, the owner told, export and deletion still work.
    await session.execute(update(ClubMailboxes).values(lapsed_at=NOW - timedelta(days=31)))
    await session.commit()
    assert (await sweep.sweep(session))["lapses"] == {"expired": 1}
    await after_commit.drain()
    assert owner_mails[-1]["subject"] == "The club mailbox's 30 days are over: export or delete its mail"
    r = await client.get(f"{BASE}/threads/{thread.id}", headers=world.h)
    assert r.status_code == 403 and r.json()["error_code"] == "MAILBOX_LAPSED"
    assert (await client.get(BASE, headers=world.h)).json()["state"] == "expired"
    editor = await member(session, world, "editor", mail=True)
    assert (await client.get(f"{BASE}/export", headers=editor)).status_code == 403
    r = await client.get(f"{BASE}/export", headers=world.h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/mbox")
    assert r.content.startswith(b"From ananya@example.org ") and b"Subject: Before" in r.content and b'filename="a.pdf"' in r.content
    assert (await client.delete(f"{BASE}/mail", params={"confirm": "yes"}, headers=world.h)).status_code == 422
    r = await client.delete(f"{BASE}/mail", params={"confirm": "DELETE"}, headers=world.h)
    assert r.status_code == 200 and r.json() == {"deleted_threads": 1}
    assert await threads_of(session, world.club.id) == [] and aws.files == {}
    # Upgrading opens it again.
    await subscriptions.assign_plan(session, club_id=world.club.id, plan=world.plans["enterprise"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    assert (await sweep.sweep(session))["lapses"] == {"reopened": 1}
    assert (await client.get(BASE, headers=world.h)).json()["state"] == "open"
    assert (await deliver(client, aws, mime())).json()["kept"] == 1


async def test_mail_past_its_retention_is_deleted_with_its_files(client, session, world, aws):
    await set_up(client, world)
    await deliver(client, aws, mime(subject="Old", attachments=[("old.pdf", "application/pdf", b"%PDF old", None)]))
    await deliver(client, aws, mime(subject="New"))
    old, new = await messages_of(session, world.club.id)
    await session.execute(update(ClubMailMessages).where(ClubMailMessages.id == old.id).values(created_at=NOW - timedelta(days=370)))
    await session.commit()
    assert (await sweep.sweep(session))["forgotten"] == 1
    assert [m.subject for m in await messages_of(session, world.club.id)] == ["New"]
    assert len(await threads_of(session, world.club.id)) == 1 and aws.files == {}
    # A shorter period of the club's own.
    await client.patch(f"{BASE}/settings", json={"retention_months": 1}, headers=world.h)
    await session.execute(update(ClubMailMessages).values(created_at=NOW - timedelta(days=40)))
    await session.commit()
    assert (await sweep.sweep(session))["forgotten"] == 1
    assert await threads_of(session, world.club.id) == []
    r = await client.patch(f"{BASE}/settings", json={"retention_months": 40}, headers=world.h)
    assert r.status_code == 422


# ─── What reaches a log, the settings check ──────────────────────────────


async def test_no_mail_content_reaches_the_log(client, session, world, aws, caplog):
    await set_up(client, world)
    with caplog.at_level(logging.DEBUG):
        await deliver(client, aws, mime(subject="Confidential budget", text="The password is hunter2"))
        await send(client, world, subject="Our secret reply", text="hunter3")
    assert "Confidential" not in caplog.text and "hunter2" not in caplog.text and "hunter3" not in caplog.text
    assert "ananya@example.org" not in caplog.text


def test_the_settings_check_warns_while_the_switch_is_on():
    s = SimpleNamespace(CLUB_MAILBOX=True, MAILBOX_DOMAIN="mail.myotherapp.in", MAILBOX_INBOUND_BUCKET="", MAILBOX_SNS_TOPIC_ARN="",
                        MAILBOX_RECEIVING_REGION="ap-south-1", MAIL_PROVIDER="ses")
    found = config_checks._club_mailbox(s)
    assert {p.setting for p in found} == {"MAILBOX_INBOUND_BUCKET", "MAILBOX_SNS_TOPIC_ARN"} and not any(p.fatal for p in found)
    s.MAILBOX_INBOUND_BUCKET, s.MAILBOX_SNS_TOPIC_ARN = BUCKET, "arn:aws:sns:us-east-1:1:x"
    assert [p.setting for p in config_checks._club_mailbox(s)] == ["MAILBOX_SNS_TOPIC_ARN"]
    s.MAILBOX_SNS_TOPIC_ARN = TOPIC
    assert config_checks._club_mailbox(s) == []
    s.CLUB_MAILBOX = False
    s.MAILBOX_INBOUND_BUCKET = ""
    assert config_checks._club_mailbox(s) == []


def test_the_mbox_escapes_from_lines():
    m = ClubMailMessages(direction="in", from_address="a@b.org", subject="S", text_body="From here on\n>From there", created_at=NOW)
    entry = export.mbox_entry(m, [])
    assert b"\n>From here on\n" in entry and b"\n>>From there\n" in entry
