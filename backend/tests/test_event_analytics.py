"""Tests for GET /events/registration/{event_id}/analytics.

The endpoint already returned `top_institutions`, but that list is sliced to
the five largest. The clubs dashboard shows a headline "Institutions" count,
and taking it from the length of that list would read 5 for an event with
fifty colleges -- wrong in exactly the case where the number is interesting.
`total_institutions` is the real distinct count.
"""

import pytest

from tests.conftest import auth_header


def _register(make_registration, event, user, college=None):
    details = {"College Name": college} if college else None
    return make_registration(event=event, user=user, additional_details=details)


@pytest.mark.asyncio
async def test_total_institutions_counts_every_distinct_college(
    client, make_club, make_event, make_user, make_registration, mint_token
):
    owner, club = await make_club(name="Inst Club", email="instclub@example.com")
    event = await make_event(club=club, name="Inst Event", created_by=owner)

    # Seven distinct colleges: more than the five `top_institutions` keeps.
    colleges = ["CET", "TKM", "NSS", "MACE", "GEC", "RIT", "SCT"]
    for index, college in enumerate(colleges):
        attendee = await make_user(email=f"attendee{index}@example.com")
        await _register(make_registration, event, attendee, college)

    token = await mint_token(owner)
    response = await client.get(
        f"/api/v1/events/registration/{event.id}/analytics", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_institutions"] == 7
    # The capped list is still capped -- that is what makes the new field needed.
    assert len(body["top_institutions"]) == 5


@pytest.mark.asyncio
async def test_registrations_without_a_college_are_not_an_institution(
    client, make_club, make_event, make_user, make_registration, mint_token
):
    """Unanswered college questions land in the "Unknown" bucket."""
    owner, club = await make_club(name="Unknown Club", email="unknownclub@example.com")
    event = await make_event(club=club, name="Unknown Event", created_by=owner)

    named = await make_user(email="named@example.com")
    await _register(make_registration, event, named, "CET")
    anonymous = await make_user(email="anonymous@example.com")
    await _register(make_registration, event, anonymous)

    token = await mint_token(owner)
    response = await client.get(
        f"/api/v1/events/registration/{event.id}/analytics", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["total_institutions"] == 1


@pytest.mark.asyncio
async def test_an_event_with_no_registrations_reports_zero(
    client, make_club, make_event, mint_token
):
    owner, club = await make_club(name="Empty Club", email="emptyclub@example.com")
    event = await make_event(club=club, name="Empty Event", created_by=owner)
    token = await mint_token(owner)

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/analytics", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_institutions"] == 0
    assert body["total_registrations"] == 0
