"""scripts/demo/live_now.py: one of a demo club's events live now, with the
main demo user and demo members checked in, one of them hidden -- written
only inside the demo, sending nothing, and the same after a second run.

On the example club the seed makes (test_demo_seed's fixtures), inside the
test's transaction."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.api.checkins.models import CheckIns
from app.api.events.models import EventRegistrationsLink, Events
from app.api.users.models import UserInterests, Users
from scripts.demo import live_now
from scripts.demo.quiet import quiet
from scripts.demo.schema import DEMO_DOMAIN
from tests.conftest import auth_header
from tests.test_demo_seed import reference, seeded  # noqa: F401 (fixtures)

CLUB = "example-chai-coders"
MAIN = f"demo.user@{DEMO_DOMAIN}"


async def _state(session) -> dict:
    async def n(sql):
        return int(await session.scalar(text(sql)) or 0)

    return {
        "events": await n("SELECT count(*) FROM events"),
        "registrations": await n("SELECT count(*) FROM event_registrations_link"),
        "check_ins": await n("SELECT count(*) FROM check_ins"),
        "hidden": await n("SELECT count(*) FROM users WHERE NOT networking_visible"),
        "jobs": await n("SELECT count(*) FROM jobs"),
        "payment_orders": await n("SELECT count(*) FROM payment_orders"),
    }


async def _run(session, **kw):
    with quiet() as q:
        plan = await live_now.run(session, club_slug=CLUB, main_email=MAIN, people=kw.pop("people", 6), out=lambda *_: None, **kw)
    return plan, q


async def test_the_dry_run_writes_nothing(seeded, session):  # noqa: F811
    before = await _state(session)
    plan, q = await _run(session, dry_run=True)
    assert await _state(session) == before and q.sent == [] and q.dropped_jobs == []
    assert plan.creating and plan.networking
    assert len(plan.sharing) == 6 and plan.hidden == plan.sharing[0][0]
    assert plan.registering == plan.checking_in == 1 + 6 + len(plan.unmatched)
    assert "DRY RUN" in live_now.render(plan, dry_run=True)


async def test_an_event_live_now_with_people_like_you_and_one_hidden(seeded, session, client, mint_token):  # noqa: F811
    # Someone real who shares the main user's interests is never touched.
    from tests.conftest import _make_user

    real = await _make_user(session, email="student@example.com", username="realstudent")
    coding = await session.scalar(text("SELECT id FROM interests WHERE slug = 'coding'"))
    session.add(UserInterests(user_id=real.id, interest_id=coding))
    await session.commit()
    before = await _state(session)

    plan, q = await _run(session)
    now = datetime.now(timezone.utc)
    ev = await session.scalar(select(Events).where(Events.slug == live_now.slug_for(CLUB)))
    assert ev is not None and not ev.has_fee and ev.duration == live_now.LENGTH_HOURS
    assert now - timedelta(minutes=11) <= ev.event_datetime <= now - timedelta(minutes=9)
    regs = (await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == ev.id))).all()
    assert len(regs) == 1 + 6 + len(plan.unmatched) and all(r.is_attended for r in regs)
    assert real.id not in {r.user_id for r in regs}
    emails = (await session.scalars(select(Users.email).where(Users.id.in_([r.user_id for r in regs])))).all()
    assert all(e.endswith("@" + DEMO_DOMAIN) for e in emails)
    # Closed and full: nobody from the public gets in.
    assert ev.reg_enddate <= now and ev.max_participants == len(regs)

    # The main user sees everyone but the one hidden.
    main = await session.scalar(select(Users).where(Users.email == MAIN))
    h = auth_header(await mint_token(main))
    session.expunge_all()
    r = await client.get(f"/api/v1/events/{ev.id}/networking", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    names = [a["name"] for a in body["attendees"]]
    assert plan.hidden not in names and len(names) == len(regs) - 2
    assert body["matches"] == 5 and body["visible"] is True

    # Nothing sent, nothing queued, no money.
    after = await _state(session)
    assert q.sent == [] and after["jobs"] == before["jobs"] == 0 and after["payment_orders"] == before["payment_orders"]
    assert after["hidden"] == before["hidden"] + 1

    # Again: the same event moved to now, the same people, nobody added.
    plan2, q2 = await _run(session)
    assert plan2.registering == 0 and plan2.checking_in == 0 and not plan2.creating
    assert plan2.hidden == plan.hidden and q2.sent == []
    assert await _state(session) == after
    assert int(await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.event_id == ev.id))) == len(regs)


async def test_it_refuses_anything_outside_the_demo(session, reference, make_club, make_user):  # noqa: F811
    owner, club = await make_club(name="Real Club", email="real-owner@example.com")
    await make_user(email="someone@example.com")
    await session.commit()
    with pytest.raises(live_now.Refused, match="not a demo club"):
        await live_now.run(session, club_slug=club.slug, main_email=MAIN, dry_run=True, out=lambda *_: None)
    with pytest.raises(live_now.Refused, match=DEMO_DOMAIN):
        await live_now.run(session, club_slug=club.slug, main_email="someone@example.com", dry_run=True, out=lambda *_: None)
