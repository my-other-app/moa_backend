"""The club's plan, enforced (app/api/subscriptions/gates.py).

Clubs without a paid subscription are on Free. Each paid feature is locked
where it is started or created; each counted limit refuses the one past it.
Admins (and admins acting as a club) and the kill switch let everything
through, and an event made before enforcement began keeps its paid tickets
and is never capped on registrations.

Every test here seeds the six draft plans with the migration's own seed, so
the plans are the ones production has.
"""
import importlib.util
import pathlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.auth.schemas import AuthTokenData
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.subscriptions import gates, service
from app.api.subscriptions.models import FEATURE_CATALOG, BillingCycle, Plans
from app.config import settings
from app.core.auth.jwt import create_access_token
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)
UPGRADE = "https://clubs-next.myotherapp.in/billing"
LABELS = dict(FEATURE_CATALOG)
NAMES = {"free": "Free", "starter": "Starter", "growth": "Growth", "pro": "Pro", "campus": "Campus", "enterprise": "Enterprise"}


# ─── The world ───────────────────────────────────────────────────────────


def _migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "club_plans.py"
    spec = importlib.util.spec_from_file_location("club_plans", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    """On, with every event in a test made after the cutoff unless it says otherwise."""
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    gates.reset()
    yield
    gates.reset()


@pytest.fixture(autouse=True)
def mail(monkeypatch, session):
    """Mail never leaves; the owner's "registrations are paused" job is held
    until the test runs it, on the test's own session, after the response."""
    from app.core.email import email as email_mod

    sent, queued = [], []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    monkeypatch.setattr(gates, "schedule", lambda coro: queued.append(coro))

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(gates, "session_scope", scope)

    async def run():
        while queued:
            await queued.pop(0)
        return [m for m in sent if m["subject"].startswith("Registrations are paused")]

    yield SimpleNamespace(sent=sent, queued=queued, run=run)
    for coro in queued:
        coro.close()


@pytest.fixture(autouse=True)
def files(monkeypatch):
    from app.core.storage import objects

    held = {}
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, bytes(data)))
    monkeypatch.setattr(objects, "get_bytes", lambda key: held.get(key))
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://files.test/{key}")
    return held


@pytest_asyncio.fixture
async def plans(session):
    module = _migration()
    connection = await session.connection()

    def seed(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            return module.seed_plans()

    await connection.run_sync(seed)
    rows = (
        await session.execute(select(Plans).options(selectinload(Plans.limits), selectinload(Plans.features)))
    ).scalars().all()
    return {p.key: p for p in rows}


@pytest_asyncio.fixture
async def world(session, make_club, make_event, mint_token, plans):
    owner, club = await make_club(name="Gate Club", email="gate-owner@example.com")
    category = EventCategories(name="Talks", created_by_id=owner.id)
    session.add(category)
    await session.flush()
    event = await make_event(club=club, created_by=owner, name="Gate Night", event_datetime=NOW + timedelta(days=5), duration=2.0)
    await session.commit()
    return SimpleNamespace(owner=owner, club=club, event=event, category=category, plans=plans, h=auth_header(await mint_token(owner)))


async def on_plan(session, w, key):
    await service.assign_plan(session, club_id=w.club.id, plan=w.plans[key], cycle=BillingCycle.monthly)
    await session.commit()


async def set_limit(session, w, plan_key, **limits):
    for key, value in limits.items():
        setattr(w.plans[plan_key].limits, key, value)
    await session.commit()


async def impersonating(session, make_admin, w) -> dict:
    """The club's own session, started by an admin."""
    admin = await make_admin(email="gate-admin@example.com")
    await session.commit()
    await session.refresh(w.owner)
    token = create_access_token(
        data=AuthTokenData(
            user_id=w.owner.id, token_type="access_token", token_version=w.owner.token_version, impersonated_by=admin.id
        ).model_dump()
    )
    return auth_header(token)


def event_form(w, **extra) -> dict:
    return {
        "name": extra.pop("name", "New Night"),
        "event_datetime": (NOW + timedelta(days=9)).isoformat(),
        "duration": "2",
        "category_id": str(w.category.id),
        "location_name": "Lab 1",
        **extra,
    }


def update_form(w, **extra) -> dict:
    """The edit form; it needs interest ids, and one that names nothing is skipped."""
    return event_form(w, interest_ids="0", **extra)


def register(client, event_id, n: int, headers=None):
    return client.post(
        f"{API}/events/registration/{event_id}/register",
        json={"full_name": f"Guest Number {n}", "email": f"guest{n}@example.com", "phone": "9876500000"},
        headers=headers or {},
    )


# ─── Each feature, on the write that starts it ───────────────────────────

#: flag -> (the cheapest plan with it, the request that starts it)
ACTIONS = {
    "tickets": ("starter", lambda c, w, h: c.post(f"{API}/events/create", data=event_form(w, has_fee="true", reg_fee="100"), headers=h)),
    "website": ("starter", lambda c, w, h: c.patch(f"{API}/clubs/site", json={"headline": "Hello"}, headers=h)),
    "quiz": ("growth", lambda c, w, h: c.post(
        f"{API}/events/{w.event.id}/live/quiz/questions",
        json={"prompt": "Which?", "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}], "correct_key": "a"},
        headers=h,
    )),
    "polls": ("growth", lambda c, w, h: c.post(f"{API}/events/{w.event.id}/live/polls", json={"prompt": "Pizza?", "options": ["Yes", "No"]}, headers=h)),
    "qa": ("growth", lambda c, w, h: c.patch(f"{API}/events/{w.event.id}/live/questions/settings", json={"approve_first": False}, headers=h)),
    "links": ("growth", lambda c, w, h: c.post(f"{API}/events/{w.event.id}/links", json={"name": "Poster", "utm_source": "poster", "utm_medium": "print"}, headers=h)),
    "report": ("growth", lambda c, w, h: c.post(f"{API}/events/{w.event.id}/closeout/reports", headers=h)),
    "merch": ("pro", lambda c, w, h: c.post(f"{API}/events/{w.event.id}/store", json={"name": "Tee", "price": "299", "stock": 10}, headers=h)),
    "swag": ("pro", lambda c, w, h: c.post(f"{API}/events/{w.event.id}/merch", json={"name": "Sticker", "stock": 50}, headers=h)),
    "badges": ("pro", lambda c, w, h: c.patch(f"{API}/events/{w.event.id}/idcards/settings", json={"auto_print_badges": True}, headers=h)),
    "roles": ("pro", lambda c, w, h: c.post(f"{API}/clubs/access/invite", json={"email": "lead@example.com", "role": "admin"}, headers=h)),
    "teams": ("pro", lambda c, w, h: c.patch(f"{API}/events/{w.event.id}/competition", json={"enabled": True}, headers=h)),
    "judging": ("pro", lambda c, w, h: c.post(f"{API}/events/{w.event.id}/competition/judges", json={"name": "Dr Rao"}, headers=h)),
    "rounds": ("pro", lambda c, w, h: c.post(f"{API}/events/{w.event.id}/competition/rounds/advance", json={"to": "shortlisted"}, headers=h)),
    "finance": ("campus", lambda c, w, h: c.post(f"{API}/clubs/finance/expenses", json={"title": "Chai", "amount": "120"}, headers=h)),
}


def test_every_built_paid_feature_has_a_gate_here():
    """Free's five are never gated; every other flag on the pricing page is."""
    gated = set(ACTIONS) | {"networking"}
    assert gated | gates.ALWAYS_ALLOWED == set(LABELS)


@pytest.mark.parametrize("flag", sorted(ACTIONS))
async def test_a_paid_feature_is_locked_on_free(client, world, flag):
    plan, act = ACTIONS[flag]
    r = await act(client, world, world.h)
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error_code"] == "PLAN_FEATURE_LOCKED"
    assert body["errors"] == {
        "feature": flag,
        "label": LABELS[flag],
        "required_plan": plan,
        "required_plan_name": NAMES[plan],
        "upgrade_url": UPGRADE,
    }
    assert body["message"].endswith(f"part of {NAMES[plan]} and up.")


@pytest.mark.parametrize("flag", sorted(ACTIONS))
async def test_a_paid_feature_opens_on_the_cheapest_plan_with_it(client, session, world, flag):
    plan, act = ACTIONS[flag]
    await on_plan(session, world, plan)
    r = await act(client, world, world.h)
    assert r.status_code < 300, r.text


@pytest.mark.parametrize("flag", sorted(ACTIONS))
async def test_an_admin_acting_as_the_club_is_never_locked(client, session, world, make_admin, flag):
    _, act = ACTIONS[flag]
    r = await act(client, world, await impersonating(session, make_admin, world))
    assert r.status_code < 300, r.text


@pytest.mark.parametrize("flag", sorted(ACTIONS))
async def test_the_kill_switch_unlocks_everything(client, world, monkeypatch, flag):
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", False)
    _, act = ACTIONS[flag]
    r = await act(client, world, world.h)
    assert r.status_code < 300, r.text


async def test_a_plan_below_the_one_with_it_stays_locked(client, session, world):
    await on_plan(session, world, "starter")
    r = await ACTIONS["links"][1](client, world, world.h)
    assert r.status_code == 403 and r.json()["errors"]["required_plan"] == "growth"
    await on_plan(session, world, "growth")
    r = await ACTIONS["merch"][1](client, world, world.h)
    assert r.status_code == 403 and r.json()["errors"]["required_plan"] == "pro"


async def test_an_admins_own_session_is_never_locked(client, session, world, make_admin, mint_token):
    admin = await make_admin(email="gate-admin2@example.com")
    await session.commit()
    h = auth_header(await mint_token(admin))
    r = await client.post(f"{API}/events/{world.event.id}/live/polls", json={"prompt": "Tea?", "options": ["Yes", "No"]}, headers=h)
    assert r.status_code == 201, r.text
    r = await client.put(f"{API}/events/update/{world.event.id}", data=update_form(world, name="Gate Night", has_fee="true", reg_fee="50"), headers=h)
    assert r.status_code == 200, r.text


async def test_without_a_free_plan_nothing_is_enforced(client, session, make_club, mint_token):
    """No plans seeded: the pre-plans status quo, everything allowed."""
    owner, club = await make_club(name="Unplanned Club", email="unplanned@example.com")
    category = EventCategories(name="Talks", created_by_id=owner.id)
    session.add(category)
    await session.commit()
    h = auth_header(await mint_token(owner))
    w = SimpleNamespace(category=category)
    for n in range(4):
        r = await client.post(f"{API}/events/create", data=event_form(w, name=f"Night {n}", has_fee="true", reg_fee="10"), headers=h)
        assert r.status_code == 200, r.text


# ─── Paid tickets in detail ──────────────────────────────────────────────


async def test_a_free_event_turned_paid_needs_tickets(client, session, world):
    r = await client.put(f"{API}/events/update/{world.event.id}", data=update_form(world, name="Gate Night", has_fee="true", reg_fee="100"), headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "tickets"
    await session.refresh(world.event)
    assert world.event.has_fee is False
    # Staying free is always fine.
    r = await client.put(f"{API}/events/update/{world.event.id}", data=update_form(world, name="Gate Night"), headers=world.h)
    assert r.status_code == 200, r.text


async def test_a_paid_event_stays_paid_through_an_edit_on_free(client, session, world, make_event):
    """Made paid on a plan with tickets; the club fell to Free. Editing it
    while keeping it paid is not starting paid tickets."""
    paid = await make_event(club=world.club, created_by=world.owner, name="Paid Night", has_fee=True, reg_fee=100.0, event_datetime=NOW + timedelta(days=3))
    await session.commit()
    r = await client.put(f"{API}/events/update/{paid.id}", data=update_form(world, name="Paid Night renamed", has_fee="true", reg_fee="120"), headers=world.h)
    assert r.status_code == 200, r.text


async def test_switching_the_store_or_swag_on_needs_the_plan(client, session, world):
    r = await client.patch(f"{API}/events/{world.event.id}/store/settings", json={"swag_enabled": True}, headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "swag"
    r = await client.put(f"{API}/events/update/{world.event.id}", data=update_form(world, name="Gate Night", merch_enabled="true"), headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "merch"
    # Off is never refused.
    r = await client.patch(f"{API}/events/{world.event.id}/store/settings", json={"merch_enabled": False, "swag_enabled": False}, headers=world.h)
    assert r.status_code == 200, r.text
    await on_plan(session, world, "pro")
    r = await client.patch(f"{API}/events/{world.event.id}/store/settings", json={"merch_enabled": True, "swag_enabled": True}, headers=world.h)
    assert r.status_code == 200 and r.json() == {"merch_enabled": True, "swag_enabled": True}


async def test_duplicating_a_paid_event_needs_tickets_and_counts_as_an_event(client, session, world, make_event):
    paid = await make_event(club=world.club, created_by=world.owner, name="Paid Original", has_fee=True, reg_fee=100.0, event_datetime=NOW + timedelta(days=3))
    await session.commit()
    body = {"name": "Copy", "event_datetime": (NOW + timedelta(days=30)).isoformat()}
    r = await client.post(f"{API}/events/{paid.id}/duplicate", json=body, headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "tickets"
    # A free one copies -- until the month's events run out (Free: 2, and
    # the world and Paid Original are two already).
    r = await client.post(f"{API}/events/{world.event.id}/duplicate", json=body, headers=world.h)
    assert r.status_code == 403 and r.json()["error_code"] == "PLAN_LIMIT_REACHED"


# ─── The live room and networking, from the attendee's side ──────────────


async def test_a_guest_asking_on_free_is_told_only_that_questions_are_closed(client, session, world):
    room = {"X-Live-Key": "browser-key-0001"}
    r = await client.post(f"{API}/events/{world.event.id}/live/questions", json={"text": "When is lunch?"}, headers=room)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error_code"] == "QA_CLOSED" and body["errors"] is None
    assert "plan" not in body["message"].lower()
    await on_plan(session, world, "growth")
    r = await client.post(f"{API}/events/{world.event.id}/live/questions", json={"text": "When is lunch?"}, headers=room)
    assert r.status_code == 201, r.text


async def test_networking_is_off_for_a_free_clubs_event(client, session, world, make_user, mint_token):
    guest = await make_user(email="networker@example.com")
    world.event.event_datetime = NOW - timedelta(minutes=30)
    await session.commit()
    h = auth_header(await mint_token(guest))
    r = await client.get(f"{API}/events/{world.event.id}/networking", headers=h)
    assert r.status_code == 403 and r.json()["error_code"] == "NETWORKING_UNAVAILABLE"
    assert "plan" not in r.json()["message"].lower()
    await on_plan(session, world, "growth")
    r = await client.get(f"{API}/events/{world.event.id}/networking", headers=h)
    # Past the plan: on to the feature's own rule (checked in first).
    assert r.status_code == 403 and r.json()["error_code"] != "NETWORKING_UNAVAILABLE"
    assert "checked in" in r.json()["message"]


async def test_starting_a_quiz_needs_the_plan_but_ending_one_never_does(client, session, world):
    await on_plan(session, world, "growth")
    add = await ACTIONS["quiz"][1](client, world, world.h)
    assert add.status_code == 201, add.text
    assert (await client.post(f"{API}/events/{world.event.id}/live/quiz/host/start", headers=world.h)).status_code == 200
    # The club drops to Free mid-quiz.
    await on_plan(session, world, "free")
    assert (await client.post(f"{API}/events/{world.event.id}/live/quiz/host/end", headers=world.h)).status_code == 200
    r = await client.post(f"{API}/events/{world.event.id}/live/quiz/host/start", headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "quiz"


async def test_launching_a_poll_needs_the_plan_but_closing_one_never_does(client, session, world):
    await on_plan(session, world, "growth")
    poll = (await ACTIONS["polls"][1](client, world, world.h)).json()
    await on_plan(session, world, "free")
    r = await client.post(f"{API}/events/{world.event.id}/live/polls/{poll['id']}/status", json={"status": "live"}, headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "polls"
    r = await client.post(f"{API}/events/{world.event.id}/live/polls/{poll['id']}/status", json={"status": "closed"}, headers=world.h)
    assert r.status_code == 200, r.text
    # And what exists is still there to read.
    assert (await client.get(f"{API}/events/{world.event.id}/live/polls", headers=world.h)).status_code == 200


async def test_naming_report_recipients_is_sending_the_report(client, session, world):
    url = f"{API}/events/{world.event.id}/closeout/settings"
    r = await client.patch(url, json={"report_recipients": ["boss@example.com"]}, headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "report"
    # Everything else on the page stays the club's to change.
    assert (await client.patch(url, json={"retention_hours": 48, "report_recipients": []}, headers=world.h)).status_code == 200


async def test_printing_an_id_card_and_designing_one_need_badges(client, session, world, make_registration, make_user):
    guest = await make_user(email="carded@example.com")
    reg = await make_registration(event=world.event, user=guest)
    await session.commit()
    r = await client.get(f"{API}/events/{world.event.id}/idcards/{reg.id}.pdf?record=false", headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "badges"
    r = await client.get(f"{API}/events/{world.event.id}/idcards/batch.pdf", headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "badges"
    r = await client.put(f"{API}/clubs/designs/badge/layout", json={}, headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "badges"
    r = await client.put(f"{API}/events/{world.event.id}/designs/badge/layout", json={}, headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "badges"
    # A ticket design is every plan's.
    r = await client.put(f"{API}/events/{world.event.id}/designs/ticket/layout", json={}, headers=world.h)
    assert r.status_code != 403


async def test_moving_a_team_through_the_rounds_by_hand_is_rounds(client, session, world):
    from app.api.competitions.models import CompetitionTeams
    from app.api.competitions import service as competitions

    c = await competitions.get_or_create(session, world.event)
    team = CompetitionTeams(competition_id=c.id, name="Rustaceans")
    session.add(team)
    await session.commit()
    url = f"{API}/events/{world.event.id}/competition/teams/{team.id}"
    r = await client.patch(url, json={"stage": "shortlisted"}, headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "rounds"
    assert (await client.patch(url, json={"name": "Crabs"}, headers=world.h)).status_code == 200


# ─── Team access: the default role, and seats ────────────────────────────


async def test_free_adds_editors_up_to_its_seats_and_no_other_role(client, session, world):
    from app.api.club_access.models import DEFAULT_ROLE

    assert DEFAULT_ROLE == "editor"
    url = f"{API}/clubs/access/invite"
    for role in ("admin", "door"):
        r = await client.post(url, json={"email": f"{role}@example.com", "role": role}, headers=world.h)
        assert r.status_code == 403 and r.json()["errors"]["feature"] == "roles", role
    first = await client.post(url, json={"email": "ed1@example.com", "role": "editor"}, headers=world.h)
    assert first.status_code == 200, first.text
    assert (await client.post(url, json={"email": "ed2@example.com", "role": "editor"}, headers=world.h)).status_code == 200
    # Free has two seats.
    r = await client.post(url, json={"email": "ed3@example.com", "role": "editor"}, headers=world.h)
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error_code"] == "PLAN_LIMIT_REACHED"
    assert body["errors"] == {
        "limit": "committee_seats", "limit_value": 2, "used": 2,
        "required_plan": "starter", "required_plan_name": "Starter", "upgrade_url": UPGRADE,
    }
    assert body["message"] == "You've used 2 of 2 team seats on Free."
    # Changing a role: to anything but Editor is the plan's.
    member_id = first.json()["member"]["id"]
    r = await client.patch(f"{API}/clubs/access/{member_id}", json={"role": "door"}, headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "roles"
    assert (await client.patch(f"{API}/clubs/access/{member_id}", json={"role": "editor"}, headers=world.h)).status_code == 200
    # Taking someone off frees their seat.
    assert (await client.delete(f"{API}/clubs/access/{member_id}", headers=world.h)).status_code == 200
    assert (await client.post(url, json={"email": "ed3@example.com", "role": "editor"}, headers=world.h)).status_code == 200


# ─── Limits ──────────────────────────────────────────────────────────────


async def test_events_per_month_refuses_the_one_past_the_cap(client, session, world):
    """Free: 2 a month, and the world's event is the first."""
    r = await client.post(f"{API}/events/create", data=event_form(world, name="Second"), headers=world.h)
    assert r.status_code == 200, r.text
    r = await client.post(f"{API}/events/create", data=event_form(world, name="Third"), headers=world.h)
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["message"] == "You've created 2 of 2 events this month on Free."
    assert body["errors"] == {
        "limit": "events_per_month", "limit_value": 2, "used": 2,
        "required_plan": "starter", "required_plan_name": "Starter", "upgrade_url": UPGRADE,
    }
    # A deleted event gives its place back.
    world.event.is_deleted = True
    await session.commit()
    assert (await client.post(f"{API}/events/create", data=event_form(world, name="Third"), headers=world.h)).status_code == 200


async def test_events_from_last_month_in_india_do_not_count(client, session, world, make_event):
    """The month turns at midnight in India, not at midnight UTC."""
    month_start = datetime.now(IST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    world.event.created_at = month_start - timedelta(seconds=1)
    await session.commit()
    assert await gates.count(session, world.club.id, "events_per_month") == 0
    on_the_turn = await make_event(club=world.club, created_by=world.owner, name="On the turn")
    on_the_turn.created_at = month_start
    await session.commit()
    assert await gates.count(session, world.club.id, "events_per_month") == 1
    assert (await client.post(f"{API}/events/create", data=event_form(world, name="Second"), headers=world.h)).status_code == 200
    assert (await client.post(f"{API}/events/create", data=event_form(world, name="Third"), headers=world.h)).status_code == 403


async def test_the_ist_month_boundary_exactly(session, world, make_event):
    """18:29:59 UTC on 30 September is still September in India; 18:30 is October."""
    turn = datetime(2026, 9, 30, 18, 30, tzinfo=timezone.utc)
    assert service.month_start(turn) == turn
    assert service.month_start(turn - timedelta(seconds=1)) == datetime(2026, 8, 31, 18, 30, tzinfo=timezone.utc)
    world.event.created_at = turn - timedelta(seconds=1)
    second = await make_event(club=world.club, created_by=world.owner, name="First of October")
    second.created_at = turn
    await session.commit()
    october = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    assert await gates.count(session, world.club.id, "events_per_month", now=october) == 1
    # In November neither counts.
    assert await gates.count(session, world.club.id, "events_per_month", now=datetime(2026, 11, 2, tzinfo=timezone.utc)) == 0


async def test_announcements_per_month(client, session, world, make_user):
    from app.api.notifications.models import Notifications

    guest = await make_user(email="listener@example.com")
    for n in range(3):
        session.add(Notifications(user_id=guest.id, title=f"News {n}", description="Body", type="club_announcement", event_id=world.event.id))
    await session.commit()
    url = f"{API}/clubs/notifications/events/{world.event.id}/send"
    # The fourth of Free's four goes (to nobody, but the gate let it through).
    assert (await client.post(url, json={"title": "Fourth", "body": "Hi"}, headers=world.h)).status_code == 200
    session.add(Notifications(user_id=guest.id, title="News 3", description="Body", type="club_announcement", event_id=world.event.id))
    await session.commit()
    r = await client.post(url, json={"title": "Fifth", "body": "Hi"}, headers=world.h)
    assert r.status_code == 403, r.text
    assert r.json()["errors"]["limit"] == "announcements_per_month" and r.json()["errors"]["used"] == 4
    assert r.json()["message"] == "You've sent 4 of 4 announcements this month on Free."


async def test_merch_items_per_event_counts_the_store_and_the_handouts_together(client, session, world):
    await on_plan(session, world, "pro")
    await set_limit(session, world, "pro", merch_items_per_event=2)
    assert (await ACTIONS["merch"][1](client, world, world.h)).status_code == 201
    assert (await ACTIONS["swag"][1](client, world, world.h)).status_code == 201
    r = await ACTIONS["merch"][1](client, world, world.h)
    assert r.status_code == 403 and r.json()["errors"]["limit"] == "merch_items_per_event"
    assert r.json()["errors"]["used"] == 2 and r.json()["errors"]["limit_value"] == 2
    # Campus has no cap: the cheapest plan with room.
    assert r.json()["errors"]["required_plan"] == "campus"


async def test_website_templates_are_the_first_n_in_catalogue_order(client, session, world):
    from app.api.sites import service as sites

    assert gates.template_order() == ("launchpad", "chapter", "campus", "arena")
    await on_plan(session, world, "starter")
    url = f"{API}/clubs/site"
    assert (await client.patch(url, json={"template": "campus"}, headers=world.h)).status_code == 200
    r = await client.patch(url, json={"template": "arena"}, headers=world.h)
    assert r.status_code == 403, r.text
    assert r.json()["errors"] == {
        "limit": "website_templates", "limit_value": 3, "used": 3,
        "required_plan": "growth", "required_plan_name": "Growth", "upgrade_url": UPGRADE,
    }
    assert r.json()["message"] == "Starter includes the first 3 website templates; Arena is not one of them."
    # Chosen on Growth, then down to Starter: kept, editable, not published.
    site = await sites.get_or_create(session, world.club)
    site.template = "arena"
    await session.commit()
    assert (await client.patch(url, json={"template": "arena", "headline": "Still us"}, headers=world.h)).status_code == 200
    assert (await client.post(f"{url}/publish", headers=world.h)).status_code == 403
    await on_plan(session, world, "growth")
    assert (await client.post(f"{url}/publish", headers=world.h)).status_code == 200


async def test_the_limits_with_nothing_to_count_are_not_enforced(session, world):
    await set_limit(session, world, "free", storage_gb=0, ticket_types_per_event=0, coupons_per_event=0)
    for key in ("storage_gb", "ticket_types_per_event", "coupons_per_event"):
        assert key in gates.NOT_ENFORCED
        await gates.require_within(session, world.club.id, key, used=5)


async def test_counting_agrees_with_what_the_dashboard_shows(session, world, make_user, make_registration):
    from app.api.club_access.models import ClubMembers
    from app.api.notifications.models import Notifications

    guest = await make_user(email="counted@example.com")
    await make_registration(event=world.event, user=guest)
    session.add(ClubMembers(club_id=world.club.id, email="m@example.com", role="editor", status="active"))
    session.add(Notifications(user_id=guest.id, title="T", description="D", type="club_announcement", event_id=world.event.id))
    await session.commit()
    usage = await service.usage(session, world.club.id)
    for key, value in usage.items():
        assert await gates.count(session, world.club.id, key) == value, key
    assert usage == {"events_per_month": 1, "registrations_per_month": 1, "committee_seats": 1, "announcements_per_month": 1}


# ─── Registrations ───────────────────────────────────────────────────────


async def test_a_full_event_on_free_closes_to_guests_without_a_word_about_plans(client, session, world, mail):
    await set_limit(session, world, "free", registrations_per_event=2)
    assert (await register(client, world.event.id, 1)).status_code == 200
    assert (await register(client, world.event.id, 2)).status_code == 200
    r = await register(client, world.event.id, 3)
    assert r.status_code == 409, r.text
    assert r.json() == {
        "message": "This event isn't taking more registrations right now.",
        "errors": None,
        "error_code": "REGISTRATIONS_CLOSED",
        "track_id": None,
    }
    for word in ("plan", "free", "upgrade", "billing", "limit"):
        assert word not in r.text.lower(), word
    # Nobody was registered, and the owner hears about it once.
    assert await session.scalar(
        select(EventRegistrationsLink.id).where(EventRegistrationsLink.email == "guest3@example.com")
    ) is None
    told = await mail.run()
    assert len(told) == 1 and told[0]["recipients"] == ["gate-owner@example.com"]
    assert "Gate Night" in told[0]["subject"] and UPGRADE in told[0]["text"] and "Starter" in told[0]["text"]


async def test_the_organisers_own_cap_holds_when_it_is_lower(client, session, world, mail):
    await set_limit(session, world, "free", registrations_per_event=3)
    world.event.max_participants = 1
    await session.commit()
    assert (await register(client, world.event.id, 1)).status_code == 200
    r = await register(client, world.event.id, 2)
    assert r.status_code == 400 and r.json()["message"] == "Event is full"
    assert await mail.run() == []
    # Higher than the plan's: the plan's holds.
    world.event.max_participants = 10
    await session.commit()
    assert (await register(client, world.event.id, 2)).status_code == 200
    assert (await register(client, world.event.id, 3)).status_code == 200
    assert (await register(client, world.event.id, 4)).status_code == 409


async def test_a_paid_event_counts_only_paid_registrations(client, session, world, make_event):
    await on_plan(session, world, "starter")
    await set_limit(session, world, "starter", registrations_per_event=1)
    paid = await make_event(club=world.club, created_by=world.owner, name="Paid Gate", has_fee=True, reg_fee=100.0, event_datetime=NOW + timedelta(days=4))
    await session.commit()
    first = await register(client, paid.id, 1)
    assert first.status_code == 200
    # Unpaid, it holds no place.
    assert (await register(client, paid.id, 2)).status_code == 200
    reg = await session.get(EventRegistrationsLink, uuid.UUID(first.json()["event_registration_id"]))
    reg.is_paid = True
    await session.commit()
    assert (await register(client, paid.id, 3)).status_code == 409


async def test_registrations_per_month_across_the_clubs_events(client, session, world, make_event):
    await set_limit(session, world, "free", registrations_per_month=3)
    other = await make_event(club=world.club, created_by=world.owner, name="Other Night", event_datetime=NOW + timedelta(days=6))
    await session.commit()
    assert (await register(client, world.event.id, 1)).status_code == 200
    assert (await register(client, other.id, 2)).status_code == 200
    assert (await register(client, other.id, 3)).status_code == 200
    r = await register(client, world.event.id, 4)
    assert r.status_code == 409 and r.json()["error_code"] == "REGISTRATIONS_CLOSED"


async def test_the_organiser_adding_by_hand_is_told_the_plans_reason(client, session, world, mail):
    await set_limit(session, world, "free", registrations_per_event=1)
    url = f"{API}/events/registration/{world.event.id}/attendees"
    assert (await client.post(url, json={"full_name": "First", "email": "first@example.com"}, headers=world.h)).status_code == 200
    r = await client.post(url, json={"full_name": "Second", "email": "second@example.com"}, headers=world.h)
    assert r.status_code == 403, r.text
    assert r.json()["errors"]["limit"] == "registrations_per_event" and r.json()["errors"]["required_plan"] == "starter"
    assert r.json()["message"] == "This event has 1 of 1 registration allowed on Free."
    assert len(await mail.run()) == 1


async def test_an_admin_adding_by_hand_is_not_capped(client, session, world, make_admin, mint_token):
    await set_limit(session, world, "free", registrations_per_event=1)
    admin = await make_admin(email="gate-admin3@example.com")
    await session.commit()
    h = auth_header(await mint_token(admin))
    url = f"{API}/events/registration/{world.event.id}/attendees"
    for n in range(3):
        r = await client.post(url, json={"full_name": f"Guest {n}", "email": f"hand{n}@example.com"}, headers=h)
        assert r.status_code == 200, r.text


async def test_the_kill_switch_reopens_registrations(client, session, world, monkeypatch):
    await set_limit(session, world, "free", registrations_per_event=1)
    assert (await register(client, world.event.id, 1)).status_code == 200
    assert (await register(client, world.event.id, 2)).status_code == 409
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", False)
    assert (await register(client, world.event.id, 2)).status_code == 200


# ─── Grandfathered events ────────────────────────────────────────────────


async def test_an_event_made_before_the_cutoff_is_never_capped(client, session, world, make_event, monkeypatch):
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", NOW - timedelta(days=1))
    await set_limit(session, world, "free", registrations_per_event=1, registrations_per_month=1)
    world.event.created_at = NOW - timedelta(days=2)
    after = await make_event(club=world.club, created_by=world.owner, name="After the cutoff", event_datetime=NOW + timedelta(days=8))
    await session.commit()
    assert gates.grandfathered(world.event) and not gates.grandfathered(after)
    for n in range(3):
        assert (await register(client, world.event.id, n)).status_code == 200
    # The month is used up by the old event; a new one is capped.
    r = await register(client, after.id, 10)
    assert r.status_code == 409 and r.json()["error_code"] == "REGISTRATIONS_CLOSED"


async def test_a_paid_event_made_before_the_cutoff_still_registers_and_pays(client, session, world, make_event, monkeypatch):
    """Free has no paid tickets. A paid event from before keeps selling: the
    guest registers, the order opens, the payment lands."""
    from app.api.payments.service import handle_razorpay_webhook, razorpay_client

    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", NOW - timedelta(days=1))
    await set_limit(session, world, "free", registrations_per_event=0, registrations_per_month=0)
    paid = await make_event(club=world.club, created_by=world.owner, name="Old Paid Night", has_fee=True, reg_fee=100.0, event_datetime=NOW + timedelta(days=4))
    paid.created_at = NOW - timedelta(days=3)
    await session.commit()

    r = await register(client, paid.id, 1)
    assert r.status_code == 200, r.text
    assert r.json()["pay_amount"] == 100.0
    token = r.json()["auth_token"]["access_token"]

    monkeypatch.setattr(razorpay_client.order, "create", lambda data: {"id": "order_grandfathered"})
    order = await client.post(
        f"{API}/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": paid.id, "event_registration_id": r.json()["event_registration_id"]}},
        headers=auth_header(token),
    )
    assert order.status_code == 200, order.text
    await handle_razorpay_webhook(
        session,
        {"entity": "event", "event": "payment.captured", "payload": {"payment": {"entity": {
            "id": "pay_grandfathered", "order_id": "order_grandfathered", "status": "captured", "method": "upi", "amount": 10000, "upi": {},
        }}}},
        event_id="evt_grandfathered", signature="sig",
    )
    reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == paid.id))
    await session.refresh(reg)
    assert reg.is_paid is True

    # And the club may keep editing it as a paid event.
    r = await client.put(f"{API}/events/update/{paid.id}", data=update_form(world, name="Old Paid Night", has_fee="true", reg_fee="100"), headers=world.h)
    assert r.status_code == 200, r.text


async def test_a_free_event_from_before_the_cutoff_does_not_turn_paid_on_free(client, session, world, monkeypatch):
    """Grandfathering keeps an event as it was; it does not let an old free
    event be made into a new paid one."""
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", NOW - timedelta(days=1))
    world.event.created_at = NOW - timedelta(days=2)
    await session.commit()
    r = await client.put(f"{API}/events/update/{world.event.id}", data=update_form(world, name="Gate Night", has_fee="true", reg_fee="100"), headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "tickets"


# ─── Telling the owner ───────────────────────────────────────────────────


async def test_the_owner_is_told_at_most_once_a_day_per_limit(client, session, world, mail):
    from app.api.audit.models import AdminAuditLogs

    await set_limit(session, world, "free", registrations_per_event=1)
    assert (await register(client, world.event.id, 1)).status_code == 200
    assert (await register(client, world.event.id, 2)).status_code == 409
    assert len(await mail.run()) == 1
    # Again the same day: not even queued in this process...
    assert (await register(client, world.event.id, 3)).status_code == 409
    assert mail.queued == []
    # ...and another worker (nothing remembered in memory) finds the record.
    gates.reset()
    assert (await register(client, world.event.id, 4)).status_code == 409
    assert len(await mail.run()) == 1

    # Another limit is another message.
    await set_limit(session, world, "free", registrations_per_event=None, registrations_per_month=1)
    assert (await register(client, world.event.id, 5)).status_code == 409
    assert len(await mail.run()) == 2

    # A day on, the first one may be sent again.
    rows = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action.like("plan.paused:%")))).all()
    assert {r.action for r in rows} == {"plan.paused:registrations_per_event", "plan.paused:registrations_per_month"}
    assert all(r.entity_type == "club" and r.entity_id == str(world.club.id) for r in rows)
    for row in rows:
        row.created_at = NOW - timedelta(hours=25)
    await session.commit()
    gates.reset()
    assert (await register(client, world.event.id, 6)).status_code == 409
    assert len(await mail.run()) == 3


# ─── The plan to suggest ─────────────────────────────────────────────────


def _card(key, price, *, contact=False, order=0, features=(), **limits):
    return gates.PlanCard(key=key, name=key.title(), price_monthly_paise=price, contact_sales=contact, sort_order=order,
                          features={f: True for f in features}, limits=limits)


def test_the_cheapest_plan_with_a_feature():
    cards = [
        _card("enterprise", 0, contact=True, features=("finance", "tickets")),
        _card("pro", 1499, features=("tickets",)),
        _card("starter", 299, features=("tickets",)),
        _card("free", 0),
    ]
    assert gates.plan_for_feature(cards, "tickets").key == "starter"
    # Only a priced-on-request plan has it: that one, not nothing.
    assert gates.plan_for_feature(cards, "finance").key == "enterprise"
    assert gates.plan_for_feature(cards, "sso") is None
    # Same price: the pricing page's order breaks the tie.
    assert gates.plan_for_feature([_card("b", 5, order=2, features=("x",)), _card("a", 5, order=1, features=("x",))], "x").key == "a"


def test_the_cheapest_plan_with_room():
    cards = [
        _card("free", 0, seats=2),
        _card("starter", 299, seats=5),
        _card("growth", 699, seats=10),
        _card("campus", 3999, seats=None),
        _card("enterprise", 0, contact=True, seats=None),
    ]
    assert gates.plan_for_limit(cards, "seats", current=2, needed=3).key == "starter"
    # A club over its cap after a downgrade needs room for all it has.
    assert gates.plan_for_limit(cards, "seats", current=2, needed=8).key == "growth"
    assert gates.plan_for_limit(cards, "seats", current=10, needed=11).key == "campus"
    # Higher than now, not merely enough.
    assert gates.plan_for_limit(cards, "seats", current=5, needed=1).key == "growth"
    assert gates.plan_for_limit([_card("free", 0, seats=2)], "seats", current=2, needed=3) is None
    # With the feature the action needs as well.
    store = [_card("starter", 299, items=5), _card("pro", 1499, features=("merch",), items=100), _card("campus", 3999, features=("merch",), items=None)]
    assert gates.plan_for_limit(store, "items", current=0, needed=1).key == "starter"
    assert gates.plan_for_limit(store, "items", current=0, needed=1, feature="merch").key == "pro"
    assert gates.plan_for_limit(store, "items", current=100, needed=101, feature="merch").key == "campus"


async def test_the_suggestions_from_the_seeded_plans(session, plans):
    cards = [gates.card_of(p) for p in await service.public_plans(session)]
    expected = {
        "tickets": "starter", "website": "starter",
        "quiz": "growth", "polls": "growth", "qa": "growth", "networking": "growth", "links": "growth", "report": "growth",
        "merch": "pro", "swag": "pro", "badges": "pro", "roles": "pro", "teams": "pro", "judging": "pro", "rounds": "pro",
        "finance": "campus",
    }
    assert {f: gates.plan_for_feature(cards, f).key for f in expected} == expected
    free = gates.card_of(plans["free"]).limits
    assert gates.plan_for_limit(cards, "events_per_month", current=free["events_per_month"], needed=3).key == "starter"
    assert gates.plan_for_limit(cards, "registrations_per_event", current=100, needed=101).key == "starter"
    assert gates.plan_for_limit(cards, "website_templates", current=3, needed=4).key == "growth"
    assert gates.plan_for_limit(cards, "events_per_month", current=15, needed=16).key == "pro"


# ─── The request, and the entitlements the dashboard reads ───────────────


async def test_one_request_resolves_the_plan_once(client, session, world, monkeypatch):
    await on_plan(session, world, "starter")
    calls = []
    real = service.resolve_entitlements

    async def counted(session_, club_id):
        calls.append(club_id)
        return await real(session_, club_id)

    monkeypatch.setattr(service, "resolve_entitlements", counted)
    # Two gates on one request: paid tickets, then the month's events.
    r = await client.post(f"{API}/events/create", data=event_form(world, has_fee="true", reg_fee="10"), headers=world.h)
    assert r.status_code == 200, r.text
    assert calls == [world.club.id]
    # The next request starts afresh.
    await client.post(f"{API}/events/create", data=event_form(world, name="Another", has_fee="true", reg_fee="10"), headers=world.h)
    assert calls == [world.club.id, world.club.id]


async def test_the_entitlements_say_whether_and_since_when(client, world, monkeypatch):
    body = (await client.get(f"{API}/clubs/entitlements", headers=world.h)).json()
    assert body["plan_key"] == "free" and body["enforced"] is True
    assert datetime.fromisoformat(body["grandfathered_before"]) == LONG_AGO
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", False)
    assert (await client.get(f"{API}/clubs/entitlements", headers=world.h)).json()["enforced"] is False


def test_the_default_cutoff_is_midnight_in_india_on_the_18th():
    from app.config import AppConfig

    default = AppConfig.model_fields["PLAN_ENFORCEMENT_FROM"].default
    assert default == datetime(2026, 10, 17, 18, 30, tzinfo=timezone.utc)
    assert AppConfig.model_fields["PLAN_ENFORCEMENT"].default is True
    # One written without an offset is India time.
    assert AppConfig(PLAN_ENFORCEMENT_FROM="2026-10-18T00:00:00").PLAN_ENFORCEMENT_FROM == default
