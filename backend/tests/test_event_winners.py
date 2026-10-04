"""Tests for recording event winners.

`is_won` and `position` have been columns on every registration since the
table was written, and the badge checker reads `position` for three of its
metrics -- events_won (1st), runner_up_count (2nd) and podium_count (3rd).
Nothing in the API ever wrote either column, so those three badges could not
be earned by anyone, by any route, ever. These endpoints are what write them.
"""

import pytest

from tests.conftest import auth_header


@pytest.fixture
async def event_and_entrant(make_club, make_event, make_user, make_registration):
    owner, club = await make_club(name="Prize Club", email="prizeclub@example.com")
    event = await make_event(club=club, name="Prize Event", created_by=owner)
    entrant = await make_user(email="entrant@example.com")
    registration = await make_registration(event=event, user=entrant)
    return owner, event, entrant, registration


@pytest.mark.asyncio
async def test_setting_a_placing_marks_both_columns(
    client, mint_token, event_and_entrant
):
    """`is_won` means "placed at all" and must agree with `position`."""
    owner, event, _, registration = event_and_entrant
    token = await mint_token(owner)

    response = await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{registration.id}",
        headers=auth_header(token),
        json={"position": 1},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["position"] == 1
    assert body["is_won"] is True


@pytest.mark.asyncio
async def test_clearing_a_placing_resets_both_columns(
    client, mint_token, event_and_entrant
):
    owner, event, _, registration = event_and_entrant
    token = await mint_token(owner)

    await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{registration.id}",
        headers=auth_header(token),
        json={"position": 2},
    )
    response = await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{registration.id}",
        headers=auth_header(token),
        json={"position": None},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["position"] is None
    assert body["is_won"] is False


@pytest.mark.asyncio
async def test_a_placing_below_one_is_rejected(client, mint_token, event_and_entrant):
    owner, event, _, registration = event_and_entrant
    token = await mint_token(owner)

    response = await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{registration.id}",
        headers=auth_header(token),
        json={"position": 0},
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_winners_are_listed_best_first(
    client, make_user, make_registration, mint_token, event_and_entrant
):
    owner, event, _, first = event_and_entrant
    second_user = await make_user(email="second@example.com")
    second = await make_registration(event=event, user=second_user)
    token = await mint_token(owner)

    # Recorded out of order on purpose.
    await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{second.id}",
        headers=auth_header(token),
        json={"position": 2},
    )
    await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{first.id}",
        headers=auth_header(token),
        json={"position": 1},
    )

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/winners", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert [w["position"] for w in response.json()] == [1, 2]


@pytest.mark.asyncio
async def test_joint_placings_are_allowed(
    client, make_user, make_registration, mint_token, event_and_entrant
):
    """Shared places are real; rejecting them would be a surprise."""
    owner, event, _, first = event_and_entrant
    other_user = await make_user(email="joint@example.com")
    other = await make_registration(event=event, user=other_user)
    token = await mint_token(owner)

    for reg in (first, other):
        response = await client.put(
            f"/api/v1/events/registration/{event.id}/winners/{reg.id}",
            headers=auth_header(token),
            json={"position": 1},
        )
        assert response.status_code == 200, response.text

    winners = await client.get(
        f"/api/v1/events/registration/{event.id}/winners", headers=auth_header(token)
    )
    assert [w["position"] for w in winners.json()] == [1, 1]


@pytest.mark.asyncio
async def test_the_placing_reaches_the_registration_list(
    client, mint_token, event_and_entrant
):
    """The list schema carried is_won but not position."""
    owner, event, _, registration = event_and_entrant
    token = await mint_token(owner)

    await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{registration.id}",
        headers=auth_header(token),
        json={"position": 3},
    )

    response = await client.get(
        f"/api/v1/events/registration/{event.id}/list", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["position"] == 3


@pytest.mark.asyncio
async def test_a_placing_can_earn_the_badge_that_depends_on_it(
    client, session, mint_token, event_and_entrant
):
    """
    The point of the whole feature: events_won counts position == 1, so a
    badge triggered on it was unreachable while nothing set the column.
    """
    owner, event, entrant, registration = event_and_entrant

    from app.api.badges.models import BadgeDefinitions, BadgeType

    session.add(
        BadgeDefinitions(
            slug="first-win",
            name="First Win",
            description="Won an event",
            emoji="🥇",
            badge_type=BadgeType.user,
            trigger_metric="events_won",
            threshold=1,
        )
    )
    await session.flush()

    token = await mint_token(owner)
    await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{registration.id}",
        headers=auth_header(token),
        json={"position": 1},
    )

    badges = await client.get(
        f"/api/v1/badges/user/{entrant.id}", headers=auth_header(token)
    )
    assert badges.status_code == 200, badges.text
    assert [b["badge"]["slug"] for b in badges.json()["badges"]] == ["first-win"]


@pytest.mark.asyncio
async def test_another_club_cannot_set_a_winner(
    client, make_club, mint_token, event_and_entrant
):
    _, event, _, registration = event_and_entrant
    intruder, _ = await make_club(name="Intruder", email="winintruder@example.com")
    token = await mint_token(intruder)

    response = await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{registration.id}",
        headers=auth_header(token),
        json={"position": 1},
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_an_admin_can_set_a_winner_for_any_club(
    client, make_admin, mint_token, event_and_entrant
):
    _, event, _, registration = event_and_entrant
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{registration.id}",
        headers=auth_header(token),
        json={"position": 1},
    )

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_a_registration_from_another_event_is_a_404(
    client, make_club, make_event, make_user, make_registration, mint_token,
    event_and_entrant,
):
    owner, event, _, _ = event_and_entrant
    other_event = await make_event(
        club=(await make_club(name="Other", email="otherwin@example.com"))[1],
        name="Other Event",
        created_by=owner,
    )
    stranger = await make_user(email="stranger@example.com")
    foreign = await make_registration(event=other_event, user=stranger)
    token = await mint_token(owner)

    response = await client.put(
        f"/api/v1/events/registration/{event.id}/winners/{foreign.id}",
        headers=auth_header(token),
        json={"position": 1},
    )

    assert response.status_code == 404, response.text
