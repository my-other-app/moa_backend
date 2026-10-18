"""The code printed under a ticket's QR is the ticket code.

The gate and the kiosk tell a guest whose QR will not scan to "type the code
under the QR", and they take the ticket's own code -- an admission number
typed there is refused (INVALID, with a hint for staff to check the guest in
by hand). The starter designs printed `{{admission_no}}` there.

Clubs' saved designs are not rewritten: one that asks for {{admission_no}}
still prints it. The names other tools use for the ticket's code
({{ticket_code}}, {{ticket_id}}) now read the ticket code.
"""
from datetime import datetime, timezone

import pytest

from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.renderer import pdf_text
from app.core.layout.starters import BADGE_STARTERS, TICKET_STARTERS, badge_starter, ticket_starter
from app.core.layout.tokens import TOKEN_BY_KEY, canonical_token, parse_ticket_template
from tests.conftest import auth_header


def _code_layers(s):
    return [l for l in s["layers"] if l.kind == "text" and l.id == "code"]


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="ticket-code@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0)
    guest = await make_user(email="ticket-code-guest@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Ananya Iyer", admission_no="MOA-0041-E99")
    await session.commit()
    return {"event": event, "reg": reg, "club": auth_header(await mint_token(owner)), "guest": auth_header(await mint_token(guest))}


async def test_the_downloaded_ticket_prints_the_ticket_code_not_the_admission_number(client, world):
    st = ticket_starter("classic")
    saved = await client.put(
        f"/api/v1/events/{world['event'].id}/designs/ticket/layout",
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])),
        headers=world["club"],
    )
    assert saved.status_code == 200, saved.text

    r = await client.get(f"/api/v1/events/tickets/{world['reg'].ticket_id}/pdf", headers=world["guest"])

    assert r.status_code == 200, r.text
    text = " ".join(pdf_text(r.content).split())
    assert world["reg"].ticket_id in text and "MOA-0041-E99" not in text


async def test_a_saved_design_asking_for_the_admission_number_still_gets_it(session, world):
    """Not rewritten: {{admission_no}} is the admission number, {{ticket_id}}
    (another tool's name for the code) the ticket code."""
    from app.api.events import service
    from app.api.events.documents import layout_values

    reg = await service.get_ticket_details(session, ticket_id=world["reg"].ticket_id)
    values = layout_values(reg, reg.event, "ticket")

    assert values["admission_no"] == "MOA-0041-E99"
    assert values["ticket_code"] == reg.ticket_id


async def test_the_designers_sample_shows_a_ticket_code_and_encodes_it(client, world, monkeypatch):
    from app.api.event_designs import router as designs_router

    layout = (await client.get(f"/api/v1/events/{world['event'].id}/designs/ticket/layout", headers=world["club"])).json()
    assert layout["sampleValues"]["ticket_code"] == TOKEN_BY_KEY["ticket_code"]["sample"]

    seen = {}
    real = designs_router._preview_png

    def capture(style, values, qr_text, *args):
        seen["qr"] = qr_text
        return real(style, values, qr_text, *args)

    monkeypatch.setattr(designs_router, "_preview_png", capture)
    st = ticket_starter("classic")
    r = await client.post(
        f"/api/v1/events/{world['event'].id}/designs/ticket/layout/preview",
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])),
        headers=world["club"],
    )
    assert r.status_code == 200 and seen["qr"] == TOKEN_BY_KEY["ticket_code"]["sample"]


# ─── The starters themselves ─────────────────────────────────────────────


@pytest.mark.parametrize("kind", list(TICKET_STARTERS))
def test_every_ticket_starter_prints_the_ticket_code_under_the_qr(kind):
    for seats in (False, True):
        s = ticket_starter(kind, seats=seats)
        (code,) = _code_layers(s)
        assert parse_ticket_template(code.text).tokens == ["ticket_code"]
        assert not [l for l in s["layers"] if l.kind == "text" and "admission_no" in parse_ticket_template(l.text).tokens]


@pytest.mark.parametrize("kind", list(BADGE_STARTERS))
def test_every_id_card_starter_prints_the_ticket_code(kind):
    s = badge_starter(kind)
    for code in _code_layers(s):
        assert parse_ticket_template(code.text).tokens == ["ticket_code"]
    assert not [l for l in s["layers"] if l.kind == "text" and "admission_no" in parse_ticket_template(l.text).tokens]


def test_the_ticket_code_is_a_token_of_its_own():
    assert canonical_token("ticket_code") == "ticket_code"
    assert canonical_token("ticket_id") == "ticket_code"
    assert canonical_token("admission_no") == "admission_no"
    assert "gate" in TOKEN_BY_KEY["ticket_code"]["note"]
