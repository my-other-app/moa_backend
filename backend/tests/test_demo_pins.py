"""The demo's venue pins (scripts/demo): every in-person venue in the data
has its own point and the seed pins the event there, not beside its club;
and fix_pins moves the pins of a demo seeded before, touching only demo
clubs' events whose pin is still the seed's.

Every test takes the session (tests/test_demo_seed.py says why)."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.discover import geo
from app.api.events.models import Events
from scripts.demo import fix_pins
from scripts.demo.local_bucket import local_bucket
from scripts.demo.quiet import quiet
from scripts.demo.schema import DEMO_DOMAIN, load
from scripts.demo.seed import Seeder, maps_link, venue_line
from tests.test_demo_seed import _data_dir, reference  # noqa: F401 (a fixture)

VAGAMON = (9.6862, 76.9052)


def _by_slug():
    clubs, _ = load()
    return {c.slug: c for c in clubs}


# ─── The data and the seed ────────────────────────────────────────────────


async def test_every_in_person_venue_has_its_own_point(session):
    clubs = _by_slug()
    borrow = clubs["borrow-checkers-kochi"]
    vagamon = next(e for e in borrow.events if e.slug == "systems-weekend-vagamon")
    assert (vagamon.venue.lat, vagamon.venue.lng) == VAGAMON
    # Some 73 km from the club in Kochi, beside which the seed used to pin it.
    assert 70 < geo.km(borrow.location.lat, borrow.location.lng, *VAGAMON) < 78
    for c in clubs.values():
        for e in c.events:
            if e.format == "in_person":
                assert 8.0 <= e.venue.lat <= 12.9 and 74.8 <= e.venue.lng <= 77.5, e.slug


async def test_the_seed_pins_an_event_at_its_venue(session, reference, tmp_path, monkeypatch):  # noqa: F811
    from app.api.events import doc_cache

    monkeypatch.setattr(doc_cache.settings, "RENDER_DOC_CACHE", False, raising=False)

    def out_of_town(club):
        club["events"][0]["venue"] = {"name": "Ilaveyil Farmstay", "address": "Kolahalamedu Road, Vagamon, Idukki 685503",
                                      "lat": VAGAMON[0], "lng": VAGAMON[1]}

    clubs, people = load(_data_dir(tmp_path, mutate=out_of_town), include_example=True)
    with local_bucket(tmp_path / "bucket"), quiet():
        await Seeder(session, clubs, people, main_email=f"demo.user@{DEMO_DOMAIN}", main_password="DemoPass123!",
                     club_password="ClubPass123!", out=lambda *_: None).run()
    for e in clubs[0].events:
        ev = await session.scalar(select(Events).where(Events.slug == e.slug))
        assert (ev.lat, ev.lng) == (e.venue.lat, e.venue.lng), e.slug
        assert ev.location_link == maps_link(e.venue.lat, e.venue.lng) and ev.geo_source == geo.MANUAL


# ─── fix_pins ─────────────────────────────────────────────────────────────


def _seed_pin(club, dlat, dlng):
    """A pin as the old seed made it: the club's point, scattered."""
    return (club.location.lat + dlat, club.location.lng + dlng)


@pytest.fixture
async def seeded_before(session, make_club, make_event):
    """Two demo clubs as the old seed left them, one event of each changed by
    hand since, and a real club that took a demo club's slug after the demo
    was removed."""
    data = _by_slug()
    borrow_data, tide_data = data["borrow-checkers-kochi"], data["neural-tide"]

    owner, borrow = await make_club(name="Borrow Checkers Kochi", email=f"borrow-checkers-kochi@{DEMO_DOMAIN}")
    borrow.slug, borrow.contact_email = "borrow-checkers-kochi", borrow_data.contact.email
    real_owner, real = await make_club(name="Neural Tide", email="founder@realclub.example")
    real.slug, real.contact_email = "neural-tide", "hello@realclub.example"
    await session.flush()

    events = {e.slug: e for e in borrow_data.events}

    async def as_seeded(club, owner, e, pin, **kw):
        kw.setdefault("location_link", maps_link(*pin))
        return await make_event(club=club, created_by=owner, name=e.name, slug=e.slug, lat=pin[0], lng=pin[1], geo_source=geo.MANUAL,
                                location_name=venue_line(e.venue.name, e.venue.address), is_online=False, **kw)

    rows = dict(
        vagamon=await as_seeded(borrow, owner, events["systems-weekend-vagamon"], _seed_pin(borrow_data, 0.0021, -0.0013)),
        meetup=await as_seeded(borrow, owner, events["meetup-27-async-without-tears"], _seed_pin(borrow_data, -0.0039, 0.0004)),
        # Moved on the dashboard's map since.
        dragged=await as_seeded(borrow, owner, events["embedded-rust-night-blink-then-think"], (10.0512, 76.3301)),
        # Its map link replaced since.
        relinked=await as_seeded(borrow, owner, events["meetup-29-profiling-in-production"], _seed_pin(borrow_data, 0.001, 0.001),
                                 location_link="https://maps.app.goo.gl/AbCdEf123"),
        # A real club's event that happens to look seeded.
        real=await as_seeded(real, real_owner, next(e for e in tide_data.events if e.slug == "tide-hack-2026"), _seed_pin(tide_data, 0.002, 0.002)),
    )
    await session.commit()
    pins = {k: (v.lat, v.lng, v.location_link) for k, v in rows.items()}
    return SimpleNamespace(clubs=[borrow_data, tide_data], rows=rows, pins=pins, borrow=borrow_data)


async def _pins(session, seeded):
    out = {}
    for key, ev in seeded.rows.items():
        await session.refresh(ev)
        out[key] = (ev.lat, ev.lng, ev.location_link)
    return out


async def test_a_dry_run_says_what_would_move_and_changes_nothing(session, seeded_before):
    report = await fix_pins.fix(session, seeded_before.clubs, dry_run=True)
    assert sorted(m.event for m in report.moved) == ["meetup-27-async-without-tears", "systems-weekend-vagamon"]
    vagamon = next(m for m in report.moved if m.event == "systems-weekend-vagamon")
    assert vagamon.new == VAGAMON and 70 < vagamon.km < 78
    assert {(c, e) for c, e, _ in report.kept} == {
        ("borrow-checkers-kochi", "embedded-rust-night-blink-then-think"), ("borrow-checkers-kochi", "meetup-29-profiling-in-production"),
    }
    # The real club that took a demo slug is no demo club.
    assert "club neural-tide" in report.missing and report.clubs == 1
    assert await _pins(session, seeded_before) == seeded_before.pins
    text = fix_pins.render(report, dry_run=True)
    assert "2 pin(s) would move" in text and "Dry run: nothing changed" in text


async def test_the_fix_moves_only_the_seeds_pins_and_can_run_again(session, seeded_before):
    report = await fix_pins.fix(session, seeded_before.clubs, dry_run=False)
    assert len(report.moved) == 2
    after = await _pins(session, seeded_before)
    meetup_venue = next(e.venue for e in seeded_before.borrow.events if e.slug == "meetup-27-async-without-tears")
    assert after["vagamon"] == (*VAGAMON, maps_link(*VAGAMON))
    assert after["meetup"] == (meetup_venue.lat, meetup_venue.lng, maps_link(meetup_venue.lat, meetup_venue.lng))
    # What a person changed, and the real club's event, are as they were.
    for key in ("dragged", "relinked", "real"):
        assert after[key] == seeded_before.pins[key], key
    assert seeded_before.rows["vagamon"].geo_source == geo.MANUAL

    again = await fix_pins.fix(session, seeded_before.clubs, dry_run=False)
    assert again.moved == [] and len(again.right) == 2 and len(again.kept) == 2
    assert await _pins(session, seeded_before) == after
