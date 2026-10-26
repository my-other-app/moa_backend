"""Registration approval under concurrency (app/api/approvals): two people
confirming the same requests at once register nobody twice; a confirmation
and a public registration never both take the last place.

As tests/test_ticketing_races.py: sessions on connections of their own that
commit, as concurrent requests do, and every committed row swept away after
each test (tests/racing.py)."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.approvals import notify
from app.api.approvals import service as approvals
from app.api.approvals.models import RegistrationRequests as R
from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.events.registration import service as registration
from app.api.ticketing import types as types_mod
from app.api.ticketing.models import WaitlistEntries
from app.api.users.models import UserTypes
from app.db.core import SESSION_KWARGS
from app.db.listeners import add_loader_criteria
from app.response import CustomHTTPException
from tests.conftest import _make_user
from tests.racing import committed_keys, sweep

NOW = datetime.now(timezone.utc)


@pytest.fixture
def make(_engine):
    def factory():
        s = async_sessionmaker(bind=_engine, **SESSION_KWARGS)()
        add_loader_criteria(s)
        return s

    return factory


@pytest.fixture(autouse=True)
async def _leaves_no_trace(_engine, monkeypatch):
    """What a test here commits is gone when it ends. The emails and the
    tickets' sends are not what these are about: left unsent."""
    from app.core import after_commit

    async def quiet(*_a, **_k):
        return 0

    monkeypatch.setattr(notify, "tell", quiet)
    monkeypatch.setattr(deliveries, "_run_due", quiet)
    before = await committed_keys(_engine)
    yield
    await after_commit.drain()
    await sweep(_engine, before)
    after = await committed_keys(_engine)
    left = {name: len(keys - before[name]) for name, keys in after.items() if keys - before[name]}
    assert not left, f"rows left behind: {left}"


async def _world(make, tag, *, requests: int, **event_values) -> dict:
    async with make() as s:
        owner = await _make_user(s, email=f"{tag}-owner@example.com", user_type=UserTypes.club, username=f"{tag}owner")
        club = Clubs(slug=f"{tag}-club", name=f"{tag} Club", user_id=owner.id, is_verified=True)
        s.add(club)
        await s.flush()
        category = EventCategories(name=tag, created_by_id=owner.id)
        s.add(category)
        await s.flush()
        event = Events(
            slug=f"{tag}-night", name=f"{tag} Night", images=[], duration=2.0, event_datetime=NOW + timedelta(days=3),
            club_id=club.id, category_id=category.id, requires_approval=True, **event_values,
        )
        s.add(event)
        await s.flush()
        type_id = (await types_mod.ensure(s, event))[0].id
        rows = [
            dict(
                id=uuid.uuid4(), event_id=event.id, full_name=f"{tag} asker {i}", email=f"{tag}-{i}@example.com",
                ticket_type_id=type_id, status="pending", token=approvals.new_token(),
                submitted_at=NOW + timedelta(microseconds=i), whatsapp_opt_in=False, club_updates_whatsapp=False,
                created_at=NOW, updated_at=NOW,
            )
            for i in range(requests)
        ]
        if rows:
            await s.execute(insert(R), rows)
        await s.commit()
        return dict(event=event.id, ids=[r["id"] for r in rows], category=category.id)


async def _approve(make, event_id, ids):
    async with make() as s:
        event = await s.get(Events, event_id)
        return await approvals.approve(s, event, ids=ids)


async def _scalar(make, stmt):
    async with make() as s:
        return await s.scalar(stmt)


async def test_two_people_confirming_the_same_requests_register_each_once(make):
    w = await _world(make, "twoapprovers", requests=40)
    a, b = await asyncio.gather(_approve(make, w["event"], w["ids"]), _approve(make, w["event"], list(reversed(w["ids"]))))
    got_a = {x["id"] for x in a["approved"]}
    got_b = {x["id"] for x in b["approved"]}
    # Every request confirmed exactly once, between the two.
    assert not got_a & got_b and got_a | got_b == set(w["ids"])
    assert {s["reason"] for s in a["skipped"] + b["skipped"]} <= {"not_pending"}
    assert len(a["skipped"]) == 40 - len(got_a) and len(b["skipped"]) == 40 - len(got_b)
    live = await _scalar(make, select(func.count()).select_from(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == w["event"], EventRegistrationsLink.is_deleted == False,  # noqa: E712
    ))
    assert live == 40
    assert await _scalar(make, select(Events.going_count).where(Events.id == w["event"])) == 40
    assert await _scalar(make, select(func.count()).select_from(R).where(R.event_id == w["event"], R.status == "approved")) == 40


async def test_a_confirmation_and_a_registration_never_both_take_the_last_place(make):
    """The setting turned off with a request still waiting: the form registers
    directly, and the club confirms the request, at the same moment, for the
    one place left. Run a few times, so both orders happen."""
    outcomes = []
    for n in range(6):
        w = await _world(make, f"lastplace{n}", requests=1, max_participants=1)
        async with make() as s:
            await s.execute(update(Events).where(Events.id == w["event"]).values(requires_approval=False))
            walker = await _make_user(s, email=f"lastplace{n}-walkin@example.com", username=f"lastplace{n}walkin")
            await s.commit()
            walker_id = walker.id

        async def public():
            async with make() as s:
                try:
                    await registration.register_event(
                        s, full_name="Walk In", email=f"lastplace{n}-walkin@example.com", event_id=w["event"],
                        user_id=walker_id, notify=False,
                    )
                    return "registered"
                except CustomHTTPException as exc:
                    return exc.error_code

        confirmed, walked = await asyncio.gather(_approve(make, w["event"], w["ids"]), public())
        won = len(confirmed["approved"])
        assert won + (walked == "registered") == 1, (confirmed, walked)
        if won:
            assert walked == "EVENT_FULL"
        else:
            assert confirmed["skipped"] == [{"id": w["ids"][0], "reason": "event_full"}]
        live = await _scalar(make, select(func.count()).select_from(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == w["event"], EventRegistrationsLink.is_deleted == False,  # noqa: E712
        ))
        assert live == 1 and await _scalar(make, select(Events.going_count).where(Events.id == w["event"])) == 1
        outcomes.append("request" if won else "walk-in")
    print("last place went to:", outcomes)


async def test_paid_holds_at_once_never_outnumber_the_places(make):
    """Two people confirming paid requests at once, three places: three holds."""
    w = await _world(make, "paidholds", requests=8, has_fee=True, reg_fee=Decimal("150"), max_participants=3)
    a, b = await asyncio.gather(_approve(make, w["event"], w["ids"][:4]), _approve(make, w["event"], w["ids"][4:]))
    held = [x for x in a["approved"] + b["approved"]]
    assert len(held) == 3 and all(x["pay_by"] for x in held)
    assert {s["reason"] for s in a["skipped"] + b["skipped"]} == {"event_full"}
    offers = await _scalar(make, select(func.count()).select_from(WaitlistEntries).where(
        WaitlistEntries.event_id == w["event"], WaitlistEntries.status == "offered", WaitlistEntries.via_request == True,  # noqa: E712
    ))
    assert offers == 3
