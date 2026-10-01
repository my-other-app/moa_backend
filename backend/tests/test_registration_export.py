"""Regression tests for GET /events/registration/{event_id}/export.

This endpoint returned 500 for every export in production:

    AttributeError: 'dict' object has no attribute 'ticket_id'
    app/api/events/registration/router.py, line 179, in <listcomp>

list_event_registrations returns plain dicts (it serialises each row by hand
so it can embed user, volunteer and institution), but the export built its
spreadsheet with attribute access.

The same listcomp spreads row["additional_details"] into its own columns --
the answers to the event's custom registration questions. The service never
included that key, so even once the crash is fixed those columns would come
out empty; it is now part of the serialised row.
"""

import pytest

from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_export_returns_a_spreadsheet(
    client, make_club, make_event, make_user, make_registration, mint_token
):
    owner, club = await make_club(name="Export Club", email="exportclub@example.com")
    event = await make_event(club=club, name="Export Event", created_by=owner)
    attendee = await make_user(email="attendee@example.com")
    await make_registration(event=event, user=attendee)
    token = await mint_token(owner)

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/export", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    # openpyxl workbooks are zip containers; "PK" is the zip magic number.
    assert response.content[:2] == b"PK"


@pytest.mark.asyncio
async def test_export_includes_custom_question_answers(
    client, make_club, make_event, make_user, make_registration, mint_token
):
    """The answers are the reason an organiser exports at all."""
    owner, club = await make_club(name="Answers Club", email="answers@example.com")
    event = await make_event(club=club, name="Answers Event", created_by=owner)
    attendee = await make_user(email="answerer@example.com")
    await make_registration(
        event=event,
        user=attendee,
        additional_details={"tshirt": "L", "college": "STIST"},
    )
    token = await mint_token(owner)

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/export", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text

    from io import BytesIO

    import openpyxl

    sheet = openpyxl.load_workbook(BytesIO(response.content)).active
    headers = [cell.value for cell in next(sheet.iter_rows(max_row=1))]
    assert "tshirt" in headers
    assert "college" in headers


@pytest.mark.asyncio
async def test_export_handles_a_registration_with_no_volunteer(
    client, make_club, make_event, make_user, make_registration, mint_token
):
    """`row.volunteer.email` was a second latent crash: most registrations have
    no volunteer until someone checks them in."""
    owner, club = await make_club(name="No Vol Club", email="novol@example.com")
    event = await make_event(club=club, name="No Vol Event", created_by=owner)
    attendee = await make_user(email="novol-attendee@example.com")
    await make_registration(event=event, user=attendee)
    token = await mint_token(owner)

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/export", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_export_is_refused_to_another_club(
    client, make_club, make_event, make_user, make_registration, mint_token
):
    """Registrant names, emails and phone numbers -- this must stay owner-only."""
    owner, club = await make_club(name="Owner Club", email="owner-exp@example.com")
    event = await make_event(club=club, name="Private Event", created_by=owner)
    attendee = await make_user(email="private-attendee@example.com")
    await make_registration(event=event, user=attendee)

    other_owner, _ = await make_club(name="Other Club", email="other-exp@example.com")
    token = await mint_token(other_owner)

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/export", headers=auth_header(token)
    )

    assert response.status_code == 403, response.text
