"""What the app is told about a guest's certificate, and what it shows.

The app lists the certificates in the person's registrations and shows the
server's render of each. The registrations used to say only `is_attended`,
so a speaker the organiser issued one to without a check-in never saw it in
the app, and a revoked one was still listed: its render refused, and the
app drew its own stand-in in place of the club's design.

`is_attended` then stopped counting the other way too: checking in at an
event whose club had issued nothing listed a certificate the club never
meant to give. Nothing is listed until the club issues one.
"""
import io
from datetime import datetime, timezone

import pytest
from PIL import Image

from app.api.events.certificates import certificate_document
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.renderer import pdf_to_png
from app.core.layout.starters import certificate_starter
from tests.conftest import auth_header


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Respawn Room", email="app-certs@example.com")
    event = await make_event(
        club=club, created_by=owner, name="LAN Night",
        event_datetime=datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc), duration=3.0,
    )
    a = await make_user(email="app-certs-a@example.com")
    b = await make_user(email="app-certs-b@example.com")
    s = await make_user(email="app-certs-s@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", is_attended=True)
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan")  # never checked in
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", role="speaker")  # spoke, no check-in
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
        "event": event, "a": r_a, "b": r_b, "s": r_s, "club": club_headers, "canvas_width": int(st["canvas"].width),
        "as_a": auth_header(await mint_token(a)),
        "as_b": auth_header(await mint_token(b)),
        "as_s": auth_header(await mint_token(s)),
    }


async def _row(client, world, who: str) -> dict:
    """The event as the app's list of the person's registrations has it."""
    items = (await client.get("/api/v1/events/list", params={"is_registered": "true"}, headers=world[f"as_{who}"])).json()["items"]
    return next(x for x in items if x["id"] == world["event"].id)


async def _info(client, world, who: str) -> dict:
    return (await client.get(f"/api/v1/events/info/{world['event'].id}", headers=world[f"as_{who}"])).json()


def _issue_url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/certificates{path}"


async def test_before_any_issuing_nobody_holds_one_not_even_a_check_in(client, world):
    """What the owner hit: they checked in while the event was still on, the
    club had done nothing, and the app listed a certificate for them."""
    for read in (_row, _info):
        a, b = await read(client, world, "a"), await read(client, world, "b")
        assert a["is_attended"] is True
        assert a["has_certificate"] is False and a["certificate_revoked"] is False
        assert a["certificate_url"] is None
        assert b["has_certificate"] is False and b["certificate_url"] is None

    # It appears the moment the club issues, and not before.
    issued = await client.post(_issue_url(world, "/issue"), json={"audience": "checked_in"}, headers=world["club"])
    assert issued.status_code == 200, issued.text
    for read in (_row, _info):
        a, b = await read(client, world, "a"), await read(client, world, "b")
        assert a["has_certificate"] is True and a["certificate_revoked"] is False
        assert a["certificate_url"].endswith(f"/certificates/{world['a'].ticket_id}")
        # The no-show was not in the audience: still nothing.
        assert b["has_certificate"] is False and b["certificate_url"] is None


async def test_one_issued_without_a_check_in_is_listed_for_the_app(client, world):
    issued = await client.post(_issue_url(world, "/issue"), json={"audience": "roles", "roles": ["speaker"]}, headers=world["club"])
    assert issued.status_code == 200, issued.text
    for read in (_row, _info):
        s = await read(client, world, "s")
        assert s["is_attended"] is False
        assert s["has_certificate"] is True
        assert s["certificate_url"].endswith(f"/certificates/{world['s'].ticket_id}")
    # And the render the app opens is served to it, as the email's PDF is.
    image = await client.get(f"/api/v1/events/certificates/{world['s'].ticket_id}/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"


async def test_a_revoked_one_is_not_listed_as_held(client, world):
    await client.post(_issue_url(world, "/issue"), json={"audience": "checked_in"}, headers=world["club"])
    revoked = await client.post(_issue_url(world, f"/{world['a'].id}/revoke"), headers=world["club"])
    assert revoked.status_code == 200, revoked.text
    for read in (_row, _info):
        a = await read(client, world, "a")
        assert a["is_attended"] is True
        assert a["has_certificate"] is False and a["certificate_revoked"] is True
        assert a["certificate_url"] is None
    assert (await client.get(f"/api/v1/events/certificates/{world['a'].ticket_id}/image")).status_code == 403

    await client.post(_issue_url(world, f"/{world['a'].id}/reinstate"), headers=world["club"])
    a = await _row(client, world, "a")
    assert a["has_certificate"] is True and a["certificate_revoked"] is False


async def test_the_image_the_app_shows_is_the_clubs_design_the_email_attaches(client, session, world):
    """The app shows /image. It is the first page of the very PDF the email
    attaches (certificate_document), drawn from the club's own layout at the
    layout's own size -- not a default design."""
    reg = world["a"]
    await client.post(_issue_url(world, "/issue"), json={"audience": "checked_in"}, headers=world["club"])
    image = await client.get(f"/api/v1/events/certificates/{reg.ticket_id}/image")
    assert image.status_code == 200, image.text
    etag = image.headers["etag"]
    assert etag

    shown = Image.open(io.BytesIO(image.content)).convert("RGB")
    assert shown.size[0] == world["canvas_width"]

    await session.refresh(reg, ["event"])
    attached = await certificate_document(session, reg)
    page = Image.open(io.BytesIO(pdf_to_png(attached.content, width_px=shown.size[0]))).convert("RGB")
    assert page.size == shown.size
    assert page.tobytes() == shown.tobytes()

    # The same address answers with the same tag until something printed on
    # it changes, so the app can keep it under the certificate's id.
    again = await client.get(f"/api/v1/events/certificates/{reg.ticket_id}/image", headers={"If-None-Match": etag})
    assert again.status_code == 304
