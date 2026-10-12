"""The club profile: tagline and cover alongside the rest, partners as their own list, and a handle nobody can edit."""
import io

import pytest
from PIL import Image

from tests.conftest import auth_header


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "#17a58f").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
async def world(client, session, make_club, mint_token):
    owner, club = await make_club(name="Yukthi", email="profile@example.com")
    rival_owner, rival = await make_club(name="Rivals", email="rivals-profile@example.com")
    club.is_verified = True  # the public page only shows a verified club
    await session.commit()
    return dict(owner=owner, club=club, rival=rival, h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)))


async def test_tagline_and_cover_join_the_profile_and_the_handle_stays(client, world, fake_s3):
    h, club = world["h"], world["club"]
    slug = club.slug
    saved = await client.put("/api/v1/clubs/update", data={"name": "Yukthi", "tagline": "  Pune's student electronics and robotics club  ", "slug": "something-else"}, headers=h)
    assert saved.status_code == 200, saved.text
    me = (await client.get("/api/v1/clubs/info", headers=h)).json()
    assert me["tagline"] == "Pune's student electronics and robotics club" and me["slug"] == slug and me["cover"] is None and me["partners"] == []
    assert (await client.put("/api/v1/clubs/update", data={"name": "Yukthi", "tagline": "x" * 121}, headers=h)).status_code == 422
    # Cleared when sent empty, like every other field on this form.
    await client.put("/api/v1/clubs/update", data={"name": "Yukthi", "tagline": ""}, headers=h)
    assert (await client.get("/api/v1/clubs/info", headers=h)).json()["tagline"] is None

    cover = await client.put("/api/v1/clubs/cover", files={"cover": ("banner.png", _png(), "image/png")}, headers=h)
    assert cover.status_code == 200, cover.text
    assert (await client.put("/api/v1/clubs/cover", files={"cover": ("notes.txt", b"hello", "text/plain")}, headers=h)).status_code == 422
    assert (await client.get("/api/v1/clubs/info", headers=h)).json()["cover"] is not None


async def test_partners_are_listed_in_order_and_kept_to_their_club(client, world):
    h = world["h"]
    kl = await client.post("/api/v1/clubs/partners", json={"name": "Kinetic Labs", "role": "Industry partner", "since": 2025}, headers=h)
    assert kl.status_code == 201, kl.text
    cm = (await client.post("/api/v1/clubs/partners", json={"name": "COEP Makerspace", "role": "Lab and equipment partner", "since": 2024}, headers=h)).json()
    gp = (await client.post("/api/v1/clubs/partners", json={"name": "GDG Pune"}, headers=h)).json()
    assert gp["role"] is None and gp["since"] is None and [p["sort_order"] for p in (kl.json(), cm, gp)] == [0, 1, 2]
    theirs = (await client.post("/api/v1/clubs/partners", json={"name": "Their Sponsor", "role": "x"}, headers=world["rival_h"])).json()

    listed = (await client.get("/api/v1/clubs/partners", headers=h)).json()
    assert [p["name"] for p in listed] == ["Kinetic Labs", "COEP Makerspace", "GDG Pune"]
    assert [p["name"] for p in (await client.get("/api/v1/clubs/info", headers=h)).json()["partners"]] == ["Kinetic Labs", "COEP Makerspace", "GDG Pune"]
    # Anyone signed in can see them on the public page too.
    public = await client.get(f"/api/v1/clubs/info/{world['club'].id}", headers=world["rival_h"])
    assert public.status_code == 200, public.text
    assert [p["name"] for p in public.json()["partners"]][:1] == ["Kinetic Labs"]

    fixed = await client.patch(f"/api/v1/clubs/partners/{gp['id']}", json={"role": "Community partner", "since": 2026}, headers=h)
    assert fixed.status_code == 200 and fixed.json()["role"] == "Community partner" and fixed.json()["since"] == 2026
    # Reordering: the first one sent to the back.
    assert (await client.patch(f"/api/v1/clubs/partners/{kl.json()['id']}", json={"sort_order": 5}, headers=h)).status_code == 200
    assert [p["name"] for p in (await client.get("/api/v1/clubs/partners", headers=h)).json()] == ["COEP Makerspace", "GDG Pune", "Kinetic Labs"]
    for bad in ({"name": ""}, {"since": 1800}, {"sort_order": -1}):
        assert (await client.patch(f"/api/v1/clubs/partners/{gp['id']}", json=bad, headers=h)).status_code == 422, bad
    assert (await client.post("/api/v1/clubs/partners", json={"name": " "}, headers=h)).status_code == 422

    assert (await client.patch(f"/api/v1/clubs/partners/{theirs['id']}", json={"name": "Ours now"}, headers=h)).status_code == 404
    assert (await client.delete(f"/api/v1/clubs/partners/{theirs['id']}", headers=h)).status_code == 404
    assert (await client.delete(f"/api/v1/clubs/partners/{cm['id']}", headers=h)).json() == {"deleted": True}
    assert [p["name"] for p in (await client.get("/api/v1/clubs/partners", headers=h)).json()] == ["GDG Pune", "Kinetic Labs"]
    assert (await client.delete(f"/api/v1/clubs/partners/{cm['id']}", headers=h)).status_code == 404
    assert [p["name"] for p in (await client.get("/api/v1/clubs/partners", headers=world["rival_h"])).json()] == ["Their Sponsor"]
