"""The search outbox: what changed, written in the same transaction as the
change, for the drain (sync.py) to carry to Meilisearch.

A row is (kind, ref_id). Triggers write them, so every write path is seen
(the dashboard, the app, imports, the status clock, the trash, raw SQL) and
a change that rolls back leaves no row behind:

- 'event': an event was made, deleted, or changed in something its search
  document holds (its text, status, dates, place, price, club, category,
  popularity to two decimals), or its interest links changed;
- 'club': a club was made, deleted, or changed in its text, listing, place,
  or its followers by about a tenth (ln(1 + followers) to one decimal: a
  follow storm writes a few rows, not one a follow);
- 'club_events': a club's listing changed (verified, deleted, its
  organisation blocked): each of its events' documents carries it;
- 'category', 'interest', 'topic': renamed, moved or deleted; the drain
  rewrites the search_text of every row they touch, in batches
  (sync.rebuild_taxonomy), and those rewrites put their own 'event' and
  'club' rows here.

A backfill of search_text or the grid cells sets `moa.quiet_outbox` for
its own transactions: filling a column every row lacked is no change to
any document (the indexes are built from the rows afterwards), and a
million outbox rows would be.

Insert-only and unordered per row: the drain reads a batch in id order,
reads the rows' current state, sends it, then deletes exactly the outbox
rows it read. A row committed late (a lower id than one already read) is
simply read next time, and the state sent is always read after the row that
asked for it, so nothing is lost and nothing older overwrites newer.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Column, DDL, Integer, String, event as sa_event, text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from app.db.base import AbstractSQLModel

KINDS = ("event", "club", "club_events", "category", "interest", "topic")


class SearchOutbox(AbstractSQLModel):
    __tablename__ = "search_outbox"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kind = Column(String(16), nullable=False)
    ref_id = Column(Integer, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"))


FUNCTIONS_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_search_outbox_event() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF current_setting('moa.quiet_outbox', true) = 'on' THEN
        RETURN NULL;
      END IF;
      INSERT INTO search_outbox (kind, ref_id) VALUES ('event', CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END);
      RETURN NULL;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_search_outbox_club() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF current_setting('moa.quiet_outbox', true) = 'on' THEN
        RETURN NULL;
      END IF;
      INSERT INTO search_outbox (kind, ref_id) VALUES ('club', CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END);
      IF TG_OP = 'UPDATE' AND (OLD.listed IS DISTINCT FROM NEW.listed OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted) THEN
        INSERT INTO search_outbox (kind, ref_id) VALUES ('club_events', NEW.id);
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_search_outbox_taxonomy() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      INSERT INTO search_outbox (kind, ref_id) VALUES (TG_ARGV[0], NEW.id);
      RETURN NULL;
    END $$
    """,
)

TRIGGERS_SQL = (
    """
    CREATE TRIGGER trg_events_search_outbox AFTER INSERT OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION moa_search_outbox_event()
    """,
    """
    CREATE TRIGGER trg_events_search_outbox_update AFTER UPDATE ON events
    FOR EACH ROW WHEN (
      OLD.search_text IS DISTINCT FROM NEW.search_text OR OLD.status IS DISTINCT FROM NEW.status
      OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted OR OLD.event_datetime IS DISTINCT FROM NEW.event_datetime
      OR OLD.duration IS DISTINCT FROM NEW.duration OR OLD.is_online IS DISTINCT FROM NEW.is_online
      OR OLD.has_fee IS DISTINCT FROM NEW.has_fee OR OLD.reg_fee IS DISTINCT FROM NEW.reg_fee
      OR OLD.lat IS DISTINCT FROM NEW.lat OR OLD.lng IS DISTINCT FROM NEW.lng
      OR OLD.club_id IS DISTINCT FROM NEW.club_id OR OLD.category_id IS DISTINCT FROM NEW.category_id
      OR OLD.created_at IS DISTINCT FROM NEW.created_at
      OR round(OLD.popularity::numeric, 2) IS DISTINCT FROM round(NEW.popularity::numeric, 2)
    )
    EXECUTE FUNCTION moa_search_outbox_event()
    """,
    """
    CREATE TRIGGER trg_clubs_search_outbox AFTER INSERT OR DELETE ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_search_outbox_club()
    """,
    """
    CREATE TRIGGER trg_clubs_search_outbox_update AFTER UPDATE ON clubs
    FOR EACH ROW WHEN (
      OLD.search_text IS DISTINCT FROM NEW.search_text OR OLD.listed IS DISTINCT FROM NEW.listed
      OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted
      OR OLD.place_lat IS DISTINCT FROM NEW.place_lat OR OLD.place_lng IS DISTINCT FROM NEW.place_lng
      OR OLD.created_at IS DISTINCT FROM NEW.created_at
      OR floor(ln(1 + greatest(OLD.follower_count, 0)) * 10) IS DISTINCT FROM floor(ln(1 + greatest(NEW.follower_count, 0)) * 10)
    )
    EXECUTE FUNCTION moa_search_outbox_club()
    """,
    """
    CREATE TRIGGER trg_event_categories_search_outbox AFTER UPDATE OF name, is_deleted ON event_categories
    FOR EACH ROW WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted)
    EXECUTE FUNCTION moa_search_outbox_taxonomy('category')
    """,
    """
    CREATE TRIGGER trg_interests_search_outbox AFTER UPDATE OF name, category_id, is_deleted ON interests
    FOR EACH ROW WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.category_id IS DISTINCT FROM NEW.category_id OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted)
    EXECUTE FUNCTION moa_search_outbox_taxonomy('interest')
    """,
    """
    CREATE TRIGGER trg_interest_categories_search_outbox AFTER UPDATE OF name, is_deleted ON interest_categories
    FOR EACH ROW WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted)
    EXECUTE FUNCTION moa_search_outbox_taxonomy('topic')
    """,
)


def _install(target, connection, **kw) -> None:
    if connection.dialect.name != "postgresql":
        return
    for sql in FUNCTIONS_SQL + TRIGGERS_SQL:
        connection.execute(DDL(sql))


sa_event.listen(AbstractSQLModel.metadata, "after_create", _install)
