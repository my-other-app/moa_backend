"""The platform's own inbox: admin@, developer@, developers@ and info@ in the
admin portal's Mail (app/api/platform_mail; feature-register/
mail-and-slugs-contract.md, Part M3).

Mail arrives through the same two hooks the clubs' does (Cloudflare's
Worker and SES's notification), faked as tests/test_club_mailbox.py and
tests/test_club_mailbox_cloudflare.py fake them; it is kept before the desk
and the clubs see it, and a mail also to a club is kept by both. Admins with
the owner or support role read and answer it; a viewer never; only an owner
deletes. What is checked is what the admins, the sender and the logs see.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.club_mailbox import cloudflare, handles
from app.api.club_mailbox.inbound import Outcome as ClubOutcome
from app.api.platform_mail import inbound as platform_inbound
from app.api.platform_mail.models import PlatformMailAttachments, PlatformMailMessages, PlatformMailThreads
from app.api.subscriptions import gates
from app.api.users.models import AdminRoles
from app.config import settings
from app.core import redact
from app.core.mail import suppression
from tests.conftest import auth_header
from tests.test_club_mailbox import (  # noqa: F401 - fixtures
    BUCKET,
    LONG_AGO,
    TOPIC,
    aws,
    envelope,
    messages_of,
    mime,
    plans,
    ses_notice,
    set_up,
    world,
)
from tests.test_club_mailbox_cloudflare import KEY, post

API = "/api/v1"
MAIL = f"{API}/admin/mail"
SES_HOOK = f"{API}/hooks/mailbox/ses"
DOMAIN = "myotherapp.in"
INFO, ADMIN, DEVELOPER, DEVELOPERS = (f"{x}@{DOMAIN}" for x in ("info", "admin", "developer", "developers"))
NOW = datetime.now(timezone.utc)
DMARC_FAIL = "mx.cloudflare.net; dkim=fail header.d=example.org; dmarc=fail header.from=example.org; spf=fail smtp.mailfrom=asha@example.org"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(settings, "CLUB_MAILBOX", True)
    monkeypatch.setattr(settings, "MAILBOX_DOMAIN", DOMAIN)
    monkeypatch.setattr(settings, "MAILBOX_HOOK_SECRET", KEY)
    monkeypatch.setattr(settings, "MAILBOX_INBOUND_BUCKET", BUCKET)
    monkeypatch.setattr(settings, "MAILBOX_SNS_TOPIC_ARN", TOPIC)
    monkeypatch.setattr(settings, "SES_REGION", "ap-south-1")
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    monkeypatch.setattr(settings, "LIVE_SSE", False)
    monkeypatch.setattr(settings, "SUPPORT_INBOUND_ADDRESSES", "")
    monkeypatch.setattr(settings, "SALES_INBOUND_ADDRESSES", "")
    monkeypatch.setattr(settings, "PLATFORM_INBOX_ADDRESSES", ",".join((ADMIN, DEVELOPER, DEVELOPERS, INFO)))
    monkeypatch.setattr(settings, "PLATFORM_MAIL_SEND_PER_HOUR", 200)
    monkeypatch.setattr(settings, "MAIL_FROM_NAME", "MyOtherApp")
    gates.reset()
    yield
    gates.reset()


@pytest.fixture
async def admins(make_admin, mint_token, session):
    viewer = await make_admin(email="viewer-inbox@example.com", role=AdminRoles.viewer)
    support = await make_admin(email="support-inbox@example.com", role=AdminRoles.support)
    owner = await make_admin(email="owner-inbox@example.com", role=AdminRoles.owner)
    support.full_name = "Sita Support"
    await session.commit()
    return SimpleNamespace(
        viewer=auth_header(await mint_token(viewer)), support=auth_header(await mint_token(support)),
        owner=auth_header(await mint_token(owner)), support_user=support,
    )


def a_mail(to=INFO, **kw):
    kw.setdefault("sender", ("Asha Rao", "asha@example.org"))
    kw.setdefault("subject", "Partnership")
    kw.setdefault("text", "Hello team, can we partner?")
    return mime(to=to, **kw)


async def ses(client, aws, raw: bytes, recipients, **verdicts):
    key = uuid.uuid4().hex
    aws.bucket[key] = raw
    return await client.post(SES_HOOK, json=envelope(ses_notice(key, recipients, **verdicts)))


async def threads(session):
    return (await session.scalars(
        select(PlatformMailThreads).order_by(PlatformMailThreads.id).execution_options(populate_existing=True)
    )).all()


async def messages(session):
    return (await session.scalars(
        select(PlatformMailMessages).order_by(PlatformMailMessages.id).execution_options(populate_existing=True)
    )).all()


async def write(client, headers, files=None, **fields):
    data = {"mode": "new", "from_mailbox": "info", "to": "asha@example.org", "subject": "Hello", "text": "Thanks for writing.", **fields}
    return await client.post(f"{MAIL}/messages", data=data, files=files or [], headers=headers)


# ─── Arriving ────────────────────────────────────────────────────────────


async def test_mail_to_info_is_kept_in_the_platform_inbox_once(client, session, aws):
    raw = a_mail()
    r = await post(client, raw, to=INFO)
    assert r.status_code == 200 and r.json()["action"] == "accept", r.text
    assert r.json()["platform"] == 1 and r.json()["kept"] == 0
    [thread] = await threads(session)
    assert thread.mailbox == "info" and thread.folder == "inbox" and thread.unread_count == 1
    assert thread.subject == "Partnership" and thread.participants == ["asha@example.org"]
    [msg] = await messages(session)
    assert msg.mailbox == "info" and msg.delivered_to == INFO and msg.provider == "cloudflare" and msg.unread
    # The sending server tries again: accepted, kept once.
    again = await post(client, raw, to=INFO)
    assert again.json()["action"] == "accept" and again.json()["platform"] == 0
    assert len(await messages(session)) == 1


async def test_plus_addressing_and_case_reach_the_base(client, session, aws):
    r = await post(client, a_mail(to="Info+Press@MyOtherApp.in"), to="Info+Press@MyOtherApp.in")
    assert r.json()["action"] == "accept" and r.json()["platform"] == 1
    [msg] = await messages(session)
    assert msg.mailbox == "info" and msg.delivered_to == "info+press@myotherapp.in"


async def test_a_mail_to_a_platform_address_and_a_club_is_kept_by_both(client, session, world, aws):
    await set_up(client, world, "yukthi")
    raw = a_mail(to=f"{DEVELOPERS}, yukthi@{DOMAIN}")
    r = await ses(client, aws, raw, [DEVELOPERS, f"yukthi@{DOMAIN}"])
    assert r.status_code == 200 and r.json()["kept"] == 1 and r.json()["platform"] == 1, r.text
    assert r.json()["dropped"] == {}
    assert len(aws.read) == 1, "the raw mail is read once for everyone"
    [msg] = await messages(session)
    assert msg.mailbox == "developers" and msg.provider == "ses"
    assert len(await messages_of(session, world.club.id)) == 1


async def test_two_platform_addresses_keep_a_copy_each(client, session, aws):
    r = await ses(client, aws, a_mail(to=f"{INFO}, {ADMIN}"), [INFO, ADMIN])
    assert r.json()["platform"] == 2
    assert sorted(t.mailbox for t in await threads(session)) == ["admin", "info"]
    again = await ses(client, aws, a_mail(to=f"{INFO}, {ADMIN}", message_id="<same@example.org>"), [INFO, ADMIN])
    assert again.json()["platform"] == 2


async def test_a_dmarc_failure_is_kept_in_spam(client, session, aws):
    r = await post(client, a_mail(), to=INFO, auth=DMARC_FAIL)
    assert r.json()["action"] == "accept"
    [thread] = await threads(session)
    assert thread.folder == "spam"
    [msg] = await messages(session)
    assert msg.verdicts["dmarc"] == "FAIL"


async def test_a_virus_is_dropped_unread(client, session, aws):
    r = await ses(client, aws, a_mail(), [INFO], virus="FAIL")
    assert r.json()["platform"] == 0 and aws.read == [] and await messages(session) == []


async def test_without_the_setting_the_address_is_nobodys(client, session, aws, monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_INBOX_ADDRESSES", "")
    r = await post(client, a_mail(), to=INFO)
    assert r.json()["action"] == "reject" and r.json()["text"] == "No mailbox here by that name"
    assert "platform" not in r.json() and await messages(session) == []


async def test_a_clubs_mail_says_nothing_of_the_platform(client, session, world, aws):
    await set_up(client, world, "yukthi")
    r = await post(client, a_mail(to=f"yukthi@{DOMAIN}"), to=f"yukthi@{DOMAIN}")
    assert r.json()["action"] == "accept" and r.json()["kept"] == 1 and "platform" not in r.json()
    assert await messages(session) == []


# ─── Reading ─────────────────────────────────────────────────────────────


async def test_only_owner_and_support_admins_open_it(client, session, aws, admins, world):
    r = await client.get(MAIL, headers=admins.viewer)
    assert r.status_code == 403 and r.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"
    assert (await client.get(f"{MAIL}/threads", headers=admins.viewer)).status_code == 403
    assert (await client.get(MAIL, headers=world.h)).status_code == 403
    await post(client, a_mail(), to=INFO)
    body = (await client.get(MAIL, headers=admins.support)).json()
    assert [a["mailbox"] for a in body["addresses"]] == ["admin", "developer", "developers", "info"]
    assert {a["mailbox"]: a["unread"] for a in body["addresses"]}["info"] == 1 and body["unread_total"] == 1
    assert body["addresses"][-1]["address"] == INFO and body["can_delete"] is False
    assert (await client.get(MAIL, headers=admins.owner)).json()["can_delete"] is True


async def test_listing_reading_and_moving(client, session, aws, admins):
    await post(client, a_mail(subject="Partnership"), to=INFO)
    await post(client, a_mail(to=ADMIN, subject="Invoice question", sender=("Ravi", "ravi@example.org")), to=ADMIN)
    h = admins.support
    listed = (await client.get(f"{MAIL}/threads", headers=h)).json()
    assert listed["total"] == 2 and [t["mailbox"] for t in listed["items"]] == ["admin", "info"]
    assert listed["counts"]["unread_total"] == 2
    only = (await client.get(f"{MAIL}/threads", params={"mailbox": "info"}, headers=h)).json()
    assert only["total"] == 1 and only["items"][0]["subject"] == "Partnership" and only["counts"]["unread_total"] == 1
    assert (await client.get(f"{MAIL}/threads", params={"mailbox": INFO}, headers=h)).json()["total"] == 1
    r = await client.get(f"{MAIL}/threads", params={"mailbox": "sales"}, headers=h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_MAILBOX"
    found = (await client.get(f"{MAIL}/threads", params={"q": "ravi"}, headers=h)).json()
    assert found["total"] == 1 and found["items"][0]["mailbox"] == "admin"
    paged = (await client.get(f"{MAIL}/threads", params={"page": 2, "page_size": 1}, headers=h)).json()
    assert paged["total"] == 2 and len(paged["items"]) == 1 and paged["page"] == 2
    thread_id = only["items"][0]["id"]
    detail = (await client.get(f"{MAIL}/threads/{thread_id}", headers=h)).json()
    assert detail["thread"]["unread_count"] == 1, "opening it doesn't mark it read"
    assert detail["messages"][0]["text"].startswith("Hello team") and detail["messages"][0]["mailbox"] == "info"
    assert INFO in detail["own_addresses"] and ADMIN in detail["own_addresses"]
    assert (await client.post(f"{MAIL}/threads/{thread_id}/read", headers=h)).json()["unread_count"] == 0
    assert (await client.get(MAIL, headers=h)).json()["unread_total"] == 1
    assert (await client.post(f"{MAIL}/threads/{thread_id}/unread", headers=h)).json()["unread_count"] == 1
    moved = await client.patch(f"{MAIL}/threads/{thread_id}", json={"folder": "archive"}, headers=h)
    assert moved.status_code == 200 and moved.json()["folder"] == "archive"
    assert (await client.get(f"{MAIL}/threads", params={"folder": "archive"}, headers=h)).json()["total"] == 1
    assert (await client.patch(f"{MAIL}/threads/{thread_id}", json={"folder": "spam"}, headers=h)).json()["folder"] == "spam"
    assert (await client.patch(f"{MAIL}/threads/{thread_id}", json={"folder": "sent"}, headers=h)).status_code == 422
    assert (await client.get(f"{MAIL}/threads/999999", headers=h)).status_code == 404


async def test_only_an_owner_deletes_a_conversation(client, session, aws, admins):
    await post(client, a_mail(attachments=[("map.pdf", "application/pdf", b"%PDF map", None)]), to=INFO)
    [thread] = await threads(session)
    assert list(aws.files) and all(k.startswith("platform-mail/") for k in aws.files)
    r = await client.delete(f"{MAIL}/threads/{thread.id}", headers=admins.support)
    assert r.status_code == 403 and r.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"
    r = await client.delete(f"{MAIL}/threads/{thread.id}", headers=admins.owner)
    assert r.status_code == 200 and r.json() == {"deleted": thread.id}
    assert await threads(session) == [] and aws.files == {}
    log = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "platform_mail.delete"))
    assert log.details["messages"] == 1 and "Partnership" not in (log.summary or "")


async def test_files_are_served_to_the_admins_alone_and_run_nothing(client, session, aws, admins):
    await post(client, a_mail(attachments=[("page.html", "text/html", b"<script>alert(1)</script>", None),
                                           ("logo.png", "image/png", b"\x89PNG fake", "logo1")]), to=INFO)
    files = {a.filename: a for a in (await session.scalars(select(PlatformMailAttachments))).all()}
    r = await client.get(f"{MAIL}/attachments/{files['page.html'].id}", headers=admins.support)
    assert r.status_code == 200 and r.content == b"<script>alert(1)</script>"
    assert r.headers["content-type"].split(";")[0] == "application/octet-stream"
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox" and r.headers["x-content-type-options"] == "nosniff"
    r = await client.get(f"{MAIL}/attachments/{files['logo.png'].id}", headers=admins.support)
    assert r.headers["content-disposition"].startswith("inline") and files["logo.png"].inline
    assert (await client.get(f"{MAIL}/attachments/{files['logo.png'].id}", headers=admins.viewer)).status_code == 403
    assert (await client.get(f"{MAIL}/attachments/999999", headers=admins.support)).status_code == 404


# ─── Writing ─────────────────────────────────────────────────────────────


async def test_new_mail_goes_from_the_picked_platform_address(client, session, aws, admins):
    r = await write(client, admins.support, from_mailbox="developers", to="Dev <dev@partner.org>", subject="API keys",
                    text="Here they are.", from_address="ceo@bigcorp.com",
                    files=[("files", ("keys.txt", b"k-123", "text/plain"))])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "sent" and out["mailbox"] == "developers" and out["from_address"] == DEVELOPERS
    assert out["from_name"] == "MyOtherApp" and out["sent_by"] == "Sita Support" and out["to"] == ["dev@partner.org"]
    sent = aws.sent[-1]
    assert sent["source"] == f"MyOtherApp <{DEVELOPERS}>" and "bigcorp" not in sent["raw"]
    m = aws.mime()
    assert m["From"] == f"MyOtherApp <{DEVELOPERS}>" and m["Message-ID"].endswith(f"@{DOMAIN}>")
    assert m.get_body(preferencelist=("plain",)).get_content().startswith("Here they are.")
    assert [p.get_filename() for p in m.iter_attachments()] == ["keys.txt"]
    [thread] = await threads(session)
    assert thread.folder == "sent" and thread.mailbox == "developers"
    assert (await client.get(f"{MAIL}/threads", params={"folder": "sent"}, headers=admins.support)).json()["total"] == 1
    assert (await client.get(f"{MAIL}/threads", headers=admins.support)).json()["total"] == 0
    log = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "platform_mail.send"))
    assert log.details["recipients"] == 1 and log.details["sent_by"] == "Sita Support" and log.details["mailbox"] == "developers"
    assert "dev@partner.org" not in json.dumps(log.details) and "API keys" not in (log.summary or "")


async def test_a_reply_goes_from_the_address_it_came_to_and_the_answer_joins_it(client, session, aws, admins):
    await post(client, a_mail(to=DEVELOPER, subject="Webhooks?", message_id="<q1@example.org>",
                              cc=f"bala@example.org, {INFO}"), to=DEVELOPER)
    [inbound] = await messages(session)
    r = await client.post(f"{MAIL}/messages", data={"mode": "reply_all", "message_id": str(inbound.id), "text": "Yes, they are signed."},
                          headers=admins.owner)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["from_address"] == DEVELOPER and out["to"] == ["asha@example.org"] and out["cc"] == ["bala@example.org"]
    assert out["subject"] == "Re: Webhooks?"
    m = aws.mime()
    assert m["In-Reply-To"] == "<q1@example.org>" and "wrote:" in m.get_body(preferencelist=("plain",)).get_content()
    sent = (await messages(session))[-1]
    answer = a_mail(to=DEVELOPER, subject="Re: Webhooks?", in_reply_to=sent.alt_message_id, references=f"<q1@example.org> {sent.alt_message_id}")
    await post(client, answer, to=DEVELOPER)
    [thread] = await threads(session)
    # Joined by its headers; the first mail was never marked read, so two wait.
    assert thread.message_count == 3 and thread.folder == "inbox" and thread.unread_count == 2
    r = await write(client, admins.owner, from_mailbox="sales")
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_MAILBOX"


@pytest.mark.parametrize("fields, status, code", [
    ({"to": ""}, 422, "NO_RECIPIENTS"), ({"to": "not-an-address"}, 422, "INVALID_ADDRESS"), ({"text": "  "}, 422, "EMPTY_MAIL"),
    ({"to": ",".join(f"p{i}@example.org" for i in range(51))}, 422, "TOO_MANY_RECIPIENTS"), ({"mode": "reply"}, 422, "NO_ORIGINAL"),
    ({"to": "gone@example.org"}, 422, "RECIPIENT_SUPPRESSED"), ({"from_mailbox": ""}, 422, "INVALID_MAILBOX"),
])
async def test_a_mail_that_cant_go_is_refused_in_words(client, session, aws, admins, fields, status, code):
    await suppression.add(session, "gone@example.org", suppression.BOUNCE, "550 no such user")
    await session.commit()
    r = await write(client, admins.support, **fields)
    assert r.status_code == status and r.json()["error_code"] == code, r.text
    assert aws.sent == [] and await messages(session) == []


async def test_who_may_send_and_how_much(client, session, aws, admins, monkeypatch):
    assert (await write(client, admins.viewer)).status_code == 403
    monkeypatch.setattr(settings, "PLATFORM_MAIL_SEND_PER_HOUR", 1)
    assert (await write(client, admins.support)).json()["status"] == "sent"
    r = await write(client, admins.owner)
    assert r.status_code == 429 and r.json()["error_code"] == "PLATFORM_MAIL_RATE_LIMITED"
    monkeypatch.setattr(settings, "PLATFORM_INBOX_ADDRESSES", "")
    r = await write(client, admins.owner)
    assert r.status_code == 404 and r.json()["error_code"] == "PLATFORM_MAIL_OFF"


async def test_a_refused_send_is_kept_failed_and_can_go_again(client, session, aws, admins):
    from botocore.exceptions import ClientError

    aws.send_error = ClientError({"Error": {"Code": "MessageRejected", "Message": "Email address is not verified."}}, "SendRawEmail")
    r = await write(client, admins.support)
    assert r.status_code == 200 and r.json()["status"] == "failed" and "MessageRejected" in r.json()["error"]
    aws.send_error = None
    again = await client.post(f"{MAIL}/messages/{r.json()['id']}/retry", headers=admins.support)
    assert again.status_code == 200 and again.json()["status"] == "sent"
    r2 = await client.post(f"{MAIL}/messages/{r.json()['id']}/retry", headers=admins.support)
    assert r2.status_code == 409 and r2.json()["error_code"] == "NOT_FAILED"


async def test_a_bounce_marks_the_mail(client, session, aws, admins):
    from app.api.deliveries.models import DeliveryOutcome
    from app.api.hooks import service as hooks
    from app.api.hooks.verdicts import Verdict

    await write(client, admins.support, to="dead@example.org")
    [msg] = await messages(session)
    await hooks.record(session, msg.provider_message_id, "email",
                       Verdict(outcome=DeliveryOutcome.bounced, reason="550 mailbox unavailable", code="550"), NOW,
                       address="dead@example.org")
    [msg] = await messages(session)
    assert msg.status == "bounced" and "550" in msg.error


async def test_no_mail_content_reaches_the_log(client, session, aws, admins, caplog):
    with caplog.at_level(logging.DEBUG):
        await post(client, a_mail(subject="Confidential budget", text="The password is hunter2"), to=INFO)
        await write(client, admins.support, subject="Our secret reply", text="hunter3")
    assert "Confidential" not in caplog.text and "hunter2" not in caplog.text and "hunter3" not in caplog.text
    assert "asha@example.org" not in caplog.text


# Tests that open no database come last: a run that starts with one sets the
# suite's loop up apart from the session's engine (tests/conftest.py).


def test_the_platform_addresses_are_never_a_clubs():
    for local in ("admin", "developer", "developers", "info"):
        assert handles.problem(local)[0] == "HANDLE_RESERVED"


def test_the_inbox_is_kept_out_of_the_logs():
    assert redact.is_sensitive("/api/v1/admin/mail/threads/7")
    assert redact.is_sensitive("/api/v1/admin/mail")
    assert not redact.is_sensitive("/api/v1/admin/mailing-lists")
    assert redact.path("/api/v1/admin/mail/threads?folder=inbox&q=asha") == "/api/v1/admin/mail/threads?folder=inbox&q=[Filtered]"


def test_cloudflare_accepts_what_the_platform_kept():
    kept = platform_inbound.Outcome(kept=[("info", 1)], addressed=True)
    again = platform_inbound.Outcome(duplicate=1, addressed=True)
    dropped = platform_inbound.Outcome(addressed=True)
    dropped.drop("too_large")
    assert cloudflare.decision({}, ClubOutcome(), kept) == ("accept", None)
    assert cloudflare.decision({}, ClubOutcome(), again) == ("accept", None)
    assert cloudflare.decision({}, ClubOutcome(), dropped) == ("reject", "too_large")
    assert cloudflare.decision({}, ClubOutcome()) == ("reject", "unknown")


def test_the_settings_lower_and_strip_plus_tags(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_INBOX_ADDRESSES", " Info@MyOtherApp.in ; ops+desk@myotherapp.in, not-an-address ")
    assert settings.platform_inbox_addresses == frozenset({"info@myotherapp.in", "ops@myotherapp.in"})
