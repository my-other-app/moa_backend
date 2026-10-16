"""Tracking links: made, shared, opened, and reconciled with the sign-ups and
follows they brought — for one event, and for the club's own page."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.clubs.models import ClubUsersLink
from app.api.events.models import EventRegistrationsLink
from app.api.links.models import TrackingClicks
from app.config import settings
from app.core.email import email as email_mod
from tests.conftest import auth_header


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, mint_token, monkeypatch):
    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None)
    monkeypatch.setattr(email_mod, "send_email", lambda **kwargs: None)
    owner, club = await make_club(name="Yukthi", email="links@example.com")
    other_owner, other = await make_club(name="Rivals", email="rivals-links@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=9))
    fan = await make_user(email="fan-links@example.com")
    await session.commit()
    return {
        "owner": owner, "club": club, "other": other, "event": event, "fan": fan,
        "h": auth_header(await mint_token(owner)), "rival": auth_header(await mint_token(other_owner)), "fan_h": auth_header(await mint_token(fan)),
    }


def _event_url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/links{path}"


async def _register(client, event_id, ref=None, headers=None, email=None):
    # Each guest is a new account, so each needs a phone of its own.
    payload = {"full_name": "Guest Attendee", "email": email or f"guest-{uuid.uuid4().hex[:8]}@example.com", "phone": f"9{uuid.uuid4().int % 10**9:09d}"}
    if ref:
        payload["ref"] = ref
    r = await client.post(f"/api/v1/events/registration/{event_id}/register", json=payload, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_link_is_made_shared_and_opened(client, session, world):
    h, event = world["h"], world["event"]
    made = await client.post(_event_url(world), json={"name": "Instagram story, Friday", "utm_source": "Instagram", "utm_medium": "social", "utm_campaign": "Hack Night"}, headers=h)
    assert made.status_code == 201, made.text
    link = made.json()
    assert len(link["code"]) == 8 and link["url"] == f"{settings.PUBLIC_API_URL}/l/{link['code']}"
    # Tags are normalised the way analytics tools expect them.
    assert link["utm_source"] == "instagram" and link["utm_campaign"] == "hack_night"
    assert link["destination"].startswith(f"{settings.PUBLIC_SITE_URL}/events/{event.slug}?utm_source=instagram&utm_medium=social&utm_campaign=hack_night&ref=")

    # Opening the short link counts the click and goes on to the page.
    opened = await client.get(f"/l/{link['code']}", headers={"user-agent": "poster-phone", "x-forwarded-for": "10.0.0.7"})
    # The short link marks its landing (`via=l`) so the club site does not count the same opening twice.
    assert opened.status_code == 302 and opened.headers["location"] == link["destination"] + "&via=l"
    again = await client.get(f"/l/{link['code']}", headers={"user-agent": "poster-phone", "x-forwarded-for": "10.0.0.7"})
    assert again.status_code == 302
    clicks = list((await session.scalars(select(TrackingClicks))).all())
    assert len(clicks) == 2 and clicks[0].visitor == clicks[1].visitor and clicks[0].user_agent == "poster-phone"
    # A code nobody has lands on the site, not on an error.
    lost = await client.get("/l/nope2wrong")
    assert lost.status_code == 302 and lost.headers["location"] == settings.PUBLIC_SITE_URL


async def test_a_sign_up_carrying_the_code_is_credited_to_the_link(client, session, world):
    h, event = world["h"], world["event"]
    link = (await client.post(_event_url(world), json={"name": "Freshers fair QR", "utm_source": "poster_qr", "utm_medium": "print"}, headers=h)).json()
    rival_link = (await client.post("/api/v1/clubs/links", json={"name": "Rival page", "utm_source": "instagram", "utm_medium": "social"}, headers=world["rival"])).json()
    for _ in range(4):
        await client.get(f"/l/{link['code']}", headers={"user-agent": f"ua-{uuid.uuid4().hex[:4]}"})

    credited = await _register(client, event.id, ref=link["code"])
    not_ours = await _register(client, event.id, ref=rival_link["code"])
    plain = await _register(client, event.id)
    by_query = await client.post(f"/api/v1/events/registration/{event.id}/register?ref={link['code']}", json={"full_name": "Query Guest", "email": "query-guest@example.com", "phone": "9123456780"})
    assert by_query.status_code == 200
    rows = {r.email: r for r in (await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id))).all()}
    assert str(rows[credited["email"]].tracking_link_id) == link["id"] if "email" in credited else True
    assert sum(1 for r in rows.values() if r.tracking_link_id is not None) == 2
    assert all(r.tracking_link_id is None for r in rows.values() if r.email in (not_ours.get("email"), plain.get("email")))

    s = (await client.get(_event_url(world), headers=h)).json()
    assert s["scope"] == "event" and s["conversion_noun"] == "registration" and s["second_noun"] == "checked in"
    row = s["links"][0]
    assert row["clicks"] == 4 and row["uniques"] == 4 and row["conversions"] == 2 and row["rate"] == 50.0 and row["second"] == 0
    assert s["by_source"] == [{"source": "poster_qr", "clicks": 4, "conversions": 2, "rate": 50.0}]
    assert s["totals"]["clicks"] == 4 and s["totals"]["conversions"] == 2 and s["totals"]["best_source"] == "poster_qr"
    # Four sign-ups in all, two through the link.
    assert s["untracked"] == {"conversions": 2, "total": 4} and s["totals"]["share_of_all"] == 50.0
    # Nothing in the period before, so no comparison yet.
    assert s["deltas"] == {"clicks": None, "conversions": None, "rate_pts": None}

    # Marking one of them present shows in the step after the sign-up.
    reg = next(r for r in rows.values() if r.tracking_link_id is not None)
    reg.is_attended = True
    await session.commit()
    assert (await client.get(_event_url(world), headers=h)).json()["links"][0]["second"] == 1


async def test_club_links_count_follows_and_what_followers_did_next(client, session, world):
    h, club, fan, event = world["h"], world["club"], world["fan"], world["event"]
    link = (await client.post("/api/v1/clubs/links", json={"name": "Instagram bio", "utm_source": "instagram", "utm_medium": "social", "utm_campaign": "club-page"}, headers=h)).json()
    assert link["destination"].startswith(settings.PUBLIC_CLUB_URL.format(slug=club.slug))
    await client.get(f"/l/{link['code']}", headers={"user-agent": "fan-phone"})

    followed = await client.post(f"/api/v1/clubs/follow/{club.id}?ref={link['code']}", headers=world["fan_h"])
    assert followed.status_code == 200, followed.text
    follow = await session.scalar(select(ClubUsersLink).where(ClubUsersLink.club_id == club.id, ClubUsersLink.user_id == fan.id))
    assert str(follow.tracking_link_id) == link["id"]

    s = (await client.get("/api/v1/clubs/links", headers=h)).json()
    assert s["scope"] == "club" and s["conversion_noun"] == "follow" and s["second_noun"] == "registered later"
    assert s["links"][0]["clicks"] == 1 and s["links"][0]["conversions"] == 1 and s["links"][0]["rate"] == 100.0 and s["links"][0]["second"] == 0
    assert s["untracked"] == {"conversions": 0, "total": 1}

    # The follower then registers for one of the club's events: the step after the follow.
    await _register(client, event.id, headers=world["fan_h"], email=fan.email)
    assert (await client.get("/api/v1/clubs/links", headers=h)).json()["links"][0]["second"] == 1
    # Event links never see club follows, and the club's own list never shows event links.
    assert (await client.get(_event_url(world), headers=h)).json()["links"] == []


async def test_links_are_edited_retired_and_kept_to_their_club(client, world):
    h = world["h"]
    link = (await client.post(_event_url(world), json={"name": "WhatsApp", "utm_source": "whatsapp", "utm_medium": "messaging"}, headers=h)).json()
    edited = await client.patch(_event_url(world, f"/{link['id']}"), json={"name": "WhatsApp community", "utm_content": "Pinned Message", "utm_medium": "Chat"}, headers=h)
    assert edited.status_code == 200, edited.text
    assert edited.json()["name"] == "WhatsApp community" and edited.json()["utm_content"] == "pinned_message" and edited.json()["utm_medium"] == "chat" and edited.json()["code"] == link["code"]
    assert (await client.patch(_event_url(world, f"/{link['id']}"), json={"utm_source": "  "}, headers=h)).status_code == 422

    # Another club cannot read, edit or retire it; a guest cannot list.
    assert (await client.get(_event_url(world), headers=world["rival"])).status_code == 403
    assert (await client.delete(f"/api/v1/clubs/links/{link['id']}", headers=world["rival"])).status_code == 404
    assert (await client.get(_event_url(world), headers=world["fan_h"])).status_code == 403

    assert (await client.delete(_event_url(world, f"/{link['id']}"), headers=h)).status_code == 200
    assert (await client.get(_event_url(world), headers=h)).json()["links"] == []
    gone = await client.get(f"/l/{link['code']}")
    assert gone.status_code == 302 and gone.headers["location"] == settings.PUBLIC_SITE_URL
    assert (await client.delete(_event_url(world, f"/{link['id']}"), headers=h)).status_code == 404


async def test_the_page_reads_every_link_of_the_club_with_its_days_channels_and_what_came_without_one(client, session, world):
    h, club, fan, event = world["h"], world["club"], world["fan"], world["event"]
    bio = (await client.post("/api/v1/clubs/links", json={"name": "Instagram bio", "utm_source": "instagram", "utm_medium": "social"}, headers=h)).json()
    poster = (await client.post(_event_url(world), json={"name": "Freshers fair QR", "utm_source": "poster_qr", "utm_medium": "print"}, headers=h)).json()
    unshared = (await client.post(_event_url(world), json={"name": "Notice board", "utm_source": "poster_qr", "utm_medium": "print"}, headers=h)).json()
    # Opened on two phones and a laptop, from Instagram's in-app browser and from nowhere in particular.
    await client.get(f"/l/{bio['code']}", headers={"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile Safari", "x-forwarded-for": "10.0.0.1", "referer": "https://l.instagram.com/?u=x"})
    await client.get(f"/l/{bio['code']}", headers={"user-agent": "Mozilla/5.0 (Linux; Android 14) Mobile Chrome", "x-forwarded-for": "10.0.0.2"})
    await client.get(f"/l/{poster['code']}", headers={"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome", "x-forwarded-for": "10.0.0.3"})
    # One follow through the bio, one sign-up through the poster, one sign-up with no link at all.
    assert (await client.post(f"/api/v1/clubs/follow/{club.id}?ref={bio['code']}", headers=world["fan_h"])).status_code == 200
    await _register(client, event.id, ref=poster["code"])
    await _register(client, event.id)

    o = (await client.get("/api/v1/clubs/links/overview", headers=h)).json()
    assert o["scope"] == "club" and o["grain"] == "day" and o["totals"]["links"] == 3
    assert o["totals"] == {**o["totals"], "clicks": 3, "uniques": 3, "conversions": 2, "follows": 1, "registrations": 1, "rate": 66.7, "share_of_all": 66.7}
    assert o["totals"]["best_source"]["source"] in ("instagram", "poster_qr") and o["totals"]["best_link"]["conversions"] == 1
    by_name = {l["name"]: l for l in o["links"]}
    assert by_name["Instagram bio"]["target"] == {"kind": "club", "id": None, "name": "Club page", "starts_at": None}
    assert by_name["Instagram bio"]["conversion_noun"] == "follow" and by_name["Instagram bio"]["clicks"] == 2 and by_name["Instagram bio"]["conversions"] == 1
    assert by_name["Freshers fair QR"]["target"]["kind"] == "event" and by_name["Freshers fair QR"]["target"]["name"] == "Hack Night"
    assert by_name["Freshers fair QR"]["conversion_noun"] == "registration" and by_name["Freshers fair QR"]["rate"] == 100.0
    assert by_name["Notice board"]["clicks_ever"] == 0 and by_name["Notice board"]["last_click_at"] is None
    # Too new to call unopened; the day's bar carries all three openings and both sign-ups.
    assert o["attention"]["never_opened"] == []
    assert sum(p["clicks"] for p in o["series"]) == 3 and sum(p["conversions"] for p in o["series"]) == 2 and len(o["series"]) == 31
    assert o["untracked"] == {"follows": {"tracked": 1, "total": 1}, "registrations": {"tracked": 1, "total": 2}, "conversions": 1, "total": 3}
    sources = {s["source"]: s for s in o["by_source"]}
    assert sources["poster_qr"]["links"] == 2 and sources["poster_qr"]["clicks"] == 1 and sources["instagram"]["conversions"] == 1

    # The event's own page sees only its links, and never the follow.
    e = (await client.get(_event_url(world, "/overview"), headers=h)).json()
    assert [l["name"] for l in e["links"]] == ["Notice board", "Freshers fair QR"] and e["totals"]["follows"] == 0 and e["untracked"]["total"] == 2

    # One link opened further: its days, devices and where the openings came from.
    d = (await client.get(f"/api/v1/clubs/links/{bio['id']}/detail", headers=h)).json()
    assert d["clicks"] == 2 and d["uniques"] == 2 and d["devices"] == [{"device": "phone", "clicks": 2}]
    assert {r["referrer"]: r["clicks"] for r in d["referrers"]} == {"Instagram": 1, "Direct · app or typed": 1}
    assert sum(p["clicks"] for p in d["series"]) == 2 and len(d["hours"]) == 24 and sum(x["clicks"] for x in d["hours"]) == 2
    assert (await client.get(f"/api/v1/clubs/links/{poster['id']}/detail", headers=world["rival"])).status_code == 404
    assert (await client.get(_event_url(world, f"/{bio['id']}/detail"), headers=h)).status_code == 404
    assert (await client.get(_event_url(world, f"/{unshared['id']}/detail"), headers=h)).json()["clicks"] == 0
