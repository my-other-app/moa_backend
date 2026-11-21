"""Lock order between a confirmation's hold running out, its payment landing
and its guest withdrawing: requests first, then entries, on every path.

The hold-expiry sweep (waitlist.expire) used to take the entries that ran
out and then their requests, while a payment or a withdrawal takes the
request and then the entry; caught between the two, one of them was
aborted by Postgres as a deadlock. And a payment held its registration FOR
UPDATE, which a confirmation making a hold for that registration (a foreign
key, checked FOR KEY SHARE) waited for while it held the event's row the
payment waited for.

As tests/test_approvals_races.py: sessions on connections of their own that
commit, and every committed row swept away after each test
(tests/racing.py). Each waiting statement runs with a short lock_timeout,
so a wait shows as an error instead of a hang."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.approvals import notify
from app.api.approvals import service as approvals
from app.api.approvals.models import RegistrationRequests as R
from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.payments.handlers import registration_for_order
from app.api.ticketing import places, waitlist
from app.api.ticketing import types as types_mod
from app.api.ticketing.models import WaitlistEntries as W
from app.api.users.models import UserTypes
from app.db.core import SESSION_KWARGS
from app.db.listeners import add_loader_criteria
from tests.conftest import _make_user
from tests.racing import committed_keys, statements, sweep

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


async def _short_wait(s, ms: int = 1500) -> None:
    await s.execute(text(f"SET LOCAL lock_timeout = '{ms}ms'"))


async def _world(make, tag) -> SimpleNamespace:
    """An event with a confirmation's hold that ran out a minute ago (its
    request confirmed, not paid), a plain waiting-list offer that ran out
    too, and a registration."""
    async with make() as s:
        owner = await _make_user(s, email=f"{tag}-owner@example.com", user_type=UserTypes.club, username=f"{tag}owner")
        guest = await _make_user(s, email=f"{tag}-guest@example.com", username=f"{tag}guest")
        club = Clubs(slug=f"{tag}-club", name=f"{tag} Club", user_id=owner.id, is_verified=True)
        s.add(club)
        await s.flush()
        category = EventCategories(name=tag, created_by_id=owner.id)
        s.add(category)
        await s.flush()
        event = Events(
            slug=f"{tag}-night", name=f"{tag} Night", images=[], duration=2.0, event_datetime=NOW + timedelta(days=3),
            club_id=club.id, category_id=category.id, requires_approval=True,
        )
        s.add(event)
        await s.flush()
        type_id = (await types_mod.ensure(s, event))[0].id
        reg = EventRegistrationsLink(event_id=event.id, user_id=guest.id, full_name="Guest", email=guest.email, ticket_id=f"T-{tag}"[:20], ticket_type_id=type_id)
        s.add(reg)
        await s.flush()
        hold, plain = uuid.uuid4(), uuid.uuid4()
        ran_out = NOW - timedelta(minutes=1)
        await s.execute(insert(W), [
            dict(id=hold, event_id=event.id, ticket_type_id=type_id, full_name="Held Guest", email=f"{tag}-held@example.com",
                 status="offered", offer_token=f"{tag}-hold", offered_at=NOW - timedelta(days=1), offer_expires_at=ran_out,
                 via_request=True, created_at=NOW, updated_at=NOW),
            dict(id=plain, event_id=event.id, ticket_type_id=type_id, full_name="Line Guest", email=f"{tag}-line@example.com",
                 status="offered", offer_token=f"{tag}-plain", offered_at=NOW - timedelta(days=1), offer_expires_at=ran_out,
                 created_at=NOW, updated_at=NOW),
        ])
        request = uuid.uuid4()
        await s.execute(insert(R), [dict(
            id=request, event_id=event.id, full_name="Held Guest", email=f"{tag}-held@example.com", ticket_type_id=type_id,
            status="approved", token=approvals.new_token(), submitted_at=NOW, whatsapp_opt_in=False,
            club_updates_whatsapp=False, waitlist_entry_id=hold, created_at=NOW, updated_at=NOW,
        )])
        await s.commit()
        return SimpleNamespace(event=event.id, type=type_id, hold=hold, plain=plain, request=request, registration=reg.id)


async def _status(make, model, row_id):
    async with make() as s:
        return await s.scalar(select(model.status).where(model.id == row_id))


async def test_the_sweep_leaves_a_hold_whose_request_a_payment_holds_and_waits_for_nothing(make):
    w = await _world(make, "lockorder")
    paying = make()
    try:
        # A payment landing for the hold: its request first (approvals.on_paid).
        await _short_wait(paying)
        await paying.execute(select(R.id).where(R.id == w.request).with_for_update())

        async with make() as sweeper:
            await _short_wait(sweeper)
            # Before, the sweep took both entries and then waited for the request.
            assert await waitlist.expire(sweeper) == [w.event]
        assert await _status(make, W, w.plain) == "expired"
        assert await _status(make, W, w.hold) == "offered"
        assert await _status(make, R, w.request) == "approved"

        # And the payment goes on to its entry without waiting.
        await paying.execute(select(W.id).where(W.id == w.hold).with_for_update(key_share=True))
    finally:
        await paying.rollback()
        await paying.close()

    # Nobody holds it now: the next minute's sweep expires it, and its request lapses.
    async with make() as sweeper:
        assert await waitlist.expire(sweeper) == [w.event]
    assert await _status(make, W, w.hold) == "expired"
    assert await _status(make, R, w.request) == "lapsed"


async def test_the_sweep_skips_an_entry_another_transaction_holds(make):
    w = await _world(make, "lockentry")
    other = make()
    try:
        await _short_wait(other)
        await other.execute(select(W.id).where(W.id == w.plain).with_for_update())
        async with make() as sweeper:
            await _short_wait(sweeper)
            assert await waitlist.expire(sweeper) == [w.event]
        assert await _status(make, W, w.plain) == "offered"
        assert await _status(make, W, w.hold) == "expired"
        assert await _status(make, R, w.request) == "lapsed"
    finally:
        await other.rollback()
        await other.close()


async def test_a_hold_for_a_registration_does_not_wait_for_its_payment_but_another_payment_does(make):
    w = await _world(make, "regkeyshare")
    order = SimpleNamespace(payload={"event_registration_id": str(w.registration)}, receipt=None)
    paying = make()
    try:
        await _short_wait(paying)
        assert (await registration_for_order(paying, order, lock=True)).id == w.registration

        # A row naming the registration (the hold a re-confirmation makes): no wait.
        async with make() as confirming:
            await _short_wait(confirming)
            await confirming.execute(insert(W).values(
                event_id=w.event, ticket_type_id=w.type, full_name="Again", email="regkeyshare-again@example.com",
                status="offered", offer_expires_at=NOW + timedelta(hours=1), via_request=True,
                registration_id=w.registration, created_at=NOW, updated_at=NOW,
            ))
            await confirming.rollback()

        # A second payment for the same registration still queues behind the first.
        async with make() as second:
            await _short_wait(second, 300)
            with pytest.raises(DBAPIError) as caught:
                await registration_for_order(second, order, lock=True)
            assert "lock timeout" in str(caught.value).lower()
            await second.rollback()
    finally:
        await paying.rollback()
        await paying.close()


async def test_a_holds_payment_takes_its_request_before_its_entry(session, make_club, make_event, make_user, make_registration):
    """capture_room, statement by statement: the request's UPDATE, then the
    entry's row lock (it used to change the entry first, written at commit)."""
    owner, club = await make_club(name="Order Club", email="order-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Order Night", event_datetime=NOW + timedelta(days=3))
    type_id = (await types_mod.ensure(session, event))[0].id
    reg = await make_registration(event=event, user=await make_user(email="order-guest@example.com"), ticket_type_id=type_id)
    entry = W(event_id=event.id, ticket_type_id=type_id, full_name="Order Guest", email="order-guest@example.com", status="offered",
              offer_expires_at=NOW + timedelta(hours=1), via_request=True, registration_id=reg.id)
    session.add(entry)
    await session.flush()
    session.add(R(event_id=event.id, full_name="Order Guest", email="order-guest@example.com", ticket_type_id=type_id,
                  status="approved", token=approvals.new_token(), waitlist_entry_id=entry.id))
    await session.flush()

    with statements(session) as seen:
        assert await places.capture_room(session, event, reg) is True
    request_update = next(i for i, sql in enumerate(seen) if sql.lstrip().upper().startswith("UPDATE REGISTRATION_REQUESTS"))
    entry_lock = next(i for i, sql in enumerate(seen) if "FROM waitlist_entries" in sql and "FOR NO KEY UPDATE" in sql)
    assert request_update < entry_lock
    await session.flush()
    assert entry.status == "claimed"
    assert await session.scalar(select(R.status).where(R.waitlist_entry_id == entry.id)) == "approved"
