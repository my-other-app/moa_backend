"""Certificates are issued, sent, verified and taken back.

A certificate used to be whatever an attended guest downloaded, and for a
while a check-in still counted at an event where nothing had been issued --
so a guest who checked in at a club that had done nothing at all was handed
one. The organiser decides who gets a certificate, and until they do there
is none: nothing to see, nothing to download, nothing to verify.
"""
import io
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.api.deliveries import service as deliveries
from app.core.email import email as email_mod
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.renderer import pdf_text
from app.core.layout.starters import certificate_starter
from app.core.mail import links
from tests.conftest import auth_header


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kw: (sent.append(kw), {"MessageId": f"ses-{len(sent)}"})[1])
    return sent


@pytest.fixture
def background(monkeypatch, session):
    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(deliveries, "session_scope", scope)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="certs@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0,
    )
    a = await make_user(email="ananya-c@example.com")
    b = await make_user(email="bala-c@example.com")
    s = await make_user(email="speaker-c@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", is_attended=True)
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan")  # registered, never came
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", is_attended=True, role="speaker")
    await session.commit()
    headers = auth_header(await mint_token(owner))
    st = certificate_starter("classic")
    r = await client.put(
        f"/api/v1/events/{event.id}/designs/certificate/layout",
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return {
        "event": event, "a": r_a, "b": r_b, "s": r_s, "club": headers,
        "a_user": auth_header(await mint_token(a)), "b_user": auth_header(await mint_token(b)),
    }


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/certificates{path}"


async def test_issuing_goes_to_whoever_checked_in_and_never_twice(client, world):
    preview = await client.post(_url(world, "/issue-preview"), json={"audience": "checked_in"}, headers=world["club"])
    assert preview.json() == {"eligible": 2, "already_issued": 0, "to_issue": 2, "outdated": 0}

    first = await client.post(_url(world, "/issue"), json={"audience": "checked_in"}, headers=world["club"])
    assert first.json() == {"issued": 2, "refreshed": 0, "already_issued": 0}
    again = await client.post(_url(world, "/issue"), json={"audience": "checked_in"}, headers=world["club"])
    assert again.json() == {"issued": 0, "refreshed": 0, "already_issued": 2}

    only_speakers = await client.post(_url(world, "/issue-preview"), json={"audience": "roles", "roles": ["speaker"]}, headers=world["club"])
    assert only_speakers.json()["eligible"] == 1
    everyone = await client.post(_url(world, "/issue"), json={"audience": "everyone"}, headers=world["club"])
    assert everyone.json()["issued"] == 1  # the no-show, now included

    page = (await client.get(_url(world), headers=world["club"])).json()
    assert page["total"] == 3
    row = next(r for r in page["items"] if r["name"] == "Ananya Iyer")
    # Its own number, drawn at registration, not the ticket id's body.
    assert row["number"] == world["a"].certificate_no
    assert world["a"].ticket_id.split("-", 1)[1] not in row["number"]
    assert row["status"] == "issued" and row["design_version"] == 1

    s = (await client.get(_url(world, "/summary"), headers=world["club"])).json()
    assert (s["issued"], s["revoked"], s["attended"], s["registrations"], s["design_version"]) == (3, 0, 2, 3, 1)


async def test_the_download_follows_issuing_and_nothing_else(client, world):
    a, b = world["a"], world["b"]
    # Before anyone is issued: neither downloads, check-in or no.
    refused = await client.get(f"/api/v1/events/certificates/{a.ticket_id}/pdf", headers=world["a_user"])
    assert refused.status_code == 403
    assert refused.json()["error_code"] == "CERTIFICATE_NOT_ISSUED"
    assert "once the organiser issues one" in refused.json()["message"]
    assert (await client.get(f"/api/v1/events/certificates/{b.ticket_id}/pdf", headers=world["b_user"])).status_code == 403

    await client.post(_url(world, "/issue"), json={"audience": "everyone"}, headers=world["club"])
    assert (await client.get(f"/api/v1/events/certificates/{a.ticket_id}/pdf", headers=world["a_user"])).status_code == 200
    pdf = await client.get(f"/api/v1/events/certificates/{b.ticket_id}/pdf", headers=world["b_user"])
    assert pdf.status_code == 200
    text = " ".join(pdf_text(pdf.content).split())
    assert "Bala Krishnan" in text and b.certificate_no in text
    assert b.ticket_id not in text and b.ticket_id.split("-", 1)[1] not in text

    revoked = await client.post(_url(world, f"/{a.id}/revoke"), headers=world["club"])
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    gone = await client.get(f"/api/v1/events/certificates/{a.ticket_id}/pdf", headers=world["a_user"])
    assert gone.status_code == 403 and "revoked" in gone.json()["message"]
    assert gone.json()["error_code"] == "CERTIFICATE_REVOKED"

    await client.post(_url(world, f"/{a.id}/reinstate"), headers=world["club"])
    assert (await client.get(f"/api/v1/events/certificates/{a.ticket_id}/pdf", headers=world["a_user"])).status_code == 200


async def test_verification_is_public_and_says_only_what_a_verifier_needs(client, world):
    a = world["a"]
    number = a.certificate_no
    # Attended but never issued: there is no certificate to verify.
    assert (await client.get(f"/api/v1/verify/certificates/{number}")).json()["reason"] == "not_found"

    await client.post(_url(world, "/issue"), json={"audience": "checked_in"}, headers=world["club"])
    r = await client.get(f"/api/v1/verify/certificates/{number}")
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert r.json()["attendee_name"] == "Ananya Iyer" and r.json()["event_name"] == "Build Night"
    assert "email" not in r.json()

    assert (await client.get("/api/v1/verify/certificates/CERT-NOPE-0000")).json() == {
        "valid": False, "reason": "not_found", "number": None, "attendee_name": None, "event_name": None,
        "org_name": None, "org_slug": None, "issued_at": None,
    }
    await client.post(_url(world, f"/{a.id}/revoke"), headers=world["club"])
    assert (await client.get(f"/api/v1/verify/certificates/{number.lower()}")).json()["reason"] == "revoked"


async def test_sending_reaches_only_those_issued_one(client, world, outbox, background):
    await client.post(_url(world, "/issue"), json={"audience": "checked_in"}, headers=world["club"])
    r = await client.post(_url(world, "/send"), json={}, headers=world["club"])
    assert r.status_code == 200, r.text
    assert r.json() == {"queued": 2, "already_sent": 0, "revoked": 1, "channels": ["email"]}
    assert sorted(m["recipients"][0] for m in outbox) == ["ananya-c@example.com", "speaker-c@example.com"]
    assert outbox[0]["subject"] == "Your certificate for Build Night"
    assert outbox[0]["attachment_filename"].startswith("certificate-")

    s = (await client.get(_url(world, "/summary"), headers=world["club"])).json()
    email = next(c for c in s["channels"] if c["channel"] == "email")
    assert (email["sent"], email["unsent"]) == (2, 0)
    log = (await client.get(f"/api/v1/events/{world['event'].id}/deliveries", params={"kind": "certificate"}, headers=world["club"])).json()
    assert log["total"] == 2 and all(row["kind"] == "certificate" for row in log["items"])


async def test_the_bundle_holds_every_issued_certificate(client, world):
    await client.post(_url(world, "/issue"), json={"audience": "checked_in"}, headers=world["club"])
    r = await client.get(_url(world, "/bundle.zip"), headers=world["club"])
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) == 2 and all(n.startswith("CERT-") and n.endswith(".pdf") for n in names)


# ─── Nothing exists until it is issued ──────────────────────────────────────
#
# The bug the owner hit: they checked in while the event was still running,
# the club did nothing at all, and a certificate was theirs at once. A
# certificate that cannot be seen but can still be downloaded or verified
# is not fixed, so every way one can be reached is asked here.


async def test_a_check_in_reaches_no_certificate_by_any_route(client, world):
    """A guest who checked in and was never issued one: `not_issued`
    everywhere -- the app's list and the event, the PDF, the PNG, the link
    in an email, the public verifier, its picture and its file check."""
    a, event = world["a"], world["event"].id
    assert a.is_attended is True and a.certificate_issued_at is None

    # The app: the list of the person's registrations, and the event itself.
    row = next(
        e for e in (await client.get(
            "/api/v1/events/list", params={"is_registered": "true"}, headers=world["a_user"]
        )).json()["items"] if e["id"] == event
    )
    info = (await client.get(f"/api/v1/events/info/{event}", headers=world["a_user"])).json()
    for shown in (row, info):
        assert shown["is_attended"] is True
        assert shown["has_certificate"] is False
        assert shown["certificate_revoked"] is False
        assert shown["certificate_url"] is None

    # The file, as the app downloads it and as it is previewed.
    for route in ("pdf", "image"):
        r = await client.get(f"/api/v1/events/certificates/{a.ticket_id}/{route}", headers=world["a_user"])
        assert r.status_code == 403, route
        assert r.json()["error_code"] == "CERTIFICATE_NOT_ISSUED", route

    # The link an email carries: properly signed, and still nothing behind it.
    signed = links.file_token("certificate", a.ticket_id)
    emailed = await client.get(f"/api/v1/files/certificates/{a.ticket_id}.pdf?s={signed}")
    assert emailed.status_code == 410
    assert emailed.json()["message"] == "This certificate is not available."

    # The public verifier: as if there were no such number, so nobody learns
    # from it that this person was at this event.
    number = a.certificate_no
    verified = (await client.get(f"/api/v1/verify/certificates/{number}")).json()
    assert verified["valid"] is False and verified["reason"] == "not_found"
    assert verified["attendee_name"] is None and verified["event_name"] is None
    assert (await client.get(f"/api/v1/verify/certificates/{number}/image")).status_code == 404
    assert (await client.get(f"/api/v1/verify/certificates/{number}/files")).json() == {"available": False, "sha256": []}


async def test_an_event_that_issues_nothing_has_nothing_for_the_club_either(client, world, outbox, background):
    """The organiser's own views, for the same event: no certificates
    listed, none counted, an empty bundle, and nothing that can be sent."""
    page = (await client.get(_url(world), headers=world["club"])).json()
    assert page["total"] == 0 and page["items"] == []

    s = (await client.get(_url(world, "/summary"), headers=world["club"])).json()
    # Two checked in, and not one certificate because of it.
    assert (s["issued"], s["revoked"], s["attended"], s["registrations"]) == (0, 0, 2, 3)

    bundle = await client.get(_url(world, "/bundle.zip"), headers=world["club"])
    assert bundle.status_code == 200
    held = zipfile.ZipFile(io.BytesIO(bundle.content))
    assert held.namelist() == ["README.txt"]
    assert b"No certificates have been issued" in held.read("README.txt")

    # Sending: nothing to send, to the whole event or to one checked-in guest.
    everyone = await client.post(_url(world, "/send"), json={}, headers=world["club"])
    assert everyone.json()["queued"] == 0 and everyone.json()["revoked"] == 3
    one = await client.post(_url(world, f"/{world['a'].id}/send"), headers=world["club"])
    assert one.json()["queued"] == 0
    assert outbox == []

    # Revoking one nobody holds is a conflict, not a certificate.
    refused = await client.post(_url(world, f"/{world['a'].id}/revoke"), headers=world["club"])
    assert refused.status_code == 409
