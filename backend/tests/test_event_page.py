"""The public site's move to myotherapp.in: an event's own page by slug (or
the old id), the sitemap, and every public address built as /events/{slug}
on PUBLIC_SITE_URL."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.agenda.models import AgendaItems
from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubTeamMembers, ClubUsersLink
from app.api.events.models import EventCategories, EventInterestsLink, Events, EventSpeakers
from app.api.interests.models import InterestCategory, Interests
from app.config import AppConfig, settings
from app.core import public_urls
from tests.conftest import auth_header

API = "/api/v1/discover"
SITE = "https://myotherapp.in"
JOIN = "https://meet.example.com/very-secret-join-link"

EVENT_KEYS = {
    "id", "slug", "name", "about", "poster", "poster_thumb", "starts_at", "ends_at", "duration_hours", "timezone",
    "is_online", "location_name", "location_link", "lat", "lng", "has_fee", "fee", "has_prize", "prize_amount",
    "capacity", "going", "seats_left", "reg_opens_at", "reg_closes_at", "status", "registration_open",
    "category", "interests", "guidelines", "contact", "url",
    # For the site's feeds: the description as one plain line, and the dates.
    "summary", "created_at", "updated_at",
    # The topics of its interests (Topics).
    "topics",
    # Set when the club cancelled the event (club_ops): the reason and when.
    "cancellation",
}


def _ids(items):
    return [x["id"] for x in items]


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    now = datetime.now(timezone.utc)
    d = timedelta(days=1)
    tech, music = EventCategories(name="EP Tech"), EventCategories(name="EP Music")
    session.add_all([tech, music])
    await session.flush()

    owner_a, a = await make_club(name="Circuit Society", email="ep-a@example.com")
    a.tagline, a.location_name = "Build things that think.", "Bengaluru"
    owner_b, b = await make_club(name="Rhythm Collective", email="ep-b@example.com")
    owner_c, c = await make_club(name="Ghost Club", email="ep-c@example.com", is_verified=False)
    owner_d, quiet_club = await make_club(name="Quiet Club", email="ep-d@example.com")
    owner_x, gone_club = await make_club(name="Gone Club", email="ep-x@example.com")
    gone_club.is_deleted = True

    asha = ClubTeamMembers(club_id=a.id, name="Asha Rao", role="President", instagram="asha", sort_order=0)
    vikram = ClubTeamMembers(club_id=a.id, name="Vikram Shah", role="Tech lead", sort_order=1)
    session.add_all([asha, vikram, ClubTeamMembers(club_id=a.id, name="Left Already", role="Ex", sort_order=2, is_deleted=True)])
    session.add_all([ClubTeamMembers(club_id=b.id, name=f"Member {i}", role="Crew", sort_order=i) for i in range(9)])

    async def ev(club, owner, name, start, category, **kw):
        kw.setdefault("lifecycle", EventStatus.published)
        return await make_event(club=club, created_by=owner, name=name, event_datetime=start, category_id=category.id, **kw)

    e = dict(
        main=await ev(
            a, owner_a, "Hack Night", now + 5 * d, tech, duration=2.5, about="All night, all hands.",
            location_name="Koramangala Hub", location_link="https://maps.google.com/?q=12.93,77.62", lat=12.93, lng=77.62,
            url=JOIN, has_fee=True, reg_fee=150, has_prize=True, prize_amount=5000, max_participants=3,
            contact_name="Asha", contact_email="asha@circuit.example", contact_phone="+91 90000 00000",
            event_guidelines="Bring a laptop.", reg_startdate=now - d, reg_enddate=now + 4 * d,
        ),
        soon=await ev(a, owner_a, "Solder Sprint", now + 2 * d, tech),
        later=await ev(a, owner_a, "Synth Jam", now + 10 * d, music),
        draft=await ev(a, owner_a, "Secret Draft", now + 3 * d, tech, lifecycle=EventStatus.draft),
        deleted=await ev(a, owner_a, "Cancelled Meetup", now + 3 * d, tech, is_deleted=True),
        closed_early=await ev(a, owner_a, "Shut Early", now + d, tech, lifecycle=EventStatus.closed),
        over=await ev(a, owner_a, "Intro to KiCad", now - 5 * d, tech),
        old=await ev(a, owner_a, "Last Year's Fest", now - 400 * d, tech, lifecycle=EventStatus.closed),
        other_tech=await ev(b, owner_b, "Beat Lab", now + d, tech),
        other_music=await ev(b, owner_b, "Mysuru Jam", now + d, music),
        quiet=await ev(quiet_club, owner_d, "Quiet Hack", now + 6 * d, tech),
        ghost=await ev(c, owner_c, "Ghost Party", now + d, tech),
        gone=await ev(gone_club, owner_x, "Gone Gig", now + d, tech),
    )
    main = e["main"]

    u1, u2, u3 = [await make_user(email=f"ep-u{i}@example.com") for i in (1, 2, 3)]
    reg = await make_registration(event=main, user=u1)
    await make_registration(event=main, user=u2)
    await make_registration(event=main, user=u3, is_deleted=True)
    session.add_all([
        ClubUsersLink(club_id=a.id, user_id=u1.id, is_following=True),
        ClubUsersLink(club_id=a.id, user_id=u2.id, is_following=True),
        ClubUsersLink(club_id=a.id, user_id=u3.id, is_following=False),
    ])

    rao = EventSpeakers(event_id=main.id, name="Dr. Rao", designation="Professor", bio="Builds robots.", link="https://rao.example", display_order=1)
    meera = EventSpeakers(event_id=main.id, name="Meera", display_order=0)
    session.add_all([rao, meera, EventSpeakers(event_id=main.id, name="Withdrew", display_order=2, is_deleted=True)])
    session.add_all([
        AgendaItems(event_id=main.id, starts_at=main.event_datetime, ends_at=main.event_datetime + timedelta(hours=1), title="Kickoff", speaker="Meera", sort_order=0),
        AgendaItems(event_id=main.id, starts_at=main.event_datetime + timedelta(hours=1), title="Build", sort_order=1),
    ])

    icat = InterestCategory(name="EP interests")
    session.add(icat)
    await session.flush()
    robotics, ai, dropped = Interests(name="EP Robotics", category_id=icat.id), Interests(name="EP AI", category_id=icat.id), Interests(name="EP Dropped", category_id=icat.id)
    session.add_all([robotics, ai, dropped])
    await session.flush()
    for i in (robotics, ai):
        session.add(EventInterestsLink(event_id=main.id, interest_id=i.id))
        await session.flush()
    session.add(EventInterestsLink(event_id=main.id, interest_id=dropped.id, is_deleted=True))
    await session.commit()
    return dict(
        now=now, a=a, b=b, c=c, quiet_club=quiet_club, e=e, tech=tech, music=music, asha=asha, vikram=vikram, rao=rao, meera=meera,
        users=(u1, u2, u3), reg=reg, h_a=auth_header(await mint_token(owner_a)),
    )


# ─── The page ─────────────────────────────────────────────────────────────


async def test_the_page_by_slug(client, session, world):
    e, a, main = world["e"], world["a"], world["e"]["main"]
    main.poster = "clubs/logos/hack.png"
    await session.commit()
    await session.refresh(main)

    r = await client.get(f"{API}/events/{main.slug}")
    assert r.status_code == 200, r.text
    page = r.json()
    assert set(page) == {"event", "club", "speakers", "agenda", "team", "team_source", "more_from_club", "similar"}
    ev = page["event"]
    assert set(ev) == EVENT_KEYS
    assert (ev["id"], ev["slug"], ev["name"], ev["about"]) == (main.id, main.slug, "Hack Night", "All night, all hands.")
    assert "hack.png" in ev["poster"] and "hack.medium.png" not in ev["poster"] and "hack.medium.png" in ev["poster_thumb"]
    start = datetime.fromisoformat(ev["starts_at"])
    assert start == main.event_datetime and datetime.fromisoformat(ev["ends_at"]) == start + timedelta(hours=2.5)
    assert ev["duration_hours"] == 2.5 and ev["timezone"] == "Asia/Kolkata"
    assert (ev["is_online"], ev["location_name"], ev["location_link"], ev["lat"], ev["lng"]) == (False, "Koramangala Hub", "https://maps.google.com/?q=12.93,77.62", 12.93, 77.62)
    assert (ev["has_fee"], ev["fee"], ev["has_prize"], ev["prize_amount"]) == (True, 150.0, True, 5000.0)
    assert (ev["capacity"], ev["going"], ev["seats_left"]) == (3, 2, 1)
    assert datetime.fromisoformat(ev["reg_opens_at"]) == main.reg_startdate and datetime.fromisoformat(ev["reg_closes_at"]) == main.reg_enddate
    assert ev["status"] == "upcoming" and ev["registration_open"] is True
    assert ev["category"] == {"id": world["tech"].id, "name": "EP Tech"} and ev["interests"] == ["EP Robotics", "EP AI"]
    assert [t["name"] for t in ev["topics"]] == ["EP interests"]
    assert ev["guidelines"] == "Bring a laptop."
    assert ev["contact"] == {"name": "Asha", "email": "asha@circuit.example", "phone": "+91 90000 00000"}
    assert ev["url"] == f"{SITE}/events/{main.slug}"

    assert page["club"] == {"id": a.id, "name": "Circuit Society", "slug": a.slug, "logo": None, "tagline": "Build things that think.", "followers": 2, "location_name": "Bengaluru",
                            "whatsapp_updates": False}
    assert page["speakers"] == [
        {"id": world["meera"].id, "name": "Meera", "designation": None, "photo": None, "bio": None, "link": None},
        {"id": world["rao"].id, "name": "Dr. Rao", "designation": "Professor", "photo": None, "bio": "Builds robots.", "link": "https://rao.example"},
    ]
    # The agenda exactly as the event's own agenda endpoint gives it.
    public = (await client.get(f"/api/v1/events/{main.id}/agenda")).json()
    assert page["agenda"] == {"timezone": public["timezone"], "items": public["items"]}
    assert [i["title"] for i in page["agenda"]["items"]] == ["Kickoff", "Build"]

    # Other things on: soonest first, never the event itself, nothing hidden.
    assert _ids(page["more_from_club"]) == [e["soon"].id, e["later"].id]
    assert _ids(page["similar"]) == [e["other_tech"].id, e["quiet"].id]
    assert main.id not in _ids(page["more_from_club"]) + _ids(page["similar"])
    assert page["more_from_club"][0]["slug"] == e["soon"].slug and page["more_from_club"][0]["url"] == f"{SITE}/events/{e['soon'].slug}"

    # Never the joining link, never who registered.
    assert JOIN not in r.text and "meet.example.com" not in r.text
    for u in world["users"]:
        assert u.email not in r.text
    assert world["reg"].ticket_id not in r.text


async def test_an_online_event_shows_no_venue_link(client, session, world):
    # A club may leave the meeting link in the map field; the public page must not carry it.
    soon = world["e"]["soon"]
    soon.is_online, soon.location_link, soon.lat, soon.lng = True, JOIN, 12.93, 77.62
    await session.commit()
    r = await client.get(f"{API}/events/{soon.slug}")
    assert r.status_code == 200, r.text
    ev = r.json()["event"]
    assert (ev["is_online"], ev["location_link"], ev["lat"], ev["lng"]) == (True, None, None, None)
    assert JOIN not in r.text


async def test_the_page_by_id_is_the_same_page(client, world):
    main = world["e"]["main"]

    def stable(page):
        page["event"].pop("poster")
        page["event"].pop("poster_thumb")
        return page

    by_slug = (await client.get(f"{API}/events/{main.slug}")).json()
    by_id = await client.get(f"{API}/events/{main.id}")
    assert by_id.status_code == 200, by_id.text
    assert stable(by_id.json()) == stable(by_slug)


async def test_what_the_public_cannot_see_is_not_found(client, world):
    e = world["e"]
    refs = [e["draft"].slug, str(e["draft"].id), e["deleted"].slug, str(e["deleted"].id), e["closed_early"].slug,
            e["ghost"].slug, e["gone"].slug, "no-such-event", "0", "99999999999999999999"]
    for ref in refs:
        r = await client.get(f"{API}/events/{ref}")
        assert r.status_code == 404, (ref, r.status_code, r.text)


async def test_an_event_that_is_over_keeps_its_page(client, world):
    for key in ("over", "old"):
        r = await client.get(f"{API}/events/{world['e'][key].slug}")
        assert r.status_code == 200, r.text
        assert r.json()["event"]["status"] == "ended" and r.json()["event"]["registration_open"] is False


async def test_registration_is_open_only_inside_the_window_and_below_capacity(client, session, world, make_user, make_registration):
    e, now = world["e"], world["now"]
    soon = e["soon"]
    soon.reg_startdate = now + timedelta(hours=12)
    await session.commit()
    ev = (await client.get(f"{API}/events/{soon.slug}")).json()["event"]
    assert ev["status"] == "upcoming" and ev["registration_open"] is False and ev["capacity"] is None and ev["seats_left"] is None

    late = await make_user(email="ep-late@example.com")
    await make_registration(event=e["main"], user=late)
    await session.commit()
    ev = (await client.get(f"{API}/events/{e['main'].slug}")).json()["event"]
    assert ev["status"] == "full" and ev["registration_open"] is False and ev["going"] == 3 and ev["seats_left"] == 0


async def test_the_team_is_the_events_organisers_else_the_clubs_else_nobody(client, world):
    e, asha, vikram = world["e"], world["asha"], world["vikram"]
    page = (await client.get(f"{API}/events/{e['main'].slug}")).json()
    assert page["team_source"] == "club"
    assert page["team"] == [
        {"name": "Asha Rao", "role": "President", "photo": None, "links": {"instagram": "https://instagram.com/asha", "linkedin": None, "x": None, "website": None}},
        {"name": "Vikram Shah", "role": "Tech lead", "photo": None, "links": {"instagram": None, "linkedin": None, "x": None, "website": None}},
    ]

    r = await client.put(f"/api/v1/events/{e['main'].id}/organisers", json={"members": [{"member_id": vikram.id, "role": "Event lead"}, {"member_id": asha.id}]}, headers=world["h_a"])
    assert r.status_code == 200, r.text
    page = (await client.get(f"{API}/events/{e['main'].slug}")).json()
    assert page["team_source"] == "event"
    # Without a role at the event, their club role.
    assert [(t["name"], t["role"]) for t in page["team"]] == [("Vikram Shah", "Event lead"), ("Asha Rao", "President")]
    # Another event of the same club still shows the club's team.
    assert (await client.get(f"{API}/events/{e['soon'].slug}")).json()["team_source"] == "club"

    # The club's team, at most eight of it.
    other = (await client.get(f"{API}/events/{e['other_tech'].slug}")).json()
    assert other["team_source"] == "club" and [t["name"] for t in other["team"]] == [f"Member {i}" for i in range(8)]
    # A club with no team: nobody.
    quiet = (await client.get(f"{API}/events/{e['quiet'].slug}")).json()
    assert quiet["team_source"] == "none" and quiet["team"] == []


async def test_more_and_similar_never_hold_the_event_itself(client, world):
    e = world["e"]
    soon = (await client.get(f"{API}/events/{e['soon'].slug}")).json()
    assert _ids(soon["more_from_club"]) == [e["main"].id, e["later"].id]
    assert _ids(soon["similar"]) == [e["other_tech"].id, e["quiet"].id]
    beat = (await client.get(f"{API}/events/{e['other_tech'].slug}")).json()
    assert e["other_tech"].id not in _ids(beat["more_from_club"]) + _ids(beat["similar"])
    assert _ids(beat["more_from_club"]) == [e["other_music"].id]
    # Up to four from other clubs, in the same category: Circuit Society's and the quiet club's tech events.
    assert _ids(beat["similar"]) == [e["soon"].id, e["main"].id, e["quiet"].id]


# ─── The card and the sitemap ─────────────────────────────────────────────


async def test_an_event_card_carries_its_slug_and_page_address(client, world):
    items = (await client.get(f"{API}/events", params={"limit": 60})).json()["items"]
    assert items
    by_id = {x.id: x for x in world["e"].values()}
    for card in items:
        assert card["slug"] == by_id[card["id"]].slug and card["url"] == f"{SITE}/events/{card['slug']}"


async def test_the_sitemap_lists_what_the_public_can_open(client, world):
    e, now = world["e"], world["now"]
    r = await client.get(f"{API}/sitemap")
    assert r.status_code == 200, r.text
    body = r.json()
    events = {x["slug"]: x for x in body["events"]}
    shown = {e[k].slug for k in ("main", "soon", "later", "over", "other_tech", "other_music", "quiet")}
    assert shown <= set(events)
    for k in ("draft", "deleted", "closed_early", "old", "ghost", "gone"):
        assert e[k].slug not in events, k
    assert abs(datetime.fromisoformat(events[e["main"].slug]["updated_at"]) - now) < timedelta(minutes=5)
    clubs = {x["slug"] for x in body["clubs"]}
    assert {world["a"].slug, world["b"].slug, world["quiet_club"].slug} <= clubs and world["c"].slug not in clubs
    assert all(set(x) == {"slug", "updated_at"} for x in body["events"] + body["clubs"])


async def test_the_sitemap_is_rate_limited(client):
    h = {"X-Forwarded-For": "203.0.113.70"}
    for _ in range(120):
        assert (await client.get(f"{API}/sitemap", headers=h)).status_code == 200
    assert (await client.get(f"{API}/sitemap", headers=h)).status_code == 429


# ─── Every public address on the site ─────────────────────────────────────


def test_public_addresses_are_built_on_the_site(monkeypatch):
    assert settings.PUBLIC_SITE_URL == SITE and settings.PUBLIC_CLUB_URL == f"{SITE}/clubs/{{slug}}"
    assert public_urls.event_url("mutate-c5d68d") == f"{SITE}/events/mutate-c5d68d"
    assert public_urls.ticket_url("MOA-1") == f"{SITE}/tickets/MOA-1"
    assert public_urls.certificate_url("MOA-1") == f"{SITE}/certificates/MOA-1"
    monkeypatch.setattr(settings, "PUBLIC_SITE_URL", "https://staging.example/")
    assert public_urls.event_url("x") == "https://staging.example/events/x"


async def test_ticket_and_certificate_links_point_at_the_site(client, session, world, mint_token):
    main, reg = world["e"]["main"], world["reg"]
    h = auth_header(await mint_token(world["users"][0]))
    info = (await client.get(f"/api/v1/events/info/{main.id}", headers=h)).json()
    assert info["ticket_url"] == f"{SITE}/tickets/{reg.ticket_id}" and info["certificate_url"] is None
    reg.is_attended = True
    await session.commit()
    info = (await client.get(f"/api/v1/events/info/{main.id}", headers=h)).json()
    assert info["certificate_url"] == f"{SITE}/certificates/{reg.ticket_id}"
    listed = (await client.get("/api/v1/events/list", params={"is_registered": "true"}, headers=h)).json()["items"]
    row = next(x for x in listed if x["id"] == main.id)
    assert row["ticket_url"] == f"{SITE}/tickets/{reg.ticket_id}" and row["certificate_url"] == f"{SITE}/certificates/{reg.ticket_id}"


async def test_a_tracking_link_lands_on_the_events_page_by_slug(client, world):
    main, h = world["e"]["main"], world["h_a"]
    made = await client.post(f"/api/v1/events/{main.id}/links", json={"name": "Poster", "utm_source": "poster", "utm_medium": "print"}, headers=h)
    assert made.status_code == 201, made.text
    link = made.json()
    landing = f"{SITE}/events/{main.slug}?utm_source=poster&utm_medium=print&ref={link['code']}"
    assert link["destination"] == landing
    summary = (await client.get(f"/api/v1/events/{main.id}/links", headers=h)).json()
    assert [x["destination"] for x in summary["links"]] == [landing]
    for path in (f"/api/v1/events/{main.id}/links/overview", "/api/v1/clubs/links/overview"):
        rows = (await client.get(path, headers=h)).json()["links"]
        assert next(x for x in rows if x["id"] == link["id"])["destination"] == landing, path
    for path in (f"/api/v1/events/{main.id}/links/{link['id']}/detail", f"/api/v1/clubs/links/{link['id']}/detail"):
        r = await client.get(path, headers=h)
        assert r.status_code == 200 and r.json()["destination"] == landing, (path, r.text)
    opened = await client.get(f"/l/{link['code']}")
    assert opened.status_code == 302 and opened.headers["location"] == landing + "&via=l"


# ─── CORS ─────────────────────────────────────────────────────────────────


def test_cors_always_lets_the_site_in_once():
    cfg = AppConfig(CORS_ORIGINS="https://clubs.example, https://myotherapp.in ,", PUBLIC_SITE_URL="https://myotherapp.in/")
    assert cfg.cors_origins == ["https://clubs.example", "https://myotherapp.in", "https://www.myotherapp.in", "https://events.myotherapp.in"]
    cfg = AppConfig(CORS_ORIGINS=["https://www.example.org"], PUBLIC_SITE_URL="https://www.example.org")
    assert cfg.cors_origins == ["https://www.example.org", "https://example.org", "https://events.myotherapp.in"]


async def test_the_site_may_call_the_api_from_the_browser(client):
    for origin in ("https://myotherapp.in", "https://www.myotherapp.in", "https://events.myotherapp.in", "http://testserver"):
        r = await client.options(f"{API}/sitemap", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin, origin
    stranger = await client.options(f"{API}/sitemap", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert stranger.headers.get("access-control-allow-origin") is None
