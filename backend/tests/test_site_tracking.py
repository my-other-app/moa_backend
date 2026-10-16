"""A club's website, tracked: publishing gives it its own link, its visits are
counted once and by where they came from, its event buttons carry its code so
the sign-ups they bring are credited, and the club reads it all back."""
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.events.models import EventRegistrationsLink
from app.api.links.models import TrackingClicks, TrackingLinks
from app.config import settings
from app.core.email import email as email_mod
from app.core.rate_limit import limiter
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0 Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) Version/17.5 Mobile/15E148 Safari/604.1"


@pytest.fixture
async def world(client, session, make_club, make_event, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None)
    owner, club = await make_club(name="Circuit Society", email="site-track@example.com")
    _, other = await make_club(name="Rivals", email="site-track-rivals@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=NOW + timedelta(days=9), location_name="Lab 3")
    await session.commit()
    return dict(club=club, other=other, event=event, h=auth_header(await mint_token(owner)), session=session)


async def _publish(client, world):
    r = await client.post("/api/v1/clubs/site/publish", headers=world["h"])
    assert r.status_code == 200, r.text
    return r.json()


async def _visit(client, slug, *, ua=CHROME, ip="49.36.1.1", **body):
    import json

    r = await client.post(f"/api/v1/sites/{slug}/visit", content=json.dumps(body), headers={"content-type": "text/plain", "user-agent": ua, "x-forwarded-for": ip})
    assert r.status_code == 204, r.text


async def _register(client, event_id, ref):
    payload = {"full_name": "Guest", "email": f"g-{uuid.uuid4().hex[:8]}@example.com", "phone": f"9{uuid.uuid4().int % 10**9:09d}", "ref": ref}
    r = await client.post(f"/api/v1/events/registration/{event_id}/register", json=payload)
    assert r.status_code == 200, r.text


async def test_publishing_gives_the_site_its_link_and_its_buttons_carry_the_code(client, world, session):
    site = await _publish(client, world)
    code = site["tracking"]["code"]
    assert site["tracking"]["short_url"].endswith(f"/l/{code}")
    links = (await session.scalars(select(TrackingLinks).where(TrackingLinks.club_id == world["club"].id))).all()
    assert [(l.kind, l.name, l.event_id) for l in links] == [("website", "Your website", None)]
    # Publishing again keeps the one link.
    assert (await _publish(client, world))["tracking"]["code"] == code

    html = (await client.get(f"/api/v1/sites/{world['club'].slug}")).text
    # The event button leaves the frame for the whole tab, carrying the site's code as a weak ref.
    assert re.search(rf'href="{re.escape(settings.PUBLIC_SITE_URL)}/events/{world["event"].slug}\?sref={code}" target="_top">Register</a>', html)
    assert f'data-visit="http' in html and f"/api/v1/sites/{world['club'].slug}/visit" in html
    # Every way out of the frame goes to the whole tab.
    assert f'href="{settings.PUBLIC_SITE_URL}/clubs/' in html and html.count('target="_top"') >= 3


async def test_a_site_published_before_tracking_gets_its_link_on_the_next_look(client, world, session):
    from app.api.sites.models import ClubSites

    session.add(ClubSites(club_id=world["club"].id, published_at=NOW, html="<!doctype html><p>old</p>", sections={}))
    await session.commit()
    site = (await client.get("/api/v1/clubs/site", headers=world["h"])).json()
    assert site["tracking"] and site["tracking"]["code"]


async def test_visits_are_counted_once_by_where_they_came_from(client, world, session):
    await _publish(client, world)
    slug = world["club"].slug
    bio = (await client.post("/api/v1/clubs/links", json={"name": "Instagram bio", "utm_source": "instagram", "utm_medium": "social"}, headers=world["h"])).json()
    rival = (await client.post("/api/v1/clubs/links", json={"name": "Rival", "utm_source": "x", "utm_medium": "social"}, headers=auth_header("nope")))

    await _visit(client, slug, referrer="https://www.google.com/search?q=circuit")
    await _visit(client, slug, referrer="https://www.google.com/search?q=circuit")  # the same person reloading: once
    await _visit(client, slug, ip="49.36.2.2", ua=IPHONE, referrer="https://l.instagram.com/")
    await _visit(client, slug, ip="49.36.3.3", referrer="javascript:alert(1)")  # not a page: counted, referrer dropped
    # Through the club's own short link: the link counted it on the way, the site does not again.
    opened = await client.get(f"/l/{bio['code']}", headers={"user-agent": CHROME, "x-forwarded-for": "49.36.4.4"})
    assert opened.status_code == 302 and "via=l" in opened.headers["location"] and f"ref={bio['code']}" in opened.headers["location"]
    await _visit(client, slug, ip="49.36.4.4", ref=bio["code"], via="l")
    # The full link pasted somewhere (no short link on the way): counted once, for that link.
    await _visit(client, slug, ip="49.36.5.5", ref=bio["code"])
    assert rival.status_code == 401
    # A code that is not this club's is just a visit.
    await _visit(client, slug, ip="49.36.6.6", ref="zzzzzzzz")
    # An unknown club or one with no site counts nothing and says nothing.
    await _visit(client, "no-such-club")
    await _visit(client, world["other"].slug)

    clicks = (await session.scalars(select(TrackingClicks))).all()
    own = (await session.scalars(select(TrackingLinks).where(TrackingLinks.kind == "website"))).one()
    by_link = {}
    for c in clicks:
        by_link[c.link_id] = by_link.get(c.link_id, 0) + 1
    assert by_link[own.id] == 4 and by_link[uuid.UUID(bio["id"])] == 2
    assert {c.referrer for c in clicks if c.link_id == own.id} == {"https://www.google.com/search?q=circuit", "https://l.instagram.com/", None}


async def test_the_site_brings_sign_ups_and_the_club_reads_it_back(client, world, session):
    site = await _publish(client, world)
    slug, code, event = world["club"].slug, site["tracking"]["code"], world["event"]
    for i in range(4):
        await _visit(client, slug, ip=f"10.0.0.{i}", referrer="https://www.google.com/" if i < 3 else None, ua=IPHONE if i == 0 else CHROME)
    await _register(client, event.id, code)
    await _register(client, event.id, code)
    reg = (await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id))).first()
    reg.is_attended = True
    await session.commit()

    a = (await client.get("/api/v1/clubs/site/analytics?days=30", headers=world["h"])).json()
    assert a["published"] is True and a["link"]["code"] == code
    assert (a["visits"], a["visitors"], a["signups"], a["came"], a["rate"]) == (4, 4, 2, 1, 50.0)
    assert a["sources"] == [{"kind": "website", "link_id": a["link"]["id"], "name": "Found the site on their own", "visits": 4, "signups": 2, "rate": 50.0}]
    assert a["referrers"][0] == {"referrer": "Google", "visits": 3}
    assert {d["device"] for d in a["devices"]} == {"phone", "desktop"}
    assert a["events"] == [{"event_id": event.id, "name": "Hack Night", "signups": 2, "came": 1}]
    assert len(a["series"]) == 31 and sum(p["visits"] for p in a["series"]) == 4 and sum(p["signups"] for p in a["series"]) == 2

    # The Tracking links page shows the site's link as its own row, and keeps it.
    o = (await client.get("/api/v1/clubs/links/overview", headers=world["h"])).json()
    row = next(r for r in o["links"] if r["kind"] == "website")
    assert row["target"]["kind"] == "website" and row["conversion_noun"] == "sign-up" and row["clicks"] == 4 and row["conversions"] == 2
    gone = await client.delete(f"/api/v1/clubs/links/{row['id']}", headers=world["h"])
    assert gone.status_code == 409
