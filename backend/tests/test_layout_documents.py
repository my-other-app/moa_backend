"""A layered design, through the real endpoints.

The point of the layout renderer is that the ticket an attendee downloads is
the design the club saw. These tests save a design through the API, fetch
the ticket PDF and PNG an attendee would, and read the attendee's name back
out of the PDF -- which is the first time a test here can say that.
"""
from datetime import datetime, timezone

import pytest

from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.renderer import pdf_text
from app.core.layout.starters import certificate_starter, ticket_starter
from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None)


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="layout@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0,
        location_name="Innovation Hall",
    )
    user = await make_user(email="lakshmi-layout@example.com")
    registration = await make_registration(event=event, user=user, full_name="Lakshminarayanan Venkataraman")
    registration.is_attended = True
    # Issued, so there is a certificate to draw: a check-in is not one
    # (events.certificates.availability).
    registration.certificate_issued_at = datetime.now(timezone.utc)
    await session.commit()
    return {"event": event, "registration": registration, "club": auth_header(await mint_token(owner)),
            "attendee": auth_header(await mint_token(user))}


def _starter_body(kind: str) -> dict:
    s = ticket_starter("classic") if kind == "ticket" else certificate_starter("classic")
    return dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"]))


async def test_an_event_with_no_design_opens_on_a_starter(client, world):
    r = await client.get(f"/api/v1/events/{world['event'].id}/designs/ticket/layout", headers=world["club"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fromStarter"] is True
    assert body["version"] == 0
    assert any(l["kind"] == "qr" for l in body["layers"])
    assert body["brand"]["orgName"] == "Yukthi"
    assert any(t["key"] == "attendee_name" for t in body["tokens"])


async def test_saving_a_design_forks_it_onto_the_event_and_versions_it(client, world):
    e = world["event"].id
    first = await client.put(f"/api/v1/events/{e}/designs/ticket/layout", json=_starter_body("ticket"), headers=world["club"])
    second = await client.put(f"/api/v1/events/{e}/designs/ticket/layout", json=_starter_body("ticket"), headers=world["club"])

    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1
    assert first.json()["isOwn"] is True
    assert second.json()["version"] == 2


async def test_a_design_the_editor_would_refuse_is_refused_with_the_field_to_fix(client, world):
    body = _starter_body("ticket")
    body["layers"] = [l for l in body["layers"] if l["kind"] != "qr"]
    # A text layer: a shape would drop the field and raise nothing.
    next(l for l in body["layers"] if l["kind"] == "text")["text"] = "{{nmae}}"

    r = await client.put(f"/api/v1/events/{world['event'].id}/designs/ticket/layout", json=body, headers=world["club"])

    assert r.status_code == 422, r.text
    fields = (r.json().get("errors") or {}).get("fields") or {}
    assert "layers" in fields and "QR" in fields["layers"]
    assert any(k.endswith(".text") and "Did you mean" in v for k, v in fields.items())


async def test_the_ticket_an_attendee_downloads_is_the_saved_design(client, world):
    e, reg = world["event"].id, world["registration"]
    saved = await client.put(f"/api/v1/events/{e}/designs/ticket/layout", json=_starter_body("ticket"), headers=world["club"])
    assert saved.status_code == 200, saved.text

    pdf = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/pdf", headers=world["attendee"])
    png = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/image", headers=world["attendee"])

    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(b"%PDF")
    text = pdf_text(pdf.content)
    assert "Lakshminarayanan Venkataraman" in text
    assert reg.ticket_id in text
    assert "Build Night" in text
    assert png.status_code == 200 and png.content.startswith(b"\\x89PNG".decode("unicode_escape").encode("latin1"))


async def test_the_certificate_follows_its_own_design(client, world):
    e, reg = world["event"].id, world["registration"]
    saved = await client.put(f"/api/v1/events/{e}/designs/certificate/layout", json=_starter_body("certificate"), headers=world["club"])
    assert saved.status_code == 200, saved.text

    pdf = await client.get(f"/api/v1/events/certificates/{reg.ticket_id}/pdf")
    png = await client.get(f"/api/v1/events/certificates/{reg.ticket_id}/image")

    assert pdf.status_code == 200, pdf.text
    # The name is set large and wraps onto two lines, as the starter allows;
    # the words are there, with a line break where the box ran out.
    text = " ".join(pdf_text(pdf.content).split())
    assert "Lakshminarayanan Venkataraman" in text
    assert "CERTIFICATE OF ATTENDANCE" in text
    assert png.status_code == 200 and png.content[:4] == b"\\x89PNG".decode("unicode_escape").encode("latin1")


async def test_the_preview_renders_the_unsaved_design_for_a_real_attendee(client, world):
    e, reg = world["event"].id, world["registration"]
    body = {**_starter_body("ticket"), "ticket_id": reg.ticket_id}

    r = await client.post(f"/api/v1/events/{e}/designs/ticket/layout/preview", json=body, headers=world["club"])

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")


async def test_the_preview_without_an_attendee_uses_the_sample_guest(client, world):
    r = await client.post(f"/api/v1/events/{world['event'].id}/designs/ticket/layout/preview",
                          json=_starter_body("ticket"), headers=world["club"])

    assert r.status_code == 200, r.text
    assert r.content[:4] == b"\\x89PNG".decode("unicode_escape").encode("latin1")


async def test_a_starter_can_be_fetched_to_start_from(client, world):
    e = world["event"].id
    r = await client.get(f"/api/v1/events/{e}/designs/ticket/layout/starters/stub", headers=world["club"])

    assert r.status_code == 200, r.text
    assert r.json()["canvas"]["background"] == "#141614"
    assert (await client.get(f"/api/v1/events/{e}/designs/ticket/layout/starters/nope", headers=world["club"])).status_code == 404


async def test_saving_colours_does_not_wipe_the_design(client, world):
    """config and layout are saved by different controls; the second must not
    drop what the first put there."""
    e = world["event"].id
    await client.put(f"/api/v1/events/{e}/designs/ticket/layout", json=_starter_body("ticket"), headers=world["club"])
    shown = await client.get(f"/api/v1/events/{e}/designs/ticket", headers=world["club"])
    config = {**shown.json()["config"], "accent_color": "#ff5a1f"}

    await client.put(f"/api/v1/events/{e}/designs/ticket", json={"config": config}, headers=world["club"])

    after = await client.get(f"/api/v1/events/{e}/designs/ticket/layout", headers=world["club"])
    assert after.json()["fromStarter"] is False
    assert after.json()["version"] == 1


async def test_another_club_cannot_read_or_save_this_events_design(client, world, make_club, mint_token, session):
    other, _ = await make_club(name="Other", email="other-layout@example.com")
    await session.commit()
    headers = auth_header(await mint_token(other))
    e = world["event"].id

    assert (await client.get(f"/api/v1/events/{e}/designs/ticket/layout", headers=headers)).status_code == 403
    assert (await client.put(f"/api/v1/events/{e}/designs/ticket/layout", json=_starter_body("ticket"), headers=headers)).status_code == 403
