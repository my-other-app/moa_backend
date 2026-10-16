"""A speaker's bio and link: sent with the speakers on create and update,
checked (a bio of at most 600 characters, a link that is a web address), and
shown wherever the event's speakers are."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.api.events.models import EventCategories
from app.api.interests.models import InterestCategory, Interests
from tests.conftest import auth_header


@pytest.fixture
async def world(session, make_club, mint_token):
    owner, club = await make_club(name="Speaker Society", email="spk-a@example.com")
    category = EventCategories(name="Talks")
    icat = InterestCategory(name="Speaker interests")
    session.add_all([category, icat])
    await session.flush()
    interest = Interests(name="Speaker AI", category_id=icat.id)
    session.add(interest)
    await session.commit()
    form = {
        "event_datetime": (datetime.now(timezone.utc) + timedelta(days=12)).isoformat(), "duration": "2",
        "has_fee": "false", "status": "published", "category_id": str(category.id),
    }
    return dict(h=auth_header(await mint_token(owner)), form=form, interest=interest)


async def _create(client, world, speakers):
    return await client.post("/api/v1/events/create", data={**world["form"], "name": "Speaker Night", "speakers": json.dumps(speakers)}, headers=world["h"])


async def _speakers(client, event_id) -> dict:
    return {s["name"]: s for s in (await client.get(f"/api/v1/events/info/{event_id}")).json()["speakers"]}


async def test_speakers_take_a_bio_and_a_link(client, world):
    made = await _create(client, world, [
        {"name": "Dr. Rao", "designation": "Professor", "bio": "  Builds robots.  ", "link": " https://rao.example/about "},
        {"name": "Meera", "bio": "", "link": "   "},
        {"name": "Kiran", "bio": " " + "x" * 600 + " "},
        {"name": "Old Client"},
    ])
    assert made.status_code in (200, 201), made.text
    got = await _speakers(client, made.json()["id"])
    assert (got["Dr. Rao"]["bio"], got["Dr. Rao"]["link"]) == ("Builds robots.", "https://rao.example/about")
    assert (got["Meera"]["bio"], got["Meera"]["link"]) == (None, None)
    assert got["Kiran"]["bio"] == "x" * 600
    assert (got["Old Client"]["bio"], got["Old Client"]["link"]) == (None, None)


@pytest.mark.parametrize("bad", [
    {"link": "ftp://rao.example"}, {"link": "rao.example"}, {"link": "https://"}, {"link": "https://rao example.com"},
    {"link": "javascript:alert(1)"}, {"link": "https://rao.example/" + "a" * 500}, {"link": 7},
    {"bio": "x" * 601}, {"bio": 7},
])
async def test_a_bad_bio_or_link_is_refused(client, world, bad):
    r = await _create(client, world, [{"name": "Rao", **bad}])
    assert r.status_code == 422, r.text


async def test_an_edit_that_leaves_them_out_keeps_them(client, world):
    made = await _create(client, world, [{"name": "Dr. Rao", "bio": "Builds robots.", "link": "https://rao.example"}])
    event_id = made.json()["id"]
    rao_id = (await _speakers(client, event_id))["Dr. Rao"]["id"]
    update = {**world["form"], "name": "Speaker Night", "interest_ids": str(world["interest"].id)}

    async def put(speaker):
        return await client.put(f"/api/v1/events/update/{event_id}", data={**update, "speakers": json.dumps([{"id": rao_id, "name": "Dr. Rao", **speaker}])}, headers=world["h"])

    # An older client sends neither: both stay.
    assert (await put({})).status_code == 200
    rao = (await _speakers(client, event_id))["Dr. Rao"]
    assert (rao["bio"], rao["link"]) == ("Builds robots.", "https://rao.example")
    # Sent, they change; an empty link clears it.
    assert (await put({"bio": "Robots, mostly.", "link": ""})).status_code == 200
    rao = (await _speakers(client, event_id))["Dr. Rao"]
    assert (rao["bio"], rao["link"]) == ("Robots, mostly.", None)
    assert (await put({"link": "mailto:rao@example.com"})).status_code == 422
