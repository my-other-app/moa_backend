""""I'm attending" posters (feature-register/posters/CONTRACT.md).

The layouts are drawn by the real builder and render client; only the render
service is faked, at the HTTP level (tests/render_transport_fake.py), and the
bucket is a dict. Jobs run inline after each request (conftest), as the API's
own runner runs them.
"""
from __future__ import annotations

import io
import re
from datetime import datetime, time, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.api.events.models import EventRegistrationsLink
from app.api.guest_posters import links as poster_links
from app.api.guest_posters import moments as mom
from app.api.guest_posters import photos, render, service
from app.api.guest_posters.models import GuestPosters, PosterSettings
from app.api.links.models import TrackingClicks, TrackingLinks
from app.config import settings
from app.core import jobs
from app.core.layout_v2 import poster
from app.core.rate_limit import limiter
from app.core.storage import objects
from tests.conftest import auth_header
from tests.render_transport_fake import install

NOW = datetime.now(timezone.utc)
# 08:30 in Kochi, five days on: the day does not cross midnight.
START = datetime.combine((NOW + timedelta(days=5)).date(), time(3, 0), tzinfo=timezone.utc)


@pytest.fixture
def render_service(monkeypatch):
    return install(monkeypatch)


class _Bucket:
    def __init__(self):
        self.held: dict = {}

    def delete_objects(self, Bucket=None, Delete=None):
        for o in (Delete or {}).get("Objects", []):
            self.held.pop(o["Key"], None)
        return {}


@pytest.fixture
def bucket(monkeypatch):
    b = _Bucket()
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: b.held.get(key))
    monkeypatch.setattr(objects, "get_bytes", lambda key: b.held.get(key))
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: b.held.__setitem__(key, bytes(data)))
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://files.test/{key}")
    monkeypatch.setattr(objects, "client", lambda: b)
    return b


@pytest.fixture(autouse=True)
def _no_limits(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)


async def _sent(session, event, reg):
    session.add(Deliveries(event_id=event.id, registration_id=reg.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email,
                           status=DeliveryStatus.sent, sent_at=NOW, attempts=1))
    await session.flush()


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    from app.api.agenda.models import AgendaItems
    from app.api.cohosts.models import ACCEPTED, EventCohosts
    from app.api.events.models import EventSpeakers
    from app.api.speakers.models import AgendaItemSpeakers

    owner, club = await make_club(name="GDG Kochi", email="gdg-posters@example.com")
    co_owner, co = await make_club(name="Robotics Club", email="robo-posters@example.com")
    other_owner, _ = await make_club(name="Chess Circle", email="chess-posters@example.com")
    event = await make_event(club=club, created_by=owner, name="DevFest Kochi", event_datetime=START, duration=8.0,
                             location_name="Hall One, Kochi",
                             additional_details=[{"key": "college", "label": "College", "field_type": "text"}])
    session.add(EventCohosts(event_id=event.id, club_id=co.id, status=ACCEPTED, invited_at=NOW, expires_at=START, responded_at=NOW))
    users = {k: await make_user(email=f"{k}-posters@example.com") for k in ("asha", "sita", "nina", "ravi", "cara", "zed")}
    regs = {
        "asha": await make_registration(event=event, user=users["asha"], full_name="Asha Nair", additional_details={"college": "CET Trivandrum"}),
        "sita": await make_registration(event=event, user=users["sita"], full_name="Sita Raman", role="speaker"),
        "nina": await make_registration(event=event, user=users["nina"], full_name="Nina Paul"),
        "ravi": await make_registration(event=event, user=users["ravi"], full_name="Ravi Menon", ticket_revoked_at=NOW),
        "cara": await make_registration(event=event, user=users["cara"], full_name="Cara Das", is_deleted=True),
    }
    for k in ("asha", "sita", "ravi", "cara"):
        await _sent(session, event, regs[k])
    sp = EventSpeakers(event_id=event.id, name="Sita Raman", talk_title="Old title", display_order=0)
    session.add(sp)
    await session.flush()
    item = AgendaItems(event_id=event.id, starts_at=START + timedelta(hours=2), title="Building for Bharat", location="Hall B")
    session.add(item)
    await session.flush()
    session.add(AgendaItemSpeakers(agenda_item_id=item.id, event_speaker_id=sp.id, position=0))
    await session.commit()
    tokens = {k: auth_header(await mint_token(u)) for k, u in users.items()}
    return {
        "event": event, "club": club, "regs": regs, "users": users, "t": tokens,
        "host": auth_header(await mint_token(owner)), "cohost": auth_header(await mint_token(co_owner)),
        "other": auth_header(await mint_token(other_owner)), "owner": owner,
    }


def _g(reg, path=""):
    return f"/api/v1/posters/{reg.ticket_id}{path}"


def _o(world, path):
    return f"/api/v1/events/{world['event'].id}{path}"


async def _turn_on(client, world, **body):
    r = await client.put(_o(world, "/posters/settings"), json={"enabled": True, **body}, headers=world["host"])
    assert r.status_code == 200, r.text
    return r.json()


def _jpeg(size=(1200, 900), *, orientation=None, gps=False, colour=(200, 40, 40)) -> bytes:
    img = Image.new("RGB", size, colour)
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    if gps:
        exif[0x8825] = {1: "N", 2: (9.0, 58.0, 0.0), 3: "E", 4: (76.0, 16.0, 0.0)}
        exif[0x010F] = "PhoneMaker"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


# ─── Off by default: no effect ──────────────────────────────────────────────


async def test_posters_are_off_by_default_and_touch_nothing(client, session, world, bucket, render_service):
    asha = world["regs"]["asha"]
    r = await client.get(_g(asha), headers=world["t"]["asha"])
    assert r.status_code == 200 and r.json() == {"available": False, "reason": "off"}
    assert (await client.post(_g(asha, "/render"), json={"format": "square"}, headers=world["t"]["asha"])).status_code == 409
    up = await client.post(_g(asha, "/photo"), files={"file": ("a.jpg", _jpeg(), "image/jpeg")}, headers=world["t"]["asha"])
    assert up.status_code == 409 and up.json()["errors"]["reason"] == "off"
    assert await session.scalar(select(func.count()).select_from(GuestPosters)) == 0
    assert await session.scalar(select(func.count()).select_from(TrackingLinks)) == 0
    assert not bucket.held and render_service.renders == 0
    settings_now = (await client.get(_o(world, "/posters/settings"), headers=world["host"])).json()
    assert settings_now["enabled"] is False and settings_now["moments"]["before"]["phrase"] == "I'm attending"
    # Switched on, then off again: hidden at once.
    await _turn_on(client, world)
    assert (await client.get(_g(asha), headers=world["t"]["asha"])).json()["available"] is True
    await client.put(_o(world, "/posters/settings"), json={"enabled": False}, headers=world["host"])
    assert (await client.get(_g(asha), headers=world["t"]["asha"])).json() == {"available": False, "reason": "off"}


async def test_the_platform_switch_turns_every_event_off(client, world, monkeypatch, bucket):
    await _turn_on(client, world)
    monkeypatch.setattr(settings, "POSTERS_ENABLED", False)
    assert (await client.get(_g(world["regs"]["asha"]), headers=world["t"]["asha"])).json() == {"available": False, "reason": "off"}


# ─── Who may ────────────────────────────────────────────────────────────────


async def test_only_the_holder_by_account_or_poster_link(client, world, bucket):
    await _turn_on(client, world)
    asha = world["regs"]["asha"]
    mine = await client.get(_g(asha), headers=world["t"]["asha"])
    assert mine.status_code == 200 and mine.json()["available"] is True
    key = mine.json()["key"]
    assert key == service.key_for(asha.ticket_id)
    # The poster link (no account at all) works; a wrong or missing key does not.
    assert (await client.get(_g(asha), params={"s": key})).status_code == 200
    assert (await client.get(_g(asha), params={"s": "0" * 32})).status_code == 403
    assert (await client.get(_g(asha))).status_code == 403
    # Another guest, the host club, a co-host and another club: refused (the photo is the guest's own).
    for who in (world["t"]["zed"], world["t"]["nina"], world["host"], world["cohost"], world["other"]):
        r = await client.get(_g(asha), headers=who)
        assert r.status_code == 403 and r.json()["error_code"] == "POSTER_FORBIDDEN"
    assert (await client.get("/api/v1/posters/MOA-NOPE0-NOPE0", headers=world["t"]["asha"])).status_code == 404
    # The email token is the signed page on the site.
    assert service.poster_page_url(asha.ticket_id) == f"{settings.PUBLIC_SITE_URL}/posters/{asha.ticket_id}?s={key}"


async def test_the_email_token_is_the_signed_poster_page(session, world):
    from app.api.emails.service import guest_values
    from app.core.mail.schema import EMAIL_TOKEN_KEYS

    asha, ravi = world["regs"]["asha"], world["regs"]["ravi"]
    assert "poster_url" in EMAIL_TOKEN_KEYS
    assert guest_values(world["event"], asha)["poster_url"] == service.poster_page_url(asha.ticket_id)
    assert guest_values(world["event"], ravi)["poster_url"] == ""


# ─── Moments ────────────────────────────────────────────────────────────────


async def test_choosing_a_moment():
    m = mom.moments_of(None)
    assert mom.choose(m, sent=False, attended=False, ended=False) == (None, "not_sent")
    assert mom.choose(m, sent=True, attended=False, ended=False) == ("before", None)
    # Only "before" on: kept during and after, with its phrase.
    assert mom.choose(m, sent=True, attended=True, ended=False) == ("before", None)
    assert mom.choose(m, sent=True, attended=True, ended=True) == ("before", None)
    on = {k: {**v, "enabled": True} for k, v in m.items()}
    assert mom.choose(on, sent=True, attended=True, ended=False) == ("during", None)
    assert mom.choose(on, sent=True, attended=False, ended=True) == ("after", None)
    assert mom.choose(on, sent=False, attended=True, ended=True) == ("after", None)
    # A walk-in checked in, with only "before" on: a moment is reached but off.
    assert mom.choose(m, sent=False, attended=True, ended=False) == (None, "moment_off")


async def test_moments_follow_the_ticket_and_the_day(client, session, world, bucket):
    await _turn_on(client, world, moments={"during": {"enabled": True, "phrase": "I'm here at"}, "after": {"enabled": True}})
    t, regs = world["t"], world["regs"]
    assert (await client.get(_g(regs["nina"]), headers=t["nina"])).json()["reason"] == "not_sent"
    assert (await client.get(_g(regs["ravi"]), headers=t["ravi"])).json()["reason"] == "revoked"
    assert (await client.get(_g(regs["cara"]), headers=t["cara"])).json()["reason"] == "cancelled"
    body = (await client.get(_g(regs["asha"]), headers=t["asha"])).json()
    assert body["moment"]["key"] == "before" and body["moment"]["phrase"] == "I'm attending"
    # Checked in: during.
    regs["asha"].is_attended, regs["asha"].attended_on = True, NOW
    await session.commit()
    body = (await client.get(_g(regs["asha"]), headers=t["asha"])).json()
    assert body["moment"] == {"key": "during", "phrase": "I'm here at", "caption": body["moment"]["caption"]}
    # The event over: after.
    world["event"].event_datetime = NOW - timedelta(hours=10)
    await session.commit()
    assert (await client.get(_g(regs["asha"]), headers=t["asha"])).json()["moment"]["key"] == "after"
    # The ticket sent later: the guest's poster appears.
    await _sent(session, world["event"], regs["nina"])
    await session.commit()
    assert (await client.get(_g(regs["nina"]), headers=t["nina"])).json()["available"] is True


# ─── The poster page ────────────────────────────────────────────────────────


async def test_availability_mints_one_share_link_and_one_row(client, session, world, bucket):
    await _turn_on(client, world, moments={"before": {"caption": "{{moment_phrase}} {{event_name}}! {{share_url}} #DevFest"}})
    asha = world["regs"]["asha"]
    a = (await client.get(_g(asha), headers=world["t"]["asha"])).json()
    assert re.fullmatch(r"https://myotherapp\.in/l/[a-z0-9]{8}", a["share_url"])
    assert a["moment"]["caption"] == f"I'm attending DevFest Kochi! {a['share_url']} #DevFest"
    assert a["guest"] == {"name": "Asha Nair", "first_name": "Asha", "initials": "AN", "role": "attendee", "role_label": "Attendee"}
    assert [f["key"] for f in a["formats"]] == ["square", "portrait", "story"]
    sq = a["formats"][0]
    assert (sq["width"], sq["height"]) == (1080, 1080) and sq["variant"] == "starter" and sq["frame"]["width"] > 0
    assert sq["framing"] == photos.DEFAULT_FRAMING and sq["render"]["status"] == "none" and a["photo"] is None
    again = (await client.get(_g(asha), headers=world["t"]["asha"])).json()
    assert again["share_url"] == a["share_url"]
    kinds = sorted((await session.scalars(select(TrackingLinks.kind))).all())
    assert kinds == ["poster", "poster_guest"]
    assert await session.scalar(select(func.count()).select_from(GuestPosters)) == 1
    # Formats offered by the organiser only.
    await client.put(_o(world, "/posters/settings"), json={"formats": ["story"]}, headers=world["host"])
    assert [f["key"] for f in (await client.get(_g(asha), headers=world["t"]["asha"])).json()["formats"]] == ["story"]
    r = await client.post(_g(asha, "/render"), json={"format": "square"}, headers=world["t"]["asha"])
    assert r.status_code == 422


async def test_a_photo_is_cleaned_kept_privately_and_removed(client, session, world, bucket):
    await _turn_on(client, world)
    asha = world["regs"]["asha"]
    # A phone photo on its side, with a location in it.
    r = await client.post(_g(asha, "/photo"), files={"file": ("p.jpg", _jpeg((1200, 900), orientation=6, gps=True), "image/jpeg")},
                          headers=world["t"]["asha"])
    assert r.status_code == 200, r.text
    photo = r.json()["photo"]
    assert (photo["width"], photo["height"]) == (900, 1200)  # upright
    row = await session.scalar(select(GuestPosters))
    assert row.photo_key.startswith(photos.folder(world["event"].id, asha.id)) and "guest_posters/" in row.photo_key
    stored = bucket.held[row.photo_key]
    with Image.open(io.BytesIO(stored)) as img:
        exif = img.getexif()
        assert not exif.get(0x8825) and not exif.get(0x010F) and not exif.get(0x0112)
    got = await client.get(_g(asha, "/photo"), headers=world["t"]["asha"])
    assert got.status_code == 200 and got.content == stored and got.headers["cache-control"] == "private, no-store"
    assert (await client.get(_g(asha, "/photo"), headers=world["t"]["zed"])).status_code == 403
    assert (await client.get(_g(asha, "/photo"), headers=world["host"])).status_code == 403
    # Not a picture.
    bad = await client.post(_g(asha, "/photo"), files={"file": ("x.jpg", b"not a picture at all", "image/jpeg")}, headers=world["t"]["asha"])
    assert bad.status_code in (415, 422)
    # Removed: the photo and every render with it.
    key = row.photo_key
    r = await client.delete(_g(asha, "/photo"), headers=world["t"]["asha"])
    assert r.status_code == 200 and r.json()["photo"] is None and key not in bucket.held


async def test_framing_crops_exactly_the_window():
    # A 2000 x 1000 photo in a square frame: zoom 1 covers it, 1000 x 1000 of the photo.
    assert photos.window(2000, 1000, 500, 500, {"zoom": 1, "x": 0.5, "y": 0.5}) == (500.0, 0.0, 1000.0, 1000.0)
    # Zoom 2, centred on the left quarter: a 500 x 500 window, clamped inside the photo.
    assert photos.window(2000, 1000, 500, 500, {"zoom": 2, "x": 0.25, "y": 0.5}) == (250.0, 250.0, 500.0, 500.0)
    assert photos.window(2000, 1000, 500, 500, {"zoom": 2, "x": 0.0, "y": 0.0}) == (0.0, 0.0, 500.0, 500.0)
    raw = _jpeg((2000, 1000))
    cut = photos.crop(raw, 400, 300, {"zoom": 1, "x": 0.5, "y": 0.5})
    with Image.open(io.BytesIO(cut)) as img:
        assert abs(img.width / img.height - 400 / 300) < 0.01 and img.width <= 600
    with pytest.raises(ValueError):
        photos.checked_framing({"zoom": 9})
    with pytest.raises(ValueError):
        photos.checked_framing({"x": -0.1})


async def test_framing_is_saved_per_format_and_changes_the_poster(client, world, bucket):
    await _turn_on(client, world)
    asha = world["regs"]["asha"]
    h = world["t"]["asha"]
    assert (await client.put(_g(asha, "/framing"), json={"zoom": 2}, headers=h)).status_code == 409  # no photo yet
    await client.post(_g(asha, "/photo"), files={"file": ("p.jpg", _jpeg(), "image/jpeg")}, headers=h)
    before = (await client.get(_g(asha), headers=h)).json()["formats"][0]["render"]["fingerprint"]
    r = await client.put(_g(asha, "/framing"), json={"format": "square", "zoom": 1.5, "x": 0.4, "y": 0.3}, headers=h)
    assert r.status_code == 200
    sq, pt = r.json()["formats"][0], r.json()["formats"][1]
    assert sq["framing"] == {"zoom": 1.5, "x": 0.4, "y": 0.3} and pt["framing"] == photos.DEFAULT_FRAMING
    assert sq["render"]["fingerprint"] != before
    assert (await client.put(_g(asha, "/framing"), json={"zoom": 7}, headers=h)).status_code == 422


# ─── Rendering ──────────────────────────────────────────────────────────────


async def test_a_poster_is_drawn_once_kept_and_served(client, session, world, bucket, render_service):
    await _turn_on(client, world)
    asha = world["regs"]["asha"]
    h = world["t"]["asha"]
    first = await client.post(_g(asha, "/render"), json={"format": "story"}, headers=h)
    assert first.status_code == 202 and first.json()["status"] == "rendering"
    # The job ran after the request (as the API's runner would).
    state = await client.get(_g(asha, "/render/story"), headers=h)
    assert state.json()["status"] == "ready", state.text
    assert render_service.renders == 1
    page = render_service.last_html()
    share = (await client.get(_g(asha), headers=h)).json()["share_url"]
    assert "Asha Nair" in page and "I&#x27;m attending" in page and share in page
    assert 'class="moa-initials"' in page  # no photo: the initials tile
    png = await client.get(state.json()["image_url"].replace(settings.PUBLIC_API_URL, ""))
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(png.content)) as img:
        assert img.width == 1080
    etag = png.headers["etag"]
    assert (await client.get(_g(asha, "/image/story"), headers={**h, "If-None-Match": etag})).status_code == 304
    dl = await client.get(_g(asha, "/image/story"), params={"download": 1}, headers=h)
    assert "attachment" in dl.headers["content-disposition"]
    # Asked again with the same inputs: ready at once, never drawn twice.
    again = await client.post(_g(asha, "/render"), json={"format": "story"}, headers=h)
    assert again.status_code == 200 and again.json()["status"] == "ready" and render_service.renders == 1
    # A photo changes the inputs; the earlier PNG goes once the new one is kept.
    old_keys = {k for k in bucket.held if k.endswith(".png")}
    await client.post(_g(asha, "/photo"), files={"file": ("p.jpg", _jpeg(), "image/jpeg")}, headers=h)
    assert not (old_keys & set(bucket.held))  # a new photo drops the renders made without it
    r = await client.post(_g(asha, "/render"), json={"format": "story"}, headers=h)
    assert r.status_code == 202 and render_service.renders == 2
    assert "data:image/jpeg;base64," in render_service.last_html()
    row = await session.scalar(select(GuestPosters).execution_options(populate_existing=True))
    assert row.made_at is not None and row.render_count == 2 and row.renders["story"]["moment"] == "before"
    assert (await client.get(_g(asha, "/image/square"), headers=h)).status_code == 404


async def test_a_render_that_fails_says_try_again_and_touches_nothing_else(client, session, world, bucket, render_service):
    await _turn_on(client, world)
    asha = world["regs"]["asha"]
    h = world["t"]["asha"]
    render_service.down = True
    r = await client.post(_g(asha, "/render"), json={"format": "square"}, headers=h)
    assert r.status_code == 202
    assert (await client.get(_g(asha, "/render/square"), headers=h)).json()["status"] == "failed"
    busy = await client.post(_g(asha, "/render"), json={"format": "square"}, headers=h)
    assert busy.status_code == 503 and busy.json()["error_code"] == "POSTER_RENDER_BUSY" and busy.json()["message"] == "Try again in a moment."
    # The ticket itself still answers.
    assert (await client.get(f"/api/v1/events/tickets/{asha.ticket_id}", headers=h)).status_code == 200
    render_service.down = False
    row = await session.scalar(select(GuestPosters))
    await render.set_state(session, row.id, "square", {**row.renders["square"], "at": (NOW - timedelta(minutes=5)).isoformat()})
    await session.commit()
    assert (await client.post(_g(asha, "/render"), json={"format": "square"}, headers=h)).status_code == 202
    assert (await client.get(_g(asha, "/render/square"), headers=h)).json()["status"] == "ready"


async def test_posters_render_at_low_priority_one_at_a_time():
    h = jobs.handlers()[render.JOB]
    assert h.priority == jobs.LOW and not h.urgent and jobs.limit_for(h, 4) == 1
    # What someone is waiting on claims before it.
    assert jobs.handlers()["checkins.after"].priority > h.priority and jobs.handlers()["checkins.after"].urgent


async def test_new_renders_are_limited_per_guest(client, world, bucket, render_service, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    await _turn_on(client, world)
    asha = world["regs"]["asha"]
    h = world["t"]["asha"]
    await client.post(_g(asha, "/photo"), files={"file": ("p.jpg", _jpeg(), "image/jpeg")}, headers=h)
    codes = []
    for i in range(7):
        await client.put(_g(asha, "/framing"), json={"zoom": 1 + i / 10}, headers=h)
        codes.append((await client.post(_g(asha, "/render"), json={"format": "square"}, headers=h)).status_code)
    assert codes[:6] == [202] * 6 and codes[6] == 429
    # Asking again for what is already drawn costs nothing.
    await client.put(_g(asha, "/framing"), json={"zoom": 1.5}, headers=h)
    assert (await client.post(_g(asha, "/render"), json={"format": "square"}, headers=h)).status_code == 200
    limiter.reset()


# ─── Variants and values ────────────────────────────────────────────────────


def _layout(fmt, text="{{moment_phrase}} {{name}}"):
    layout = poster.starter(poster.BLANK, fmt)
    layout["layers"].append({"id": "mark", "name": "Mark", "kind": "text", "x": 40, "y": 40, "width": 600, "height": 40, "text": text,
                             "font": {"family": "Inter", "weight": 400, "style": "normal"}, "size": 24})
    return layout


async def test_variants_resolve_role_then_ticket_type_then_default(client, session, world, bucket, render_service):
    from app.api.ticketing.models import TicketTypes

    await _turn_on(client, world)
    vip = TicketTypes(event_id=world["event"].id, name="VIP")
    session.add(vip)
    await session.flush()
    world["regs"]["asha"].ticket_type_id = vip.id
    await session.commit()
    put = lambda v, text: client.put(_o(world, "/designs/poster/layout"), params={"variant": v, "format": "square"},  # noqa: E731
                                     json=_layout("square", text), headers=world["host"])
    assert (await put("default", "DEFAULT {{name}}")).status_code == 200
    asha, sita = world["regs"]["asha"], world["regs"]["sita"]
    sq = lambda reg, who: client.get(_g(reg), headers=world["t"][who])  # noqa: E731
    assert (await sq(asha, "asha")).json()["formats"][0]["variant"] == "default"
    assert (await sq(asha, "asha")).json()["formats"][1]["variant"] == "starter"  # no portrait saved: the platform's
    assert (await put(f"type:{vip.id}", "VIP {{ticket_type}}")).status_code == 200
    assert (await put("role:speaker", "SPEAKER {{talk_title}} {{talk_time}} {{talk_hall}}")).status_code == 200
    assert (await sq(asha, "asha")).json()["formats"][0]["variant"] == f"type:{vip.id}"
    s = (await sq(sita, "sita")).json()
    assert s["formats"][0]["variant"] == "role:speaker" and s["guest"]["role"] == "speaker"
    await client.post(_g(sita, "/render"), json={"format": "square"}, headers=world["t"]["sita"])
    assert "SPEAKER Building for Bharat" in render_service.last_html() and "Hall B" in render_service.last_html()
    await client.post(_g(asha, "/render"), json={"format": "square"}, headers=world["t"]["asha"])
    assert "VIP VIP" in render_service.last_html()
    listed = (await client.get(_o(world, "/designs/poster/variants"), headers=world["host"])).json()
    assert [v["variant"] for v in listed["variants"]] == ["default", "role:speaker", f"type:{vip.id}"]
    assert {"variant": f"type:{vip.id}", "label": "Ticket: VIP"} in listed["options"]


async def test_values_for_a_guest(session, world):
    from app.api.doc_presets.values import subject_of
    from app.api.guest_posters import values as v

    regs = world["regs"]
    asha = await service.registration_by_ticket(session, regs["asha"].ticket_id)
    sita = await service.registration_by_ticket(session, regs["sita"].ticket_id)
    event = asha.event
    assert await v.poster_role(session, event, asha) == "attendee"
    assert await v.poster_role(session, event, sita) == "speaker"
    talk = await v.talk_of(session, event, sita)
    assert (talk.title, talk.hall) == ("Building for Bharat", "Hall B") and re.fullmatch(r"\d\d:\d\d", talk.time)
    assert (await v.talk_of(session, event, asha)).title == ""
    subject = subject_of(asha, event)
    assert v.organisation_of(event, asha, subject) == "CET Trivandrum"
    values = v.poster_values(event, asha, subject, role="attendee", organisation="CET Trivandrum", ticket_type="", talk=v.Talk(),
                             moment_phrase="I'm attending", share_url="https://myotherapp.in/l/abc")
    for token in ("name", "first_name", "initials", "role", "role_label", "ticket_type", "organisation", "talk_title", "talk_time",
                  "talk_hall", "event_name", "event_date", "event_time", "venue", "city", "club_name", "moment_phrase", "share_url"):
        assert token in values
    assert values["club_name"] == "GDG Kochi" and values["initials"] == "AN" and values["name"] == "Asha Nair"


async def test_volunteers_and_organisers_get_their_variant(session, world):
    from app.api.club_access.models import ClubMembers
    from app.api.events.volunteer.models import Volunteer
    from app.api.guest_posters import values as v

    event = world["event"]
    nina, ravi = world["regs"]["nina"], world["regs"]["ravi"]
    session.add(Volunteer(email=nina.email.upper(), event_id=event.id, is_approved=True))
    session.add(ClubMembers(club_id=event.club_id, email=ravi.email, role="editor", status="active"))
    await session.commit()
    assert await v.poster_role(session, event, nina) == "volunteer"
    assert await v.poster_role(session, event, ravi) == "organiser"


# ─── The organiser ──────────────────────────────────────────────────────────


async def test_settings_by_role_cohost_and_stranger(client, session, world, mint_token):
    from app.api.auth.schemas import AuthTokenData
    from app.api.club_access.models import ClubMembers
    from app.core.auth.jwt import create_access_token

    url = _o(world, "/posters/settings")
    assert (await client.put(url, json={"enabled": True}, headers=world["cohost"])).status_code == 200
    assert (await client.put(url, json={"enabled": True}, headers=world["other"])).status_code in (403, 404)
    assert (await client.get(url, headers=world["t"]["asha"])).status_code == 403

    async def member(role):
        m = ClubMembers(club_id=world["club"].id, email=f"{role}-team@example.com", role=role, status="active")
        session.add(m)
        await session.commit()
        owner = world["owner"]
        await session.refresh(owner)
        token = create_access_token(data=AuthTokenData(user_id=owner.id, token_type="access_token", token_version=owner.token_version,
                                                      member_id=m.id).model_dump())
        return auth_header(token)

    door, editor = await member("door"), await member("editor")
    assert (await client.get(url, headers=door)).status_code == 200
    assert (await client.get(_o(world, "/posters/stats"), headers=door)).status_code == 200
    refused = await client.put(url, json={"enabled": False}, headers=door)
    assert refused.status_code == 403 and refused.json()["error_code"] == "ROLE_FORBIDDEN"
    assert (await client.put(_o(world, "/designs/poster/layout"), json=_layout("square"), headers=door)).status_code == 403
    ok = await client.put(url, json={"moments": {"before": {"phrase": "  I'm   going to  ", "caption": "See you at {{event_name}} {{share_url}}"}},
                                     "formats": ["square", "story"]}, headers=editor)
    assert ok.status_code == 200 and ok.json()["moments"]["before"]["phrase"] == "I'm going to" and ok.json()["formats"] == ["square", "story"]
    bad = await client.put(url, json={"moments": {"before": {"caption": "{{nope}}"}}}, headers=editor)
    assert bad.status_code == 422 and "nope" in bad.json()["message"]
    assert (await client.put(url, json={"formats": []}, headers=editor)).status_code == 422
    assert (await client.put(url, json={"moments": {"before": {"phrase": ""}}}, headers=editor)).status_code == 422


async def test_the_board_starters_save_apply_preview_and_drop(client, world, bucket, render_service):
    base = _o(world, "/designs/poster")
    h = world["host"]
    board = (await client.get(f"{base}/layout", params={"variant": "role:speaker", "format": "portrait"}, headers=h)).json()
    assert board["from"] == "starter" and board["layout"]["canvas"] == {**board["layout"]["canvas"], "width": 1080, "height": 1350}
    assert any(t["key"] == "moment_phrase" for t in board["tokens"]) and board["values"]["role"] == "speaker"
    assert board["page"] == {"widthMm": 285.75, "heightMm": 357.1875}
    starters = (await client.get(f"{base}/starters", headers=h)).json()
    assert starters and starters[-1]["id"] == poster.BLANK and starters[-1]["formats"] == ["square", "portrait", "story"]
    copy = (await client.post(f"{base}/layout/starters/{poster.BLANK}", params={"format": "story"}, headers=h)).json()
    assert copy["layout"]["canvas"]["height"] == 1920 and set(copy["formats"]) == {"square", "portrait", "story"}
    # The wrong format, a ticket's canvas: refused.
    wrong = await client.put(f"{base}/layout", params={"variant": "default", "format": "story"}, json=_layout("square"), headers=h)
    assert wrong.status_code == 422 and wrong.json()["error_code"] == "INVALID_LAYOUT"
    bad = _layout("square")
    bad["canvas"]["width"] = 1000
    r = await client.put(f"{base}/layout", params={"variant": "default", "format": "square"}, json=bad, headers=h)
    assert r.status_code == 422
    # A personal QR is the ID card's alone; an unbound QR (the share link) is fine.
    with_qr = _layout("square")
    with_qr["layers"].append({"id": "qr", "name": "QR", "kind": "qr", "x": 900, "y": 900, "width": 140, "height": 140})
    saved = await client.put(f"{base}/layout", params={"variant": "default", "format": "square"}, json=with_qr, headers=h)
    assert saved.status_code == 200 and saved.json()["from"] == "saved" and saved.json()["revision"] == 1
    applied = await client.post(f"{base}/apply", json={"starter_id": poster.BLANK, "variant": "role:speaker"}, headers=h)
    assert applied.status_code == 200
    speaker = next(v for v in applied.json()["variants"] if v["variant"] == "role:speaker")
    assert all(f["saved"] for f in speaker["formats"].values())
    html = await client.post(f"{base}/layout/preview", params={"variant": "default", "format": "square", "as": "html"}, json={}, headers=h)
    assert html.status_code == 200 and "Aisha Rahman" in html.json()["html"]
    png = await client.post(f"{base}/layout/preview", params={"variant": "default", "format": "square"},
                            json={"ticket_id": world["regs"]["asha"].ticket_id}, headers=h)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and "Asha Nair" in render_service.last_html()
    # A guest's photo is never in the organiser's preview.
    await _turn_on(client, world)
    await client.post(_g(world["regs"]["asha"], "/photo"), files={"file": ("p.jpg", _jpeg(), "image/jpeg")}, headers=world["t"]["asha"])
    await client.post(f"{base}/layout/preview", params={"variant": "default", "format": "square"},
                      json={"ticket_id": world["regs"]["asha"].ticket_id}, headers=h)
    assert "data:image/jpeg" not in render_service.last_html() and 'class="moa-initials"' in render_service.last_html()
    dropped = await client.delete(f"{base}/layout", params={"variant": "role:speaker"}, headers=h)
    assert [v["variant"] for v in dropped.json()["variants"]] == ["default"]
    assert (await client.delete(f"{base}/layout", params={"variant": "role:speaker"}, headers=h)).status_code == 404
    assert (await client.get(f"{base}/layout", params={"variant": "role:nobody"}, headers=h)).status_code == 422


# ─── Attribution and the numbers ────────────────────────────────────────────


async def test_a_guests_link_brings_people_counted_as_one_row(client, session, world, bucket, render_service, make_user, make_registration):
    from app.api.links import service as links_service

    await _turn_on(client, world)
    asha = world["regs"]["asha"]
    a = (await client.get(_g(asha), headers=world["t"]["asha"])).json()
    code = a["share_url"].rsplit("/", 1)[1]
    # Opened (the website's /l/ forwards to the API's), by a phone.
    opened = await client.get(f"/l/{code}", headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile Safari"}, follow_redirects=False)
    assert opened.status_code == 302 and f"ref={code}" in opened.headers["location"] and world["event"].slug in opened.headers["location"]
    assert await session.scalar(select(func.count()).select_from(TrackingClicks)) == 1
    # A sign-up carrying the code is credited to Asha's link by the existing machinery.
    friend = await make_user(email="friend-posters@example.com")
    reg = await make_registration(event=world["event"], user=friend, full_name="Friend One")
    await links_service.attribute_registration(session, reg, world["event"], code)
    await session.commit()
    guest_link = await session.scalar(select(TrackingLinks).where(TrackingLinks.code == code))
    assert reg.tracking_link_id == guest_link.id and guest_link.kind == poster_links.GUEST
    await client.post(_g(asha, "/render"), json={"format": "square"}, headers=world["t"]["asha"])
    for action in ("download", "share", "share", "copy_caption", "nonsense"):
        r = await client.post(_g(asha, "/events"), json={"action": action, "format": "square"}, headers=world["t"]["asha"])
        assert r.status_code == (422 if action == "nonsense" else 204)
    stats = (await client.get(_o(world, "/posters/stats"), headers=world["host"])).json()
    assert stats["posters_made"] == 1 and stats["link_clicks"] == 1 and stats["registrations"] == 1
    assert (stats["downloads"], stats["shares"], stats["copies"]) == (1, 2, 1) and stats["guests_eligible"] == 2
    assert stats["by_format"]["square"] == 1 and stats["by_moment"]["before"] == 1
    assert stats["top"][0] == {"name": "Asha Nair", "role": "attendee", "clicks": 1, "registrations": 1, "requests": 0}
    # The Tracking links page: one "Guest posters" row, never a row per guest.
    over = (await client.get(_o(world, "/links/overview"), headers=world["host"])).json()
    rows = [r for r in over["links"] if r["kind"] in ("poster", "poster_guest")]
    assert [(r["name"], r["kind"], r["clicks"], r["conversions"]) for r in rows] == [("Guest posters", "poster", 1, 1)]
    detail = (await client.get(_o(world, f"/links/{rows[0]['id']}/detail"), headers=world["host"])).json()
    assert detail["clicks"] == 1
    summary = (await client.get(_o(world, "/links"), headers=world["host"])).json()
    assert [(r["name"], r["clicks"], r["conversions"]) for r in summary["links"]] == [("Guest posters", 1, 1)]
    club_wide = (await client.get("/api/v1/clubs/links/overview", headers=world["host"])).json()
    assert [r["name"] for r in club_wide["links"] if r["kind"].startswith("poster")] == ["Guest posters"]


async def test_tracking_lists_without_posters_are_unchanged(client, session, world):
    session.add(TrackingLinks(club_id=world["club"].id, event_id=world["event"].id, name="Instagram bio", code="bcdfghjk",
                              utm_source="instagram", utm_medium="social"))
    await session.commit()
    assert await poster_links.fold(session, (await session.scalars(select(TrackingLinks))).all()) == {}
    over = (await client.get(_o(world, "/links/overview"), headers=world["host"])).json()
    assert [(r["name"], r["clicks"], r["conversions"]) for r in over["links"]] == [("Instagram bio", 0, 0)]


# ─── Retention ──────────────────────────────────────────────────────────────


async def test_the_sweep_removes_photos_and_posters_after_30_days(client, session, world, bucket, render_service):
    from app.api.guest_posters import sweep

    await _turn_on(client, world)
    asha, sita = world["regs"]["asha"], world["regs"]["sita"]
    for reg, who in ((asha, "asha"), (sita, "sita")):
        await client.post(_g(reg, "/photo"), files={"file": ("p.jpg", _jpeg(), "image/jpeg")}, headers=world["t"][who])
        await client.post(_g(reg, "/render"), json={"format": "square"}, headers=world["t"][who])
    assert len(bucket.held) == 4
    # Not yet: the event has not happened.
    assert (await sweep.sweep(session))["posters"] == 0
    # Sita's ticket revoked: hers go at once.
    sita.ticket_revoked_at = NOW
    await session.commit()
    assert (await sweep.sweep(session)) == {"posters": 1, "objects": 2} and len(bucket.held) == 2
    # Ended 10 days ago: kept; 31 days ago: gone, the counts kept.
    world["event"].event_datetime = NOW - timedelta(days=10)
    await session.commit()
    assert (await sweep.sweep(session))["posters"] == 0
    world["event"].event_datetime = NOW - timedelta(days=31)
    await session.commit()
    assert (await sweep.sweep(session))["posters"] == 1 and not bucket.held
    row = await session.scalar(select(GuestPosters).where(GuestPosters.registration_id == asha.id).execution_options(populate_existing=True))
    assert row.photo_key is None and row.renders == {} and row.purged_at is not None and row.render_count == 1
    assert (await sweep.sweep(session))["posters"] == 0


# ─── Existing documents ─────────────────────────────────────────────────────


async def test_other_kinds_catalogues_are_unchanged():
    from app.core.layout_v2 import vocabulary as tk

    poster_only = {"name", "role_label", "organisation", "talk_title", "talk_time", "talk_hall", "club_name", "moment_phrase", "share_url"}
    for kind in ("ticket", "certificate", "badge"):
        assert not (tk.design_tokens(kind) & poster_only)
        assert all(t["kinds"] != ["poster"] for t in tk.tokens(kind))
    assert not (tk.all_design_tokens() & poster_only)
    assert tk.design_tokens("poster") >= poster_only
    assert tk.parse("{{moment_phrase}} {{name | upper}} {{share_url}}", "poster").errors == []
    assert tk.parse("{{moment_phrase}}", "ticket").errors


async def test_poster_layouts_validate_by_format():
    from app.core import layout_v2

    for fmt in poster.ORDER:
        layout = poster.starter(poster.BLANK, fmt)
        assert poster.format_of(layout) == fmt
        assert [p for p in layout_v2.problems(layout, "poster") if p["level"] == "error"] == []
        assert "poster_format" in {p["code"] for p in layout_v2.problems({**layout, "page": {"widthMm": 100, "heightMm": 100}}, "poster")}
    # A ticket's layout is not a poster; the poster check never runs for a ticket.
    ticket = layout_v2.starter("ticket/blank")
    assert "poster_format" not in {p["code"] for p in layout_v2.problems(ticket, "ticket")}
    assert "poster_format" in {p["code"] for p in layout_v2.problems(ticket, "poster")}


async def test_settings_rows_are_only_made_by_the_organiser(session, world):
    assert await session.get(PosterSettings, world["event"].id) is None
    assert mom.moments_of(None)["before"]["enabled"] is True
    assert service.formats_of(None) == ["square", "portrait", "story"]


async def test_the_registration_query_reads_removed_ones_too(session, world):
    cara = world["regs"]["cara"]
    found = await service.registration_by_ticket(session, cara.ticket_id)
    assert isinstance(found, EventRegistrationsLink) and found.is_deleted


# ─── Contact details: never on a poster ─────────────────────────────────────


async def test_a_poster_design_cannot_print_contact_details(client, session, world):
    from app.core import layout_v2

    def codes(layout, kind="poster"):
        return [p["code"] for p in layout_v2.problems(layout, kind) if p["level"] == "error"]

    for text in ("{{email}}", "Mail me: {{ email | upper }}", "{% if email %}x{% endif %}", "{{field.phone_number}}", "{{field.whatsapp}}",
                 "{{field.mobile_no}}", "{{field.email_id}}"):
        assert codes(_layout("square", text)) == ["poster_contact"], text
    spans = _layout("square")
    spans["layers"][-1] = {**spans["layers"][-1], "spans": [{"text": "Hi "}, {"text": "{{email}}"}]}
    spans["layers"][-1].pop("text", None)
    assert codes(spans) == ["poster_contact"]
    shown = _layout("square", "{{name}}")
    shown["layers"][-1]["showIf"] = "email"
    assert codes(shown) == ["poster_contact"]
    message = next(p for p in layout_v2.problems(_layout("square", "{{email}}"), "poster") if p["code"] == "poster_contact")["message"]
    assert message == "Mark: a poster is shared in public, so it cannot show an email address or phone number ({{email}})."
    # Its other words, and every other kind, as before.
    assert codes(_layout("square", "{{name}} · {{field.college}} · {{organisation}}")) == []
    ticket = layout_v2.starter("ticket/blank")
    ticket["layers"][0]["text"] = "{{email}}"
    assert "poster_contact" not in codes(ticket, "ticket")

    # The board: a question the form asks as a phone number is refused by name, whatever its name says.
    world["event"].additional_details = [
        {"key": "college", "label": "College", "field_type": "text"},
        {"key": "reach_me", "label": "How to reach you", "field_type": "tel"},
        {"key": "work_address", "label": "Work address", "field_type": "email"},
    ]
    await session.commit()
    base = _o(world, "/designs/poster")
    for text in ("{{field.reach_me}}", "{{field.how_to_reach_you}}", "{{field.work_address}}", "{{email}}"):
        r = await client.put(f"{base}/layout", params={"variant": "default", "format": "square"}, json=_layout("square", text), headers=world["host"])
        assert r.status_code == 422 and r.json()["error_code"] == "INVALID_LAYOUT" and "email address or phone number" in r.json()["message"], text
    ok = await client.put(f"{base}/layout", params={"variant": "default", "format": "square"}, json=_layout("square", "{{field.college}}"),
                          headers=world["host"])
    assert ok.status_code == 200
    keys = {t["key"] for t in ok.json()["tokens"]}
    assert "field.college" in keys and not keys & {"email", "field.reach_me", "field.how_to_reach_you", "field.work_address"}
    # Nor in a caption, which is shared with the poster.
    url = _o(world, "/posters/settings")
    for caption in ("Write to {{email}}", "Call {{field.reach_me}}", "{{field.phone}}"):
        r = await client.put(url, json={"moments": {"before": {"caption": caption}}}, headers=world["host"])
        assert r.status_code == 422 and "email address or phone number" in r.json()["message"], caption


async def test_contact_details_never_reach_the_poster_builder(client, session, world, bucket, render_service):
    from app.api.doc_presets.values import subject_of
    from app.api.guest_posters import values as v
    from app.api.guest_posters.models import PosterDesigns

    world["event"].additional_details = [
        {"key": "college", "label": "College", "field_type": "text"},
        {"key": "reach_me", "label": "How to reach you", "field_type": "tel"},
    ]
    asha = world["regs"]["asha"]
    asha.additional_details = {"college": "CET Trivandrum", "reach_me": "+91 98470 12345", "phone": "+91 90000 00000"}
    # A design that names them anyway (saved around the checks, as an old row or a script might).
    session.add(PosterDesigns(event_id=world["event"].id, variant="default", revision=1,
                              layouts={"square": _layout("square", "{{email}}|{{field.reach_me}}|{{field.phone}}|{{field.college}}")}))
    await session.commit()
    reg = await service.registration_by_ticket(session, asha.ticket_id)
    values = v.poster_values(reg.event, reg, subject_of(reg, reg.event), role="attendee", organisation="", ticket_type="", talk=v.Talk(),
                             moment_phrase="I'm attending", share_url="https://myotherapp.in/l/abc")
    assert "email" not in values and "field.reach_me" not in values and "field.how_to_reach_you" not in values and "field.phone" not in values
    assert values["field.college"] == "CET Trivandrum"
    assert asha.email not in values.values() and "+91 98470 12345" not in values.values()

    await _turn_on(client, world)
    r = await client.post(_g(asha, "/render"), json={"format": "square"}, headers=world["t"]["asha"])
    assert r.status_code == 202
    page = render_service.last_html()
    assert "CET Trivandrum" in page and asha.email not in page and "98470" not in page and "90000" not in page
