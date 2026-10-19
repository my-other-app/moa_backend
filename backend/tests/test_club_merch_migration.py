"""The club_merch migration's backfill, on the shape it meets in production:
store items with an event and no club, and no offers.

The backfill is imported from the migration file itself and run on the
suite's schema (which create_all already gave club_id, NOT NULL, and the
offers table), inside the test's transaction: club_id is made nullable again
for the test, so items can stand as they were before the upgrade. What it
must do: every item takes its event's club and is offered on its event, in
its old order; an item whose event has no club is left, and counted; and a
second run changes nothing -- not even an offer taken away since."""
import importlib.util
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

NOW = datetime.now(timezone.utc)


def _migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "club_merch.py"
    spec = importlib.util.spec_from_file_location("club_merch_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(session, module) -> dict:
    connection = await session.connection()
    return await connection.run_sync(lambda sync: module.backfill(sync))


async def _item(session, event_id, name, sort_order=0, club_id=None) -> uuid.UUID:
    item_id = uuid.uuid4()
    await session.execute(text(
        "INSERT INTO event_store_items (id, club_id, event_id, name, price, stock, sold_count, reserved_count, free_roles, on_sale, sort_order, "
        "created_at, updated_at, is_deleted) VALUES (:id, :club, :event, :name, 100, 5, 0, 0, '[]', true, :sort, :at, :at, false)"
    ), {"id": item_id, "club": club_id, "event": event_id, "name": name, "sort": sort_order, "at": NOW - timedelta(days=3)})
    return item_id


async def _offers(session) -> set[tuple]:
    return {tuple(r) for r in await session.execute(text("SELECT event_id, item_id, sort_order FROM event_store_offers"))}


async def test_the_backfill_gives_each_item_its_events_club_and_an_offer(session, make_club, make_event):
    module = _migration()
    owner, club = await make_club(name="Backfill Club", email="backfill@example.com")
    rival_owner, rival = await make_club(name="Backfill Rival", email="backfill-rival@example.com")
    a = await make_event(club=club, created_by=owner, name="Night A")
    b = await make_event(club=club, created_by=owner, name="Night B", is_deleted=True)
    r = await make_event(club=rival, created_by=rival_owner, name="Rival Night")
    orphan_event = await make_event(club=None, created_by=owner, name="No Club Night")
    await session.flush()
    await session.execute(text("ALTER TABLE event_store_items ALTER COLUMN club_id DROP NOT NULL"))

    tee = await _item(session, a.id, "Tee", sort_order=1)
    cap = await _item(session, a.id, "Cap", sort_order=0)
    old = await _item(session, b.id, "Old mug", sort_order=3)  # an item of a deleted event moves too
    theirs = await _item(session, r.id, "Rival tee")
    stray = await _item(session, orphan_event.id, "Stray")
    await session.execute(text("UPDATE event_store_items SET is_deleted = true WHERE id = :id"), {"id": old})

    done = await _run(session, module)
    assert done == {"moved": 4, "offers": 4, "without_club": 1}
    clubs = dict(tuple(r) for r in await session.execute(text("SELECT id, club_id FROM event_store_items")))
    assert clubs == {tee: club.id, cap: club.id, old: club.id, theirs: rival.id, stray: None}
    assert await _offers(session) == {(a.id, tee, 1), (a.id, cap, 0), (b.id, old, 3), (r.id, theirs, 0)}
    # Each item keeps the event it was made for.
    events = dict(tuple(r) for r in await session.execute(text("SELECT id, event_id FROM event_store_items")))
    assert events[tee] == a.id and events[theirs] == r.id

    # The organiser takes the cap off Night A; a second run puts nothing back.
    await session.execute(text("DELETE FROM event_store_offers WHERE item_id = :id"), {"id": cap})
    assert await _run(session, module) == {"moved": 0, "offers": 0, "without_club": 1}
    assert (a.id, cap, 0) not in await _offers(session)

    # Once the stray's event has a club, it moves like the others.
    await session.execute(text("UPDATE events SET club_id = :c WHERE id = :e"), {"c": club.id, "e": orphan_event.id})
    assert await _run(session, module) == {"moved": 1, "offers": 1, "without_club": 0}
    assert (orphan_event.id, stray, 0) in await _offers(session)


async def test_an_item_already_in_a_clubs_store_is_left_as_it_is(session, make_club, make_event):
    module = _migration()
    owner, club = await make_club(name="Kept Club", email="kept@example.com")
    await make_event(club=club, created_by=owner, name="Kept Night")
    await session.flush()
    made_in_store = await _item(session, None, "Club cap", club_id=club.id)
    assert await _run(session, module) == {"moved": 0, "offers": 0, "without_club": 0}
    assert await _offers(session) == set()
    assert (await session.execute(text("SELECT club_id, event_id FROM event_store_items WHERE id = :id"), {"id": made_in_store})).one() == (club.id, None)
