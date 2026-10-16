"""A club's website: made from its own data in one of four templates, previewed
as it is edited, published as one HTML page, served to anyone, with images
through the page's own route."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.closeout.models import EventStatus
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, mint_token):
    owner, club = await make_club(name="Circuit Society", email="site@example.com")
    club.tagline = "Build things that think."
    club.about = "Hack nights, build weekends and competitions for Pune's makers. Free to follow, mostly free to attend."
    club.brand_primary = "#17a58f"
    club.location_name = "Pune"
    soon = await make_event(club=club, created_by=owner, name="Hack Night 04: Edge AI", event_datetime=NOW + timedelta(days=9), has_fee=True, reg_fee=100, location_name="Lab 3, COEP")
    await make_event(club=club, created_by=owner, name="Intro to KiCad", event_datetime=NOW - timedelta(days=30), lifecycle=EventStatus.closed)
    await make_event(club=club, created_by=owner, name="Secret draft", event_datetime=NOW + timedelta(days=20), lifecycle=EventStatus.draft)
    await session.commit()
    return dict(owner=owner, club=club, soon=soon, h=auth_header(await mint_token(owner)))


async def test_the_site_is_made_from_the_club_previewed_published_and_served(client, world):
    h, club = world["h"], world["club"]
    s = (await client.get("/api/v1/clubs/site", headers=h)).json()
    assert s["template"] == "launchpad" and s["theme"] == "light" and s["published_at"] is None and s["stale"] is False
    assert s["default_headline"] == "Build things that think." and s["default_subline"].startswith("Hack nights, build weekends")
    assert s["available"]["team"] is False and s["available"]["past"] is True and s["url"] == f"https://{club.slug}.myotherapp.in"
    assert [t["key"] for t in s["templates"]] == ["launchpad", "chapter", "campus", "arena"] and len(s["fonts"]) == 5
    # Nothing public yet.
    assert (await client.get(f"/api/v1/sites/{club.slug}")).status_code == 404

    # A preview of unsaved choices: the headline typed, the dark look, the upcoming event, never the draft.
    p = (await client.post("/api/v1/clubs/site/preview", json={"theme": "dark", "headline": "Make <things> that think"}, headers=h)).json()["html"]
    assert p.startswith("<!doctype html>") and "Make &lt;things&gt; that think" in p and "--bg:#0b0e0d" in p and "Hack Night 04: Edge AI" in p and "Secret draft" not in p
    assert "Intro to KiCad" in p and "Lab 3, COEP · ₹100" in p and "Pune" in p and "DM+Sans" in p
    assert (await client.get("/api/v1/clubs/site", headers=h)).json()["headline"] is None  # a preview saves nothing

    # Saved choices: the Arena template with a countdown to the next event, the team section off, a font.
    s = (await client.patch("/api/v1/clubs/site", json={"template": "arena", "theme": "dark", "font": "space_grotesk", "subline": "  Pune's makers.  ", "sections": {"team": False, "past": False}}, headers=h)).json()
    assert s["template"] == "arena" and s["subline"] == "Pune's makers." and s["sections"]["team"] is False and s["sections"]["hero"] is True
    assert (await client.patch("/api/v1/clubs/site", json={"template": "fancy"}, headers=h)).status_code == 422

    # Publish: the page goes to the public address, with a cache header, and the console says it is current.
    s = (await client.post("/api/v1/clubs/site/publish", headers=h)).json()
    assert s["published_at"] is not None and s["stale"] is False
    r = await client.get(f"/api/v1/sites/{club.slug}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and r.headers["cache-control"] == "public, max-age=300"
    html = r.text
    assert "data-countdown" in html and "Space+Grotesk" in html and "Past events" not in html and "The team" not in html and "Pune&#x27;s makers." in html
    assert '<a href="#events">Events</a>' in html and "Made with MyOtherApp" in html and "application/ld+json" in html
    # A change after publishing makes the published page stale; a new publish catches up.
    assert (await client.patch("/api/v1/clubs/site", json={"theme": "light"}, headers=h)).json()["stale"] is True
    assert "--bg:#0b0e0d" in (await client.get(f"/api/v1/sites/{club.slug}")).text
    await client.post("/api/v1/clubs/site/publish", headers=h)
    assert "--bg:#f7f8f6" in (await client.get(f"/api/v1/sites/{club.slug}")).text

    # Images come through the page's own route, which only ever points into the club's folder.
    assert (await client.get(f"/api/v1/sites/{club.slug}/i", params={"k": "clubs/logos/abc.medium.png"})).status_code == 302
    assert (await client.get(f"/api/v1/sites/{club.slug}/i", params={"k": "secrets/db.sql"})).status_code == 404
    assert (await client.get(f"/api/v1/sites/{club.slug}/i", params={"k": "clubs/../x.png"})).status_code == 404
    assert (await client.get("/api/v1/sites/nobody-here")).status_code == 404


async def test_a_published_site_is_remade_when_the_events_change(client, session, world, make_event):
    h, club = world["h"], world["club"]
    await client.post("/api/v1/clubs/site/publish", headers=h)
    assert "Robo Sumo League" not in (await client.get(f"/api/v1/sites/{club.slug}")).text
    # A new event through the API lands on the page without another publish.
    made = await client.post("/api/v1/events/create", data={"name": "Robo Sumo League", "description": "Fight night for robots.", "event_datetime": (NOW + timedelta(days=12)).isoformat(), "duration": "3", "has_fee": "false", "status": "published", "category_id": str(world["soon"].category_id)}, headers=h)
    assert made.status_code in (200, 201), made.text
    assert "Robo Sumo League" in (await client.get(f"/api/v1/sites/{club.slug}")).text


def test_the_site_address_is_the_clubs_subdomain_when_the_slug_can_be_one():
    from types import SimpleNamespace

    from app.api.sites.service import public_url

    assert public_url(SimpleNamespace(slug="circuit-society-a1b2c3")) == "https://circuit-society-a1b2c3.myotherapp.in"
    # Longer than a DNS label allows, or not a label at all: the public site's club page.
    long = "a" * 70
    assert public_url(SimpleNamespace(slug=long)) == f"https://myotherapp.in/clubs/{long}"
    assert public_url(SimpleNamespace(slug="-a1b2c3")) == "https://myotherapp.in/clubs/-a1b2c3"
