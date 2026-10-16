"""An event's organisers: picked from the club's team, in the order the public
page lists them, each with their part in the event. Only the club that owns
the event (or an admin) reads or sets them; door staff may only look."""
import re

import pytest

from app.api.club_access import policy
from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubTeamMembers
from app.api.security import service as security
from app.core.email import email as email_mod
from tests.conftest import auth_header


def _url(event) -> str:
    return f"/api/v1/events/{event.id}/organisers"


@pytest.fixture
async def world(session, make_club, make_event, mint_token):
    owner, club = await make_club(name="Circuit Society", email="org-a@example.com")
    other_owner, other = await make_club(name="Rhythm Collective", email="org-b@example.com")
    lead = ClubTeamMembers(club_id=club.id, name="Asha Rao", role="President", instagram="asha", sort_order=0)
    tech = ClubTeamMembers(club_id=club.id, name="Vikram Shah", role="Tech lead", sort_order=1)
    gone = ClubTeamMembers(club_id=club.id, name="Left Already", role="Ex", sort_order=2, is_deleted=True)
    theirs = ClubTeamMembers(club_id=other.id, name="Not Ours", role="Lead")
    session.add_all([lead, tech, gone, theirs])
    event = await make_event(club=club, created_by=owner, name="Hack Night", lifecycle=EventStatus.published)
    await session.commit()
    return dict(
        owner=owner, club=club, other=other, event=event, lead=lead, tech=tech, gone=gone, theirs=theirs,
        h=auth_header(await mint_token(owner)), other_h=auth_header(await mint_token(other_owner)),
    )


async def test_organisers_are_set_in_order_and_read_back(client, session, world):
    h, event, lead, tech = world["h"], world["event"], world["lead"], world["tech"]
    lead.photo = "clubs/team/asha.png"
    await session.commit()
    await session.refresh(lead)
    assert (await client.get(_url(event), headers=h)).json() == []

    r = await client.put(_url(event), json={"members": [{"member_id": tech.id, "role": "  Event lead "}, {"member_id": lead.id}]}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [o["member_id"] for o in body] == [tech.id, lead.id]
    assert body[0] == {"member_id": tech.id, "name": "Vikram Shah", "club_role": "Tech lead", "role": "Event lead", "photo": None}
    assert body[1]["role"] is None and body[1]["club_role"] == "President" and "asha.thumbnail.png" in body[1]["photo"]
    assert (await client.get(_url(event), headers=h)).json() == body

    # The list replaces the last one, order and all.
    again = await client.put(_url(event), json={"members": [{"member_id": lead.id, "role": ""}]}, headers=h)
    assert [(o["member_id"], o["role"]) for o in again.json()] == [(lead.id, None)]
    assert (await client.put(_url(event), json={"members": []}, headers=h)).json() == []
    assert (await client.get(_url(event), headers=h)).json() == []


async def test_only_the_clubs_own_current_team_can_organise(client, world):
    h, event, lead = world["h"], world["event"], world["lead"]
    assert (await client.put(_url(event), json={"members": [{"member_id": lead.id}]}, headers=h)).status_code == 200

    for bad in (world["theirs"].id, world["gone"].id, 99999999):
        r = await client.put(_url(event), json={"members": [{"member_id": lead.id}, {"member_id": bad}]}, headers=h)
        assert r.status_code == 422, r.text
        assert "team" in r.json()["message"] and str(bad) in r.json()["message"]
    twice = await client.put(_url(event), json={"members": [{"member_id": lead.id}, {"member_id": lead.id}]}, headers=h)
    assert twice.status_code == 422 and "once" in twice.json()["message"]
    too_many = await client.put(_url(event), json={"members": [{"member_id": lead.id}] * 25}, headers=h)
    assert too_many.status_code == 422
    long_role = await client.put(_url(event), json={"members": [{"member_id": lead.id, "role": "x" * 81}]}, headers=h)
    assert long_role.status_code == 422
    # A refused change leaves the set as it was.
    assert [o["member_id"] for o in (await client.get(_url(event), headers=h)).json()] == [lead.id]


async def test_only_the_owning_club_or_an_admin_reaches_them(client, world, make_admin, mint_token):
    event, lead = world["event"], world["lead"]
    body = {"members": [{"member_id": lead.id}]}
    assert (await client.get(_url(event), headers=world["other_h"])).status_code == 403
    assert (await client.put(_url(event), json=body, headers=world["other_h"])).status_code == 403
    assert (await client.get(_url(event))).status_code == 401
    assert (await client.put("/api/v1/events/99999999/organisers", json=body, headers=world["h"])).status_code == 404
    admin = await make_admin(email="org-admin@example.com")
    ah = auth_header(await mint_token(admin))
    r = await client.put(_url(event), json=body, headers=ah)
    assert r.status_code == 200, r.text
    assert [o["member_id"] for o in (await client.get(_url(event), headers=ah)).json()] == [lead.id]


async def test_someone_taken_off_the_team_drops_out(client, world):
    h, event, lead, tech = world["h"], world["event"], world["lead"], world["tech"]
    await client.put(_url(event), json={"members": [{"member_id": tech.id}, {"member_id": lead.id}]}, headers=h)
    assert (await client.delete(f"/api/v1/clubs/team/{tech.id}", headers=h)).status_code in (200, 204)
    assert [o["member_id"] for o in (await client.get(_url(event), headers=h)).json()] == [lead.id]


async def _member_session(client, world, monkeypatch, email, role) -> dict:
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)
    r = await client.post("/api/v1/clubs/access/invite", json={"email": email, "role": role}, headers=world["h"])
    assert r.status_code == 200, r.text
    token = re.search(r"token=([A-Za-z0-9_\-]+)", sent[-1]["text"]).group(1)
    assert (await client.post(f"/api/v1/clubs/access/invite/{token}/accept", json={"password": "Member-pass-1"})).status_code == 200
    signed = await client.post("/api/v1/auth/token", data={"username": email, "password": "Member-pass-1", "scope": "club"})
    assert signed.status_code == 200, signed.text
    return auth_header(signed.json()["access_token"])


async def test_door_staff_see_the_organisers_and_editors_change_them(client, world, monkeypatch):
    event, lead = world["event"], world["lead"]
    body = {"members": [{"member_id": lead.id, "role": "Host"}]}
    door = await _member_session(client, world, monkeypatch, "door-org@example.com", "door")
    assert (await client.get(_url(event), headers=door)).status_code == 200
    refused = await client.put(_url(event), json=body, headers=door)
    assert refused.status_code == 403 and refused.json()["error_code"] == "ROLE_FORBIDDEN"
    assert (await client.get(_url(event), headers=world["h"])).json() == []

    editor = await _member_session(client, world, monkeypatch, "editor-org@example.com", "editor")
    r = await client.put(_url(event), json=body, headers=editor)
    assert r.status_code == 200, r.text
    assert r.json()[0]["role"] == "Host"


@pytest.mark.parametrize("role,method,allowed", [
    ("door", "GET", True), ("door", "PUT", False),
    ("editor", "GET", True), ("editor", "PUT", True),
    ("admin", "PUT", True),
])
def test_the_role_rules_match_the_agenda(role, method, allowed):
    assert policy.allows(role, method, "/api/v1/events/7/organisers") is allowed
    assert policy.allows(role, method if method == "GET" else "POST", "/api/v1/events/7/agenda") is allowed


async def test_a_duplicated_event_keeps_its_organisers(client, world):
    h, event, lead, tech = world["h"], world["event"], world["lead"], world["tech"]
    await client.put(_url(event), json={"members": [{"member_id": tech.id, "role": "Event lead"}, {"member_id": lead.id}]}, headers=h)
    made = await client.post(f"/api/v1/events/{event.id}/duplicate", json={"name": "Hack Night II", "event_datetime": "2027-11-20T03:30:00Z"}, headers=h)
    assert made.status_code == 201, made.text
    assert made.json()["copied"]["organisers"] == 2
    copy = (await client.get(f"/api/v1/events/{made.json()['id']}/organisers", headers=h)).json()
    assert [(o["member_id"], o["role"]) for o in copy] == [(tech.id, "Event lead"), (lead.id, None)]
    # The original is untouched.
    assert len((await client.get(_url(event), headers=h)).json()) == 2
