"""The app's list of events a volunteer works at (GET /events/volunteer/my-events):
the same people the gate lets in, so a volunteer always has the event to open."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.closeout.models import EventStatus
from app.api.cohosts.models import ACCEPTED, EventCohosts
from app.api.events.volunteer.models import Volunteer
from tests.conftest import auth_header

API = "/api/v1"
START = datetime.now(timezone.utc) + timedelta(days=8)


@pytest.fixture
async def team(session, make_club, make_event, make_user, mint_token):
    owner, club = await make_club(name="Gate Society", email="gate-society@example.com")
    _, partner = await make_club(name="Partner Guild", email="partner-guild@example.com")
    _, stranger_club = await make_club(name="Other Circle", email="other-circle@example.com")
    fest = await make_event(club=club, created_by=owner, name="Fest", event_datetime=START, duration=8.0)
    older = await make_event(club=club, created_by=owner, name="Earlier Meetup", event_datetime=START - timedelta(days=30), duration=2.0)
    draft = await make_event(club=club, created_by=owner, name="Not Yet", event_datetime=START + timedelta(days=30), duration=2.0)
    draft.lifecycle = EventStatus.draft
    elsewhere = await make_event(club=stranger_club, created_by=owner, name="Someone Else's", event_datetime=START, duration=2.0)
    now = datetime.now(timezone.utc)
    session.add(EventCohosts(event_id=fest.id, club_id=partner.id, status=ACCEPTED, invited_at=now, expires_at=now + timedelta(days=3), responded_at=now))
    people = {}
    for key in ("on_event", "club_wide", "partner_wide", "removed", "unapproved", "nobody"):
        people[key] = await make_user(email=f"{key.replace('_', '-')}-vol@example.com")
    session.add_all([
        Volunteer(email="on-event-vol@example.com", event_id=fest.id, user_id=people["on_event"].id, is_approved=True),
        Volunteer(email="club-wide-vol@example.com", club_id=club.id, user_id=people["club_wide"].id, is_approved=True),
        Volunteer(email="partner-wide-vol@example.com", club_id=partner.id, user_id=people["partner_wide"].id, is_approved=True),
        Volunteer(email="removed-vol@example.com", event_id=fest.id, user_id=people["removed"].id, is_approved=True, is_deleted=True),
        Volunteer(email="unapproved-vol@example.com", club_id=club.id, user_id=people["unapproved"].id, is_approved=False),
    ])
    # The older meetup has finished: the status clock has closed it.
    older.lifecycle = EventStatus.closed
    await session.commit()
    headers = {k: auth_header(await mint_token(u)) for k, u in people.items()}
    return {"fest": fest, "older": older, "draft": draft, "elsewhere": elsewhere, "h": headers}


async def _mine(client, h) -> list[int]:
    r = await client.get(f"{API}/events/volunteer/my-events", headers=h)
    assert r.status_code == 200, r.text
    return [e["id"] for e in r.json()]


async def _gate(client, event_id, h) -> int:
    return (await client.get(f"{API}/events/{event_id}/gate/context", headers=h)).status_code


async def test_every_volunteer_the_gate_admits_sees_the_event(client, team):
    fest = team["fest"].id
    # On the event itself, club-wide in the host club, club-wide in a co-host's club.
    for who in ("on_event", "club_wide", "partner_wide"):
        assert await _mine(client, team["h"][who]) == [fest], who
        assert await _gate(client, fest, team["h"][who]) == 200, who


async def test_nobody_else_sees_it(client, team):
    fest = team["fest"].id
    for who in ("removed", "unapproved", "nobody"):
        assert await _mine(client, team["h"][who]) == [], who
        assert await _gate(client, fest, team["h"][who]) == 403, who


async def test_a_club_wide_place_lists_only_events_whose_door_can_open(client, session, team):
    h = team["h"]["club_wide"]
    # Not the closed meetup, not the draft, not another club's event.
    assert await _mine(client, h) == [team["fest"].id]
    team["older"].lifecycle = EventStatus.published
    await session.commit()
    # Newest first, each once.
    assert await _mine(client, h) == [team["fest"].id, team["older"].id]


async def test_a_place_on_the_event_still_lists_it_after_it_ends(client, session, team):
    team["fest"].lifecycle = EventStatus.closed
    await session.commit()
    assert await _mine(client, team["h"]["on_event"]) == [team["fest"].id]
    assert await _mine(client, team["h"]["club_wide"]) == []


async def test_a_deleted_event_is_not_listed(client, session, team):
    team["fest"].soft_delete()
    await session.commit()
    assert await _mine(client, team["h"]["on_event"]) == []
    assert await _mine(client, team["h"]["partner_wide"]) == []
