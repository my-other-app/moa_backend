"""Clubs' mail through Cloudflare Email Routing: the club-mail Worker posts
each mail here (app/api/club_mailbox/cloudflare.py, /hooks/mailbox/cloudflare).

The Worker is played by `post()`: it signs the envelope the way the Worker
does (infra/cloudflare/club-mail/worker.js) and sends the raw MIME. What is
checked: the signature (nothing read without it), what the Worker is told
(accept, or reject with words the sender's server shows), what the club sees
in its dashboard, and the team desk's support@ through the same door.

Addresses are on the platform's own domain now: yukthi@myotherapp.in.
"""
import hashlib
import hmac
import time
from urllib.parse import quote

import pytest
from sqlalchemy import select

from app.api.club_mailbox import cloudflare, handles
from app.api.club_mailbox.models import ClubMailboxes
from app.api.subscriptions import gates
from app.api.support.models import SupportCases
from app.config import settings
from tests.support_desk_helpers import _reference_fresh, desk, desk_files, outbox, pushes  # noqa: F401 - fixtures
from tests.test_club_mailbox import (  # noqa: F401 - fixtures
    BASE,
    LONG_AGO,
    aws,
    changes,
    messages_of,
    mime,
    plans,
    set_up,
    threads_of,
    world,
)

API = "/api/v1"
HOOK = f"{API}/hooks/mailbox/cloudflare"
DOMAIN = "myotherapp.in"
KEY = "k" * 64
CF_PASS = "mx.cloudflare.net; dkim=pass header.d=example.org header.s=s1; dmarc=pass header.from=example.org; spf=pass (mx.cloudflare.net: domain of ananya@example.org designates 192.0.2.1 as permitted sender) smtp.mailfrom=ananya@example.org"


@pytest.fixture(autouse=True)
def _cloudflare_on(monkeypatch):
    monkeypatch.setattr(settings, "CLUB_MAILBOX", True)
    monkeypatch.setattr(settings, "MAILBOX_DOMAIN", DOMAIN)
    monkeypatch.setattr(settings, "MAILBOX_HOOK_SECRET", KEY)
    monkeypatch.setattr(settings, "MAILBOX_INBOUND_BUCKET", "")
    monkeypatch.setattr(settings, "MAILBOX_SNS_TOPIC_ARN", "")
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    monkeypatch.setattr(settings, "LIVE_SSE", False)
    monkeypatch.setattr(settings, "SUPPORT_INBOUND_ADDRESSES", "")
    monkeypatch.setattr(settings, "SALES_INBOUND_ADDRESSES", "")
    gates.reset()
    yield
    gates.reset()


def signed(raw: bytes, to: str, sender: str = "ananya@example.org", auth: str = CF_PASS, *, key: str = KEY,
           at: float | None = None, size: int | None = None, v1: bool = True, v2: bool = False) -> dict:
    """The headers the Worker sends: each value percent-encoded, then signed
    (v1: the Worker from before; v2: the one that covers the body too)."""
    ts = str(int(at if at is not None else time.time()))
    to_q, from_q, auth_q = quote(to, safe="@"), quote(sender, safe="@"), quote(auth, safe="")
    size_s = str(len(raw) if size is None else size)
    headers = {
        "X-Moa-Timestamp": ts, "X-Moa-Envelope-To": to_q, "X-Moa-Envelope-From": from_q, "X-Moa-Size": size_s,
        "X-Moa-Auth-Results": auth_q, "Content-Type": "message/rfc822",
    }
    if v1:
        text = "\n".join(("v1", ts, to_q, from_q, size_s, auth_q))
        headers["X-Moa-Signature"] = hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()
    if v2:
        body = hashlib.sha256(raw).hexdigest()
        text = "\n".join(("v2", ts, to_q, from_q, size_s, auth_q, body))
        headers["X-Moa-Body-Sha256"] = body
        headers["X-Moa-Signature-V2"] = hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()
    return headers


async def post(client, raw: bytes, to: str = f"yukthi@{DOMAIN}", **kw):
    return await client.post(HOOK, content=raw, headers=signed(raw, to, **kw))


# ─── The signature: nothing is read without it ───────────────────────────


async def test_unsigned_stale_or_another_keys_delivery_is_refused(client, session, world, aws):
    await set_up(client, world)
    raw = mime(to=f"yukthi@{DOMAIN}")
    assert (await client.post(HOOK, content=raw)).status_code == 401
    r = await post(client, raw, key="x" * 64)
    assert r.status_code == 401 and r.json()["reason"] == "signature"
    r = await post(client, raw, at=time.time() - 3600)
    assert r.status_code == 401 and r.json()["reason"] == "stale"
    # Signed for another recipient than the one it is posted for.
    headers = signed(raw, f"yukthi@{DOMAIN}")
    headers["X-Moa-Envelope-To"] = quote(f"robotics@{DOMAIN}", safe="@")
    assert (await client.post(HOOK, content=raw, headers=headers)).status_code == 401
    # A body that isn't the signed size.
    assert (await client.post(HOOK, content=raw + b"x", headers=signed(raw, f"yukthi@{DOMAIN}"))).status_code == 400
    assert await threads_of(session, world.club.id) == []


async def test_the_body_covering_signature_and_the_older_one(client, session, world, aws, monkeypatch, caplog):
    from app.api.club_mailbox import router as mailbox_router

    monkeypatch.setattr(mailbox_router, "_signatures_seen", set())
    await set_up(client, world)
    to = f"yukthi@{DOMAIN}"
    raw = mime(to=to, subject="Parking", message_id="<v2@example.org>")
    # The Worker from now on sends both; the body-covering one is checked.
    with caplog.at_level("INFO", logger="app.api.club_mailbox.router"):
        r = await client.post(HOOK, content=raw, headers=signed(raw, to, v2=True))
    assert r.status_code == 200 and r.json()["kept"] == 1
    assert "deliveries arrive signed v2" in caplog.text
    # Another body of the same size under the same signatures: refused, nothing kept.
    other = mime(to=to, subject="Parkink", message_id="<v3@example.org>")
    assert len(other) == len(raw)
    headers = signed(raw, to, v2=True)
    r = await client.post(HOOK, content=other, headers=headers)
    assert r.status_code == 400 and r.json()["reason"] == "body"
    # A v2 that doesn't check out isn't saved by a good v1 beside it.
    headers = signed(other, to, v2=True)
    headers["X-Moa-Signature-V2"] = "0" * 64
    assert (await client.post(HOOK, content=other, headers=headers)).json()["reason"] == "signature"
    headers = signed(other, to, v2=True)
    headers["X-Moa-Body-Sha256"] = "not-a-hash"
    assert (await client.post(HOOK, content=other, headers=headers)).status_code == 401
    # A header with any bytes in it is a refusal, never an error.
    headers = signed(other, to, v2=True)
    headers["X-Moa-Signature-V2"] = "é".encode("latin-1") * 64
    assert (await client.post(HOOK, content=other, headers=headers)).status_code == 401
    assert len(await messages_of(session, world.club.id)) == 1
    # The Worker from before (v1 only) is taken while the setting is off...
    r = await client.post(HOOK, content=other, headers=signed(other, to))
    assert r.status_code == 200 and r.json()["kept"] == 1
    # ... and refused once it is on: the sender's server tries again later.
    monkeypatch.setattr(settings, "MAILBOX_HOOK_REQUIRE_BODY_SIGNATURE", True)
    third = mime(to=to, subject="Lunch", message_id="<v4@example.org>")
    r = await client.post(HOOK, content=third, headers=signed(third, to))
    assert r.status_code == 401 and r.json()["reason"] == "body_unsigned"
    r = await client.post(HOOK, content=third, headers=signed(third, to, v2=True, v1=False))
    assert r.status_code == 200 and r.json()["kept"] == 1
    assert len(await messages_of(session, world.club.id)) == 3


def test_the_signatures_match_the_workers_vectors():
    # The same vectors as infra/cloudflare/club-mail/test/worker.test.js in the app repo.
    fields = ("1700000000", "Yukthi%2BEvents%40myotherapp.in", "ananya%40example.org", "1234", "mx.cloudflare.net%3B%20dmarc%3Dpass")
    assert cloudflare.sign(KEY, *fields) == "a56d9288597c17eed940afd1c06f08f13113a0785a9592fed6d1f9798aa677aa"
    body = cloudflare.body_sha256(b"hello")
    assert body == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert cloudflare.sign_v2(KEY, *fields, body) == "4a917b041982b97a02e938d1083a7a1ef94e304665a01d9993126dcfe9dc62cb"


async def test_without_the_key_the_hook_says_try_later(client, world, monkeypatch):
    monkeypatch.setattr(settings, "MAILBOX_HOOK_SECRET", "")
    r = await post(client, mime(to=f"yukthi@{DOMAIN}"))
    # 503: the Worker gives the sender a temporary error; the mail comes again.
    assert r.status_code == 503 and r.json()["reason"] == "not_set_up"


# ─── Kept, and shown in the dashboard ────────────────────────────────────


async def test_a_mail_to_the_clubs_address_is_in_its_inbox(client, session, world, aws, changes):
    mailbox = await set_up(client, world)
    assert mailbox["address"] == f"yukthi@{DOMAIN}" and mailbox["domain"] == DOMAIN
    raw = mime(to=f"yukthi@{DOMAIN}", subject="Is parking free?", text="Is parking free on Saturday?")
    r = await post(client, raw)
    assert r.status_code == 200 and r.json()["action"] == "accept" and r.json()["kept"] == 1
    [thread] = await threads_of(session, world.club.id)
    assert thread.folder == "inbox" and thread.unread_count == 1 and thread.subject == "Is parking free?"
    [msg] = await messages_of(session, world.club.id)
    assert msg.provider == "cloudflare" and msg.provider_message_id.startswith("cf-")
    assert msg.delivered_to == f"yukthi@{DOMAIN}" and msg.verdicts == {"dkim": "PASS", "dmarc": "PASS", "spf": "PASS"}
    assert changes == [(world.club.id, thread.id, "message")]
    # What the dashboard lists.
    listed = (await client.get(f"{BASE}/threads", params={"folder": "inbox"}, headers=world.h)).json()
    assert listed["total"] == 1 and listed["items"][0]["subject"] == "Is parking free?"
    mailbox_row = await session.scalar(select(ClubMailboxes).where(ClubMailboxes.club_id == world.club.id))
    assert mailbox_row.last_received_at is not None


async def test_the_sender_trying_again_is_kept_once(client, session, world, aws):
    await set_up(client, world)
    raw = mime(to=f"yukthi@{DOMAIN}", message_id="<once@example.org>")
    first, again = await post(client, raw), await post(client, raw)
    assert first.json()["action"] == again.json()["action"] == "accept"
    assert again.json()["kept"] == 0 and again.json()["duplicate"] == 1
    assert len(await messages_of(session, world.club.id)) == 1


async def test_plus_addressing_and_case_reach_the_club(client, session, world, aws):
    await set_up(client, world)
    r = await post(client, mime(to=f"Yukthi+Events@{DOMAIN}"), to=f"Yukthi+Events@{DOMAIN}")
    assert r.json()["action"] == "accept" and len(await threads_of(session, world.club.id)) == 1


async def test_a_mail_that_failed_dmarc_goes_to_spam(client, session, world, aws):
    await set_up(client, world)
    auth = "mx.cloudflare.net; dkim=none; dmarc=fail header.from=bank.example; spf=softfail smtp.mailfrom=x@spoof.example"
    r = await post(client, mime(to=f"yukthi@{DOMAIN}", subject="Verify your account"), auth=auth)
    assert r.json()["action"] == "accept"
    [thread] = await threads_of(session, world.club.id)
    assert thread.folder == "spam"


# ─── Refused in the SMTP session: the sender's server says why ───────────


async def test_an_address_nobody_has_is_rejected(client, session, world, aws):
    await set_up(client, world)
    r = await post(client, mime(to=f"nobody-here@{DOMAIN}"), to=f"nobody-here@{DOMAIN}")
    body = r.json()
    assert r.status_code == 200 and body["action"] == "reject" and body["reason"] == "unknown"
    assert body["text"] == "No mailbox here by that name"
    assert await threads_of(session, world.club.id) == []


async def test_a_club_whose_plan_has_no_mailbox_is_rejected(client, session, world, aws):
    # Growth has no `mailbox`: its address can't even be set up, so it is nobody's.
    r = await post(client, mime(to=f"chess@{DOMAIN}"), to=f"chess@{DOMAIN}")
    assert r.json()["action"] == "reject"


async def test_with_mailboxes_off_everything_is_rejected(client, world, aws, monkeypatch):
    await set_up(client, world)
    monkeypatch.setattr(settings, "CLUB_MAILBOX", False)
    r = await post(client, mime(to=f"yukthi@{DOMAIN}"))
    assert r.json()["action"] == "reject" and r.json()["reason"] == "off"


async def test_too_large_is_rejected_before_it_is_read(client, world, aws, monkeypatch):
    await set_up(client, world)
    monkeypatch.setattr(settings, "MAILBOX_INBOUND_MAX_MB", 1)
    raw = mime(to=f"yukthi@{DOMAIN}", attachments=[("big.pdf", "application/pdf", b"%PDF" + b"0" * (2 * 1024 * 1024), None)])
    r = await post(client, raw)
    assert r.json()["action"] == "reject" and r.json()["reason"] == "too_large"


# ─── The platform's own addresses on the domain ──────────────────────────


async def test_the_platforms_addresses_are_never_a_clubs(client, world, monkeypatch):
    monkeypatch.setattr(settings, "MAIL_FROM_ADDRESS", "info@myotherapp.in")
    monkeypatch.setattr(settings, "SUPPORT_INBOUND_ADDRESSES", "helpdesk2@myotherapp.in")
    for handle in ("info", "support", "appreview", "developers", "hello", "helpdesk2", "events"):
        assert handles.problem(handle) == ("HANDLE_RESERVED", "That address is kept for the platform. Pick another."), handle
    assert handles.problem("gdccloudtravender") is None
    r = await client.put(f"{BASE}/address", json={"handle": "appreview"}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "HANDLE_RESERVED"


async def test_support_at_the_domain_reaches_the_desk(client, session, desk, aws, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "SUPPORT_INBOUND_ADDRESSES", f"support@{DOMAIN}")
    raw = mime(to=f"support@{DOMAIN}", sender=("Gita Guest", "gita@example.org"), subject="Ticket missing", text="No ticket.")
    r = await post(client, raw, to=f"support@{DOMAIN}", sender="gita@example.org")
    assert r.json()["action"] == "accept" and r.json()["desk"] == {"support": "opened"}
    again = await post(client, raw, to=f"support@{DOMAIN}", sender="gita@example.org")
    assert again.json()["action"] == "accept" and again.json()["desk"] == {"support": "duplicate"}
    [case] = (await session.scalars(select(SupportCases))).all()
    assert case.requester_email == "gita@example.org"


# ─── Reading Cloudflare's verdicts ───────────────────────────────────────


def test_only_cloudflares_own_results_are_read():
    assert cloudflare.verdicts_from(CF_PASS) == {"dkim": "PASS", "dmarc": "PASS", "spf": "PASS"}
    # The sender's own header after Cloudflare's (joined with a comma) is not read.
    joined = "mx.cloudflare.net; dmarc=fail header.from=x.example, mx.google.com; dmarc=pass; spf=pass"
    assert cloudflare.verdicts_from(joined) == {"dmarc": "FAIL"}
    # Not Cloudflare's first: nothing.
    assert cloudflare.verdicts_from("evil.example; dmarc=pass") == {}
    assert cloudflare.verdicts_from("") == {}
    # A comment can't smuggle a result in; one passing DKIM signature is enough.
    assert cloudflare.verdicts_from("mx.cloudflare.net; spf=fail (dmarc=pass) smtp.mailfrom=a@b; dkim=fail; dkim=pass") == {
        "spf": "FAIL", "dkim": "PASS",
    }


def test_the_same_mail_has_the_same_key():
    raw = mime(to=f"yukthi@{DOMAIN}", message_id="<k@example.org>")
    assert cloudflare.message_key(raw, "a@example.org") == cloudflare.message_key(raw + b"\n", "a@example.org")
    assert cloudflare.message_key(raw, "a@example.org") != cloudflare.message_key(raw, "b@example.org")
    no_id = b"Subject: hi\r\n\r\nbody"
    assert cloudflare.message_key(no_id, "a@example.org") != cloudflare.message_key(no_id + b"!", "a@example.org")
