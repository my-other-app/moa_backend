"""Every volunteer refusal carries a machine-readable error_code.

The mobile scanner decides which screen to show from these codes. Before
they existed it matched English substrings against the message, which is how
an event called "Paid Members Night" got classified as an unpaid fee: the
event's own name is interpolated into the wrong-event refusal.

So these strings are a contract with
moa_mobileapp/lib/features/volunteer/data/ticket_refusal.dart. Renaming one
here does not break the app -- it silently falls back to prose matching --
which is exactly why it needs a test rather than a comment.
"""
import pytest

from tests.conftest import auth_header


@pytest.fixture
async def make_volunteer(session):
    async def _factory(*, user, event=None, club=None):
        from app.api.events.volunteer.models import Volunteer

        volunteer = Volunteer(
            email=user.email,
            user_id=user.id,
            event_id=event.id if event else None,
            club_id=club.id if club else None,
            is_approved=True,
        )
        session.add(volunteer)
        await session.flush()
        return volunteer

    return _factory


async def _scan(client, token, event_id, ticket_id, endpoint="validate"):
    return await client.post(
        f"/api/v1/events/volunteer/{endpoint}/{event_id}",
        json={"ticket_id": ticket_id},
        headers=auth_header(token),
    )


@pytest.mark.parametrize("endpoint", ["validate", "checkin"])
async def test_unknown_ticket_is_registration_not_found(
    client, make_club, make_event, make_user, make_volunteer, mint_token, endpoint
):
    club_user, club = await make_club(name="Club", email=f"c-{endpoint}@example.com")
    event = await make_event(club=club, created_by=club_user)
    volunteer_user = await make_user(email=f"v-{endpoint}@example.com")
    await make_volunteer(user=volunteer_user, event=event)
    token = await mint_token(volunteer_user)

    response = await _scan(client, token, event.id, "MOA-NOPE1-NOPE2", endpoint)

    assert response.status_code == 400
    assert response.json()["error_code"] == "REGISTRATION_NOT_FOUND"


@pytest.mark.parametrize("endpoint", ["validate", "checkin"])
async def test_a_used_ticket_is_ticket_already_used(
    client,
    make_club,
    make_event,
    make_user,
    make_registration,
    make_volunteer,
    mint_token,
    endpoint,
):
    club_user, club = await make_club(name="Club", email=f"c2-{endpoint}@example.com")
    event = await make_event(club=club, created_by=club_user)
    attendee = await make_user(email=f"a2-{endpoint}@example.com")
    registration = await make_registration(event=event, user=attendee, is_attended=True)
    volunteer_user = await make_user(email=f"v2-{endpoint}@example.com")
    await make_volunteer(user=volunteer_user, event=event)
    token = await mint_token(volunteer_user)

    response = await _scan(client, token, event.id, registration.ticket_id, endpoint)

    assert response.status_code == 400
    assert response.json()["error_code"] == "TICKET_ALREADY_USED"


@pytest.mark.parametrize("endpoint", ["validate", "checkin"])
async def test_an_unpaid_ticket_is_fee_unpaid(
    client,
    make_club,
    make_event,
    make_user,
    make_registration,
    make_volunteer,
    mint_token,
    endpoint,
):
    club_user, club = await make_club(name="Club", email=f"c3-{endpoint}@example.com")
    event = await make_event(
        club=club, created_by=club_user, has_fee=True, reg_fee=250
    )
    attendee = await make_user(email=f"a3-{endpoint}@example.com")
    registration = await make_registration(event=event, user=attendee, is_paid=False)
    volunteer_user = await make_user(email=f"v3-{endpoint}@example.com")
    await make_volunteer(user=volunteer_user, event=event)
    token = await mint_token(volunteer_user)

    response = await _scan(client, token, event.id, registration.ticket_id, endpoint)

    assert response.status_code == 400
    assert response.json()["error_code"] == "FEE_UNPAID"


@pytest.mark.parametrize("endpoint", ["validate", "checkin"])
async def test_a_ticket_for_another_event_is_ticket_wrong_event(
    client,
    make_club,
    make_event,
    make_user,
    make_registration,
    make_volunteer,
    mint_token,
    endpoint,
):
    club_user, club = await make_club(name="Club", email=f"c4-{endpoint}@example.com")
    scanned_at = await make_event(club=club, created_by=club_user, name="Door Event")
    # The event name goes into the message, and the name here is the one that
    # used to be misread as an unpaid fee.
    elsewhere = await make_event(
        club=club, created_by=club_user, name="Paid Members Night"
    )
    attendee = await make_user(email=f"a4-{endpoint}@example.com")
    registration = await make_registration(event=elsewhere, user=attendee)
    volunteer_user = await make_user(email=f"v4-{endpoint}@example.com")
    await make_volunteer(user=volunteer_user, event=scanned_at)
    token = await mint_token(volunteer_user)

    response = await _scan(
        client, token, scanned_at.id, registration.ticket_id, endpoint
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "TICKET_WRONG_EVENT"
    # The name is still in the prose, which is where the app reads it from to
    # show "this ticket is for ...".
    assert "Paid Members Night" in body["message"]


@pytest.mark.parametrize("endpoint", ["validate", "checkin"])
async def test_a_non_volunteer_is_not_a_volunteer_not_a_bad_ticket(
    client,
    make_club,
    make_event,
    make_user,
    make_registration,
    mint_token,
    endpoint,
):
    # The refusal the app used to render as "ticket not found", sending a
    # volunteer whose access was revoked to argue with the attendee.
    club_user, club = await make_club(name="Club", email=f"c5-{endpoint}@example.com")
    event = await make_event(club=club, created_by=club_user)
    attendee = await make_user(email=f"a5-{endpoint}@example.com")
    registration = await make_registration(event=event, user=attendee)
    outsider = await make_user(email=f"v5-{endpoint}@example.com")
    token = await mint_token(outsider)

    response = await _scan(client, token, event.id, registration.ticket_id, endpoint)

    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_A_VOLUNTEER"


async def test_a_valid_ticket_still_validates(
    client,
    make_club,
    make_event,
    make_user,
    make_registration,
    make_volunteer,
    mint_token,
):
    club_user, club = await make_club(name="Club", email="c6@example.com")
    event = await make_event(club=club, created_by=club_user, name="Hack Night 04")
    attendee = await make_user(email="a6@example.com")
    registration = await make_registration(event=event, user=attendee)
    volunteer_user = await make_user(email="v6@example.com")
    await make_volunteer(user=volunteer_user, event=event)
    token = await mint_token(volunteer_user)

    response = await _scan(client, token, event.id, registration.ticket_id)

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["ticket_id"] == registration.ticket_id
    assert body["event_name"] == "Hack Night 04"
