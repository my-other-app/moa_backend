"""Club websites v2 (feature-register/site-templates-contract.md, builder B):
the template packages, the club's layout (order, hidden, titles, photo
slots, accent), the photo library, the render input, and the page kept
current as any of them change.

Rendered markup is the tests' fixture template's (tests/fixtures/site_template)."""
import copy
import io
import json
import re
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import select

from app.api.closeout.models import EventStatus
from app.api.events.models import EventSpeakers
from app.api.sites import layout as layouts
from app.api.sites import registry, sweep
from app.api.sites import render as site_render
from app.api.sites.models import ClubSiteImages, ClubSites
from app.api.sites.service import resolve_slots
from app.api.speakers.models import SpeakerProfiles
from app.response import CustomHTTPException
from tests.conftest import SITE_FIXTURE, auth_header

NOW = datetime.now(timezone.utc)
API = "/api/v1/clubs/site"
FX = registry.load_package(SITE_FIXTURE, key="fixture")
DEFAULT_ORDER = ["upcoming", "about", "expertise", "stats", "past", "photos", "speakers", "team", "sponsors", "join"]


def _png(size=(1200, 800), colour=(23, 165, 143), fmt="PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, fmt)
    return out.getvalue()


def _other_package(tmp_path, key="other"):
    """A second template: fewer sections in another order, fewer slots."""
    dst = tmp_path / key
    shutil.copytree(SITE_FIXTURE, dst)
    meta = json.loads((dst / "template.json").read_text())
    meta["key"], meta["name"] = key, key.title()
    by_key = {s["key"]: s for s in meta["sections"]}
    meta["sections"] = [by_key[k] for k in ("team", "photos", "upcoming", "about", "join")]
    meta["slots"] = [s for s in meta["slots"] if s["key"] in ("hero.cover", "photos.1", "photos.2")]
    (dst / "template.json").write_text(json.dumps(meta))
    return dst


@pytest.fixture
def other(tmp_path):
    path = _other_package(tmp_path)
    registry.add("other", path)
    yield registry.get("other")
    registry._extra.pop("other", None)
    registry.reset()


# The tests that use the database come first: run on its own, a module whose
# first test does not leaves the suite's event loop unusable for the rest (conftest).


# ─── The API ─────────────────────────────────────────────────────────────


@pytest.fixture
async def world(client, session, make_club, make_event, mint_token):
    owner, club = await make_club(name="Circuit Society", email="layout@example.com")
    club.about = "Makers in Pune. Robots, mostly."
    soon = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=NOW + timedelta(days=9))
    await make_event(club=club, created_by=owner, name="Intro to KiCad", event_datetime=NOW - timedelta(days=30), lifecycle=EventStatus.closed)
    await session.commit()
    h = auth_header(await mint_token(owner))
    assert (await client.patch(API, json={"template": "fixture"}, headers=h)).status_code == 200
    return dict(owner=owner, club=club, soon=soon, h=h)


def _sections_in(html: str) -> list[str]:
    return re.findall(r'<section id="([a-z-]+)"', html)


async def test_the_page_follows_the_clubs_order_titles_and_hidden_sections(client, world):
    h = world["h"]
    r = await client.patch(API, json={"layout": {"order": ["past", "team", "nope", "upcoming"], "hidden": ["stats"], "titles": {"past": "Before now"}}}, headers=h)
    s = r.json()
    assert r.status_code == 200 and s["layout"]["order"][:3] == ["past", "team", "upcoming"] and s["sections"]["stats"] is False
    assert s["adjusted"]["order_dropped"] == ["nope"] and "team" not in s["adjusted"]["order_appended"]
    assert (await client.get(API, headers=h)).json()["adjusted"] is None
    html = (await client.post(f"{API}/preview", json={}, headers=h)).json()["html"]
    # top (the hero, fixed) first; no team (none yet), no stats (hidden); the rest in the club's order.
    assert _sections_in(html)[:3] == ["top", "past", "events"]
    assert "stats" not in _sections_in(html) and "team" not in _sections_in(html)
    assert "Before now" in html and '<a href="#past">Past</a><a href="#events">Events</a>' in html
    # A preview with its own layout changes nothing saved.
    p = (await client.post(f"{API}/preview", json={"layout": {"order": ["upcoming", "past"], "accent": "#ff0055"}}, headers=h)).json()["html"]
    assert _sections_in(p)[:3] == ["top", "events", "past"] and '<html lang="en" style="--accent:#ff0055">' in p
    assert (await client.get(API, headers=h)).json()["layout"]["accent"] is None
    for bad, code in (
        ({"hidden": ["about"]}, "SITE_SECTION_NOT_HIDEABLE"), ({"titles": {"team": "x" * 61}}, "SITE_TITLE_TOO_LONG"),
        ({"slots": {"hero.image": 1}}, "SITE_SLOT_UNKNOWN"), ({"slots": {"hero.cover": 123456}}, "SITE_IMAGE_NOT_FOUND"), ({"accent": "#zzzzzz"}, "SITE_ACCENT_INVALID"),
    ):
        for url in (API, f"{API}/preview"):
            send = client.patch if url == API else client.post
            r = await send(url, json={"layout": bad}, headers=h)
            assert r.status_code == 422 and r.json()["error_code"] == code, (bad, r.text)


async def test_switching_template_through_the_api_carries_the_layout(client, world, other):
    h = world["h"]
    await client.patch(API, json={"layout": {"order": ["join", "team", "upcoming"], "titles": {"stats": "Numbers!"}}}, headers=h)
    s = (await client.patch(API, json={"template": "other"}, headers=h)).json()
    # The shared sections keep the order they had (about and photos followed upcoming there too).
    assert s["template"] == "other" and s["layout"]["order"] == ["join", "team", "upcoming", "about", "photos"] and s["layout"]["titles"] == {}
    s = (await client.patch(API, json={"template": "fixture"}, headers=h)).json()
    assert s["layout"]["order"][:3] == ["join", "team", "upcoming"] and s["layout"]["titles"] == {"stats": "Numbers!"}


async def test_the_join_line_is_the_clubs_own_or_one_true_of_every_club(client, world, other):
    h = world["h"]
    s = (await client.get(API, headers=h)).json()
    assert s["layout"]["join_text"] is None
    html = (await client.post(f"{API}/preview", json={}, headers=h)).json()["html"]
    assert f"<p>{layouts.DEFAULT_JOIN_TEXT}</p>" in html
    s = (await client.patch(API, json={"layout": {"join_text": "  Members get   the <first> seat. "}}, headers=h)).json()
    assert s["layout"]["join_text"] == "Members get the <first> seat."
    html = (await client.post(f"{API}/preview", json={}, headers=h)).json()["html"]
    assert "<p>Members get the &lt;first&gt; seat.</p>" in html and layouts.DEFAULT_JOIN_TEXT not in html
    # Unsent, it stays; across a template switch too.
    assert (await client.patch(API, json={"layout": {"accent": "#ff0055"}}, headers=h)).json()["layout"]["join_text"] == "Members get the <first> seat."
    assert (await client.patch(API, json={"template": "other"}, headers=h)).json()["layout"]["join_text"] == "Members get the <first> seat."
    await client.patch(API, json={"template": "fixture"}, headers=h)
    r = await client.patch(API, json={"layout": {"join_text": "x" * 241}}, headers=h)
    assert r.status_code == 422 and r.json()["error_code"] == "SITE_JOIN_TEXT_TOO_LONG"
    assert (await client.patch(API, json={"layout": {"join_text": "x " * 120}}, headers=h)).status_code == 200  # 239 once trimmed
    for clear in ("", None, "   "):
        await client.patch(API, json={"layout": {"join_text": "Something"}}, headers=h)
        assert (await client.patch(API, json={"layout": {"join_text": clear}}, headers=h)).json()["layout"]["join_text"] is None


async def _upload(client, h, *, alt=None, size=(1200, 800), name="p.png", content=None, kind="image/png"):
    data = {"alt": alt} if alt is not None else {}
    return await client.post(f"{API}/images", files={"file": (name, content if content is not None else _png(size), kind)}, data=data, headers=h)


async def test_the_photo_library_slots_order_alt_and_delete(client, session, world, fake_s3):
    h = world["h"]
    made = []
    for i in range(3):
        r = await _upload(client, h, alt=f"  Photo   {i} ", size=(1200 + i, 800))
        assert r.status_code == 201, r.text
        made.append(r.json())
    assert [m["position"] for m in made] == [0, 1, 2] and made[0]["alt"] == "Photo 0" and (made[0]["width"], made[0]["height"]) == (1200, 800)
    assert made[0]["url"] and "clubs%2Fsite%2Fv-" in made[0]["url"] and made[0]["url"].endswith(".w960.png")
    # Every size is stored beside the original.
    assert len([k for k in fake_s3 if "/clubs/site/v-" in k]) == 3 * 5
    ids = [m["id"] for m in made]
    s = (await client.get(API, headers=h)).json()
    assert [(i["id"], i["used_in"]) for i in s["images"]] == [(ids[0], ["photos.1"]), (ids[1], ["photos.2"]), (ids[2], ["photos.3"])]
    assert s["available"]["photos"] is True

    # The last one on the cover: the gallery fills with the others.
    s = (await client.patch(API, json={"layout": {"slots": {"hero.cover": ids[2]}}}, headers=h)).json()
    assert s["layout"]["slots"] == {"hero.cover": ids[2]}
    assert [(i["id"], i["used_in"]) for i in s["images"]] == [(ids[0], ["photos.1"]), (ids[1], ["photos.2"]), (ids[2], ["hero.cover"])]
    html = (await client.post(f"{API}/preview", json={}, headers=h)).json()["html"]
    cover = re.search(r'<img src="([^"]+)" alt="Photo 2" width="1202" height="800" fetchpriority="high"', html)
    assert cover and cover.group(1).endswith(".w2400.png")  # 1440 px wide slot at 2x: the largest size
    assert len(re.findall(r'class="photo"', html)) == 2

    # Order and alt.
    r = await client.put(f"{API}/images/order", json={"ids": [ids[1], ids[0], ids[2]]}, headers=h)
    assert r.status_code == 200 and [(i["id"], i["position"], i["used_in"]) for i in r.json()] == [(ids[1], 0, ["photos.1"]), (ids[0], 1, ["photos.2"]), (ids[2], 2, ["hero.cover"])]
    for bad in ([ids[0], ids[1]], [ids[0], ids[1], ids[2], ids[2]], [ids[0], ids[1], 999]):
        r = await client.put(f"{API}/images/order", json={"ids": bad}, headers=h)
        assert r.status_code == 422 and r.json()["error_code"] == "SITE_IMAGE_ORDER_INVALID"
    r = await client.patch(f"{API}/images/{ids[0]}", json={"alt": "The demo night"}, headers=h)
    assert r.status_code == 200 and r.json()["alt"] == "The demo night"
    assert (await client.patch(f"{API}/images/{ids[0]}", json={"alt": None}, headers=h)).json()["alt"] is None

    # Delete: out of every slot (and the other template's keys), the rest closed up.
    site = (await session.execute(select(ClubSites).where(ClubSites.club_id == world["club"].id).execution_options(populate_existing=True))).scalar_one()
    site.layout = {**site.layout, "slots": {**site.layout["slots"], "join.image": ids[2]}}
    await session.commit()
    r = await client.delete(f"{API}/images/{ids[2]}", headers=h)
    assert r.status_code == 200 and r.json() == {"deleted": True, "republished": False}
    s = (await client.get(API, headers=h)).json()
    assert s["layout"]["slots"] == {} and [(i["id"], i["position"]) for i in s["images"]] == [(ids[1], 0), (ids[0], 1)]
    site = (await session.execute(select(ClubSites).where(ClubSites.club_id == world["club"].id).execution_options(populate_existing=True))).scalar_one()
    assert site.layout["slots"] == {}
    assert (await client.delete(f"{API}/images/{ids[2]}", headers=h)).status_code == 404


async def test_another_clubs_photo_is_out_of_reach(client, session, world, make_club, mint_token, fake_s3):
    owner, rival = await make_club(name="Rivals", email="rivals-layout@example.com")
    await session.commit()
    rh = auth_header(await mint_token(owner))
    theirs = (await _upload(client, rh)).json()["id"]
    h = world["h"]
    assert (await client.patch(f"{API}/images/{theirs}", json={"alt": "mine now"}, headers=h)).status_code == 404
    assert (await client.delete(f"{API}/images/{theirs}", headers=h)).status_code == 404
    r = await client.patch(API, json={"layout": {"slots": {"hero.cover": theirs}}}, headers=h)
    assert r.status_code == 422 and r.json()["error_code"] == "SITE_IMAGE_NOT_FOUND"
    assert (await client.put(f"{API}/images/order", json={"ids": [theirs]}, headers=h)).status_code == 422


async def test_the_library_holds_twenty_photos_and_only_photos(client, session, world, fake_s3):
    h = world["h"]
    for bad, kind in ((b"not a picture at all", "text/plain"), (_png(fmt="GIF"), "image/gif")):
        r = await _upload(client, h, content=bad, kind=kind, name="x.bin")
        assert r.status_code == 422, r.text
    assert (await _upload(client, h, alt="x" * 201)).status_code == 422
    club_id = world["club"].id
    for i in range(20):
        session.add(ClubSiteImages(club_id=club_id, image=f"clubs/site/v-{i:032x}.png", position=i, width=10, height=10))
    await session.commit()
    r = await _upload(client, h)
    assert r.status_code == 409 and r.json()["error_code"] == "SITE_IMAGES_FULL"
    assert not [k for k in fake_s3 if "/clubs/site/" in k]  # refused before anything was stored
    assert len((await client.get(API, headers=h)).json()["images"]) == 20


async def test_a_photo_the_live_page_shows_leaves_it_at_once(client, session, world, fake_s3):
    h, slug = world["h"], world["club"].slug
    photo = (await _upload(client, h, alt="On the cover")).json()
    await client.patch(API, json={"layout": {"slots": {"hero.cover": photo["id"]}}}, headers=h)
    await client.post(f"{API}/publish", headers=h)
    stem = re.search(r"(v-[0-9a-f]{32})", photo["url"]).group(1)
    assert stem in (await client.get(f"/api/v1/sites/{slug}")).text
    r = await client.delete(f"{API}/images/{photo['id']}", headers=h)
    assert r.json() == {"deleted": True, "republished": True}
    assert stem not in (await client.get(f"/api/v1/sites/{slug}")).text
    assert (await client.get(API, headers=h)).json()["stale"] is False


async def test_the_sweep_makes_a_published_page_again_after_layout_and_library_changes(client, session, world, fake_s3):
    h, club = world["h"], world["club"]
    await client.post(f"{API}/publish", headers=h)
    assert club.id not in await sweep.due(session, datetime.now(timezone.utc))

    s = (await client.patch(API, json={"layout": {"titles": {"upcoming": "Coming up"}}}, headers=h)).json()
    assert s["stale"] is True and club.id in await sweep.due(session, datetime.now(timezone.utc))
    assert await sweep.refresh(session) == 1
    assert "Coming up" in (await client.get(f"/api/v1/sites/{club.slug}")).text
    assert (await client.get(API, headers=h)).json()["stale"] is False and await sweep.due(session, datetime.now(timezone.utc)) == []

    photo = (await _upload(client, h, alt="Fresh")).json()
    assert club.id in await sweep.due(session, datetime.now(timezone.utc)) and (await client.get(API, headers=h)).json()["stale"] is True
    assert await sweep.refresh(session) == 1
    assert 'alt="Fresh"' in (await client.get(f"/api/v1/sites/{club.slug}")).text
    await client.patch(f"{API}/images/{photo['id']}", json={"alt": "Renamed"}, headers=h)
    assert club.id in await sweep.due(session, datetime.now(timezone.utc))


async def test_the_speakers_are_the_clubs_events_speakers_once_each_latest_first(client, session, world, make_event):
    club, owner = world["club"], world["owner"]
    older = await make_event(club=club, created_by=owner, name="Old talk", event_datetime=NOW - timedelta(days=60), lifecycle=EventStatus.closed)
    draft = await make_event(club=club, created_by=owner, name="Draft", event_datetime=NOW + timedelta(days=3), lifecycle=EventStatus.draft)
    shown = SpeakerProfiles(slug="asha-rao", name="Asha Rao", name_key="asha rao", status="active")
    hidden = SpeakerProfiles(slug="hidden-person", name="Hidden Person", name_key="hidden person", status="hidden")
    session.add_all([shown, hidden])
    await session.flush()
    session.add_all([
        EventSpeakers(event_id=older.id, name="Asha Rao", designation="CTO · Acme", speaker_profile_id=shown.id),
        EventSpeakers(event_id=world["soon"].id, name="Asha Rao", designation="CTO at Acme Labs", speaker_profile_id=shown.id),
        EventSpeakers(event_id=older.id, name="Ravi K", designation="Maker", link="javascript:alert(1)"),
        EventSpeakers(event_id=older.id, name="ravi k", designation="Maker"),
        EventSpeakers(event_id=older.id, name="Hidden Person", speaker_profile_id=hidden.id),
        EventSpeakers(event_id=draft.id, name="Not Yet"),
    ])
    await session.commit()
    from app.api.sites import service

    site = await service.get_or_create(session, club)
    data = await service.site_data(session, club, service.choices(site))
    assert [(s["name"], s["title"], s["org"]) for s in data["speakers"]] == [("Asha Rao", "CTO", "Acme Labs"), ("Ravi K", "Maker", None)]
    assert data["speakers"][0]["url"] == "https://myotherapp.in/speakers/asha-rao" and data["speakers"][1]["url"] is None


async def test_the_render_input_has_the_contracts_shape(client, session, world):
    from app.api.sites import service

    club = world["club"]
    club.contact_email = "hello@circuit.org"
    await session.commit()
    site = await service.get_or_create(session, club)
    data = await service.site_data(session, club, service.choices(site), "abc123")
    assert {"site", "page", "headline", "subline", "join_text", "club", "images", "stats", "upcoming", "past", "speakers", "team", "sponsors", "socials", "links"} <= set(data)
    assert data["join_text"] == "Joining is free. Follow us to hear about every event first."
    assert set(data["site"]) == {"template", "headline", "subline", "accent", "title_overrides", "layout"}
    assert set(data["club"]) == {"name", "initials", "tagline", "about", "about_short", "location_name", "contact_email", "slug", "logo", "logo_url", "logo_srcset", "cover", "founded_year", "topics", "brand_colour"}
    assert set(data["images"]) == {"hero.cover", "hero.image", "about.image", "join.image", "event.fallback", "photos"}
    assert data["stats"] == {"followers": 0, "events_held": 1, "upcoming": 1, "members": None}
    ev = data["upcoming"][0]
    assert set(ev) == {"id", "name", "url", "starts_at", "ends_at", "day", "day2", "month", "month_long", "year", "weekday", "weekday_long", "time", "end_time", "date_label", "where", "is_online", "fee", "kind", "blurb", "seats_left", "going", "poster", "poster_is_fallback"}
    assert ev["url"].endswith("?sref=abc123") and ev["starts_at"].endswith("+05:30") and re.match(r"^\d{1,2}:\d\d [AP]M$", ev["time"])
    assert data["links"]["contact_mailto"] == "mailto:hello@circuit.org" and data["club"]["initials"] == "CS"
    json.dumps(data)  # plain data all the way down: what a sample.json holds


def _full(pkg) -> dict:
    """A club with everything: six events each way, eight speakers, twelve people, ten sponsors, twenty photos."""
    data = copy.deepcopy(FX.sample)
    img = lambda i, w=1440, h=960: {"url": f"https://media.myotherapp.in/clubs/site/v-{i:032x}.w1440.jpeg", "srcset": ", ".join(f"https://media.myotherapp.in/clubs/site/v-{i:032x}.w{x}.jpeg {x}w" for x in (480, 960, 1440, 2400)), "alt": f"Photo {i}", "w": w, "h": h}  # noqa: E731
    ev = lambda i: {"id": i, "name": f"Event number {i} with a longer name", "url": f"https://myotherapp.in/events/e-{i}?sref=abc", "starts_at": "2026-12-12T16:00:00+05:30", "ends_at": "2026-12-12T20:00:00+05:30", "day": "12", "day2": "12", "month": "Dec", "month_long": "December", "year": "2026", "weekday": "Sat", "weekday_long": "Saturday", "time": "4:00 PM", "end_time": "8:00 PM", "date_label": "Sat 12 Dec 2026", "where": "Lab 3, COEP", "is_online": False, "fee": "₹100", "kind": "Meetup", "blurb": "Members demo what they built this year.", "seats_left": 12, "going": 138, "poster": img(100 + i, 1080, 1350), "poster_is_fallback": False}  # noqa: E731
    data["site"] = {**data["site"], "template": pkg.key}
    data["images"] = {"hero.cover": img(1), "hero.image": img(2), "about.image": img(3), "join.image": img(4), "event.fallback": img(5), "photos": [img(10 + i) for i in range(len(pkg.photo_slots))]}
    data["upcoming"], data["past"] = [ev(i) for i in range(6)], [ev(10 + i) for i in range(6)]
    data["speakers"] = [{"name": f"Speaker {i}", "title": "Engineer", "org": "Acme", "photo": img(200 + i, 200, 200), "url": "https://myotherapp.in/speakers/s"} for i in range(8)]
    data["team"] = [{"name": f"Person {i}", "role": "Lead", "initials": "P", "photo": img(300 + i, 400, 400), "links": [{"kind": "linkedin", "label": "LinkedIn", "href": "https://linkedin.com/in/p"}]} for i in range(12)]
    data["sponsors"] = [{"name": f"Sponsor {i}", "role": "Partner", "initials": "S", "logo": img(400 + i, 400, 400)} for i in range(10)]
    data["socials"] = [{"kind": "instagram", "label": "Instagram", "href": "https://instagram.com/c"}]
    data["club"]["topics"] = ["Design", "Hardware", "Robotics", "AI"]
    data["visit_url"] = "https://api.myotherapp.in/api/v1/sites/harbour/visit"
    return data


@pytest.mark.parametrize("template", list(registry.packages()))
def test_a_full_clubs_page_keeps_to_the_budget(template):
    """≤ 150 KB of HTML; the cover fetched first, every other picture lazy,
    each with its width, height and alt; nothing left of the template language."""
    pkg = registry.get(template)
    html = site_render.render(_full(pkg))
    assert len(html.encode("utf-8")) <= 150 * 1024
    assert "{{" not in html and "{%" not in html
    imgs = re.findall(r"<img\b[^>]*>", html)
    assert imgs
    for tag in imgs:
        assert re.search(r'\swidth="\d+"', tag) and re.search(r'\sheight="\d+"', tag) and re.search(r'\salt="', tag), tag
        assert ('loading="lazy"' in tag) != ('fetchpriority="high"' in tag), tag


# ─── The packages ────────────────────────────────────────────────────────


def test_every_package_in_the_checkout_loads_strictly():
    """A broken package is left out (and logged) in production; here it fails."""
    loaded = registry.load(strict=True)
    assert list(loaded)[:len(registry.CATALOGUE)] == list(registry.CATALOGUE)
    for pkg in loaded.values():
        assert pkg.env.auto_reload is False and pkg.env.autoescape is True
        assert pkg.movable and all(k not in registry.FRAME for k in pkg.movable)


def _broken(tmp_path, change) -> None:
    dst = tmp_path / "broken"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(SITE_FIXTURE, dst)
    meta = json.loads((dst / "template.json").read_text())
    change(meta, dst)
    (dst / "template.json").write_text(json.dumps(meta))
    with pytest.raises(registry.PackageError):
        registry.load_package(dst, key="broken")


def test_a_package_that_is_not_right_is_refused(tmp_path):
    _broken(tmp_path, lambda m, d: m["sections"].append({"key": "gallery", "label": "G", "default_title": "G"}))
    _broken(tmp_path, lambda m, d: m["sections"].append({"key": "footer", "label": "F", "default_title": "F"}))
    _broken(tmp_path, lambda m, d: m["sections"].append(dict(m["sections"][0])))
    _broken(tmp_path, lambda m, d: m["slots"].append({"key": "sidebar.image", "label": "S", "size": [10, 10]}))
    _broken(tmp_path, lambda m, d: m["slots"].append({"key": "photos.9", "label": "S", "size": [10, "x"]}))
    _broken(tmp_path, lambda m, d: m.update(accent={"default": "blue"}))
    _broken(tmp_path, lambda m, d: m.update(layout="three_column"))
    _broken(tmp_path, lambda m, d: m["sections"][0].update(default_title="x" * 61))
    _broken(tmp_path, lambda m, d: m["sections"][0].update(anchor="a b"))
    _broken(tmp_path, lambda m, d: (d / "sections" / "team.html.j2").unlink())
    _broken(tmp_path, lambda m, d: (d / "sections" / "team.html.j2").write_text("{% if %}"))
    _broken(tmp_path, lambda m, d: (d / "style.css").write_text("a{}</style><script>x</script>"))


# ─── The layout ──────────────────────────────────────────────────────────


def test_a_stored_layout_is_read_as_valid_for_the_template():
    got = layouts.normalise({
        "order": ["team", "bogus", "upcoming", "team"], "hidden": ["about", "past", "nope"],
        "titles": {"team": "  The   crew ", "nav": "x", "past": "", "photos": "p" * 80}, "slots": {"hero.cover": 4, "hero.image": 5, "photos.1": "6", "photos.2": True},
        "accent": "#ABC",
    }, FX)
    assert got["order"] == ["team", "upcoming", *[k for k in DEFAULT_ORDER if k not in ("team", "upcoming")]]
    assert got["hidden"] == ["past"]  # about is never hidden in this template; nope is no section
    assert got["titles"] == {"team": "The crew", "photos": "p" * 60}
    assert got["slots"] == {"hero.cover": 4}  # the fixture has no hero.image; ids are numbers
    assert got["accent"] == "#aabbcc"
    assert layouts.normalise(None, FX) == {"order": DEFAULT_ORDER, "hidden": [], "titles": {}, "slots": {}, "accent": None, "join_text": None}


def test_a_layout_change_says_what_it_adjusted_and_refuses_what_cannot_be():
    stored, adjusted = layouts.apply({}, {"order": ["team", "bogus", "upcoming"], "titles": {"team": " Crew ", "bogus": "x"}, "accent": "#3355FF"}, FX, {1, 2})
    assert stored["order"][:2] == ["team", "upcoming"] and sorted(stored["order"]) == sorted(DEFAULT_ORDER)
    assert adjusted == {"order_dropped": ["bogus"], "order_appended": [k for k in DEFAULT_ORDER if k not in ("team", "upcoming")], "titles_dropped": ["bogus"]}
    assert stored["titles"] == {"team": "Crew"} and stored["accent"] == "#3355ff"
    # Titles and slots merge; null clears one.
    stored, _ = layouts.apply(stored, {"titles": {"team": None, "past": "Done"}, "slots": {"hero.cover": 1, "photos.1": 2}}, FX, {1, 2})
    assert stored["titles"] == {"past": "Done"} and stored["slots"] == {"hero.cover": 1, "photos.1": 2} and stored["order"][0] == "team"
    stored, _ = layouts.apply(stored, {"slots": {"hero.cover": None}, "accent": None}, FX, {1, 2})
    assert stored["slots"] == {"photos.1": 2} and stored["accent"] is None

    def refused(patch, code):
        with pytest.raises(CustomHTTPException) as e:
            layouts.apply(stored, patch, FX, {1, 2})
        assert e.value.status_code == 422 and e.value.error_code == code, e.value.error_code

    refused({"hidden": ["about"]}, "SITE_SECTION_NOT_HIDEABLE")
    refused({"hidden": ["hero"]}, "SITE_SECTION_NOT_HIDEABLE")
    refused({"titles": {"team": "x" * 61}}, "SITE_TITLE_TOO_LONG")
    refused({"slots": {"hero.image": 1}}, "SITE_SLOT_UNKNOWN")
    refused({"slots": {"photos.1": 99}}, "SITE_IMAGE_NOT_FOUND")
    refused({"accent": "red"}, "SITE_ACCENT_INVALID")
    refused({"join_text": "y" * 241}, "SITE_JOIN_TEXT_TOO_LONG")
    stored, _ = layouts.apply(stored, {"join_text": " Free,\n always. "}, FX, {1, 2})
    assert stored["join_text"] == "Free, always." and layouts.normalise(stored, FX)["join_text"] == "Free, always."
    stored, _ = layouts.apply(stored, {"join_text": ""}, FX, {1, 2})
    assert stored["join_text"] is None


def test_switching_template_keeps_the_shared_order_and_everything_by_key(tmp_path):
    other = registry.load_package(_other_package(tmp_path), key="other")
    stored, _ = layouts.apply({}, {"order": ["join", "team", "photos", "upcoming"], "hidden": ["team", "past"], "titles": {"past": "Before"}, "slots": {"hero.cover": 1, "about.image": 2}}, FX, {1, 2})
    moved = layouts.switch(stored, FX, other)
    assert moved["order"] == ["join", "team", "photos", "upcoming", "about"] and moved["hidden"] == ["team"]
    # Titles and slots are kept by key, used where the template has them.
    assert moved["titles"] == {"past": "Before"} and moved["slots"] == {"hero.cover": 1, "about.image": 2}
    assert layouts.normalise(moved, other)["slots"] == {"hero.cover": 1}
    back = layouts.normalise(layouts.switch(moved, other, FX), FX)
    assert back["order"][:4] == ["join", "team", "photos", "upcoming"] and back["titles"] == {"past": "Before"} and back["slots"] == {"hero.cover": 1, "about.image": 2}


def test_the_gallery_fills_from_the_library_with_photos_no_other_slot_uses():
    lib = [ClubSiteImages(id=i, club_id=1, position=i, width=10, height=10) for i in (10, 11, 12, 13, 14)]
    placed = resolve_slots(FX, {"hero.cover": 10, "photos.2": 13}, lib)
    assert {k: r.id for k, r in placed.items()} == {"hero.cover": 10, "photos.1": 11, "photos.2": 13, "photos.3": 12}
    assert {k: r.id for k, r in resolve_slots(FX, {}, lib[:2]).items()} == {"photos.1": 10, "photos.2": 11}
    assert resolve_slots(FX, {"hero.cover": 99}, []) == {}


