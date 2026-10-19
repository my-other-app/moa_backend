"""The store's items belong to the club; each event offers the ones it picks.

Until now every store item was one event's (`event_store_items.event_id`),
so a club running three or four events at once had to make the same
t-shirt on each, with its own stock. Now:

- `event_store_items.club_id` (FK clubs, indexed, NOT NULL): the club whose
  catalogue the item is in. Filled from each item's event.
- `event_store_items.event_id` becomes nullable, and its foreign key ON
  DELETE SET NULL (it was CASCADE): it only records the event an item was
  first made for. An item made in the club's catalogue has none, and
  removing that first event must not take with it an item other events sell.
- `event_store_offers` (event_id, item_id) -> sort_order, created_at: which
  of the club's items an event sells, in the event's order. One offer per
  existing item, for its event, in the item's old order (`backfill`).

Stock and its counts stay on the items and their sizes, so they are shared
by every event that offers the item. Orders keep their event_id; what each
event sold is read from its orders.

`backfill` moves only the items that have no club yet, and makes offers only
for those, in one statement: run again, it changes nothing, and it never puts
back an offer an organiser has taken away. An item whose event has no club
cannot be moved; the upgrade then stops, before anything is NOT NULL, and
says which.

Every table touched is small (tens of rows in production), and each step is
under a short lock_timeout. The foreign keys to events and clubs take a
brief SHARE ROW EXCLUSIVE on those tables, held until this migration commits,
which is milliseconds; env.py retries the migration when a lock is not
granted in time. Every step checks first, so the upgrade is safe to run again.

Downgrade refuses, with a message, while any item has no event_id (a club
item made after this upgrade): before it, every item had one. Otherwise it
puts event_id back to NOT NULL with ON DELETE CASCADE, and drops the offers
and club_id; an item offered on events other than its own is then sold on
its own event only.

Revision ID: club_merch
Revises: interest_emoji
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "club_merch"
down_revision = "interest_emoji"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

#: The items still to move, as the upgrade and its test count them.
WITHOUT_CLUB = "SELECT count(*) FROM event_store_items WHERE club_id IS NULL"

#: Each item without a club takes its event's, and the event offers it, in one
#: statement: only the rows moved now get an offer.
MOVE = """
WITH moved AS (
    UPDATE event_store_items AS i SET club_id = e.club_id
    FROM events AS e
    WHERE e.id = i.event_id AND i.club_id IS NULL AND e.club_id IS NOT NULL
    RETURNING i.id, i.event_id, i.sort_order, i.created_at
)
INSERT INTO event_store_offers (event_id, item_id, sort_order, created_at)
SELECT event_id, id, sort_order, created_at FROM moved
ON CONFLICT (event_id, item_id) DO NOTHING
"""

#: The foreign keys on event_store_items.event_id, with their ON DELETE action
#: (pg_constraint.confdeltype: c = CASCADE, n = SET NULL).
EVENT_FKS = """
SELECT c.conname, c.confdeltype
FROM pg_constraint AS c
JOIN pg_class AS t ON t.oid = c.conrelid
JOIN pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
WHERE t.relname = 'event_store_items' AND c.contype = 'f' AND a.attname = 'event_id'
"""

CLUB_FK = """
SELECT 1 FROM pg_constraint AS c JOIN pg_class AS t ON t.oid = c.conrelid
WHERE t.relname = 'event_store_items' AND c.conname = 'event_store_items_club_id_fkey'
"""


def backfill(bind) -> dict:
    """Move the items that have no club yet: each takes its event's club and
    is offered on that event. Returns what it did: `moved` (items given a
    club), `offers` (offers made) and `without_club` (items it could not
    move: no event, or an event without a club)."""
    before = bind.execute(sa.text(WITHOUT_CLUB)).scalar() or 0
    offers = bind.execute(sa.text(MOVE)).rowcount or 0
    after = bind.execute(sa.text(WITHOUT_CLUB)).scalar() or 0
    return {"moved": before - after, "offers": offers, "without_club": after}


def _event_fk(bind, action: str) -> None:
    """The event_id foreign key, ON DELETE `action` ("SET NULL" or "CASCADE"),
    named as Postgres names it; any other one on the column is dropped."""
    want = {"SET NULL": "n", "CASCADE": "c"}[action]
    rows = bind.execute(sa.text(EVENT_FKS)).all()
    if len(rows) == 1 and rows[0][1] == want:
        return
    for name, _ in rows:
        op.execute(f'ALTER TABLE event_store_items DROP CONSTRAINT "{name}"')
    op.execute(
        "ALTER TABLE event_store_items ADD CONSTRAINT event_store_items_event_id_fkey "
        f"FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE {action}"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    bind = op.get_bind()
    op.execute("ALTER TABLE event_store_items ADD COLUMN IF NOT EXISTS club_id integer")
    op.execute(
        "CREATE TABLE IF NOT EXISTS event_store_offers ("
        " event_id integer NOT NULL REFERENCES events (id) ON DELETE CASCADE,"
        " item_id uuid NOT NULL REFERENCES event_store_items (id) ON DELETE CASCADE,"
        " sort_order integer NOT NULL DEFAULT 0,"
        " created_at timestamp with time zone NOT NULL DEFAULT now(),"
        " CONSTRAINT event_store_offers_pkey PRIMARY KEY (event_id, item_id))"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_event_store_offers_item_id ON event_store_offers (item_id)")

    done = backfill(bind)
    log.info("club_merch: %(moved)s store items given their event's club, %(offers)s offers made", done)
    if done["without_club"]:
        ids = [str(r[0]) for r in bind.execute(sa.text("SELECT id FROM event_store_items WHERE club_id IS NULL ORDER BY id LIMIT 20")).all()]
        raise RuntimeError(
            f"club_merch: {done['without_club']} store item(s) belong to an event without a club, so they cannot join a "
            f"club's catalogue: {', '.join(ids)}. Give their events a club (or delete the items) and run the upgrade again."
        )

    op.execute("ALTER TABLE event_store_items ALTER COLUMN club_id SET NOT NULL")
    if bind.execute(sa.text(CLUB_FK)).first() is None:
        op.execute(
            "ALTER TABLE event_store_items ADD CONSTRAINT event_store_items_club_id_fkey "
            "FOREIGN KEY (club_id) REFERENCES clubs (id) ON DELETE CASCADE"
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_event_store_items_club_id ON event_store_items (club_id)")
    op.execute("ALTER TABLE event_store_items ALTER COLUMN event_id DROP NOT NULL")
    _event_fk(bind, "SET NULL")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    bind = op.get_bind()
    orphans = bind.execute(sa.text("SELECT count(*) FROM event_store_items WHERE event_id IS NULL")).scalar() or 0
    if orphans:
        raise RuntimeError(
            f"club_merch downgrade refused: {orphans} store item(s) have no event_id (made in a club's catalogue, or "
            "their first event was removed). Before club_merch every item belonged to one event, so event_id cannot be "
            "NOT NULL again. Give each of them an event_id (or delete them) and run the downgrade again."
        )
    if bind.execute(sa.text("SELECT to_regclass('event_store_offers')")).scalar() is not None:
        elsewhere = bind.execute(sa.text(
            "SELECT count(*) FROM event_store_offers AS o JOIN event_store_items AS i ON i.id = o.item_id "
            "WHERE o.event_id <> i.event_id"
        )).scalar() or 0
        if elsewhere:
            log.warning("club_merch downgrade: %s offers of an item on an event other than its own are dropped", elsewhere)
    _event_fk(bind, "CASCADE")
    op.execute("ALTER TABLE event_store_items ALTER COLUMN event_id SET NOT NULL")
    op.execute("DROP TABLE IF EXISTS event_store_offers")
    op.execute("DROP INDEX IF EXISTS ix_event_store_items_club_id")
    op.execute("ALTER TABLE event_store_items DROP COLUMN IF EXISTS club_id")
