"""A club's website: made from its own data in one of the designer's
templates, arranged by the club, previewed as it is edited, published as one
HTML page, served to anyone, with images through the page's own route.

The markup checked here is the tests' fixture template's
(tests/fixtures/site_template), chosen by its key "fixture"; the catalogue's
templates are their builders' to test (tests/test_site_templates.py)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.closeout.models import EventStatus
from app.api.sites import registry
from tests.conftest import SITE_FIXTURE, auth_header

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
    assert s["template"] == "classic" and s["published_at"] is None and s["stale"] is False
    assert s["default_headline"] == "Build things that think." and s["default_subline"].startswith("Hack nights, build weekends")
    assert s["available"]["team"] is False and s["available"]["past"] is True and s["available"]["photos"] is False
    assert s["url"] == f"https://{club.slug}.myotherapp.in" and s["club_colour"] == "#17a58f"
    assert [t["key"] for t in s["templates"]] == [*registry.CATALOGUE, "fixture"]
    assert s["limits"] == {"images": 20, "image_mb": 10} and s["images"] == [] and s["adjusted"] is None
    fixture = next(t for t in s["templates"] if t["key"] == "fixture")
    assert fixture["sections"][0] == {"key": "upcoming", "label": "Events", "default_title": "Upcoming events", "hideable": True}
    assert {"key": "hero.cover", "label": "Cover photo", "size": [1440, 300], "section": "hero"} in fixture["slots"]
    assert fixture["accent"] == {"default": "#2b4bff"} and fixture["layout"] == "single"
    assert fixture["fixed"] == {"top": ["nav", "hero"], "side": [], "bottom": ["footer"]}
    assert fixture["thumb_url"].startswith("https://api.myotherapp.in/static/site-templates/fixture.png?v=")
    assert fixture["thumb_mobile_url"].startswith("https://api.myotherapp.in/static/site-templates/fixture-mobile.png?v=")
    # Nothing public yet.
    assert (await client.get(f"/api/v1/sites/{club.slug}")).status_code == 404

    # A preview of unsaved choices: the template, the headline typed, the upcoming event, never the draft.
    p = (await client.post("/api/v1/clubs/site/preview", json={"template": "fixture", "headline": "Make <things> that think"}, headers=h)).json()["html"]
    assert p.startswith("<!doctype html>") and "Make &lt;things&gt; that think" in p and "Hack Night 04: Edge AI" in p and "Secret draft" not in p
    assert "Intro to KiCad" in p and "Lab 3, COEP · ₹100" in p and "Pune" in p and "Upcoming events" in p
    assert (await client.get("/api/v1/clubs/site", headers=h)).json()["headline"] is None  # a preview saves nothing

    # Saved: the fixture template, past events hidden, a subline, a title of the club's own.
    s = (await client.patch("/api/v1/clubs/site", json={
        "template": "fixture", "subline": "  Pune's makers.  ",
        "layout": {"hidden": ["past"], "titles": {"upcoming": "What's next"}},
    }, headers=h)).json()
    assert s["template"] == "fixture" and s["subline"] == "Pune's makers." and s["sections"]["past"] is False and s["sections"]["upcoming"] is True
    assert s["titles"]["upcoming"] == "What's next" and s["layout"]["titles"] == {"upcoming": "What's next"}
    assert (await client.patch("/api/v1/clubs/site", json={"template": "fancy"}, headers=h)).status_code == 422

    # Publish: the page goes to the public address, with a cache header and the CSP, and the console says it is current.
    s = (await client.post("/api/v1/clubs/site/publish", headers=h)).json()
    assert s["published_at"] is not None and s["stale"] is False and s["tracking"]["code"]
    r = await client.get(f"/api/v1/sites/{club.slug}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and r.headers["cache-control"] == "public, max-age=300"
    assert "style-src-elem 'sha256-" in r.headers["content-security-policy"]
    html = r.text
    assert "What&#39;s next" in html and "Past events" not in html and "Intro to KiCad" not in html and "Pune&#39;s makers." in html
    assert '<a href="#events">Events</a>' in html and '<a href="#past">' not in html and "Made with MyOtherApp" in html and "application/ld+json" in html
    assert f'<link rel="canonical" href="https://{club.slug}.myotherapp.in">' in html
    # A change after publishing makes the published page stale; a new publish catches up.
    assert (await client.patch("/api/v1/clubs/site", json={"layout": {"hidden": []}}, headers=h)).json()["stale"] is True
    assert "Intro to KiCad" not in (await client.get(f"/api/v1/sites/{club.slug}")).text
    await client.post("/api/v1/clubs/site/publish", headers=h)
    assert "Intro to KiCad" in (await client.get(f"/api/v1/sites/{club.slug}")).text

    # Images come through the page's own route, which only ever points into the club's and the events' picture folders.
    assert (await client.get(f"/api/v1/sites/{club.slug}/i", params={"k": "clubs/logos/abc.medium.png"})).status_code == 302
    assert (await client.get(f"/api/v1/sites/{club.slug}/i", params={"k": "clubs/site/v-abc.w960.png"})).status_code == 302
    assert (await client.get(f"/api/v1/sites/{club.slug}/i", params={"k": "events/speakers/v-abc.medium.png"})).status_code == 302
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


async def test_the_templates_pictures_are_served_from_their_packages(client, world):
    r = await client.get("/static/site-templates/fixture.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == (SITE_FIXTURE / "thumb.png").read_bytes()
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    m = await client.get("/static/site-templates/fixture-mobile.png")
    assert m.status_code == 200 and m.content == (SITE_FIXTURE / "thumb-mobile.png").read_bytes()
    for bad in ("fixture.css", "nope.png", "..%2F..%2Fapp.png", "FIXTURE.png", "fixture-desktop.png", "fixture/thumb.png"):
        assert (await client.get(f"/static/site-templates/{bad}")).status_code == 404, bad


@pytest.mark.parametrize("template", list(registry.packages()))
async def test_every_template_shows_what_the_club_has(client, session, world, make_event, template):
    """Whatever the design: the platform's head and beacon, the events with
    the site's code on their links, and each kind of thing the club has in
    the sections the template declares. A template that drops one fails here."""
    import re

    from app.api.clubs.models import ClubPartners, ClubTeamMembers
    from app.api.events.models import EventSpeakers
    from app.api.sites.models import ClubSiteImages
    from app.config import settings

    h, club = world["h"], world["club"]
    session.add_all([
        ClubTeamMembers(club_id=club.id, name="Asha Rao", role="Lead"),
        ClubPartners(club_id=club.id, name="Acme Labs", role="Sponsor"),
        EventSpeakers(event_id=world["soon"].id, name="Dr. Meera Iyer", designation="Researcher · IISc"),
        ClubSiteImages(club_id=club.id, image=f"clubs/site/v-{'a' * 32}.png", alt="Demo night", position=0, width=1200, height=800),
    ])
    await session.commit()
    pkg = registry.get(template)
    assert (await client.patch("/api/v1/clubs/site", json={"template": template}, headers=h)).status_code == 200
    code = (await client.post("/api/v1/clubs/site/publish", headers=h)).json()["tracking"]["code"]
    html = (await client.get(f"/api/v1/sites/{club.slug}")).text
    site = f"https://{club.slug}.myotherapp.in"

    missing = []

    def has(what: str, ok: bool) -> None:
        if not ok:
            missing.append(what)

    has("canonical", f'<link rel="canonical" href="{site}">' in html)
    has("og:url", f'<meta property="og:url" content="{site}">' in html)
    has("visit beacon", f'<meta name="moa:visit" content="{settings.PUBLIC_API_URL}/api/v1/sites/{club.slug}/visit">' in html and html.count("<script>") == 1)
    has("headline", "Build things that think." in html)
    soon = world["soon"].slug
    links = re.findall(rf'<a\b[^>]*href="{re.escape(settings.PUBLIC_SITE_URL)}/events/{soon}[^"]*"[^>]*>', html)
    has("the upcoming event", "Hack Night 04: Edge AI" in html)
    has("its link with ?sref=", bool(links) and all(f"?sref={code}" in a for a in links))
    has("its link to the whole tab", bool(links) and all('target="_top"' in a for a in links))
    has("follow / join link", f"{settings.PUBLIC_SITE_URL}/clubs/{club.slug}?follow=1" in html)
    sections = set(pkg.movable) | set(pkg.fixed_top) | set(pkg.fixed_side) | set(pkg.fixed_bottom)
    if "past" in sections:
        has("the past event", "Intro to KiCad" in html)
    if "team" in sections:
        has("the team", "Asha Rao" in html)
    if "sponsors" in sections:
        has("the sponsors", "Acme Labs" in html)
    if "speakers" in sections:
        has("the speakers", "Dr. Meera Iyer" in html)
    if "photos" in sections and pkg.photo_slots:
        has("the library photo", f"v-{'a' * 32}" in html and 'alt="Demo night"' in html)
    if "about" in sections:
        has("the about", "Hack nights, build weekends" in html)
    assert not missing, f"{template} drops: {', '.join(missing)}"


def test_the_site_address_is_the_clubs_subdomain_when_the_slug_can_be_one():
    from types import SimpleNamespace

    from app.api.sites.service import public_url

    assert public_url(SimpleNamespace(slug="circuit-society-a1b2c3")) == "https://circuit-society-a1b2c3.myotherapp.in"
    # Longer than a DNS label allows, or not a label at all: the public site's club page.
    long = "a" * 70
    assert public_url(SimpleNamespace(slug=long)) == f"https://myotherapp.in/clubs/{long}"
    assert public_url(SimpleNamespace(slug="-a1b2c3")) == "https://myotherapp.in/clubs/-a1b2c3"
