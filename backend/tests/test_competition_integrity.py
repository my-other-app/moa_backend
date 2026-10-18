"""Competitions held by the database: a ticket on one team, a team no bigger
than its size, one name per team, one booking per slot and per mentor;
rounds that advance to a size and report a tie at the cut; the smallest
team enforced at submission; a public board that never writes; and judges
put on their own scales when the organiser asks.

The concurrency tests run on connections of their own and commit, as
concurrent requests do. They clean up after themselves."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.api.competitions import service
from app.api.competitions.models import CompetitionMentors, Competitions, CompetitionSlots, CompetitionTeamMembers, CompetitionTeams
from app.response import CustomHTTPException
from tests.conftest import auth_header
from tests.test_attendance_integrity import Gate, _committed_world, _drop, _event, make  # noqa: F401

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    owner, club = await make_club(name="Circuit Society", email="comp-int@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night 05", event_datetime=NOW + timedelta(days=2), duration=8.0)
    users = [await make_user(email=f"hacker-int{i}@example.com") for i in range(4)]
    regs = [await make_registration(event=event, user=u, full_name=f"Hacker {i}") for i, u in enumerate(users)]
    await session.commit()
    return dict(event=event, regs=regs, h=auth_header(await mint_token(owner)), u=[auth_header(await mint_token(u)) for u in users])


def _u(world, path=""):
    return f"/api/v1/events/{world['event'].id}/competition{path}"


async def _scored_teams(client, world, scores: dict):
    """Teams with one member each, scored by one judge: {name: technical score}."""
    h = world["h"]
    await client.patch(_u(world), json={"enabled": True, "rubric": [{"key": "technical", "label": "Technical", "weight": 100}]}, headers=h)
    judge = (await client.post(_u(world, "/judges"), json={"name": "Judge"}, headers=h)).json()["judges"][0]
    teams = {}
    for i, (name, score) in enumerate(scores.items()):
        s = (await client.post(_u(world, "/teams"), json={"name": name, "registration_ids": [str(world["regs"][i].id)]}, headers=h)).json()
        team = next(t for t in s["teams"] if t["name"] == name)
        teams[name] = team["id"]
        await client.put(_u(world, f"/judges/{judge['id']}/scores/{team['id']}"), json={"scores": {"technical": score}}, headers=h)
    return teams


async def test_advancing_twice_moves_nobody_the_second_time(client, world):
    await _scored_teams(client, world, {"Alpha": 9, "Beta": 7, "Gamma": 5})
    first = (await client.post(_u(world, "/rounds/advance"), json={"to": "shortlisted", "size": 2}, headers=world["h"])).json()
    again = (await client.post(_u(world, "/rounds/advance"), json={"to": "shortlisted", "size": 2}, headers=world["h"])).json()
    assert [r["name"] for r in first["rounds"]["shortlisted"]] == ["Alpha", "Beta"]
    assert [r["name"] for r in again["rounds"]["shortlisted"]] == ["Alpha", "Beta"] and [r["name"] for r in again["rounds"]["entered"]] == ["Gamma"]


async def test_a_tie_at_the_cut_is_reported_not_broken_by_name(client, world):
    await _scored_teams(client, world, {"Zeta": 9, "Alpha": 7, "Beta": 7})
    tie = await client.post(_u(world, "/rounds/advance"), json={"to": "shortlisted", "size": 2}, headers=world["h"])
    assert tie.status_code == 409 and tie.json()["error_code"] == "TIE_AT_CUT"
    assert sorted(t["name"] for t in tie.json()["errors"]["tied"]) == ["Alpha", "Beta"] and tie.json()["errors"]["places_left"] == 1
    state = (await client.get(_u(world), headers=world["h"])).json()
    assert state["rounds"]["shortlisted"] == []
    # Tied teams share a rank on the board.
    assert [(r["name"], r["rank"]) for r in state["board"]] == [("Zeta", 1), ("Alpha", 2), ("Beta", 2)]
    # A size the tie does not split goes through.
    ok = (await client.post(_u(world, "/rounds/advance"), json={"to": "shortlisted", "size": 3}, headers=world["h"])).json()
    assert sorted(r["name"] for r in ok["rounds"]["shortlisted"]) == ["Alpha", "Beta", "Zeta"]


async def test_a_team_smaller_than_the_minimum_cannot_submit(client, world):
    h, u = world["h"], world["u"]
    await client.patch(_u(world), json={"enabled": True, "team_min": 2}, headers=h)
    me = await client.post(_u(world, "/me/team"), json={"name": "Solo"}, headers=u[0])
    assert me.status_code == 201
    small = await client.put(_u(world, "/me/submission"), json={"fields": {"repo": "https://github.com/x/y"}}, headers=u[0])
    assert small.status_code == 409 and small.json()["error_code"] == "TEAM_TOO_SMALL"
    await client.post(_u(world, f"/me/team/join/{me.json()['team']['id']}"), headers=u[1])
    assert (await client.put(_u(world, "/me/submission"), json={"fields": {"repo": "https://github.com/x/y"}}, headers=u[0])).status_code == 200


async def test_the_public_leaderboard_never_makes_a_competition(client, session, world):
    r = await client.get(_u(world, "/leaderboard"))
    assert r.json() == {"event_id": world["event"].id, "enabled": False, "public": False, "rows": []}
    assert (await client.get(_u(world, "/me"), headers=world["u"][0])).status_code == 404
    assert await session.scalar(select(func.count()).select_from(Competitions).where(Competitions.event_id == world["event"].id)) == 0


async def test_judges_can_be_put_on_their_own_scales(client, world):
    h = world["h"]
    teams = await _scored_teams(client, world, {"Alpha": 9, "Beta": 6, "Gamma": 3})
    # A second judge marks hard, and only Beta and Gamma, in the same order.
    s = (await client.post(_u(world, "/judges"), json={"name": "Hard marker"}, headers=h)).json()
    hard = s["judges"][1]
    await client.put(_u(world, f"/judges/{hard['id']}/scores/{teams['Beta']}"), json={"scores": {"technical": 2}}, headers=h)
    await client.put(_u(world, f"/judges/{hard['id']}/scores/{teams['Gamma']}"), json={"scores": {"technical": 1}}, headers=h)
    mean = (await client.get(_u(world), headers=h)).json()
    assert mean["settings"]["score_method"] == "mean"
    assert {r["name"]: r["score"] for r in mean["board"]} == {"Alpha": 90.0, "Beta": 40.0, "Gamma": 20.0}
    z = (await client.patch(_u(world), json={"score_method": "zscore"}, headers=h)).json()
    # Each judge on their own scale (their average team at 50, a spread at 10):
    # the hard marker's 20 for Beta is their best, and counts as such.
    assert {r["name"]: r["score"] for r in z["board"]} == {"Alpha": 62.2, "Beta": 55.0, "Gamma": 38.9}


# ─── races ────────────────────────────────────────────────────────────────


async def _competition(make, w, *, team_max=4, teams=("Red", "Blue"), members=None, mentor=False):
    async with make() as s:
        c = Competitions(event_id=w["event"], enabled=True, team_max=team_max, requirements=[], rubric=[])
        s.add(c)
        await s.flush()
        out = {"c": c.id, "teams": []}
        for name in teams:
            t = CompetitionTeams(competition_id=c.id, name=name)
            s.add(t)
            await s.flush()
            out["teams"].append(t.id)
        for team_index, reg_index in members or []:
            s.add(CompetitionTeamMembers(competition_id=c.id, team_id=out["teams"][team_index], registration_id=w["regs"][reg_index]))
        if mentor:
            m = CompetitionMentors(competition_id=c.id, name="Mentor")
            s.add(m)
            await s.flush()
            slot = CompetitionSlots(mentor_id=m.id, starts_at=datetime.now(timezone.utc) + timedelta(hours=2))
            s.add(slot)
            await s.flush()
            out["slot"] = slot.id
        await s.commit()
        return out


async def _join(make, w, comp, team_index, reg_index):
    async with make() as s:
        event = await _event(s, w["event"])
        c = await s.get(Competitions, comp["c"])
        team = await s.get(CompetitionTeams, comp["teams"][team_index])
        try:
            await service.add_member(s, c, event, team, w["regs"][reg_index])
            return "joined"
        except CustomHTTPException as exc:
            return exc.status_code


def _gate_after(monkeypatch, name, n):
    gate = Gate(n)
    real = getattr(service, name)

    async def wrapped(*args, **kwargs):
        out = await real(*args, **kwargs)
        await gate.wait()
        return out

    monkeypatch.setattr(service, name, wrapped)


async def test_one_ticket_joining_two_teams_at_once_lands_on_one(make, monkeypatch):
    w = await _committed_world(make, "twoteams", guests=1)
    try:
        comp = await _competition(make, w)
        _gate_after(monkeypatch, "team_of_registration", 2)
        assert sorted(map(str, await asyncio.gather(_join(make, w, comp, 0, 0), _join(make, w, comp, 1, 0)))) == ["409", "joined"]
        async with make() as s:
            assert await s.scalar(select(func.count()).select_from(CompetitionTeamMembers).where(CompetitionTeamMembers.registration_id == w["regs"][0])) == 1
    finally:
        await _drop(make, w)


async def test_two_joining_the_last_place_at_once_leave_the_team_at_its_size(make, monkeypatch):
    w = await _committed_world(make, "lastplace", guests=3)
    try:
        comp = await _competition(make, w, team_max=2, teams=("Red",), members=[(0, 0)])
        _gate_after(monkeypatch, "team_of_registration", 2)
        assert sorted(map(str, await asyncio.gather(_join(make, w, comp, 0, 1), _join(make, w, comp, 0, 2)))) == ["409", "joined"]
        async with make() as s:
            assert await s.scalar(select(func.count()).select_from(CompetitionTeamMembers).where(CompetitionTeamMembers.team_id == comp["teams"][0])) == 2
    finally:
        await _drop(make, w)


async def test_two_teams_named_alike_at_once_make_one(make, monkeypatch):
    w = await _committed_world(make, "samename", guests=0)
    try:
        comp = await _competition(make, w, teams=())
        _gate_after(monkeypatch, "_track_or_none", 2)

        async def create(name):
            async with make() as s:
                event = await _event(s, w["event"])
                c = await s.get(Competitions, comp["c"])
                try:
                    await service.create_team(s, c, event, name, None, [])
                    return "made"
                except CustomHTTPException as exc:
                    return exc.status_code

        assert sorted(map(str, await asyncio.gather(create("Bytes"), create("bytes")))) == ["409", "made"]
    finally:
        await _drop(make, w)


async def test_two_teams_booking_the_last_slot_at_once_one_gets_it(make, monkeypatch):
    w = await _committed_world(make, "lastslot", guests=0)
    try:
        comp = await _competition(make, w, mentor=True)
        _gate_after(monkeypatch, "team_for", 2)

        async def book(team_index):
            async with make() as s:
                c = await s.get(Competitions, comp["c"])
                team = await s.get(CompetitionTeams, comp["teams"][team_index])
                slot = await s.get(CompetitionSlots, comp["slot"])
                try:
                    await service.set_slot(s, c, slot, team.id, by_team=team)
                    return team.id
                except CustomHTTPException as exc:
                    return exc.status_code

        got = await asyncio.gather(book(0), book(1))
        assert got.count(409) == 1, got
        winner = next(g for g in got if g != 409)
        async with make() as s:
            assert await s.scalar(select(CompetitionSlots.team_id).where(CompetitionSlots.id == comp["slot"])) == winner
    finally:
        await _drop(make, w)


async def test_a_team_holds_one_slot_per_mentor_whoever_books_it(client, session, world):
    h = world["h"]
    await client.patch(_u(world), json={"enabled": True}, headers=h)
    s = (await client.post(_u(world, "/teams"), json={"name": "Red", "registration_ids": [str(world["regs"][0].id)]}, headers=h)).json()
    team = s["teams"][0]["id"]
    mentor = (await client.post(_u(world, "/mentors"), json={"name": "Mentor"}, headers=h)).json()["mentors"][0]
    start = NOW + timedelta(days=1)
    slots = (await client.post(_u(world, f"/mentors/{mentor['id']}/slots"), json={"starts_at": start.isoformat(), "ends_at": (start + timedelta(minutes=30)).isoformat()}, headers=h)).json()["mentors"][0]["slots"]
    assert (await client.patch(_u(world, f"/slots/{slots[0]['id']}"), json={"team_id": team}, headers=h)).status_code == 200
    second = await client.patch(_u(world, f"/slots/{slots[1]['id']}"), json={"team_id": team}, headers=h)
    assert second.status_code == 409
    assert await session.scalar(select(func.count()).select_from(CompetitionSlots).where(CompetitionSlots.team_id == team)) == 1
