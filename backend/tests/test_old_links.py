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
