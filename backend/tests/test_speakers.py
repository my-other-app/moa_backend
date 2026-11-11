"""The speakers directory (app/api/speakers, docs/SPEAKERS.md): an event's
speakers linked to profiles, the club's picker and list with its private
contacts, the admin page with merges, claiming, the public page, following
and the push, and the backfill of what is there already."""
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.api.agenda.models import AgendaItems
from app.api.closeout.models import EventStatus
from app.api.events.models import EventCategories, Events, EventSpeakers
from app.api.notifications.models import Notifications
from app.api.speakers import backfill, names
from app.api.speakers import service as sp
from app.api.speakers.models import (
    AgendaItemSpeakers,
    SpeakerClaims,
    SpeakerContacts,
    SpeakerFollows,
    SpeakerHistory,
    SpeakerMergeCandidates,
    SpeakerProfiles,
)
from app.api.users.models import AdminRoles
from tests.club_ops_helpers import member_header
from tests.conftest import auth_header

API = "/api/v1"
PHOTO = "https://test-bucket.s3.amazonaws.com/events/speakers/v-rao.jpg"


def _days(n: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=n)


@pytest.fixture
async def world(session, make_club, make_user, make_admin, mint_token):
    a_owner, club_a = await make_club(name="Alpha Coders", email="spk-alpha@example.com")
    b_owner, club_b = await make_club(name="Beta Makers", email="spk-beta@example.com")
    category = EventCategories(name="Talks")
    session.add(category)
    await session.flush()
    fan = await make_user(email="spk-fan@example.com")
    speaker_user = await make_user(email="spk-rao@example.com")
    admin = await make_admin(email="spk-admin@example.com")
    viewer = await make_admin(email="spk-viewer@example.com", role=AdminRoles.viewer)
    await session.commit()
    return SimpleNamespace(
        club_a=club_a, club_b=club_b, a=auth_header(await mint_token(a_owner)), b=auth_header(await mint_token(b_owner)),
        category=category, fan=fan, fan_h=auth_header(await mint_token(fan)),
        speaker_user=speaker_user, speaker_h=auth_header(await mint_token(speaker_user)),
        admin=auth_header(await mint_token(admin)), viewer=auth_header(await mint_token(viewer)),
    )


def _form(world, name="Speaker Night", days=10, status="published"):
    return {
        "name": name, "event_datetime": _days(days).isoformat(), "duration": "2", "has_fee": "false",
        "status": status, "category_id": str(world.category.id),
    }


async def _create(client, world, speakers, *, h=None, **kw):
    r = await client.post(f"{API}/events/create", data={**_form(world, **kw), "speakers": json.dumps(speakers)}, headers=h or world.a)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _update(client, world, event_id, speakers, *, h=None, **kw):
    return await client.put(f"{API}/events/update/{event_id}", data={**_form(world, **kw), "speakers": json.dumps(speakers)}, headers=h or world.a)


async def _info(client, event_id, h=None) -> dict:
    r = await client.get(f"{API}/events/info/{event_id}", headers=h or {})
    assert r.status_code == 200, r.text
    return {s["name"]: s for s in r.json()["speakers"]}


async def _event_with(session, world, club, speakers, *, days=10, status=EventStatus.published, name="Old Event"):
    """An event with speaker rows written as before the directory (no profile)."""
    from app.core.utils.keys import generate_slug

    e = Events(
        slug=generate_slug(name), name=name, images=[], event_datetime=_days(days), duration=2.0,
        club_id=club.id, category_id=world.category.id, lifecycle=status,
    )
    session.add(e)
    await session.flush()
    rows = []
    for n, s in enumerate(speakers):
        row = EventSpeakers(event_id=e.id, display_order=n, **s)
        session.add(row)
        rows.append(row)
    await session.commit()
    return e, rows


# ─── An event's speakers, linked ─────────────────────────────────────────


async def test_saving_an_event_links_each_speaker_to_a_profile(client, session, world):
    first = await _create(client, world, [{"name": "Dr. Rao", "designation": "Professor", "bio": "Robots.", "link": "https://rao.example"}])
    rao = (await _info(client, first))["Dr. Rao"]
    assert rao["speaker_profile_id"] and rao["profile_slug"] == "rao" and rao["talk_title"] is None
    profile = await session.get(SpeakerProfiles, rao["speaker_profile_id"])
    assert (profile.name, profile.headline, profile.bio, profile.links, profile.created_by_club_id) == (
        "Dr. Rao", "Professor", "Robots.", {"website": "https://rao.example"}, world.club_a.id,
    )
    # The same name at the same club, later: the same profile, not a new one.
    second = await _create(client, world, [{"name": "rao", "talk_title": "  Robots at home  "}], name="Second Night")
    again = (await _info(client, second))["rao"]
    assert again["speaker_profile_id"] == profile.id and again["talk_title"] == "Robots at home"
    # The same name at another club: its own profile, and a pair for the admins.
    third = await _create(client, world, [{"name": "Dr. Rao"}], h=world.b, name="Beta Night")
    other = (await _info(client, third))["Dr. Rao"]["speaker_profile_id"]
    assert other != profile.id
    pair = await session.scalar(select(SpeakerMergeCandidates).where(SpeakerMergeCandidates.a_id == min(other, profile.id)))
    assert pair is not None and pair.reason == sp.SAME_NAME_OTHER_CLUB and pair.status == "open"


async def test_an_older_client_keeps_the_link_and_the_talk_title(client, session, world):
    event_id = await _create(client, world, [{"name": "Meera", "talk_title": "Flutter"}])
    meera = (await _info(client, event_id))["Meera"]
    # An edit from a client that knows nothing of profiles or talk titles.
    r = await _update(client, world, event_id, [{"id": meera["id"], "name": "Meera N", "designation": "GDE"}])
    assert r.status_code == 200, r.text
    now = (await _info(client, event_id))["Meera N"]
    assert (now["speaker_profile_id"], now["talk_title"], now["designation"]) == (meera["speaker_profile_id"], "Flutter", "GDE")
    # The profile is not the club's copy: it keeps the name it was made with.
    assert (await session.get(SpeakerProfiles, meera["speaker_profile_id"])).name == "Meera"


async def test_picking_a_profile_fills_the_photo_and_is_limited_to_the_clubs_own_or_claimed(client, session, world):
    past, rows = await _event_with(session, world, world.club_a, [{"name": "Kiran", "photo": "events/speakers/v-kiran.jpg"}], days=-30)
    await backfill.run(session)
    await session.refresh(rows[0])
    kiran = rows[0].speaker_profile_id
    event_id = await _create(client, world, [{"name": "Kiran K", "speaker_profile_id": kiran, "talk_title": "State"}])
    got = (await _info(client, event_id))["Kiran K"]
    assert got["speaker_profile_id"] == kiran and "v-kiran" in (got["photo"] or {}).get("original", "")
    # Another club cannot put Alpha's unclaimed speaker on its event.
    r = await client.post(f"{API}/events/create", data={**_form(world), "speakers": json.dumps([{"name": "Kiran", "speaker_profile_id": kiran}])}, headers=world.b)
    assert r.status_code == 422 and r.json()["error_code"] == "SPEAKER_NOT_AVAILABLE", r.text
    assert await session.scalar(select(func.count()).select_from(Events).where(Events.club_id == world.club_b.id)) == 0
    # Once Kiran claims the profile, any club may pick it.
    profile = await session.get(SpeakerProfiles, kiran)
    profile.claimed_by_user_id = world.speaker_user.id
    await session.commit()
    beta = await _create(client, world, [{"name": "Kiran", "speaker_profile_id": kiran}], h=world.b, name="Beta Night")
    assert (await _info(client, beta))["Kiran"]["speaker_profile_id"] == kiran


async def test_a_hidden_profile_keeps_its_name_on_the_event_but_loses_its_page_link(client, session, world):
    event_id = await _create(client, world, [{"name": "Asha"}])
    asha = (await _info(client, event_id))["Asha"]
    r = await client.post(f"{API}/admin/speakers/{asha['speaker_profile_id']}/hide", headers=world.admin)
    assert r.status_code == 200 and r.json()["status"] == "hidden", r.text
    after = (await _info(client, event_id))["Asha"]
    assert after["name"] == "Asha" and after["profile_slug"] is None
    page = (await client.get(f"{API}/discover/events/{event_id}")).json()
    assert page["speakers"][0]["name"] == "Asha" and page["speakers"][0]["profile_slug"] is None
    # Not suggested to its own club any more.
    assert (await client.get(f"{API}/clubs/speakers", headers=world.a)).json() == []


# ─── Agenda sessions ─────────────────────────────────────────────────────


async def test_agenda_sessions_pick_the_events_speakers(client, session, world):
    event_id = await _create(client, world, [{"name": "Meera"}, {"name": "Rao"}])
    other_id = await _create(client, world, [{"name": "Kiran"}], name="Other Night")
    ids = {n: s["id"] for n, s in (await _info(client, event_id)).items()}
    kiran = (await _info(client, other_id))["Kiran"]["id"]
    start = _days(10).isoformat()
    r = await client.post(f"{API}/events/{event_id}/agenda", json={"starts_at": start, "title": "Keynote", "event_speaker_ids": [ids["Rao"], ids["Meera"]]}, headers=world.a)
    assert r.status_code == 201, r.text
    block = r.json()
    assert block["event_speaker_ids"] == [ids["Rao"], ids["Meera"]] and block["speaker"] == "Rao, Meera"
    assert [s["name"] for s in block["speakers"]] == ["Rao", "Meera"] and block["speakers"][0]["profile_slug"] == "rao"
    # Only this event's speakers.
    bad = await client.patch(f"{API}/events/{event_id}/agenda/{block['id']}", json={"event_speaker_ids": [kiran]}, headers=world.a)
    assert bad.status_code == 422 and bad.json()["error_code"] == "NOT_THIS_EVENTS_SPEAKER"
    # An older client's free text still works, and an edit without the ids keeps them.
    kept = await client.patch(f"{API}/events/{event_id}/agenda/{block['id']}", json={"title": "Opening keynote"}, headers=world.a)
    assert kept.json()["event_speaker_ids"] == [ids["Rao"], ids["Meera"]]
    plain = await client.post(f"{API}/events/{event_id}/agenda", json={"starts_at": start, "title": "Lunch", "speaker": "Everyone"}, headers=world.a)
    assert plain.status_code == 201 and plain.json()["speakers"] == [] and plain.json()["speaker"] == "Everyone"
    # The public agenda shows them; a speaker removed from the event leaves the session.
    public = (await client.get(f"{API}/events/{event_id}/agenda")).json()["items"]
    assert [s["name"] for s in next(i for i in public if i["title"] == "Opening keynote")["speakers"]] == ["Rao", "Meera"]
    r = await _update(client, world, event_id, [{"id": ids["Meera"], "name": "Meera"}])
    assert r.status_code == 200, r.text
    public = (await client.get(f"{API}/events/{event_id}/agenda")).json()["items"]
    assert [s["name"] for s in next(i for i in public if i["title"] == "Opening keynote")["speakers"]] == ["Meera"]


# ─── The club's picker, list and contacts ────────────────────────────────


async def test_suggestions_are_the_clubs_own_then_claimed_ones_by_name(client, session, world):
    await _create(client, world, [{"name": "Meera Nair"}, {"name": "Rao"}], days=-20, name="Alpha Past")
    beta = await _create(client, world, [{"name": "Meera Iyer"}, {"name": "Sunil"}], h=world.b, name="Beta Night")
    own = (await client.get(f"{API}/clubs/speakers", headers=world.a)).json()
    assert {s["name"] for s in own} == {"Meera Nair", "Rao"} and all(s["own"] for s in own)
    assert own[0]["talks_count"] == 1 and own[0]["last_talk"]["event_name"] == "Alpha Past"
    # Beta's speakers are not Alpha's to see: not by name either, until claimed.
    found = (await client.get(f"{API}/clubs/speakers", params={"q": "meera"}, headers=world.a)).json()
    assert [s["name"] for s in found] == ["Meera Nair"]
    sunil = (await _info(client, beta))["Sunil"]["speaker_profile_id"]
    assert (await client.get(f"{API}/clubs/speakers/{sunil}", headers=world.a)).status_code == 404
    profile = await session.get(SpeakerProfiles, (await _info(client, beta))["Meera Iyer"]["speaker_profile_id"])
    profile.claimed_by_user_id = world.speaker_user.id
    await session.commit()
    found = (await client.get(f"{API}/clubs/speakers", params={"q": "meera", "limit": 5}, headers=world.a)).json()
    assert [(s["name"], s["own"], s["claimed"]) for s in found] == [("Meera Nair", True, False), ("Meera Iyer", False, True)]
    # Without a name, only the club's own: a claimed profile is found by name.
    assert {s["name"] for s in (await client.get(f"{API}/clubs/speakers", headers=world.a)).json()} == {"Meera Nair", "Rao"}
    # A near spelling still finds them.
    assert [s["name"] for s in (await client.get(f"{API}/clubs/speakers", params={"q": "Dr Meera Nayr"}, headers=world.a)).json()][:1] == ["Meera Nair"]


async def test_contacts_stay_with_the_club_that_entered_them(client, session, world):
    event_id = await _create(client, world, [{"name": "Rao"}])
    rao = (await _info(client, event_id))["Rao"]["speaker_profile_id"]
    url = f"{API}/clubs/speakers/{rao}/contact"
    no = await client.put(url, json={"email": "rao@example.com"}, headers=world.a)
    assert no.status_code == 422 and no.json()["error_code"] == "CONSENT_REQUIRED"
    bad = await client.put(url, json={"email": "not-an-email", "consent": True}, headers=world.a)
    assert bad.status_code == 422
    ok = await client.put(url, json={"email": " Rao@Example.com ", "phone": "+91 98765 43210", "notes": "Needs HDMI", "consent": True}, headers=world.a)
    assert ok.status_code == 200, ok.text
    assert ok.json()["contact"]["email"] == "rao@example.com" and ok.json()["contact"]["consent_at"]
    # Notes alone later: the email stays, and needs no new tick.
    assert (await client.put(url, json={"notes": "Bring HDMI"}, headers=world.a)).json()["contact"]["email"] == "rao@example.com"
    mine = (await client.get(f"{API}/clubs/speakers/{rao}", headers=world.a)).json()
    assert mine["contact"]["notes"] == "Bring HDMI" and mine["talks"][0]["event_id"] == event_id
    listed = (await client.get(f"{API}/clubs/speakers/list", headers=world.a)).json()
    assert listed["total"] == 1 and listed["items"][0]["contact"]["phone"] == "+91 98765 43210"
    # Claimed, the profile is visible to another club: its own contact (none), never Alpha's.
    profile = await session.get(SpeakerProfiles, rao)
    profile.claimed_by_user_id = world.speaker_user.id
    await session.commit()
    theirs = (await client.get(f"{API}/clubs/speakers/{rao}", headers=world.b)).json()
    assert theirs["contact"] is None and theirs["talks"] == []
    assert (await client.get(f"{API}/clubs/speakers/list", headers=world.b)).json()["total"] == 0
    found = (await client.get(f"{API}/clubs/speakers", params={"q": "rao"}, headers=world.b)).json()
    assert found and "contact" not in found[0] and "rao@example.com" not in json.dumps(found)
    # Admins see the email and phone per club, never a club's notes.
    detail = (await client.get(f"{API}/admin/speakers/{rao}", headers=world.admin)).json()
    assert detail["contacts"] == [{
        "club": {"id": world.club_a.id, "name": "Alpha Coders"}, "email": "rao@example.com", "phone": "+91 98765 43210",
        "consent_at": detail["contacts"][0]["consent_at"], "consent_by": detail["contacts"][0]["consent_by"],
        "updated_at": detail["contacts"][0]["updated_at"],
    }]
    assert "HDMI" not in json.dumps(detail)
    # The history says a contact changed, not what it is.
    lines = (await session.scalars(select(SpeakerHistory.after).where(SpeakerHistory.profile_id == rao, SpeakerHistory.action == "contact_updated"))).all()
    assert lines and "rao@example.com" not in json.dumps(lines)
    # Emptied, the contact is gone.
    assert (await client.put(url, json={"email": "", "phone": "", "notes": ""}, headers=world.a)).json() == {"contact": None}


async def test_door_staff_have_no_speakers_and_editors_do(client, session, world):
    await _create(client, world, [{"name": "Rao"}])
    door = await member_header(client, session, world.club_a, "door")
    for url in ("/clubs/speakers", "/clubs/speakers/list", "/clubs/speakers/list?format=xlsx"):
        r = await client.get(f"{API}{url}", headers=door)
        assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN", (url, r.text)
    editor = await member_header(client, session, world.club_a, "editor")
    assert (await client.get(f"{API}/clubs/speakers", headers=editor)).status_code == 200
    assert (await client.get(f"{API}/clubs/access/me", headers=editor)).json()["can"]["speakers"] is True


async def test_the_clubs_list_and_the_admins_as_spreadsheets(client, session, world):
    event_id = await _create(client, world, [{"name": "=HYPERLINK(1)", "designation": "Founder"}, {"name": "Rao"}])
    rao = (await _info(client, event_id))["Rao"]["speaker_profile_id"]
    await client.put(f"{API}/clubs/speakers/{rao}/contact", json={"email": "rao@example.com", "consent": True}, headers=world.a)
    r = await client.get(f"{API}/clubs/speakers/list", params={"format": "xlsx"}, headers=world.a)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats"), r.text[:200]
    sheet = load_workbook(io.BytesIO(r.content)).active
    rows = [[c.value for c in row] for row in sheet.iter_rows()]
    assert rows[0][0] == "Name" and {row[0] for row in rows[1:]} == {"=HYPERLINK(1)", "Rao"}
    assert any(row[7] == "rao@example.com" for row in rows[1:])
    r = await client.get(f"{API}/admin/speakers", params={"format": "xlsx"}, headers=world.viewer)
    assert r.status_code == 200
    rows = [[c.value for c in row] for row in load_workbook(io.BytesIO(r.content)).active.iter_rows()]
    assert len(rows) == 3 and "rao@example.com" not in json.dumps(rows, default=str)


async def test_apply_to_upcoming_changes_only_the_clubs_upcoming_copies(client, session, world):
    past, past_rows = await _event_with(session, world, world.club_a, [{"name": "Rao", "designation": "Old"}], days=-30, name="Past")
    await backfill.run(session)
    await session.refresh(past_rows[0])
    rao = past_rows[0].speaker_profile_id
    upcoming = await _create(client, world, [{"name": "Rao", "designation": "Stale", "speaker_profile_id": rao}])
    # Beta's copy, on a claimed profile: never touched by Alpha.
    profile = await session.get(SpeakerProfiles, rao)
    profile.claimed_by_user_id = world.speaker_user.id
    await session.commit()
    beta = await _create(client, world, [{"name": "Rao", "designation": "Beta's", "speaker_profile_id": rao}], h=world.b, name="Beta")
    r = await client.patch(f"{API}/speakers/me", json={"headline": "Professor of Robotics", "links": {"linkedin": "https://linkedin.com/in/rao"}}, headers=world.speaker_h)
    assert r.status_code == 200, r.text
    done = await client.post(f"{API}/clubs/speakers/{rao}/apply-to-upcoming", headers=world.a)
    assert done.status_code == 200 and done.json()["event_ids"] == [upcoming], done.text
    assert (await _info(client, upcoming))["Rao"]["designation"] == "Professor of Robotics"
    assert (await _info(client, upcoming))["Rao"]["link"] == "https://linkedin.com/in/rao"
    await session.refresh(past_rows[0])
    assert past_rows[0].designation == "Old"
    assert (await _info(client, beta))["Rao"]["designation"] == "Beta's"


# ─── Admins ──────────────────────────────────────────────────────────────


async def test_the_admin_list_filters_and_needs_manage_content_to_change(client, session, world):
    from app.api.interests.models import InterestCategory, Interests

    topic = InterestCategory(name="Mobile", slug="mobile")
    session.add(topic)
    await session.flush()
    flutter = Interests(name="Flutter", category_id=topic.id)
    session.add(flutter)
    await session.commit()
    await _create(client, world, [{"name": "Meera"}], days=-400, name="Last Year")
    r = await client.post(f"{API}/events/create", data={**_form(world, name="Flutter Day"), "interest_ids": json.dumps([flutter.id]), "speakers": json.dumps([{"name": "Rao"}])}, headers=world.a)
    assert r.status_code in (200, 201), r.text
    await _create(client, world, [{"name": "Sunil"}], h=world.b, name="Beta")
    base = f"{API}/admin/speakers"
    every = (await client.get(base, headers=world.viewer)).json()
    assert every["total"] == 3 and {i["name"] for i in every["items"]} == {"Meera", "Rao", "Sunil"}
    assert next(i for i in every["items"] if i["name"] == "Rao")["clubs"] == [{"id": world.club_a.id, "name": "Alpha Coders"}]

    async def names_for(**params):
        return {i["name"] for i in (await client.get(base, params=params, headers=world.viewer)).json()["items"]}

    assert await names_for(club_id=world.club_b.id) == {"Sunil"}
    assert await names_for(year=_days(-400).year) == {"Meera"} or _days(-400).year == _days(10).year
    assert await names_for(topic="mobile") == {"Rao"} and await names_for(topic=str(topic.id)) == {"Rao"}
    assert await names_for(q="sun") == {"Sunil"}
    assert await names_for(claimed=True) == set()
    page = (await client.get(base, params={"page_size": 2, "sort": "name"}, headers=world.viewer)).json()
    assert page["total"] == 3 and [i["name"] for i in page["items"]] == ["Meera", "Rao"]
    rao = next(i for i in every["items"] if i["name"] == "Rao")["id"]
    # A viewer reads; a change needs MANAGE_CONTENT.
    assert (await client.post(f"{base}/{rao}/hide", headers=world.viewer)).status_code == 403
    edit = await client.patch(f"{base}/{rao}", json={"headline": "Robotics", "city": "Kochi", "topics": [topic.id], "links": {"github": "https://github.com/rao"}}, headers=world.admin)
    assert edit.status_code == 200, edit.text
    body = edit.json()
    assert (body["headline"], body["city"], body["topics"][0]["slug"], body["links"]) == ("Robotics", "Kochi", "mobile", {"github": "https://github.com/rao"})
    assert body["history"][0]["action"] == "edited" and body["history"][0]["before"]["headline"] is None
    assert (await client.patch(f"{base}/{rao}", json={"topics": [999999]}, headers=world.admin)).status_code == 422
    assert (await client.patch(f"{base}/{rao}", json={"links": {"myspace": "https://x.example"}}, headers=world.admin)).status_code == 422
    assert await names_for(city="koch") == {"Rao"}
    assert (await client.post(f"{base}/{rao}/hide", headers=world.admin)).json()["status"] == "hidden"
    assert await names_for(status="hidden") == {"Rao"}
    assert (await client.post(f"{base}/{rao}/unhide", headers=world.admin)).json()["status"] == "active"


async def test_remove_on_request_hides_it_and_deletes_every_clubs_contact(client, session, world):
    alpha = await _create(client, world, [{"name": "Rao"}])
    rao = (await _info(client, alpha))["Rao"]["speaker_profile_id"]
    profile = await session.get(SpeakerProfiles, rao)
    profile.claimed_by_user_id = world.speaker_user.id
    await session.commit()
    await _create(client, world, [{"name": "Rao", "speaker_profile_id": rao}], h=world.b, name="Beta")
    for h in (world.a, world.b):
        assert (await client.put(f"{API}/clubs/speakers/{rao}/contact", json={"email": "rao@example.com", "consent": True}, headers=h)).status_code == 200
    assert (await client.post(f"{API}/speakers/{profile.slug}/follow", headers=world.fan_h)).status_code == 200
    r = await client.post(f"{API}/admin/speakers/{rao}/remove-on-request", headers=world.admin)
    assert r.status_code == 200 and r.json()["contacts_deleted"] == 2 and r.json()["followers_removed"] == 1, r.text
    assert await session.scalar(select(func.count()).select_from(SpeakerContacts).where(SpeakerContacts.profile_id == rao)) == 0
    assert (await client.get(f"{API}/speakers/{profile.slug}")).status_code == 404
    # The event page keeps the name the club published.
    assert (await _info(client, alpha))["Rao"]["name"] == "Rao"
    again = await client.put(f"{API}/clubs/speakers/{rao}/contact", json={"email": "rao@example.com", "consent": True}, headers=world.a)
    assert again.status_code == 409 and again.json()["error_code"] == "SPEAKER_REMOVED"
    assert (await client.get(f"{API}/admin/speakers/{rao}", headers=world.admin)).json()["history"][0]["action"] == "removed_on_request"


async def test_merge_moves_talks_contacts_and_followers_and_unmerge_puts_them_back(client, session, world):
    alpha = await _create(client, world, [{"name": "Dr. Rao", "bio": "Robots."}], name="Alpha Night")
    beta = await _create(client, world, [{"name": "Rao", "designation": "Professor"}], h=world.b, name="Beta Night")
    keep = (await _info(client, alpha))["Dr. Rao"]["speaker_profile_id"]
    gone = (await _info(client, beta))["Rao"]["speaker_profile_id"]
    await client.put(f"{API}/clubs/speakers/{gone}/contact", json={"email": "rao@beta.example", "consent": True}, headers=world.b)
    gone_slug = (await session.get(SpeakerProfiles, gone)).slug
    session.add(SpeakerFollows(user_id=world.fan.id, profile_id=gone))
    await session.commit()
    queue = (await client.get(f"{API}/admin/speakers/duplicates", headers=world.viewer)).json()
    assert [{q["a"]["id"], q["b"]["id"]} for q in queue] == [{keep, gone}] and queue[0]["a"]["clubs"]
    assert (await client.post(f"{API}/admin/speakers/merge", json={"keep_id": keep, "merge_id": gone}, headers=world.viewer)).status_code == 403
    r = await client.post(f"{API}/admin/speakers/merge", json={"keep_id": keep, "merge_id": gone}, headers=world.admin)
    assert r.status_code == 200 and r.json()["moved_talks"] == 1, r.text
    assert (await _info(client, beta))["Rao"]["speaker_profile_id"] == keep
    kept = await session.get(SpeakerProfiles, keep)
    await session.refresh(kept)
    assert kept.headline == "Professor" and kept.bio == "Robots."
    contact = await session.scalar(select(SpeakerContacts).where(SpeakerContacts.club_id == world.club_b.id))
    assert contact.profile_id == keep
    # Beta may still open it (its talk is on it now); its old address points at the survivor.
    assert (await client.get(f"{API}/clubs/speakers/{gone}", headers=world.b)).json()["id"] == keep
    assert (await client.get(f"{API}/speakers/{gone_slug}")).json() == {"redirect_slug": kept.slug}
    assert (await client.get(f"{API}/admin/speakers/duplicates", headers=world.viewer)).json() == []
    assert (await client.get(f"{API}/admin/speakers/duplicates", params={"status": "merged"}, headers=world.viewer)).json()[0]["status"] == "merged"
    again = await client.post(f"{API}/admin/speakers/merge", json={"keep_id": keep, "merge_id": gone}, headers=world.admin)
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_MERGED"

    r = await client.post(f"{API}/admin/speakers/{gone}/unmerge", headers=world.admin)
    assert r.status_code == 200 and r.json()["restored_talks"] == 1, r.text
    assert (await _info(client, beta))["Rao"]["speaker_profile_id"] == gone
    await session.refresh(kept)
    assert kept.headline is None and kept.bio == "Robots."
    await session.refresh(contact)
    assert contact.profile_id == gone
    assert (await session.scalar(select(SpeakerFollows.profile_id).where(SpeakerFollows.user_id == world.fan.id))) == gone
    assert (await session.get(SpeakerProfiles, gone)).status == "active"
    assert (await client.get(f"{API}/admin/speakers/duplicates", params={"status": "dismissed"}, headers=world.viewer)).json()[0]["status"] == "dismissed"
    assert (await client.post(f"{API}/admin/speakers/{gone}/unmerge", headers=world.admin)).json()["error_code"] == "NOT_MERGED"


async def test_a_pair_is_dismissed_and_two_claimed_profiles_are_never_merged(client, session, world, make_user):
    alpha = await _create(client, world, [{"name": "Rao"}])
    beta = await _create(client, world, [{"name": "Rao"}], h=world.b, name="Beta")
    a = (await _info(client, alpha))["Rao"]["speaker_profile_id"]
    b = (await _info(client, beta))["Rao"]["speaker_profile_id"]
    pair = (await client.get(f"{API}/admin/speakers/duplicates", headers=world.admin)).json()[0]
    r = await client.post(f"{API}/admin/speakers/duplicates/{pair['id']}/dismiss", headers=world.admin)
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    assert (await client.post(f"{API}/admin/speakers/duplicates/{pair['id']}/dismiss", headers=world.admin)).status_code == 409
    other = await make_user(email="spk-other@example.com")
    for pid, user in ((a, world.speaker_user), (b, other)):
        (await session.get(SpeakerProfiles, pid)).claimed_by_user_id = user.id
    await session.commit()
    r = await client.post(f"{API}/admin/speakers/merge", json={"keep_id": a, "merge_id": b}, headers=world.admin)
    assert r.status_code == 409 and r.json()["error_code"] == "BOTH_CLAIMED"


# ─── Claiming ────────────────────────────────────────────────────────────


@pytest.fixture
def mail(monkeypatch):
    from app.core.email import email as email_mod

    sent = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    return sent


async def test_an_invite_is_claimed_once_by_a_signed_in_app_user(client, session, world, mail, mint_token):
    event_id = await _create(client, world, [{"name": "Rao", "designation": "Professor"}], days=-5, name="Past Talk")
    rao = (await _info(client, event_id))["Rao"]["speaker_profile_id"]
    r = await client.post(f"{API}/admin/speakers/{rao}/invite", json={"email": "Rao@Example.com"}, headers=world.admin)
    assert r.status_code == 200 and r.json()["email_masked"] == "r•••@example.com", r.text
    assert len(mail) == 1 and mail[0]["recipients"] == ["rao@example.com"]
    link = next(w for w in mail[0]["text"].split() if "/speakers/claim/" in w)
    token = link.rsplit("/", 1)[-1]
    assert await session.scalar(select(SpeakerClaims.token_hash)) != token
    preview = (await client.get(f"{API}/speakers/claim/{token}")).json()
    assert preview["valid"] is True and preview["profile"]["name"] == "Rao" and preview["email_masked"] == "r•••@example.com"
    assert (await client.get(f"{API}/speakers/claim/not-a-token")).status_code == 404
    assert (await client.post(f"{API}/speakers/claim/{token}")).status_code == 401
    assert (await client.post(f"{API}/speakers/claim/{token}", headers=world.a)).status_code == 403
    done = await client.post(f"{API}/speakers/claim/{token}", headers=world.speaker_h)
    assert done.status_code == 200 and done.json() == {"claimed": True, "slug": "rao"}, done.text
    twice = await client.post(f"{API}/speakers/claim/{token}", headers=world.fan_h)
    assert twice.status_code == 409 and twice.json()["error_code"] == "CLAIM_USED"
    assert (await client.get(f"{API}/speakers/claim/{token}")).json()["reason"] == "used"
    me = (await client.get(f"{API}/speakers/me", headers=world.speaker_h)).json()
    assert me["name"] == "Rao" and me["claimed"] is True and me["talks"]["past"][0]["event_id"] == event_id
    assert (await client.get(f"{API}/speakers/me", headers=world.fan_h)).json()["error_code"] == "NO_SPEAKER_PROFILE"
    edit = await client.patch(f"{API}/speakers/me", json={"bio": "I build robots.", "name": "Dr. Rao"}, headers=world.speaker_h)
    assert edit.status_code == 200 and edit.json()["bio"] == "I build robots."
    # The club's later edit touches only its copy.
    r = await _update(client, world, event_id, [{"id": (await _info(client, event_id))["Rao"]["id"], "name": "Prof Rao"}], days=-5, name="Past Talk")
    assert (await session.get(SpeakerProfiles, rao)).name == "Dr. Rao"
    # Already claimed: no more invites.
    assert (await client.post(f"{API}/admin/speakers/{rao}/invite", json={"email": "x@example.com"}, headers=world.admin)).json()["error_code"] == "ALREADY_CLAIMED"


async def test_an_expired_or_replaced_invite_is_refused(client, session, world, mail):
    event_id = await _create(client, world, [{"name": "Rao"}])
    rao = (await _info(client, event_id))["Rao"]["speaker_profile_id"]
    for email in ("one@example.com", "two@example.com"):
        await client.post(f"{API}/admin/speakers/{rao}/invite", json={"email": email}, headers=world.admin)
    first, second = (next(w for w in m["text"].split() if "/speakers/claim/" in w).rsplit("/", 1)[-1] for m in mail)
    r = await client.post(f"{API}/speakers/claim/{first}", headers=world.speaker_h)
    assert r.status_code == 409 and r.json()["error_code"] == "CLAIM_REPLACED"
    claim = await session.scalar(select(SpeakerClaims).where(SpeakerClaims.email == "two@example.com"))
    claim.expires_at = _days(-1)
    await session.commit()
    r = await client.post(f"{API}/speakers/claim/{second}", headers=world.speaker_h)
    assert r.status_code == 410 and r.json()["error_code"] == "CLAIM_EXPIRED"


# ─── The public page, following, the push ────────────────────────────────


async def test_the_public_page_shows_published_talks_only(client, session, world):
    draft = await _create(client, world, [{"name": "Meera", "talk_title": "Secret"}], status="draft", name="Draft Night")
    meera = (await _info(client, draft, world.a))["Meera"]["speaker_profile_id"]
    slug = (await session.get(SpeakerProfiles, meera)).slug
    # Only a draft: no page yet (it would tell what is being prepared).
    assert (await client.get(f"{API}/speakers/{slug}")).status_code == 404
    assert (await client.get(f"{API}/speakers/sitemap")).json() == {"speakers": []}
    past = await _create(client, world, [{"name": "Meera", "speaker_profile_id": meera, "talk_title": "Widgets"}], days=-3, name="Past Night")
    upcoming = await _create(client, world, [{"name": "Meera", "speaker_profile_id": meera}], days=4, name="Next Night")
    page = (await client.get(f"{API}/speakers/{slug}")).json()
    assert [t["event_id"] for t in page["talks"]["upcoming"]] == [upcoming]
    assert [(t["event_id"], t["talk_title"]) for t in page["talks"]["past"]] == [(past, "Widgets")]
    assert "Secret" not in json.dumps(page) and page["following"] is None and page["followers_count"] == 0
    assert page["talks"]["past"][0]["club"]["slug"] == world.club_a.slug
    assert (await client.get(f"{API}/speakers/sitemap")).json()["speakers"][0]["slug"] == slug
    assert (await client.get(f"{API}/speakers/no-such-speaker")).status_code == 404


async def test_followers_are_pushed_once_when_a_followed_speaker_is_on_a_published_event(client, session, world, monkeypatch):
    from app.core.notifications import service as push_service

    pushes = []

    async def fake_push(session, user_ids, title, body, data=None, image_url=None):
        pushes.append((sorted(user_ids), title, data))
        return len(user_ids)

    monkeypatch.setattr(push_service, "send_notification_to_users", fake_push)
    first = await _create(client, world, [{"name": "Rao"}], days=-2, name="Past")
    rao = (await _info(client, first))["Rao"]["speaker_profile_id"]
    slug = (await session.get(SpeakerProfiles, rao)).slug
    r = await client.post(f"{API}/speakers/{slug}/follow", headers=world.fan_h)
    assert r.json() == {"slug": slug, "following": True, "followers_count": 1}
    assert (await client.post(f"{API}/speakers/{slug}/follow", headers=world.fan_h)).json()["followers_count"] == 1
    assert (await client.get(f"{API}/speakers/following", headers=world.fan_h)).json()[0]["slug"] == slug
    assert (await client.get(f"{API}/speakers/{slug}", headers=world.fan_h)).json()["following"] is True
    # A draft tells nobody; publishing it does, once.
    draft = await _create(client, world, [{"name": "Rao", "speaker_profile_id": rao}], status="draft", name="Coming Up")
    assert pushes == []
    r = await client.patch(f"{API}/events/{draft}/closeout/settings", json={"status": "published"}, headers=world.a)
    assert r.status_code == 200, r.text
    lines = (await session.scalars(select(Notifications).where(Notifications.type == "speaker_event"))).all()
    assert [(n.user_id, n.event_id) for n in lines] == [(world.fan.id, draft)]
    assert len(pushes) == 1 and pushes[0][0] == [world.fan.id] and "Rao" in pushes[0][1]
    assert pushes[0][2]["type"] == "speaker_event" and pushes[0][2]["event_id"] == str(draft) and pushes[0][2]["slug"]
    # Saved again: nobody is told twice.
    r = await _update(client, world, draft, [{"id": (await _info(client, draft))["Rao"]["id"], "name": "Rao"}], name="Coming Up")
    assert r.status_code == 200, r.text
    assert len(pushes) == 1
    assert await session.scalar(select(func.count()).select_from(Notifications).where(Notifications.type == "speaker_event")) == 1
    assert (await client.delete(f"{API}/speakers/{slug}/follow", headers=world.fan_h)).json()["following"] is False


# ─── The backfill ────────────────────────────────────────────────────────


async def test_the_backfill_links_what_is_there_and_runs_again_harmlessly(client, session, world):
    a1, a1_rows = await _event_with(session, world, world.club_a, [
        {"name": "Dr. Meera Nair", "link": "https://www.linkedin.com/in/meera/"}, {"name": "Rao", "photo": "events/speakers/v-rao.jpg"},
    ], days=-60, name="A1")
    a2, a2_rows = await _event_with(session, world, world.club_a, [{"name": "meera nair"}, {"name": "Kiran"}], days=-30, name="A2")
    b1, b1_rows = await _event_with(session, world, world.club_b, [
        {"name": "Meera", "link": "https://linkedin.com/in/meera"},  # the same link, a name plainly hers
        {"name": "Rao", "photo": "events/speakers/v-rao.jpg"},  # the same name and photo
        {"name": "Kiran"},  # the same name only: a pair for the queue
        {"name": "Someone Else", "link": "https://linkedin.com/in/meera"},  # the same link, another name
    ], days=-10, name="B1")
    session.add_all([
        AgendaItems(event_id=a1.id, starts_at=a1.event_datetime, title="Panel", speaker="Meera Nair & Rao", sort_order=0),
        AgendaItems(event_id=a1.id, starts_at=a1.event_datetime, title="Talk", speaker="dr meera nair", sort_order=1),
        AgendaItems(event_id=a1.id, starts_at=a1.event_datetime, title="Guest", speaker="A Stranger", sort_order=2),
    ])
    await session.commit()
    dry = await backfill.run(session, apply=False)
    assert dry["applied"] is False and dry["linked"] == 8
    assert await session.scalar(select(func.count()).select_from(SpeakerProfiles)) == 0

    done = await backfill.run(session)
    # Meera, Rao, Kiran at Alpha, Kiran at Beta, Someone Else: five people as far as the rules can tell.
    assert (done["profiles_made"], done["by_club_name"], done["by_link"], done["by_photo"], done["unlinked_left"]) == (5, 1, 1, 1, 0), done
    pid = {}
    for r in a1_rows + a2_rows + b1_rows:
        await session.refresh(r)
        pid.setdefault(r.name, set()).add(r.speaker_profile_id)
    meera = (await session.get(EventSpeakers, a1_rows[0].id)).speaker_profile_id
    assert {(await session.get(EventSpeakers, r.id)).speaker_profile_id for r in (a1_rows[0], a2_rows[0], b1_rows[0])} == {meera}
    assert (await session.get(EventSpeakers, a1_rows[1].id)).speaker_profile_id == (await session.get(EventSpeakers, b1_rows[1].id)).speaker_profile_id
    kirans = {(await session.get(EventSpeakers, r.id)).speaker_profile_id for r in (a2_rows[1], b1_rows[2])}
    assert len(kirans) == 2
    reasons = {(c.reason, frozenset((c.a_id, c.b_id))) for c in (await session.scalars(select(SpeakerMergeCandidates))).all()}
    else_id = (await session.get(EventSpeakers, b1_rows[3].id)).speaker_profile_id
    assert reasons == {(sp.SAME_NAME_OTHER_CLUB, frozenset(kirans)), (sp.SAME_LINK_OTHER_NAME, frozenset((meera, else_id)))}
    # Agenda free text: by the whole name, else each name; a stranger stays text.
    links = (await session.execute(
        select(AgendaItems.title, AgendaItemSpeakers.event_speaker_id).join(AgendaItemSpeakers, AgendaItemSpeakers.agenda_item_id == AgendaItems.id)
        .order_by(AgendaItems.title, AgendaItemSpeakers.position)
    )).all()
    assert [(t, s) for t, s in links] == [("Panel", a1_rows[0].id), ("Panel", a1_rows[1].id), ("Talk", a1_rows[0].id)]
    assert (done["agenda_sessions_linked"], done["agenda_sessions_unmatched"]) == (2, 1)
    # Nothing shown changed: each event keeps its copy.
    assert (await session.get(EventSpeakers, b1_rows[0].id)).name == "Meera"
    assert (await session.get(SpeakerProfiles, meera)).name == "Dr. Meera Nair"

    profiles = await session.scalar(select(func.count()).select_from(SpeakerProfiles))
    pairs = await session.scalar(select(func.count()).select_from(SpeakerMergeCandidates))
    again = await backfill.run(session)
    assert (again["linked"], again["candidates"], again["agenda_sessions_linked"]) == (0, 0, 0)
    assert await session.scalar(select(func.count()).select_from(SpeakerProfiles)) == profiles
    assert await session.scalar(select(func.count()).select_from(SpeakerMergeCandidates)) == pairs


async def test_the_admins_backfill_button_queues_the_job(client, session, world):
    _e, rows = await _event_with(session, world, world.club_a, [{"name": "Rao"}])
    assert (await client.post(f"{API}/admin/speakers/backfill", headers=world.viewer)).status_code == 403
    r = await client.post(f"{API}/admin/speakers/backfill", headers=world.admin)
    assert r.status_code == 200 and r.json()["queued"] is True
    # The suite runs the job right after the request, as the API's runner would.
    await session.refresh(rows[0])
    assert rows[0].speaker_profile_id is not None


# ─── Names, the AI draft, the MCP tool ───────────────────────────────────


def test_names_read_titles_case_and_punctuation_away():
    assert names.name_key("Dr. Meera  Nair") == names.name_key("meera nair") == "meera nair"
    assert names.name_key("Dr.") == "dr"
    assert names.link_key("https://www.LinkedIn.com/in/Meera/?utm=1") == "linkedin.com/in/meera"
    assert names.links_from("https://github.com/rao") == {"github": "https://github.com/rao"}
    assert names.links_from("https://x.com/rao") == {"x": "https://x.com/rao"}
    assert names.agenda_names("Meera, Rao & Dr. Kiran and Sunil / Asha") == ["meera", "rao", "kiran", "sunil", "asha"]
    assert names.slug_base("Dr. Méera Nair!") == "meera-nair"
    assert names.names_alike("meera", "meera nair") and not names.names_alike("meera", "kiran")


async def test_the_ai_draft_and_the_mcp_tool_offer_the_clubs_past_speakers(client, session, world):
    from app.api.ai.schemas import Speaker
    from app.api.club_access import policy
    from app.api.mcp import tools
    from app.api.speakers import club as speakers_club

    await _create(client, world, [{"name": "Dr. Meera Nair", "designation": "GDE"}], days=-10)
    draft = [Speaker(name="Meera Nair"), Speaker(name="Meera Nayar"), Speaker(name="Nobody Known")]
    await speakers_club.match_draft(session, world.club_a.id, draft)
    assert draft[0].speaker_profile_id and draft[0].matches[0].headline == "GDE"
    assert draft[1].speaker_profile_id is None and draft[1].matches and draft[1].matches[0].name == "Dr. Meera Nair"
    assert (draft[2].speaker_profile_id, draft[2].matches) == (None, [])
    # Beta's assistant sees none of Alpha's.
    beta_draft = [Speaker(name="Meera Nair")]
    await speakers_club.match_draft(session, world.club_b.id, beta_draft)
    assert beta_draft[0].matches == []
    call = SimpleNamespace(session=session, club=world.club_a)
    found = await tools.list_past_speakers(call, {"names": ["meera nair"]})
    assert found["results"][0]["matches"][0]["speaker_profile_id"] == draft[0].speaker_profile_id
    assert "email" not in json.dumps(found)
    assert policy.allows_tool("editor", "list_past_speakers") and not policy.allows_tool("door", "list_past_speakers")


async def test_the_event_page_payloads_keep_their_old_fields(client, session, world):
    event_id = await _create(client, world, [{"name": "Rao", "designation": "Prof", "bio": "B", "link": "https://rao.example"}])
    s = (await _info(client, event_id))["Rao"]
    assert {"id", "name", "designation", "photo", "bio", "link", "display_order"} <= set(s)
    page = (await client.get(f"{API}/discover/events/{event_id}")).json()["speakers"][0]
    assert set(page) == {"id", "name", "designation", "photo", "bio", "link", "profile_slug", "talk_title"}
    assert page["profile_slug"] == "rao"


async def test_a_claimed_speaker_and_an_admin_change_the_photo(client, session, world, fake_s3):
    from PIL import Image

    event_id = await _create(client, world, [{"name": "Rao"}])
    rao = (await _info(client, event_id))["Rao"]["speaker_profile_id"]
    (await session.get(SpeakerProfiles, rao)).claimed_by_user_id = world.speaker_user.id
    await session.commit()
    buf = io.BytesIO()
    Image.new("RGB", (240, 240), (200, 80, 40)).save(buf, format="PNG")
    r = await client.put(f"{API}/speakers/me/photo", files={"photo": ("rao.png", buf.getvalue(), "image/png")}, headers=world.speaker_h)
    assert r.status_code == 200, r.text
    assert r.json()["photo"]["thumbnail"] and any("events/speakers/" in k for k in fake_s3)
    bad = await client.put(f"{API}/admin/speakers/{rao}/photo", files={"photo": ("x.png", b"not a picture", "image/png")}, headers=world.admin)
    assert bad.status_code == 422, bad.text
    ok = await client.put(f"{API}/admin/speakers/{rao}/photo", files={"photo": ("rao.png", buf.getvalue(), "image/png")}, headers=world.admin)
    assert ok.status_code == 200 and ok.json()["photo"]["medium"], ok.text
    # The event's own copy keeps the photo the club gave it (none).
    assert (await _info(client, event_id))["Rao"]["photo"] is None
