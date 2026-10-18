"""Emailed links survive a change of key, and an old one gets a way back.

The ticket and certificate links in emails are HMACs (app/core/mail/links.py).
APP_SECRET_KEY signed them until it leaked and was rotated, which broke every
one already sent; the leaked key must never verify again. Now a list of link
keys signs (the first) and verifies (any), and a person following a link
that no longer verifies gets a page with a form for a fresh one, which goes
only to the address the ticket was registered with.

The checks that need no database come last: run alone, a file whose first
test touches none trips the suite's shared event loop (tests/conftest.py).
"""
import hashlib
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.deliveries import service as deliveries
from app.api.emails import old_links
from app.config import settings
from app.core.email import email as email_mod
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import certificate_starter, ticket_starter
from app.core.mail import links
from tests.conftest import auth_header

BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
RESEND = "/api/v1/files/resend"
#: The key that leaked: what the links in emails sent before the rotation were signed with.
LEAKED = "the-app-secret-that-leaked-in-october-0123456789"
K1, K2 = "a" * 32 + "-first-link-key", "b" * 32 + "-second-link-key"


def _hmac(key: str, message: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()[:32]


def _path(url: str) -> str:
    p = urlparse(url)
    return f"{p.path}?{p.query}" if p.query else p.path


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
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(deliveries, "session_scope", scope)
    owner, club = await make_club(name="Yukthi", email="old-links@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0,
    )
    a = await make_user(email="ananya-links@example.com")
    b = await make_user(email="bala-links@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer")
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan", is_attended=True)
    await session.commit()
    headers = auth_header(await mint_token(owner))
    # Layered designs, so the documents render with reportlab rather than a browser.
    for kind, s in (("ticket", ticket_starter("classic")), ("certificate", certificate_starter("classic"))):
        r = await client.put(
            f"/api/v1/events/{event.id}/designs/{kind}/layout",
            json=dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"])), headers=headers,
        )
        assert r.status_code == 200, r.text
    return {"event": event, "a": r_a, "b": r_b, "club": headers}


# ─── The keys ────────────────────────────────────────────────────────────


async def test_a_link_signed_with_the_leaked_key_is_refused(client, world, monkeypatch):
    code = world["a"].ticket_id
    old = f"/api/v1/files/tickets/{code}.pdf?s={_hmac(LEAKED, f'file:ticket:{code}')}"
    for keys in ("", f"{K1},{K2}"):  # before the link keys are set, and after
        monkeypatch.setattr(settings, "LINK_SIGNING_KEYS", keys)
        r = await client.get(old)
        assert r.status_code == 403 and r.json()["message"] == "This link is not valid."
        assert (await client.get(_path(links.file_url("ticket", code)))).status_code == 200
    # A link the old list signed still opens while its key is listed, and not after.
    monkeypatch.setattr(settings, "LINK_SIGNING_KEYS", K2)
    by_k2 = _path(links.file_url("ticket", code))
    monkeypatch.setattr(settings, "LINK_SIGNING_KEYS", f"{K1},{K2}")
    assert (await client.get(by_k2)).status_code == 200
    monkeypatch.setattr(settings, "LINK_SIGNING_KEYS", K1)
    assert (await client.get(by_k2)).status_code == 403


# ─── The page ────────────────────────────────────────────────────────────


async def test_a_browser_gets_a_page_and_an_app_the_json_it_always_had(client, world):
    code = world["a"].ticket_id
    bad = f"/api/v1/files/tickets/{code}.pdf?s={_hmac(LEAKED, f'file:ticket:{code}')}"

    r = await client.get(bad, headers=BROWSER)
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "This link no longer works" in body and "your ticket is in the <strong>MyOtherApp</strong> app" in body
    assert 'href="https://myotherapp.in/me/tickets"' in body
    assert f'<form method="post" action="{RESEND}">' in body
    assert f'name="code" value="{code}"' in body and 'name="kind" value="ticket"' in body
    assert "script" not in body.lower()
    assert "default-src 'none'" in r.headers["content-security-policy"] and "form-action 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"

    # Anything not asking for a page gets the JSON, with the same status as before.
    for accept in (None, "*/*", "application/json", "application/json, text/html", "text/html;q=0.5, application/json"):
        r = await client.get(bad, headers={"accept": accept} if accept else {})
        assert (r.status_code, r.json()["message"]) == (403, "This link is not valid."), accept

    # A ticket that is not there: the same page, 404; a cancelled one: no form.
    gone = "MOA-ZZZZZ-ZZZZZ"
    r = await client.get(_path(links.file_url("ticket", gone)), headers=BROWSER)
    assert r.status_code == 404 and "This link no longer works" in r.text
    assert (await client.get(_path(links.file_url("ticket", gone)))).json()["message"] == "No such ticket"
    await client.post(f"/api/v1/events/{world['event'].id}/tickets/{world['a'].id}/revoke", json={}, headers=world["club"])
    r = await client.get(_path(links.file_url("ticket", code)), headers=BROWSER)
    assert r.status_code == 410 and "This ticket is no longer valid" in r.text and "<form" not in r.text
    # A certificate link: the page speaks of the certificate.
    r = await client.get(f"/api/v1/files/certificates/{code}.pdf?s=nope", headers=BROWSER)
    assert r.status_code == 403 and 'name="kind" value="certificate"' in r.text and "your certificate" in r.text


async def test_what_is_typed_into_the_page_is_never_markup(client, world):
    r = await client.get('/api/v1/files/tickets/"><img src=x onerror=alert(1)>.pdf?s=x', headers=BROWSER)
    assert r.status_code == 403
    assert "<img" not in r.text and "&quot;&gt;&lt;img" in r.text


# ─── A fresh link ────────────────────────────────────────────────────────


async def _ask(client, kind, code, email, *, ip="198.51.100.7", page=False):
    headers = {"x-forwarded-for": ip}
    if page:
        return await client.post(RESEND, data={"kind": kind, "code": code, "email": email}, headers={**headers, **BROWSER})
    return await client.post(RESEND, json={"kind": kind, "code": code, "email": email}, headers=headers)


async def test_a_fresh_link_goes_only_on_a_match_and_only_to_the_registered_address(client, session, world, outbox):
    code, owner = world["a"].ticket_id, world["a"].email
    generic = {"message": "If that email has a ticket for this event, we've sent a fresh link."}

    # Someone else's address, and a ticket that does not exist: the same answer, nothing sent.
    for kind, c, email in (("ticket", code, "someone-else@example.com"), ("ticket", "MOA-ZZZZZ-ZZZZZ", owner)):
        r = await _ask(client, kind, c, email)
        assert (r.status_code, r.json()) == (200, generic)
    assert outbox == []

    # The registered address, however it is typed: the ticket email again, to that address.
    r = await _ask(client, "ticket", code, f"  {owner.upper()} ")
    assert (r.status_code, r.json()) == (200, generic)
    assert [m["recipients"] for m in outbox] == [[owner]]
    mail = outbox[0]
    assert any(a["filename"] == f"ticket-{code}.pdf" for a in mail["attachments"])
    # Its link is a fresh one, signed with today's key.
    assert links.file_token("ticket", code) in mail["html"]

    # Asked again at once: still the same answer, and not sent twice in ten minutes.
    assert (await _ask(client, "ticket", code, owner, ip="198.51.100.8")).json() == generic
    assert len(outbox) == 1

    rows = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "ticket_link.resend").order_by(AdminAuditLogs.id))).all()
    assert [(r.details["found"], r.details["matched"], r.details["sent"]) for r in rows] == [
        (True, False, False), (False, False, False), (True, True, True), (True, True, False),
    ]
    assert rows[2].entity_id == str(world["a"].id) and rows[2].ip_address == "198.51.100.7" and rows[2].actor_id is None
    assert all(owner not in (r.summary or "") and "someone-else" not in str(r.details) for r in rows)


async def test_a_certificate_link_gets_the_certificate_again(client, session, world, outbox):
    b = world["b"]
    assert (await _ask(client, "certificate", b.ticket_id, b.email)).status_code == 200
    assert outbox == []  # not issued yet: nothing to send
    await client.post(f"/api/v1/events/{world['event'].id}/certificates/issue", json={"audience": "everyone"}, headers=world["club"])
    outbox.clear()
    assert (await _ask(client, "certificate", b.ticket_id, b.email, ip="198.51.100.9")).status_code == 200
    assert [m["recipients"] for m in outbox] == [[b.email]]
    assert outbox[0].get("attachment_filename", "").endswith(".pdf") or any(a["filename"].endswith(".pdf") for a in outbox[0].get("attachments", []))


async def test_the_page_form_answers_with_a_page(client, world, outbox):
    r = await _ask(client, "ticket", world["a"].ticket_id, world["a"].email, page=True)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Check your inbox" in r.text and "we&#x27;ve sent a fresh link" in r.text
    assert len(outbox) == 1
    # A blank address: the form again, with what is wrong.
    r = await _ask(client, "ticket", world["a"].ticket_id, "", page=True, ip="198.51.100.20")
    assert r.status_code == 422 and "Enter the email address you registered with." in r.text and "<form" in r.text
    r = await _ask(client, "ticket", world["a"].ticket_id, "not-an-address", ip="198.51.100.21")
    assert r.status_code == 422 and r.json()["message"] == "Enter the email address you registered with."
    # (Another code: this one has had its three asks this hour.)
    assert (await _ask(client, "vip", "MOA-QQQQQ-QQQQQ", world["a"].email, ip="198.51.100.22")).status_code == 422


async def test_asking_is_limited_per_ticket_and_per_address(client, world, outbox):
    code, owner = world["a"].ticket_id, world["a"].email
    # Three asks an hour for one ticket, whoever asks and whatever they type.
    for i in range(3):
        assert (await _ask(client, "ticket", code, f"guess{i}@example.com", ip=f"203.0.113.{i + 1}")).status_code == 200
    r = await _ask(client, "ticket", code, owner, ip="203.0.113.50")
    assert (r.status_code, r.json()["error_code"]) == (429, "RATE_LIMITED")
    assert outbox == []  # the owner's own ask came after the limit
    # Five a minute from one address, whatever ticket.
    for i in range(5):
        assert (await _ask(client, "ticket", f"MOA-AAAA{i}-BBBBB", owner, ip="203.0.113.99")).status_code == 200
    assert (await _ask(client, "ticket", "MOA-AAAA9-BBBBB", owner, ip="203.0.113.99")).status_code == 429
    page = await _ask(client, "ticket", "MOA-AAAA9-BBBBB", owner, ip="203.0.113.99", page=True)
    assert page.status_code == 429 and "Too many tries" in page.text


# ─── Without a database (last: see the note at the top) ──────────────────


def test_unset_the_links_are_signed_with_the_app_secret_exactly_as_before(monkeypatch):
    monkeypatch.setattr(settings, "LINK_SIGNING_KEYS", "")
    assert links.file_token("ticket", "MOA-AAAAA-BBBBB") == _hmac(settings.SECRET_KEY, "file:ticket:MOA-AAAAA-BBBBB")
    assert links.file_token_ok("ticket", "MOA-AAAAA-BBBBB", _hmac(settings.SECRET_KEY, "file:ticket:MOA-AAAAA-BBBBB"))


def test_the_key_list_signs_with_the_first_and_verifies_with_any(monkeypatch):
    from app.api.live import service as live

    monkeypatch.setattr(settings, "LINK_SIGNING_KEYS", f"{K1},{K2}")
    msg = "file:certificate:MOA-AAAAA-BBBBB"
    assert links.file_token("certificate", "MOA-AAAAA-BBBBB") == _hmac(K1, msg)
    assert links.file_token_ok("certificate", "MOA-AAAAA-BBBBB", _hmac(K1, msg))
    assert links.file_token_ok("certificate", "MOA-AAAAA-BBBBB", _hmac(K2, msg))
    # Not the app secret once the list is set, and never the key that leaked.
    assert not links.file_token_ok("certificate", "MOA-AAAAA-BBBBB", _hmac(settings.SECRET_KEY, msg))
    assert not links.file_token_ok("certificate", "MOA-AAAAA-BBBBB", _hmac(LEAKED, msg))
    # Signed for one file, it opens no other; and nonsense is refused, not a crash.
    assert not links.file_token_ok("ticket", "MOA-AAAAA-BBBBB", _hmac(K1, msg))
    assert not links.file_token_ok("certificate", "MOA-AAAAA-BBBBB", "ünïcode")
    # The feedback survey's link is signed the same way.
    assert live._survey_token("reg-1") == _hmac(K1, "feedback:reg-1")
    assert live._survey_token_ok("reg-1", _hmac(K2, "feedback:reg-1")) and not live._survey_token_ok("reg-1", _hmac(LEAKED, "feedback:reg-1"))


def test_a_short_link_key_stops_the_app_at_startup():
    with pytest.raises(ValidationError, match="at least 32 characters"):
        type(settings)(LINK_SIGNING_KEYS=f"{K1},too-short")
    assert type(settings)(LINK_SIGNING_KEYS=f" {K1} , {K2} ,").link_signing_keys == [K1, K2]


def test_only_a_browser_asks_for_a_page():
    class R:
        def __init__(self, accept):
            self.headers = {"accept": accept} if accept is not None else {}

    assert old_links.wants_html(R(BROWSER["accept"]))
    assert old_links.wants_html(R("text/html"))
    for accept in (None, "", "*/*", "application/json", "text/html;q=0", "application/json;q=0.9, text/html;q=0.8", "text/html, application/json"):
        assert not old_links.wants_html(R(accept)), accept
