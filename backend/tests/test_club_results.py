"""An event's results from the club's dashboard: 1st, 2nd and 3rd for guests,
and for teams within each competition track -- the placing printed on each
member's certificate and counted by the badge rules; who may set them."""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.badges.models import BadgeDefinitions, BadgeType, UserBadges
from app.api.competitions.models import CompetitionTeamMembers, CompetitionTeams, Competitions, CompetitionTracks
from app.api.events.documents import document_values
from app.api.events.models import EventRegistrationsLink
from app.api.security import service as security
from app.core.rate_limit import limiter
from tests.club_ops_helpers import member_header
from tests.conftest import auth_header


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)
    owner, club = await make_club(name=f"Hack Club {uuid.uuid4().hex[:4]}", email=f"hack{uuid.uuid4().hex[:6]}@example.com")
    rival_owner, _ = await make_club(name=f"Rival {uuid.uuid4().hex[:4]}", email=f"rv{uuid.uuid4().hex[:6]}@example.com")
    event = await make_event(club=club, created_by=owner, name="Hackathon")
    people = [await make_user(email=f"hacker{i}-{uuid.uuid4().hex[:6]}@example.com") for i in range(5)]
    regs = [await make_registration(event=event, user=p) for p in people]
    comp = Competitions(event_id=event.id, enabled=True)
    session.add(comp)
    await session.flush()
    hardware = CompetitionTracks(competition_id=comp.id, name="Hardware", sort_order=0)
    software = CompetitionTracks(competition_id=comp.id, name="Software", sort_order=1)
    session.add_all([hardware, software])
    await session.flush()
    nova = CompetitionTeams(competition_id=comp.id, name="Nova", track_id=hardware.id)
    orbit = CompetitionTeams(competition_id=comp.id, name="Orbit", track_id=software.id)
    loose = CompetitionTeams(competition_id=comp.id, name="Wanderers", track_id=None)
    session.add_all([nova, orbit, loose])
    await session.flush()
    for team, members in ((nova, regs[:2]), (orbit, regs[2:4]), (loose, regs[4:])):
        for r in members:
            session.add(CompetitionTeamMembers(competition_id=comp.id, team_id=team.id, registration_id=r.id))
    session.add(BadgeDefinitions(slug=f"first-win-{uuid.uuid4().hex[:4]}", name="First Win", description="Won", emoji="🥇",
                                 badge_type=BadgeType.user, trigger_metric="events_won", threshold=1))
    await session.commit()
    return SimpleNamespace(
        owner=owner, club=club, event=event, people=people, regs=regs, comp=comp, hardware=hardware, software=software,
        nova=nova, orbit=orbit, loose=loose, h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)),
    )


def _url(w, path=""):
    return f"/api/v1/events/{w.event.id}/results{path}"


async def test_results_list_each_tracks_teams_and_their_members(client, world):
    r = await client.get(_url(world), headers=world.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_competition"] is True and body["winners"] == []
    assert [t["name"] for t in body["tracks"]] == ["Hardware", "Software", "No track"]
    hardware = body["tracks"][0]
    assert hardware["id"] == world.hardware.id and [t["name"] for t in hardware["teams"]] == ["Nova"]
    assert len(hardware["teams"][0]["members"]) == 2 and hardware["teams"][0]["position"] is None


async def test_placing_a_team_places_every_member_for_certificates_and_badges(client, session, world):
    r = await client.put(_url(world, f"/teams/{world.nova.id}"), json={"position": 1}, headers=world.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tracks"][0]["teams"][0]["position"] == 1
    assert {w["full_name"] for w in body["winners"]} == {world.regs[0].full_name, world.regs[1].full_name}
    assert {w["track_name"] for w in body["winners"]} == {"Hardware"} and {w["team_name"] for w in body["winners"]} == {"Nova"}
    for reg in world.regs[:2]:
        await session.refresh(reg)
        assert reg.position == 1 and reg.is_won is True
        await session.refresh(reg, attribute_names=["event"])
        assert document_values(reg, world.event)["placing"] == "First"
    earned = (await session.scalars(select(UserBadges.user_id))).all()
    assert set(earned) >= {world.people[0].id, world.people[1].id}

    # Each track has its own 1st.
    assert (await client.put(_url(world, f"/teams/{world.orbit.id}"), json={"position": 1}, headers=world.h)).status_code == 200
    # Moving Nova to 2nd moves its members; clearing takes the place away.
    await client.put(_url(world, f"/teams/{world.nova.id}"), json={"position": 2}, headers=world.h)
    await session.refresh(world.regs[0])
    assert world.regs[0].position == 2
    cleared = await client.put(_url(world, f"/teams/{world.nova.id}"), json={"position": None}, headers=world.h)
    assert cleared.status_code == 200
    await session.refresh(world.regs[0])
    assert world.regs[0].position is None and world.regs[0].is_won is False
    assert {w["team_name"] for w in cleared.json()["winners"]} == {"Orbit"}


async def test_only_first_second_and_third(client, world):
    r = await client.put(_url(world, f"/teams/{world.nova.id}"), json={"position": 4}, headers=world.h)
    assert r.status_code == 422


async def test_a_guest_is_placed_one_by_one_without_a_competition(client, session, world, make_event, make_user, make_registration):
    plain = await make_event(club=world.club, created_by=world.owner, name="Quiz Night")
    guest = await make_user(email=f"quizzer{uuid.uuid4().hex[:6]}@example.com")
    reg = await make_registration(event=plain, user=guest)
    await session.commit()
    put = await client.put(f"/api/v1/events/registration/{plain.id}/winners/{reg.id}", json={"position": 3}, headers=world.h)
    assert put.status_code == 200, put.text
    body = (await client.get(f"/api/v1/events/{plain.id}/results", headers=world.h)).json()
    assert body["has_competition"] is False and body["tracks"] == []
    assert [(w["full_name"], w["position"], w["team_name"]) for w in body["winners"]] == [(reg.full_name, 3, None)]


async def test_who_may_set_results(client, session, world, make_admin, mint_token):
    editor = await member_header(client, session, world.club, "editor")
    door = await member_header(client, session, world.club, "door")
    assert (await client.get(_url(world), headers=door)).status_code == 200
    assert (await client.put(_url(world, f"/teams/{world.nova.id}"), json={"position": 1}, headers=door)).status_code == 403
    assert (await client.put(f"/api/v1/events/registration/{world.event.id}/winners/{world.regs[0].id}",
                             json={"position": 1}, headers=door)).status_code == 403
    assert (await client.put(_url(world, f"/teams/{world.nova.id}"), json={"position": 1}, headers=editor)).status_code == 200
    assert (await client.get(_url(world), headers=world.rival_h)).status_code == 403
    assert (await client.put(_url(world, f"/teams/{world.nova.id}"), json={"position": 2}, headers=world.rival_h)).status_code == 403
    admin = await make_admin(email=f"adm{uuid.uuid4().hex[:6]}@example.com")
    await session.commit()
    assert (await client.put(_url(world, f"/teams/{world.orbit.id}"), json={"position": 3},
                             headers=auth_header(await mint_token(admin)))).status_code == 200


async def test_a_team_of_another_event_is_not_found(client, session, world, make_event):
    other = await make_event(club=world.club, created_by=world.owner, name="Other")
    await session.commit()
    r = await client.put(f"/api/v1/events/{other.id}/results/teams/{world.nova.id}", json={"position": 1}, headers=world.h)
    assert r.status_code == 404
    rows = (await session.scalars(select(EventRegistrationsLink.position).where(EventRegistrationsLink.event_id == world.event.id))).all()
    assert set(rows) == {None}
