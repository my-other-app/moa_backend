"""A revoked ticket's documents, and what the holder is told.

Revoking a ticket made the door refuse it (app/api/checkins: "REVOKED";
app/api/events/volunteer: TICKET_REVOKED) but left every way of getting the
ticket itself open: the image the app shows, the PDF it downloads and the
Apple Wallet pass all rendered a live-looking QR for a ticket that would be
turned away at the door. `GET /events/tickets/{id}` did not say so either,
which is the only thing a guest holding the link from their email can ask.

These tests go through the real endpoints: the documents before and after a
revocation, the detail response's flag, the tag and the stored key the
document is kept under, reinstating, and that the door's own refusals are
untouched.
"""
import io
import uuid
from datetime import datetime, timezone

import pytest

from app.api.events import service as events_service
from app.api.events.volunteer.models import Volunteer
from app.api.events import wallet_service
from app.api.events.tickets import ticket_job, ticket_style
from app.api.templates_audience import TemplateAudience
from app.api.ticket_templates.models import TicketTemplates
from tests.conftest import auth_header

LAYERS = [
    {"id": "n", "type": "text", "text": "{{name}}", "x": 10, "y": 10, "width": 600,
     "font_size": 40, "color": "#fff", "align": "left", "bold": False,
     "max_lines": 1, "line_spacing": 0},
    {"id": "q", "type": "qr", "text": "", "x": 700, "y": 10, "width": 160,
     "font_size": 12, "color": "#fff", "align": "left", "bold": False,
     "max_lines": 1, "line_spacing": 0},
]
CANVAS = {"width": 1560, "height": 690}


@pytest.fixture
def drawn(monkeypatch):
    """Every ticket render, so a refusal can be shown to draw nothing.

    wkhtmltopdf is not installed where the tests run, and this is not a test
    of it: the PDF renders in `events.tickets`, the PNG in the router.
    """
    calls: list[str] = []

    import app.api.events.router as events_router
    import app.api.events.tickets as tickets

    def pdf(template_path, context, options=None, template_dir=None):
        calls.append("pdf")
        return io.BytesIO(b"%PDF-1.4 stub")

    def png(template_path, context, width=1400, template_dir=None):
        calls.append("png")
        return io.BytesIO(b"\x89PNG stub")

    monkeypatch.setattr(tickets, "generate_pdf_bytes", pdf)
    monkeypatch.setattr(events_router, "generate_image_bytes", png)
    return calls


@pytest.fixture
def pass_made(monkeypatch):
    """The Wallet pass, without the signing certificates: every call to it,
    so a refused one can be shown never to have reached the signer."""
    made: list[str] = []

    async def generate(session, ticket_id):
        made.append(ticket_id)
        return b"PK\x03\x04 stub pkpass"

    monkeypatch.setattr(wallet_service, "generate_wallet_pass", generate)
    return made


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="revoked-docs@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc),
        duration=3.0, location_name="Innovation Hall",
    )
    session.add(
        TicketTemplates(
            event_id=event.id, audience=TemplateAudience.event_default,
            name="T", config={"layers": LAYERS, "canvas": CANVAS},
        )
    )
    guest = await make_user(email="revoked-docs-guest@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Ananya Iyer")
    vol_user = await make_user(email="revoked-docs-volunteer@example.com")
    session.add(
        Volunteer(email=vol_user.email, event_id=event.id, user_id=vol_user.id, is_approved=True)
    )
    await session.commit()
    return {
        "event": event,
        "reg": reg,
        "guest": auth_header(await mint_token(guest)),
        "club": auth_header(await mint_token(owner)),
        "volunteer": auth_header(await mint_token(vol_user)),
    }


def _docs(world) -> dict[str, str]:
    code = world["reg"].ticket_id
    return {
        "image": f"/api/v1/events/tickets/{code}/image",
        "pdf": f"/api/v1/events/tickets/{code}/pdf",
        "wallet": f"/api/v1/events/tickets/{code}/wallet.pkpass",
    }


async def _revoke(client, world):
    r = await client.post(
        f"/api/v1/events/{world['event'].id}/tickets/{world['reg'].id}/revoke",
        json={"reason": "transferred"}, headers=world["club"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["revoked_at"] is not None
    return r


async def _reinstate(client, world):
    r = await client.post(
        f"/api/v1/events/{world['event'].id}/tickets/{world['reg'].id}/reinstate",
        headers=world["club"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["revoked_at"] is None
    return r


# --- the documents -------------------------------------------------------------


async def test_the_three_documents_are_served_and_then_refused(client, world, drawn, pass_made):
    """The whole of it: all three work, and none of them does once the
    organiser has taken the ticket back."""
    docs = _docs(world)

    for name, url in docs.items():
        r = await client.get(url, headers=world["guest"])
        assert r.status_code == 200, f"{name}: {r.text}"
    assert drawn == ["png", "pdf"] and pass_made == [world["reg"].ticket_id]

    await _revoke(client, world)

    for name, url in docs.items():
        r = await client.get(url, headers=world["guest"])
        assert r.status_code == 410, f"{name}: {r.status_code} {r.text}"
        body = r.json()
        assert body["error_code"] == "TICKET_REVOKED", name
        assert "revoked" in body["message"].lower(), name

    # Nothing was drawn and no pass was made for the refused ones.
    assert drawn == ["png", "pdf"] and pass_made == [world["reg"].ticket_id]


async def test_the_owning_club_is_refused_the_revoked_ticket_too(client, world, drawn, pass_made):
    """A club can read its attendees' tickets; a revoked one is no longer a
    ticket, and the one place it is still shown is the attendee list."""
    await _revoke(client, world)

    for url in _docs(world).values():
        r = await client.get(url, headers=world["club"])
        assert r.status_code == 410, r.text


async def test_a_stranger_is_still_refused_before_the_revocation_is_consulted(
    client, world, drawn, pass_made, make_user, mint_token
):
    """Access first: a revoked ticket must not tell a stranger it exists."""
    stranger = await make_user(email="revoked-docs-stranger@example.com")
    await _revoke(client, world)

    for url in _docs(world).values():
        r = await client.get(url, headers=auth_header(await mint_token(stranger)))
        assert r.status_code == 403, r.text
    assert drawn == [] and pass_made == []


async def test_reinstating_gives_the_documents_back(client, world, drawn, pass_made):
    await _revoke(client, world)
    await _reinstate(client, world)

    for name, url in _docs(world).items():
        r = await client.get(url, headers=world["guest"])
        assert r.status_code == 200, f"{name}: {r.text}"
    assert drawn == ["png", "pdf"] and pass_made == [world["reg"].ticket_id]


# --- what the ticket's own page says -------------------------------------------


async def test_the_detail_response_says_the_ticket_was_revoked(client, world):
    """The guest reading the link from their email has no event list to read
    the flag from, so the ticket's own page has to carry it -- spelled as the
    list spells it (`ticket_revoked`)."""
    url = f"/api/v1/events/tickets/{world['reg'].ticket_id}"

    before = await client.get(url, headers=world["guest"])
    assert before.status_code == 200, before.text
    assert before.json()["ticket_revoked"] is False

    await _revoke(client, world)

    after = await client.get(url, headers=world["guest"])
    # Still readable: this is where the holder is told why, and it carries
    # the event and the ticket id the screen prints beside the message.
    assert after.status_code == 200, after.text
    assert after.json()["ticket_revoked"] is True
    assert after.json()["ticket_id"] == world["reg"].ticket_id

    await _reinstate(client, world)
    again = await client.get(url, headers=world["guest"])
    assert again.json()["ticket_revoked"] is False


async def test_the_event_list_and_the_ticket_page_use_one_name(client, world):
    """`ticket_revoked` in both, so a client reads one name everywhere."""
    await _revoke(client, world)

    listed = await client.get("/api/v1/events/list", headers=world["guest"])
    assert listed.status_code == 200, listed.text
    rows = [e for e in listed.json()["items"] if e["id"] == world["event"].id]
    assert rows and rows[0]["ticket_revoked"] is True

    page = await client.get(
        f"/api/v1/events/tickets/{world['reg'].ticket_id}", headers=world["guest"]
    )
    assert page.json()["ticket_revoked"] == rows[0]["ticket_revoked"]


# --- the tag, and the copy kept in the bucket ----------------------------------


async def test_the_tag_and_the_stored_key_change_with_the_revocation(client, session, world, drawn):
    """A revoked ticket's PDF was kept in the bucket and in browsers under an
    unchanged tag, so even with the guard above an already-drawn one could
    have come back from either. The fingerprint names the revocation now, and
    it is both the ETag and the key the PDF is kept under."""
    code = world["reg"].ticket_id

    async def job():
        # Loaded the way the endpoints load it (its event and club with it),
        # so the render reads nothing lazily off the session.
        reg = await events_service.get_ticket_details(session, ticket_id=code)
        return reg, ticket_job(reg, await ticket_style(session, reg))

    reg, before = await job()

    await _revoke(client, world)
    _, revoked = await job()

    assert revoked.fingerprint != before.fingerprint
    assert revoked.cache_key("ticket", reg.id) != before.cache_key("ticket", reg.id)

    # And back again when it is reinstated, so the kept PDF is read, not redrawn.
    await _reinstate(client, world)
    _, again = await job()
    assert again.fingerprint == before.fingerprint
    assert again.cache_key("ticket", reg.id) == before.cache_key("ticket", reg.id)


async def test_a_tag_held_from_before_the_revocation_is_not_answered_304(client, world, drawn):
    """A phone holding the picture it was given while the ticket worked asks
    again with that tag. It must not be told the ticket is unchanged."""
    url = _docs(world)["image"]
    first = await client.get(url, headers=world["guest"])
    assert first.status_code == 200
    tag = first.headers["etag"]

    # The tag works while the ticket does.
    unchanged = await client.get(url, headers={**world["guest"], "If-None-Match": tag})
    assert unchanged.status_code == 304 and drawn == ["png"]

    await _revoke(client, world)
    stale = await client.get(url, headers={**world["guest"], "If-None-Match": tag})
    assert stale.status_code == 410, stale.text
    assert stale.json()["error_code"] == "TICKET_REVOKED"

    # Reinstated, the held tag is current again: the same ticket, the same
    # design, so nothing is redrawn.
    await _reinstate(client, world)
    back = await client.get(url, headers={**world["guest"], "If-None-Match": tag})
    assert back.status_code == 304 and drawn == ["png"]


# --- the door, unchanged -------------------------------------------------------


async def test_the_door_still_refuses_a_revoked_ticket(client, world):
    """What already worked: the gate's outcome and the volunteer scanner's
    error code. The refusal above borrows that code, not that status."""
    e, code = world["event"].id, world["reg"].ticket_id
    await _revoke(client, world)

    scan = await client.post(
        f"/api/v1/events/{e}/gate/scan",
        json={"code": code, "door": "Gate A",
              "scanned_at": datetime.now(timezone.utc).isoformat(),
              "client_ref": uuid.uuid4().hex},
        headers=world["club"],
    )
    assert scan.status_code == 200, scan.text
    assert scan.json()["outcome"] == "REVOKED"

    checkin = await client.post(
        f"/api/v1/events/volunteer/checkin/{e}", json={"ticket_id": code},
        headers=world["volunteer"],
    )
    assert checkin.status_code == 400, checkin.text
    assert checkin.json()["error_code"] == "TICKET_REVOKED"

    await _reinstate(client, world)
    ok = await client.post(
        f"/api/v1/events/volunteer/checkin/{e}", json={"ticket_id": code},
        headers=world["volunteer"],
    )
    assert ok.status_code == 200, ok.text
