"""Ticketing under concurrency: no ticket type sold past its places, no code
used past its uses, no more offers than places, one registration per claim.

These run their sessions on connections of their own and commit, as
concurrent requests do (the usual rolled-back test session cannot show a
race). So each test is followed by a sweep of every row it committed, in
every table (tests/racing.py): the list of what to delete by hand it
replaces missed the ledger lines and search outbox rows a registration
writes, and the ledger and demo-seed tests that ran after this file found
them."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.clubs.models import Clubs
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.events.registration import service as registration
from app.api.ticketing import waitlist
from app.api.ticketing.models import DiscountCodes, TicketTypes, WaitlistEntries, WaitlistSettings
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
async def _leaves_no_trace(_engine):
    """Whatever a test here commits is gone when it ends, pass or fail: first
    the work it left queued (run later, it would write after the sweep), then
    every row that was not there before it."""
    from app.core import after_commit

    before = await committed_keys(_engine)
    yield
    await after_commit.drain()
    await sweep(_engine, before)
    after = await committed_keys(_engine)
    left = {name: len(keys - before[name]) for name, keys in after.items() if keys - before[name]}
    assert not left, f"rows left behind: {left}"


async def _world(make, tag, *, guests: int, **event_values):
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
            club_id=club.id, category_id=category.id, **event_values,
        )
        s.add(event)
        await s.flush()
        users = [await _make_user(s, email=f"{tag}{i}@example.com", username=f"{tag}{i}") for i in range(guests)]
        await s.commit()
        return dict(owner=owner.id, club=club.id, category=category.id, event=event.id, people=[(u.id, u.email) for u in users])


async def _types(make, event_id, rows: list[dict]) -> list[int]:
    async with make() as s:
        made = []
        for n, row in enumerate(rows):
            t = TicketTypes(event_id=event_id, sort_order=n, is_default=n == 0, taken_count=0, **{"price": Decimal("0"), **row})
            s.add(t)
            made.append(t)
        await s.commit()
        return [t.id for t in made]


async def _register_all(make, event_id, people, **kw):
    async def one(user_id, email):
        async with make() as s:
            try:
                data = await registration.register_event(
                    s, full_name="Burst Guest", email=email, event_id=event_id, user_id=user_id, notify=False, **kw,
                )
                return ("ok", data)
            except CustomHTTPException as exc:
                return (exc.status_code, exc.error_code or exc.message)

    return await asyncio.gather(*(one(u, e) for u, e in people))


async def _scalar(make, stmt):
    async with make() as s:
        return await s.scalar(stmt)


async def test_a_burst_on_a_type_never_sells_past_its_places(make):
    w = await _world(make, "typeburst", guests=12)
    hall, _other = await _types(make, w["event"], [{"name": "Hall", "capacity": 3}, {"name": "Lawn"}])
    outcomes = await _register_all(make, w["event"], w["people"], ticket_type_id=hall)
    assert sum(1 for o in outcomes if o[0] == "ok") == 3
    assert all(o == (409, "TICKET_TYPE_SOLD_OUT") for o in outcomes if o[0] != "ok")
    live = await _scalar(make, select(func.count()).select_from(EventRegistrationsLink).where(
        EventRegistrationsLink.ticket_type_id == hall, EventRegistrationsLink.is_deleted == False,  # noqa: E712
    ))
    assert live == 3 and await _scalar(make, select(TicketTypes.taken_count).where(TicketTypes.id == hall)) == 3


async def test_a_burst_of_free_tickets_on_a_paid_event_keeps_its_paid_places(make):
    """Free tickets on a paid event take paid places: the event's cap holds them too."""
    w = await _world(make, "freepaid", guests=10, has_fee=True, reg_fee=Decimal("300"), max_participants=4)
    await _types(make, w["event"], [{"name": "Paid", "price": Decimal("300")}, {"name": "Comp"}])
    comp = await _scalar(make, select(TicketTypes.id).where(TicketTypes.event_id == w["event"], TicketTypes.name == "Comp"))
    outcomes = await _register_all(make, w["event"], w["people"], ticket_type_id=comp)
    assert sum(1 for o in outcomes if o[0] == "ok") == 4
    assert all(o[0] == 400 and o[1] == "EVENT_FULL" for o in outcomes if o[0] != "ok")
    assert await _scalar(make, select(Events.paid_count).where(Events.id == w["event"])) == 4


async def test_a_burst_on_a_code_never_uses_it_past_its_limit(make):
    w = await _world(make, "codeburst", guests=8)
    await _types(make, w["event"], [{"name": "Entry", "price": Decimal("200")}])
    async with make() as s:
        await s.execute(update(Events).where(Events.id == w["event"]).values(has_fee=True, reg_fee=200))
        s.add(DiscountCodes(club_id=w["club"], event_id=w["event"], code="FREEBIE", kind="percent", value=100, max_uses=2))
        await s.commit()
    outcomes = await _register_all(make, w["event"], w["people"], discount_code="freebie")
    winners = [o for o in outcomes if o[0] == "ok"]
    assert len(winners) == 2 and all(o[1]["pay_amount"] == 0 and o[1]["is_paid"] for o in winners)
    assert all(o == (409, "DISCOUNT_CODE_USED_UP") for o in outcomes if o[0] != "ok")


async def test_offers_at_once_never_outnumber_the_places_and_nobody_new_takes_them(make, monkeypatch):
    # The offers' mails are not what this is about.
    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(waitlist, "_notify_later", nothing)
    w = await _world(make, "offerburst", guests=8)
    # Two types, so each has its own places (one type alone is the event's: its cap).
    seat, _lawn = await _types(make, w["event"], [{"name": "Seat", "capacity": 2}, {"name": "Lawn"}])
    holders = await _register_all(make, w["event"], w["people"][:2], ticket_type_id=seat)
    assert [o[0] for o in holders] == ["ok", "ok"]
    async with make() as s:
        s.add(WaitlistSettings(event_id=w["event"], enabled=True))
        for i in range(3):
            s.add(WaitlistEntries(event_id=w["event"], ticket_type_id=seat, full_name=f"Waiter {i}", email=f"offerburst-w{i}@example.com", status="waiting"))
        await s.commit()
        # One place frees up.
        await s.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.id == holders[0][1]["event_registration_id"]).values(is_deleted=True))
        await s.commit()

    async def fill():
        async with make() as s:
            return await waitlist.fill(s, w["event"])

    public = _register_all(make, w["event"], w["people"][2:5], ticket_type_id=seat)
    results = await asyncio.gather(fill(), fill(), fill(), public)
    assert sum(len(r) for r in results[:3]) == 1
    assert all(o == (409, "TICKET_TYPE_SOLD_OUT") for o in results[3])
    statuses = (await _scalar(make, select(func.array_agg(WaitlistEntries.status)).where(WaitlistEntries.event_id == w["event"])))
    assert sorted(statuses) == ["offered", "waiting", "waiting"]
    assert await _scalar(make, select(TicketTypes.taken_count).where(TicketTypes.id == seat)) == 1


async def test_one_offer_claimed_twice_at_once_is_one_registration(make, monkeypatch):
    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(waitlist, "_notify_later", nothing)
    w = await _world(make, "claimrace", guests=1, max_participants=1)
    # One type: the event's own cap is its places.
    seat, = await _types(make, w["event"], [{"name": "Seat"}])
    async with make() as s:
        s.add(WaitlistSettings(event_id=w["event"], enabled=True))
        s.add(WaitlistEntries(event_id=w["event"], ticket_type_id=seat, full_name="Claimer", email="claimrace-claim@example.com", status="waiting"))
        await s.commit()
        assert len(await waitlist.fill(s, w["event"])) == 1
    token = await _scalar(make, select(WaitlistEntries.offer_token).where(WaitlistEntries.event_id == w["event"]))
    user_id, _email = w["people"][0]

    async def claim():
        async with make() as s:
            entry = await waitlist.by_token(s, token)
            try:
                await registration.hold_address(s, w["event"], "claimrace-claim@example.com")
                entry = await s.scalar(select(WaitlistEntries).where(WaitlistEntries.id == entry.id).execution_options(populate_existing=True))
                waitlist.check_claimable(entry)
                return ("ok", await registration.register_event(
                    s, full_name="Claimer", email="claimrace-claim@example.com", event_id=w["event"], user_id=user_id,
                    notify=False, offer=entry,
                ))
            except CustomHTTPException as exc:
                return (exc.status_code, exc.error_code)

    outcomes = await asyncio.gather(claim(), claim())
    assert sorted(str(o[0]) for o in outcomes) == ["400", "ok"] or sorted(str(o[0]) for o in outcomes) == ["409", "ok"]
    assert await _scalar(make, select(func.count()).select_from(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == w["event"], EventRegistrationsLink.is_deleted == False,  # noqa: E712
    )) == 1
    assert await _scalar(make, select(WaitlistEntries.status).where(WaitlistEntries.event_id == w["event"])) == "claimed"
