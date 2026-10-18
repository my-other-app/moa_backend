"""A published club site does not go stale (app/api/sites/sweep.py).

The page is a stored snapshot. Past events stayed under "Upcoming" with a
Register button until something happened to republish it, and neither the
clock nor a profile, team or partner edit ever did.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from app.api.clubs.models import ClubPartners, ClubTeamMembers
from app.api.closeout.models import EventStatus
from app.api.sites import sweep
from app.api.sites.models import ClubSites
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, mint_token):
    owner, club = await make_club(name="Circuit Society", email="refresh@example.com")
    club.about = "Makers in Pune."
    soon = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=NOW + timedelta(days=9))
    await session.commit()
    h = auth_header(await mint_token(owner))
    assert (await client.post("/api/v1/clubs/site/publish", headers=h)).status_code == 200
    return dict(owner=owner, club=club, soon=soon, h=h)


async def _site(session, club) -> ClubSites:
    return (await session.execute(select(ClubSites).where(ClubSites.club_id == club.id).execution_options(populate_existing=True))).scalar_one()


async def _made_at(session, club, when: datetime) -> None:
    site = await _site(session, club)
    site.published_at = when
    await session.commit()


async def test_a_page_just_published_is_not_made_again(session, world):
    assert await sweep.due(session, datetime.now(timezone.utc)) == []
    assert await sweep.refresh(session) == 0


async def test_an_event_whose_start_has_passed_moves_to_past_events(session, world, make_event):
    """The event was ahead when the page was made, and has started since."""
    club = world["club"]
    started = await make_event(club=club, created_by=world["owner"], name="Robo Sumo", event_datetime=NOW - timedelta(hours=1), lifecycle=EventStatus.published)
    started.updated_at = NOW - timedelta(days=3)  # nothing saved it since
    site = await _site(session, club)
    site.html = site.html.replace("Past events", "")  # the page as it was: Robo Sumo upcoming
    site.published_at = NOW - timedelta(days=2)
    world["soon"].updated_at = NOW - timedelta(days=3)
    club.updated_at = NOW - timedelta(days=3)
    await session.commit()

    assert await sweep.due(session, datetime.now(timezone.utc)) == [club.id]
    assert await sweep.refresh(session) == 1
    html = (await _site(session, club)).html
    past = html.split("Past events", 1)
    assert len(past) == 2 and "Robo Sumo" in past[1] and "Robo Sumo" not in html.split("Upcoming events", 1)[1].split("</section>", 1)[0]
    assert await sweep.due(session, datetime.now(timezone.utc)) == []


async def test_the_status_clock_moving_an_event_makes_the_page_again(session, world):
    from app.api.closeout.service import advance_clock

    soon = world["soon"]
    soon.status_clock = True
    soon.event_datetime = NOW - timedelta(minutes=5)
    await session.commit()
    await _made_at(session, world["club"], datetime.now(timezone.utc))

    assert await advance_clock(session) == 1  # published -> live
    assert world["club"].id in await sweep.due(session, datetime.now(timezone.utc))


async def test_an_event_deleted_since_makes_the_page_again(session, world):
    await _made_at(session, world["club"], datetime.now(timezone.utc) - timedelta(seconds=1))
    world["soon"].soft_delete()
    await session.commit()

    assert await sweep.due(session, datetime.now(timezone.utc)) == [world["club"].id]
    assert await sweep.refresh(session) == 1
    assert "Hack Night" not in (await _site(session, world["club"])).html


@pytest.mark.parametrize("change", ["profile", "team", "partner"])
async def test_a_profile_team_or_partner_change_makes_the_page_again(session, world, change):
    club = world["club"]
    await _made_at(session, club, datetime.now(timezone.utc) - timedelta(seconds=1))
    if change == "profile":
        club.about = "Robots, mostly."
    elif change == "team":
        session.add(ClubTeamMembers(club_id=club.id, name="Asha Rao", role="Lead"))
    else:
        session.add(ClubPartners(club_id=club.id, name="Acme Labs"))
    await session.commit()

    assert await sweep.due(session, datetime.now(timezone.utc)) == [club.id]
    assert await sweep.refresh(session) == 1
    html = (await _site(session, club)).html
    assert {"profile": "Robots, mostly.", "team": "Asha Rao", "partner": "Acme Labs"}[change] in html


async def test_every_published_page_is_made_again_overnight(session, world):
    club = world["club"]
    await _made_at(session, club, NOW - timedelta(days=3))
    for row in (club, world["soon"]):
        row.updated_at = NOW - timedelta(days=4)
    await session.commit()
    ist = timezone(timedelta(hours=5, minutes=30))
    night = datetime.now(ist).replace(hour=2, minute=30).astimezone(timezone.utc)
    noon = night.astimezone(ist).replace(hour=12).astimezone(timezone.utc)

    assert await sweep.due(session, noon) == []
    assert await sweep.due(session, night) == [club.id]


async def test_the_sweep_takes_a_batch_at_a_time(session, world, make_club, client, mint_token):
    clubs = [world["club"]]
    for i in range(3):
        owner, other = await make_club(name=f"Batch {i}", email=f"batch-{i}@example.com")
        await session.commit()
        await client.post("/api/v1/clubs/site/publish", headers=auth_header(await mint_token(owner)))
        clubs.append(other)
    for c in clubs:
        await _made_at(session, c, NOW - timedelta(days=1))
        c.about = "changed"
    await session.commit()

    assert len(await sweep.due(session, datetime.now(timezone.utc), limit=2)) == 2
    assert await sweep.refresh(session, limit=2) == 2
    assert len(await sweep.due(session, datetime.now(timezone.utc))) == 2


async def test_the_page_is_read_in_one_query(session, world):
    """It loaded the club with every one of its interests, which the page never shows."""
    from app.api.sites.service import published_html

    statements = []
    engine = session.bind.engine.sync_engine if hasattr(session.bind, "engine") else session.bind.sync_engine

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        html = await published_html(session, world["club"].slug)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert html and len(statements) == 1


async def test_the_image_route_signs_with_the_shared_signer(client, world, monkeypatch):
    """A new boto3 client per image request: ~4 ms of service model each."""
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: pytest.fail("made a boto3 client per request"))
    r = await client.get(f"/api/v1/sites/{world['club'].slug}/i", params={"k": "clubs/logos/abc.medium.png"})

    assert r.status_code == 302 and "clubs/logos/abc.medium.png" in r.headers["location"]


async def test_the_republish_script_makes_every_published_page_again(session, world):
    """Run once on deploy, before the Worker sends the hashed policy: a page
    stored in the old form would otherwise show unstyled until the night."""
    from scripts.republish_sites import run

    site = await _site(session, world["club"])
    site.html = "<!doctype html><style>:root{--brand:#123456}</style>"
    await session.commit()

    assert await run(session) == {"published": 1, "made": 1}
    assert "--brand:#123456" not in (await _site(session, world["club"])).html
