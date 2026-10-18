"""A competition on an event: the organiser sets it up, teams form from tickets,
submit until the close, judges score the rubric, the rounds advance, the board
goes public, and teams book a mentor's slot."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth_header

#: The competition's clock, frozen here for every test (the `world` fixture).
#: Read at import, it used to drift from the wall clock while the suite ran:
#: a mentor's first slot, on the hour after NOW, was already past by the time
#: the office-hours test booked it whenever NOW fell late in an hour.
NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    from app.api.competitions import service as competitions
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(competitions, "_now", lambda: NOW)
    owner, club = await make_club(name="Circuit Society", email="comp@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night 04", event_datetime=NOW + timedelta(days=2), duration=8.0)
    users = [await make_user(email=f"hacker{i}@example.com") for i in range(5)]
    regs = [await make_registration(event=event, user=u, full_name=n) for u, n in zip(users, ("Aarav Kulkarni", "Diya Kapoor", "Kabir Mehta", "Sneha Joshi", "Rohan Patil"))]
    stranger = await make_user(email="stranger@example.com")
    await session.commit()
    other_owner, _ = await make_club(name="Other", email="other-comp@example.com")
    return dict(event=event, regs=regs, h=auth_header(await mint_token(owner)), other=auth_header(await mint_token(other_owner)), u=[auth_header(await mint_token(u)) for u in users], stranger=auth_header(await mint_token(stranger)))


def _u(world, path=""):
    return f"/api/v1/events/{world['event'].id}/competition{path}"


async def test_the_organiser_sets_it_up_and_teams_form_from_tickets(client, world):
    h, u, regs = world["h"], world["u"], world["regs"]
    # Off until turned on: a guest sees nothing, another club sees nothing of ours.
    assert (await client.get(_u(world, "/me"), headers=u[0])).status_code == 404
    assert (await client.get(_u(world), headers=world["other"])).status_code == 403
    s = (await client.get(_u(world), headers=h)).json()
    assert s["settings"]["enabled"] is False and [r["key"] for r in s["settings"]["requirements"]] == ["repo", "video", "deck", "photo"] and sum(r["weight"] for r in s["settings"]["rubric"]) == 100
    s = (await client.patch(_u(world), json={"enabled": True, "team_max": 3}, headers=h)).json()
    assert s["settings"]["enabled"] and s["settings"]["team_max"] == 3
    assert (await client.patch(_u(world), json={"team_min": 5}, headers=h)).status_code == 422

    wake = (await client.post(_u(world, "/tracks"), json={"name": "Wake word on a ₹300 chip", "description": "Under 100 KB of model.", "sponsor": "Kinetic Labs", "prize": "₹3,000 + dev kits"}, headers=h)).json()["tracks"][0]
    s = (await client.post(_u(world, "/tracks"), json={"name": "Sensors for good"}, headers=h)).json()
    sensors = s["tracks"][1]
    assert [t["name"] for t in s["tracks"]] == ["Wake word on a ₹300 chip", "Sensors for good"] and wake["sponsor"] == "Kinetic Labs"

    # Aarav makes a team from his ticket; Diya joins it; Kabir looks for one; a stranger cannot.
    me = (await client.post(_u(world, "/me/team"), json={"name": "Byte Benders", "track_id": wake["id"]}, headers=u[0])).json()
    assert me["team"]["name"] == "Byte Benders" and me["team"]["track"] == "Wake word on a ₹300 chip" and me["team"]["open_spots"] == 2 and [m["name"] for m in me["team"]["members"]] == ["Aarav Kulkarni"]
    assert (await client.post(_u(world, "/me/team"), json={"name": "Byte Benders"}, headers=u[1])).status_code == 409  # the name is taken
    assert (await client.post(_u(world, "/me/team"), json={"name": "Again"}, headers=u[0])).status_code == 409  # already on one
    assert (await client.get(_u(world, "/me"), headers=world["stranger"])).status_code == 403
    team_id = me["team"]["id"]
    me1 = (await client.get(_u(world, "/me"), headers=u[1])).json()
    assert me1["team"] is None and [t["name"] for t in me1["open_teams"]] == ["Byte Benders"] and me1["open_teams"][0]["open_spots"] == 2
    me1 = (await client.post(_u(world, f"/me/team/join/{team_id}"), headers=u[1])).json()
    assert [m["name"] for m in me1["team"]["members"]] == ["Aarav Kulkarni", "Diya Kapoor"]
    me2 = (await client.put(_u(world, "/me/seeking"), json={"on": True, "skills": ["ML", "Python", ""], "note": "Evenings only"}, headers=u[2])).json()
    assert me2["seeking"]["skills"] == ["ML", "Python"] and me2["team"] is None
    assert [x["name"] for x in (await client.get(_u(world, "/me"), headers=u[0])).json()["seekers"]] == ["Kabir Mehta"]

    # The organiser puts Kabir on the team: he stops seeking; the team is then full and Sneha cannot join.
    s = (await client.post(_u(world, f"/teams/{team_id}/members"), json={"registration_id": str(regs[2].id)}, headers=h)).json()
    assert [m["name"] for m in s["teams"][0]["members"]] == ["Aarav Kulkarni", "Diya Kapoor", "Kabir Mehta"] and s["figures"]["seekers"] == 0 and s["teams"][0]["members"][0]["email"] == "hacker0@example.com"
    assert (await client.post(_u(world, f"/me/team/join/{team_id}"), headers=u[3])).status_code == 409
    assert (await client.post(_u(world, f"/teams/{team_id}/members"), json={"registration_id": str(regs[1].id)}, headers=h)).status_code == 409  # already on it
    # Sneha makes her own; the organiser makes one for Rohan; the tracks count their teams.
    assert (await client.post(_u(world, "/me/team"), json={"name": "Tiny Tensors", "track_id": sensors["id"]}, headers=u[3])).status_code == 201
    s = (await client.post(_u(world, "/teams"), json={"name": "Solder Squad", "registration_ids": [str(regs[4].id)]}, headers=h)).json()
    assert s["figures"] == {"teams": 3, "people": 5, "seekers": 0, "submitted": 0, "judges": 0, "scored_pairs": 0}
    assert {t["name"]: t["teams"] for t in s["tracks"]} == {"Wake word on a ₹300 chip": 1, "Sensors for good": 1}
    # Locked teams: nobody moves from a ticket; the organiser still can.
    assert (await client.patch(_u(world), json={"teams_locked": True}, headers=h)).status_code == 200
    assert (await client.post(_u(world, "/me/team/leave"), headers=u[1])).status_code == 409
    solder = next(t for t in s["teams"] if t["name"] == "Solder Squad")
    s = (await client.delete(_u(world, f"/teams/{solder['id']}/members/{regs[4].id}"), headers=h)).json()
    assert next(t for t in s["teams"] if t["name"] == "Solder Squad")["members"] == []
    assert (await client.patch(_u(world), json={"teams_locked": False}, headers=h)).status_code == 200
    # Leaving an empty team dissolves it.
    assert (await client.post(_u(world, "/me/team/leave"), headers=u[3])).json()["team"] is None
    assert [t["name"] for t in (await client.get(_u(world), headers=h)).json()["teams"]] == ["Byte Benders", "Solder Squad"]


async def test_submissions_judging_rounds_board_and_office_hours(client, world):
    h, u, regs = world["h"], world["u"], world["regs"]
    await client.patch(_u(world), json={"enabled": True, "submissions_close_at": (NOW + timedelta(hours=3)).isoformat()}, headers=h)
    track = (await client.post(_u(world, "/tracks"), json={"name": "Open track"}, headers=h)).json()["tracks"][0]
    a = (await client.post(_u(world, "/teams"), json={"name": "Byte Benders", "track_id": track["id"], "registration_ids": [str(regs[0].id), str(regs[1].id)]}, headers=h)).json()["teams"][0]
    b = (await client.post(_u(world, "/teams"), json={"name": "Null Pointers", "registration_ids": [str(regs[2].id)]}, headers=h)).json()["teams"][1]
    (await client.post(_u(world, "/teams"), json={"name": "Solder Squad", "registration_ids": [str(regs[3].id)]}, headers=h)).json()

    # Submissions: a link at a time; complete once everything required is in; nothing unknown; nothing after the close.
    me = (await client.put(_u(world, "/me/submission"), json={"fields": {"repo": "https://github.com/x/y"}}, headers=u[0])).json()
    assert me["team"]["submission"]["status"] == "in_progress" and me["team"]["submission"]["missing"] == ["video"]
    me = (await client.put(_u(world, "/me/submission"), json={"fields": {"repo": "https://github.com/x/y", "video": "https://youtu.be/abc", "deck": " "}}, headers=u[1])).json()
    assert me["team"]["submission"]["status"] == "complete" and me["team"]["submission"]["completed_at"] and me["team"]["submission"]["fields"] == {"repo": "https://github.com/x/y", "video": "https://youtu.be/abc"}
    assert (await client.put(_u(world, "/me/submission"), json={"fields": {"essay": "x"}}, headers=u[2])).status_code == 422
    assert (await client.put(_u(world, "/me/submission"), json={"fields": {"repo": "https://github.com/n/p"}}, headers=u[4])).status_code == 409  # Rohan has no team
    s = (await client.post(_u(world, "/submissions/close"), headers=h)).json()
    assert s["settings"]["submissions_open"] is False and s["figures"]["submitted"] == 1
    assert (await client.put(_u(world, "/me/submission"), json={"fields": {"repo": "https://github.com/n/p"}}, headers=u[2])).status_code == 409
    assert (await client.post(_u(world, "/submissions/reopen"), headers=h)).json()["settings"]["submissions_open"] is True

    # Judging: two judges, scores on the rubric, weighted out of a hundred; a team's score is the mean of its judges.
    s = (await client.post(_u(world, "/judges"), json={"name": "Dr. Neha Kulkarni"}, headers=h)).json()
    s = (await client.post(_u(world, "/judges"), json={"name": "Arjun Menon", "email": "arjun@example.com"}, headers=h)).json()
    neha, arjun = s["judges"]
    full = {"technical": 8, "innovation": 7, "demo": 9, "impact": 6}
    s = (await client.put(_u(world, f"/judges/{neha['id']}/scores/{a['id']}"), json={"scores": full}, headers=h)).json()
    assert next(x for x in s["scores"] if x["team_id"] == a["id"])["total"] == 77.0  # 35·.8 + 25·.7 + 25·.9 + 15·.6
    s = (await client.put(_u(world, f"/judges/{arjun['id']}/scores/{a['id']}"), json={"scores": {"technical": 6, "innovation": 7, "demo": 5, "impact": 6}}, headers=h)).json()
    s = (await client.put(_u(world, f"/judges/{neha['id']}/scores/{b['id']}"), json={"scores": {"technical": 9, "innovation": 8, "demo": 8, "impact": 7}}, headers=h)).json()
    assert (await client.put(_u(world, f"/judges/{neha['id']}/scores/{b['id']}"), json={"scores": {"technical": 11}}, headers=h)).status_code == 422
    assert (await client.put(_u(world, f"/judges/{neha['id']}/scores/{b['id']}"), json={"scores": {"vibes": 5}}, headers=h)).status_code == 422
    assert [j["scored"] for j in s["judges"]] == [2, 1]
    board = s["board"]
    assert [(r["rank"], r["name"], r["score"], r["judges_scored"], r["judges_total"]) for r in board] == [(1, "Null Pointers", 82.0, 1, 2), (2, "Byte Benders", 68.5, 2, 2), (None, "Solder Squad", None, 0, 2)]
    # Private until the organiser says otherwise; then anyone can read it, and a team sees it on their side.
    assert (await client.get(_u(world, "/leaderboard"))).json() == {"event_id": world["event"].id, "enabled": True, "public": False, "rows": []}
    assert (await client.get(_u(world, "/me"), headers=u[0])).json()["board"] is None
    await client.patch(_u(world), json={"leaderboard_public": True}, headers=h)
    pub = (await client.get(_u(world, "/leaderboard"))).json()
    assert pub["public"] and [r["name"] for r in pub["rows"]][:2] == ["Null Pointers", "Byte Benders"]
    assert (await client.get(_u(world, "/me"), headers=u[0])).json()["board"][0]["name"] == "Null Pointers"

    # Rounds: the top two of the entered go to the shortlist; the unscored stay; one can be overridden by hand.
    s = (await client.post(_u(world, "/rounds/advance"), json={"to": "shortlisted", "size": 2}, headers=h)).json()
    assert [r["name"] for r in s["rounds"]["shortlisted"]] == ["Null Pointers", "Byte Benders"] and [r["name"] for r in s["rounds"]["entered"]] == ["Solder Squad"]
    s = (await client.post(_u(world, "/rounds/advance"), json={"to": "finalist", "size": 1}, headers=h)).json()
    assert [r["name"] for r in s["rounds"]["finalists"]] == ["Null Pointers"] and [r["name"] for r in s["rounds"]["shortlisted"]] == ["Byte Benders"]
    s = (await client.patch(_u(world, f"/teams/{a['id']}"), json={"stage": "finalist"}, headers=h)).json()
    assert [r["name"] for r in s["rounds"]["finalists"]] == ["Null Pointers", "Byte Benders"]

    # Office hours: a mentor's window becomes slots; a team books a free one, not a taken one, one per mentor; frees only its own.
    s = (await client.post(_u(world, "/mentors"), json={"name": "Arjun Menon", "topic": "Firmware, debugging"}, headers=h)).json()
    mentor = s["mentors"][0]
    start = (NOW + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    s = (await client.post(_u(world, f"/mentors/{mentor['id']}/slots"), json={"starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat()}, headers=h)).json()
    slots = s["mentors"][0]["slots"]
    assert len(slots) == 4 and all(x["team"] is None for x in slots)
    assert (await client.post(_u(world, f"/mentors/{mentor['id']}/slots"), json={"starts_at": start.isoformat(), "ends_at": start.isoformat()}, headers=h)).status_code == 422
    me = (await client.post(_u(world, f"/me/slots/{slots[0]['id']}"), headers=u[0])).json()
    assert me["mentors"][0]["slots"][0]["team"] == "Byte Benders"
    assert (await client.post(_u(world, f"/me/slots/{slots[0]['id']}"), headers=u[2])).status_code == 409  # taken
    assert (await client.post(_u(world, f"/me/slots/{slots[1]['id']}"), headers=u[1])).status_code == 409  # Byte Benders already has one
    assert (await client.delete(_u(world, f"/me/slots/{slots[0]['id']}"), headers=u[2])).status_code == 403  # not theirs
    assert (await client.post(_u(world, f"/me/slots/{slots[1]['id']}"), headers=u[2])).json()["mentors"][0]["slots"][1]["team"] == "Null Pointers"
    s = (await client.patch(_u(world, f"/slots/{slots[0]['id']}"), json={"team_id": None}, headers=h)).json()
    assert s["mentors"][0]["slots"][0]["team"] is None
    assert (await client.delete(_u(world, f"/slots/{slots[3]['id']}"), headers=h)).json()["mentors"][0]["slots"].__len__() == 3
    # Another club touches none of it.
    assert (await client.patch(_u(world, f"/teams/{a['id']}"), json={"name": "Ours"}, headers=world["other"])).status_code == 403
