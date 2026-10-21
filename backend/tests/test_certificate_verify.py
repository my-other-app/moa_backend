"""The public certificate verifier, and the number it checks.

Anyone can check a certificate at myotherapp.in/verify by its number, its
QR or a link ending in either. The number used to be the ticket id with
CERT- for MOA-, so a certificate posted anywhere gave away the ticket the
door scans, and the QR printed the ticket id itself. Numbers are now drawn
on their own; the QR opens the verifier; the answer says only what a
verifier needs; and the checks are counted per person, a miss as much as a
match, even when the site's server asks on the person's behalf.
"""
import hashlib
import re
import secrets
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.certificates import service as verify_service
from app.api.events import certificates
from app.api.events.certificates import certificate_context, number_of, qr_text
from app.api.events.documents import document_values, layout_values
from app.api.events.models import EventRegistrationsLink, Events
from app.config import settings
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.renderer import pdf_text
from app.core.layout.starters import certificate_starter
from app.core.storage import objects
from app.core.utils import keys
from tests.conftest import auth_header

API = "/api/v1/verify/certificates"
#: Every answer has exactly these keys, whatever it says.
KEYS = {"valid", "reason", "number", "attendee_name", "event_name", "org_name", "org_slug", "issued_at"}
NUMBER = re.compile(rf"^CERT-[{keys.TICKET_ALPHABET}]{{5}}-[{keys.TICKET_ALPHABET}]{{5}}$")
VERCEL = "76.76.21.21"
TOKEN = secrets.token_hex(32)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Respawn Verify", email="verify-club@example.com")
    event = await make_event(
        club=club, created_by=owner, name="LAN Night",
        event_datetime=datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc), duration=3.0,
    )
    a = await make_user(email="verify-a@example.com")
    b = await make_user(email="verify-b@example.com")
    old = await make_user(email="verify-old@example.com")
    r_a = await make_registration(
        event=event, user=a, full_name="Ananya Iyer", is_attended=True, phone="9876543210",
        attended_on=datetime(2026, 9, 12, 18, 30, tzinfo=timezone.utc),
    )
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan")  # never came, never issued
    # Printed before numbers were drawn on their own: no number of its own.
    r_old = await make_registration(event=event, user=old, full_name="Old Timer", is_attended=True, ticket_id="MOA-2BCEBA8-PFZG3")
    r_old.certificate_no = None
    await session.commit()
    club_headers = auth_header(await mint_token(owner))
    st = certificate_starter("classic")
    r = await client.put(
        f"/api/v1/events/{event.id}/designs/certificate/layout",
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])),
        headers=club_headers,
    )
    assert r.status_code == 200, r.text
    return {
        "event": event, "club": club, "a": r_a, "b": r_b, "old": r_old, "club_headers": club_headers,
        "as_a": auth_header(await mint_token(a)),
    }


def _from(ip: str) -> dict:
    return {"X-Forwarded-For": ip}


async def _check(client, code: str, headers=None):
    return await client.get(f"{API}/{code}", headers=headers or _from("49.37.10.1"))


# ─── The number ──────────────────────────────────────────────────────────────


async def test_a_registration_draws_a_number_unrelated_to_its_ticket(world):
    a = world["a"]
    assert NUMBER.match(a.certificate_no)
    assert a.ticket_id.split("-", 1)[1] not in a.certificate_no
    assert number_of(a) == a.certificate_no
    # The older one keeps the number made from its ticket, as printed.
    assert world["old"].certificate_no is None
    assert number_of(world["old"]) == "CERT-2BCEBA8-PFZG3"


async def test_a_number_already_taken_is_drawn_again(session, world, make_user):
    from app.api.events.models import EventRegistrationsLink
    from app.api.events.registration.service import insert_registration

    user = await make_user(email="verify-clash@example.com")
    clash = EventRegistrationsLink(
        event_id=world["event"].id, user_id=user.id, full_name="Clash", email=user.email,
        ticket_id=keys.generate_ticket_id(), certificate_no=world["a"].certificate_no,
    )
    await insert_registration(session, clash)
    assert NUMBER.match(clash.certificate_no) and clash.certificate_no != world["a"].certificate_no


# ─── What the certificate prints ────────────────────────────────────────────


async def test_the_qr_opens_the_verifier_with_the_number_only(world):
    a, old = world["a"], world["old"]
    assert qr_text(a) == f"https://myotherapp.in/verify/{a.certificate_no}"
    assert a.ticket_id not in qr_text(a)
    # An older certificate's QR names the number already printed on it.
    assert qr_text(old) == "https://myotherapp.in/verify/CERT-2BCEBA8-PFZG3"


async def test_the_printed_certificate_carries_the_number_never_the_ticket(client, session, world, monkeypatch):
    seen = {}
    real = certificates.render_layout

    def capture(style, values, qr, *args, **kwargs):
        seen["qr"] = qr
        return real(style, values, qr, *args, **kwargs)

    monkeypatch.setattr(certificates, "render_layout", capture)
    a = world["a"]
    pdf = await client.get(f"/api/v1/events/certificates/{a.ticket_id}/pdf", headers=world["as_a"])
    assert pdf.status_code == 200
    text = " ".join(pdf_text(pdf.content).split())
    assert a.certificate_no in text and a.ticket_id not in text
    assert seen["qr"] == f"https://myotherapp.in/verify/{a.certificate_no}"
    assert a.ticket_id not in pdf.headers["content-disposition"]
    assert a.certificate_no in pdf.headers["content-disposition"]

    # The HTML designs: the footer's "Certificate ID", the QR, and the token.
    reg = await session.scalar(
        select(EventRegistrationsLink)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club))
        .where(EventRegistrationsLink.id == a.id)
        .execution_options(populate_existing=True)
    )
    context = certificate_context(reg, {}, qr=False)
    assert context["email_payload"]["certificate_no"] == a.certificate_no
    assert context["qr_text"] == f"https://myotherapp.in/verify/{a.certificate_no}"
    assert document_values(reg, reg.event)["certificate_id"] == a.certificate_no
    values = layout_values(reg, reg.event, "certificate")
    assert a.ticket_id not in values.values()
    template = open("templates/pdf/events/certificate.html").read()
    assert "email_payload.ticket_id" not in template and "email_payload.certificate_no" in template


async def test_the_owner_is_told_the_number(client, world):
    r = await client.get(f"/api/v1/events/tickets/{world['a'].ticket_id}", headers=world["as_a"])
    assert r.status_code == 200, r.text
    assert r.json()["certificate_no"] == world["a"].certificate_no


# ─── What the verifier answers ──────────────────────────────────────────────


async def test_every_answer_has_exactly_the_same_keys_and_nothing_personal(client, world):
    a = world["a"]
    valid = await _check(client, a.certificate_no)
    assert valid.status_code == 200 and valid.headers["cache-control"] == "no-store"
    body = valid.json()
    assert set(body) == KEYS
    assert body["valid"] is True and body["reason"] == "ok"
    assert (body["number"], body["attendee_name"], body["event_name"], body["org_name"]) == (
        a.certificate_no, "Ananya Iyer", "LAN Night", "Respawn Verify",
    )
    assert body["org_slug"] == world["club"].slug and body["issued_at"]
    for secret in (a.ticket_id, a.ticket_id.split("-", 1)[1], str(a.id), str(a.user_id), a.email, a.phone):
        assert secret not in valid.text

    not_found = (await _check(client, "CERT-ZZZZZ-ZZZZZ")).json()
    malformed = (await _check(client, "<script>")).json()
    assert set(not_found) == set(malformed) == KEYS
    assert not_found == malformed == {k: None for k in KEYS} | {"valid": False, "reason": "not_found"}

    # Registered, never issued one, never came: as if there were no such number.
    assert (await _check(client, world["b"].certificate_no)).json() == not_found


async def test_a_revoked_one_says_who_took_it_back_not_whose_it_was(client, world):
    a = world["a"]
    base = f"/api/v1/events/{world['event'].id}/certificates"
    await client.post(f"{base}/issue", json={"audience": "checked_in"}, headers=world["club_headers"])
    await client.post(f"{base}/{a.id}/revoke", headers=world["club_headers"])
    body = (await _check(client, a.certificate_no)).json()
    assert set(body) == KEYS
    assert (body["valid"], body["reason"], body["number"], body["event_name"], body["org_name"]) == (
        False, "revoked", a.certificate_no, "LAN Night", "Respawn Verify",
    )
    assert body["attendee_name"] is None and body["issued_at"] is None


async def test_the_club_is_linked_only_while_it_is_public(client, session, world):
    world["club"].is_verified = False
    await session.flush()
    await session.refresh(world["club"])
    body = (await _check(client, world["a"].certificate_no)).json()
    assert body["valid"] is True and body["org_name"] == "Respawn Verify" and body["org_slug"] is None


@pytest.mark.parametrize("shape", ["{n}", "{n_lower}", " {n_spaced} ", "certificate:{n}", "{n_flat}"])
async def test_the_number_reads_however_it_was_typed(client, world, shape):
    n = world["a"].certificate_no
    code = shape.format(n=n, n_lower=n.lower(), n_spaced=n.replace("-", " - "), n_flat=n.replace("-", " "))
    assert (await _check(client, code)).json()["number"] == n


async def test_a_number_drawn_on_its_own_never_answers_to_the_ticket(client, world):
    """Else anyone holding a ticket id could ask whose it is."""
    a = world["a"]
    body = a.ticket_id.split("-", 1)[1]
    for code in (a.ticket_id, f"certificate:{a.ticket_id}", f"CERT-{body}"):
        assert (await _check(client, code)).json()["reason"] == "not_found", code


async def test_an_older_certificate_verifies_by_its_printed_number_and_its_old_qr(client, world):
    for code in ("CERT-2BCEBA8-PFZG3", "certificate:MOA-2BCEBA8-PFZG3", "MOA-2BCEBA8-PFZG3", "cert-2bceba8-pfzg3", "CERT2BCEBA8PFZG3"):
        body = (await _check(client, code)).json()
        assert (body["valid"], body["number"], body["attendee_name"]) == (True, "CERT-2BCEBA8-PFZG3", "Old Timer"), code
        assert "MOA-" not in str(body)


async def test_a_malformed_code_costs_the_same_lookup_as_a_miss(client, world, monkeypatch):
    asked = []
    real = verify_service.certificate_by_number

    async def spy(session, number):
        asked.append(number)
        return await real(session, number)

    monkeypatch.setattr(verify_service, "certificate_by_number", spy)
    for code in ("CERT-ZZZZZ-ZZZZZ", "Ananya Iyer", "verify-a@example.com", "' OR 1=1 --", "x" * 500):
        assert (await _check(client, code)).json()["reason"] == "not_found"
    assert len(asked) == 5
    # Never a name or an address: what cannot be a number is not looked for as typed.
    assert asked[0] == "CERT-ZZZZZ-ZZZZZ" and asked[1:] == [None] * 4


# ─── Counted per person ─────────────────────────────────────────────────────


async def test_twenty_checks_a_minute_per_address_misses_counted_too(client, world):
    me, other = _from("49.37.10.1"), _from("49.37.10.2")
    codes = [world["a"].certificate_no, "CERT-ZZZZZ-ZZZZZ", "not-a-code", world["b"].certificate_no] * 5
    assert [(await _check(client, c, me)).status_code for c in codes] == [200] * 20
    refused = await _check(client, world["a"].certificate_no, me)
    assert refused.status_code == 429
    assert refused.json()["error_code"] == "TOO_MANY_CHECKS" and "Ananya" not in refused.text
    # Someone else is not held up.
    assert (await _check(client, world["a"].certificate_no, other)).status_code == 200


async def test_an_hourly_cap_beside_the_minutes(client, world, monkeypatch):
    monkeypatch.setattr(settings, "CERTIFICATE_VERIFY_LIMIT", "1000/minute;3/hour")
    assert [(await _check(client, "CERT-ZZZZZ-ZZZZZ")).status_code for _ in range(4)] == [200, 200, 200, 429]


@pytest.fixture
def site_token(monkeypatch):
    monkeypatch.setattr(settings, "SITE_TOKENS", TOKEN)


def _site(visitor: str, token: str = TOKEN) -> dict:
    return {"X-Forwarded-For": VERCEL, "X-Site-Token": token, "X-Visitor-IP": visitor}


async def test_the_site_checks_for_each_visitor_on_their_own_count(client, world, site_token):
    """The site's server asks from Vercel's few addresses; the visitor it
    names is who is counted, so one person's twenty spend nobody else's."""
    n = world["a"].certificate_no
    assert [(await _check(client, n, _site("49.37.10.1"))).status_code for _ in range(20)] == [200] * 20
    assert (await _check(client, n, _site("49.37.10.1"))).status_code == 429
    assert (await _check(client, n, _site("49.37.10.2"))).status_code == 200
    # The person themselves, asking the API directly from the same address, has spent theirs too.
    assert (await _check(client, n, _from("49.37.10.1"))).status_code == 429


async def test_a_visitor_named_without_the_token_is_not_believed(client, world, site_token):
    """Anyone else naming visitors is counted by their own address."""
    n = world["a"].certificate_no
    spoofed = [(await _check(client, n, {"X-Forwarded-For": VERCEL, "X-Visitor-IP": f"10.9.{i}.1"})).status_code for i in range(20)]
    assert spoofed == [200] * 20
    assert (await _check(client, n, {"X-Forwarded-For": VERCEL, "X-Visitor-IP": "10.9.99.1"})).status_code == 429
    wrong = _site("49.37.10.9", token=secrets.token_hex(32))
    assert (await _check(client, n, wrong)).status_code == 429  # the same address's count


async def test_the_site_as_a_whole_is_bounded(client, world, site_token, monkeypatch):
    """A token that leaked could name a new visitor every time: the site's
    own bucket stops it at CERTIFICATE_VERIFY_SITE_LIMIT."""
    monkeypatch.setattr(settings, "CERTIFICATE_VERIFY_SITE_LIMIT", "5/minute")
    statuses = [(await _check(client, "CERT-ZZZZZ-ZZZZZ", _site(f"49.37.11.{i}"))).status_code for i in range(6)]
    assert statuses == [200] * 5 + [429]


# ─── The picture and the file check ─────────────────────────────────────────


async def test_the_picture_is_shown_only_for_a_valid_certificate(client, world):
    a = world["a"]
    r = await client.get(f"{API}/{a.certificate_no}/image", headers=_from("49.37.12.1"))
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    assert r.headers["cache-control"] == "no-store"
    for code in ("CERT-ZZZZZ-ZZZZZ", "nonsense", world["b"].certificate_no, a.ticket_id):
        missing = await client.get(f"{API}/{code}/image", headers=_from("49.37.12.1"))
        assert missing.status_code == 404 and missing.json()["message"] == "No certificate has this number."
    base = f"/api/v1/events/{world['event'].id}/certificates"
    await client.post(f"{base}/issue", json={"audience": "checked_in"}, headers=world["club_headers"])
    await client.post(f"{base}/{a.id}/revoke", headers=world["club_headers"])
    assert (await client.get(f"{API}/{a.certificate_no}/image", headers=_from("49.37.12.1"))).status_code == 404


@pytest.fixture
def bucket(monkeypatch):
    """The bucket as a dict, the cache on, and every read counted."""
    held: dict = {}
    reads: list = []
    monkeypatch.setattr(settings, "RENDER_DOC_CACHE", True)
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: (reads.append(key), held.get(key))[1])
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, data))
    monkeypatch.setattr(objects, "list_keys", lambda base, limit=100: [k for k in held if k.startswith(base)][:limit])
    return held, reads


async def test_a_file_is_checked_against_the_copies_handed_out(client, world, bucket):
    held, reads = bucket
    a = world["a"]
    pdf = (await client.get(f"/api/v1/events/certificates/{a.ticket_id}/pdf", headers=world["as_a"])).content
    r = await client.get(f"{API}/{a.certificate_no}/files", headers=_from("49.37.13.1"))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"available": True, "sha256": [hashlib.sha256(pdf).hexdigest()]}

    # Hashed once and remembered: asking again reads nothing from the bucket.
    reads.clear()
    again = await client.get(f"{API}/{a.certificate_no}/files", headers=_from("49.37.13.1"))
    assert again.json()["sha256"] == r.json()["sha256"]
    assert not [k for k in reads if "/certificate/" in k]

    # Nothing to check for a number that is not a valid certificate.
    for code in ("CERT-ZZZZZ-ZZZZZ", "nonsense", world["b"].certificate_no):
        assert (await client.get(f"{API}/{code}/files", headers=_from("49.37.13.1"))).json() == {"available": False, "sha256": []}


async def test_with_no_copy_kept_there_is_no_file_check(client, world):
    r = await client.get(f"{API}/{world['a'].certificate_no}/files", headers=_from("49.37.14.1"))
    assert r.json() == {"available": False, "sha256": []}


async def test_a_bucket_that_does_not_answer_only_loses_the_file_check(client, world, monkeypatch):
    monkeypatch.setattr(settings, "RENDER_DOC_CACHE", True)

    def down(base, limit=100):
        raise ConnectionError("bucket down")

    monkeypatch.setattr(objects, "list_keys", down)
    r = await client.get(f"{API}/{world['a'].certificate_no}/files", headers=_from("49.37.15.1"))
    assert r.status_code == 200 and r.json() == {"available": False, "sha256": []}


# ─── Without the database (last: the suite's loop is the database tests') ───


def test_a_number_is_ten_characters_from_the_csprng(monkeypatch):
    drawn = []
    real = keys.secrets.choice

    def spy(seq):
        drawn.append(seq)
        return real(seq)

    monkeypatch.setattr(keys.secrets, "choice", spy)
    number = keys.generate_certificate_no()
    assert NUMBER.match(number)
    assert len(drawn) == 10 and set(drawn) == {keys.TICKET_ALPHABET}
    # 31 ** 10, about 8.2e14 numbers: 49.5 bits.
    assert len(keys.TICKET_ALPHABET) == 31 and round(10 * __import__("math").log2(31), 1) == 49.5


def test_numbers_do_not_repeat():
    many = {keys.generate_certificate_no() for _ in range(20000)}
    assert len(many) == 20000
