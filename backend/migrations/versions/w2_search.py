"""Search and geo, Wave 2: search_text, the search outbox, grid cells.

- unaccent, for search_text.
- events.search_text, clubs.search_text, users.search_text: everything a row
  is found by, lowercased and unaccented, in one column, kept by triggers
  (app.api.search.text); events.geocell and clubs.geocell, the 0.5-degree
  grid cell of an event's point (held in person) or a club's place, kept by
  triggers (app.api.discover.cells).
- search_outbox, written by triggers in the writer's transaction, which the
  search sync carries to Meilisearch (app.api.search.outbox, .sync).
- The trigram indexes the searches now read -- ix_events_search_trgm,
  ix_events_shown_search_trgm, ix_clubs_search_trgm, ix_users_search_trgm --
  and the cell indexes "near you" reads, ix_events_shown_cell and
  ix_clubs_listed_cell.
- The per-column trigram indexes nothing reads any more are dropped:
  ix_events_location_trgm, ix_events_shown_name_trgm,
  ix_events_shown_location_trgm, ix_clubs_tagline_trgm,
  ix_clubs_location_trgm, ix_users_full_name_trgm, ix_users_email_trgm,
  ix_users_username_trgm. (ix_events_name_trgm and ix_clubs_name_trgm stay:
  the dashboards' and the admin's "name contains" read them.)

Lock-safe (2026-10-18: the Wave 1 deploy stalled production behind
migration locks):

- The columns are nullable with no default: a catalogue change, no rewrite.
  Each ALTER and CREATE TRIGGER runs under lock_timeout 3s, so a long query
  on the table makes this fail fast (run it again) rather than queue every
  request behind it.
- The backfill runs outside any long transaction: a statement a batch,
  each committed, each batch sized to take about 0.2 s (a writer that wants
  one of its rows waits no longer than that), rows another writer holds
  skipped (SKIP LOCKED) and taken on a later pass. The triggers keep every row written
  meanwhile right. It writes no outbox rows (moa.quiet_outbox): the indexes
  are built from the rows when search is switched on.
- Indexes are built and dropped CONCURRENTLY, in an autocommit block; one a
  failed run left INVALID is dropped and built again.

The SQL is written out here, not imported: what this migration did must not
change when the app's code does.

Revision ID: w2_search
Revises: topics
"""
import time

import sqlalchemy as sa
from alembic import op

revision = "w2_search"
down_revision = "topics"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
#: Rows per backfill statement to start with, its bounds, and how long a
#: statement should take: the batch is resized after each so a statement
#: holds its rows about this long -- a writer that wants one of them waits
#: no longer -- however fast or loaded the database is.
BACKFILL_BATCH = 500
BACKFILL_MIN, BACKFILL_MAX = 20, 5000
BACKFILL_SECONDS = 0.2
#: Passes over the rows other writers held meanwhile.
BACKFILL_PASSES = 5

COLUMNS = (
    ("events", "search_text", "text"),
    ("events", "geocell", "integer"),
    ("clubs", "search_text", "text"),
    ("clubs", "geocell", "integer"),
    ("users", "search_text", "text"),
)

TEXT_FUNCTIONS = (
    r"""
    CREATE OR REPLACE FUNCTION moa_search_norm(t text) RETURNS text
    LANGUAGE sql STABLE PARALLEL SAFE AS $$
      SELECT nullif(btrim(regexp_replace(lower(unaccent(coalesce(t, ''))), '[ \t\n\r\f\v]+', ' ', 'g')), '')
    $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_event_search_text(eid integer, ename text, eplace text, etag text, cid integer, catid integer)
    RETURNS text LANGUAGE plpgsql STABLE AS $$
    DECLARE
      club_part text;
      category_part text;
      interest_part text;
    BEGIN
      SELECT concat_ws(' ', c.name, c.location_name) INTO club_part FROM clubs c WHERE c.id = cid;
      SELECT k.name INTO category_part FROM event_categories k WHERE k.id = catid AND NOT k.is_deleted;
      SELECT string_agg(n, ' ' ORDER BY n) INTO interest_part FROM (
        SELECT i.name AS n FROM event_interests_link l JOIN interests i ON i.id = l.interest_id
        WHERE l.event_id = eid AND NOT l.is_deleted AND NOT i.is_deleted
        UNION
        SELECT t.name FROM event_interests_link l JOIN interests i ON i.id = l.interest_id
          JOIN interest_categories t ON t.id = i.category_id
        WHERE l.event_id = eid AND NOT l.is_deleted AND NOT i.is_deleted AND NOT t.is_deleted
      ) named;
      RETURN moa_search_norm(concat_ws(' ', ename, eplace, etag, club_part, category_part, interest_part));
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_club_search_text(cid integer, cname text, ctagline text, cplace text)
    RETURNS text LANGUAGE plpgsql STABLE AS $$
    DECLARE
      interest_part text;
    BEGIN
      SELECT string_agg(n, ' ' ORDER BY n) INTO interest_part FROM (
        SELECT i.name AS n FROM clubs_interests_link l JOIN interests i ON i.id = l.interest_id
        WHERE l.club_id = cid AND NOT l.is_deleted AND NOT i.is_deleted
        UNION
        SELECT t.name FROM clubs_interests_link l JOIN interests i ON i.id = l.interest_id
          JOIN interest_categories t ON t.id = i.category_id
        WHERE l.club_id = cid AND NOT l.is_deleted AND NOT i.is_deleted AND NOT t.is_deleted
      ) named;
      RETURN moa_search_norm(concat_ws(' ', cname, ctagline, cplace, interest_part));
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_user_search_text(fname text, uname text, mail text)
    RETURNS text LANGUAGE sql STABLE PARALLEL SAFE AS $$
      SELECT concat_ws(' ‖ ', moa_search_norm(fname), moa_search_norm(uname), moa_search_norm(mail))
    $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_refresh_event_search(eid integer) RETURNS void LANGUAGE plpgsql AS $$
    BEGIN
      IF eid IS NULL THEN
        RETURN;
      END IF;
      UPDATE events e SET search_text = s.t
      FROM (SELECT x.id, moa_event_search_text(x.id, x.name, x.location_name, x.event_tag, x.club_id, x.category_id) AS t
            FROM events x WHERE x.id = eid) s
      WHERE e.id = s.id AND e.search_text IS DISTINCT FROM s.t;
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_refresh_club_search(cid integer) RETURNS void LANGUAGE plpgsql AS $$
    BEGIN
      IF cid IS NULL THEN
        RETURN;
      END IF;
      UPDATE clubs c SET search_text = s.t
      FROM (SELECT x.id, moa_club_search_text(x.id, x.name, x.tagline, x.location_name) AS t FROM clubs x WHERE x.id = cid) s
      WHERE c.id = s.id AND c.search_text IS DISTINCT FROM s.t;
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_events_search_text() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.search_text := moa_event_search_text(NEW.id, NEW.name, NEW.location_name, NEW.event_tag, NEW.club_id, NEW.category_id);
      RETURN NEW;
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_clubs_search_text() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.search_text := moa_club_search_text(NEW.id, NEW.name, NEW.tagline, NEW.location_name);
      RETURN NEW;
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_users_search_text() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.search_text := moa_user_search_text(NEW.full_name, NEW.username, NEW.email);
      RETURN NEW;
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_event_links_search() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM moa_refresh_event_search(OLD.event_id);
        INSERT INTO search_outbox (kind, ref_id) VALUES ('event', OLD.event_id);
      END IF;
      IF TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND NEW.event_id IS DISTINCT FROM OLD.event_id) THEN
        PERFORM moa_refresh_event_search(NEW.event_id);
        INSERT INTO search_outbox (kind, ref_id) VALUES ('event', NEW.event_id);
      END IF;
      RETURN NULL;
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_club_links_search() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM moa_refresh_club_search(OLD.club_id);
        INSERT INTO search_outbox (kind, ref_id) VALUES ('club', OLD.club_id);
      END IF;
      IF TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND NEW.club_id IS DISTINCT FROM OLD.club_id) THEN
        PERFORM moa_refresh_club_search(NEW.club_id);
        INSERT INTO search_outbox (kind, ref_id) VALUES ('club', NEW.club_id);
      END IF;
      RETURN NULL;
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_club_events_search() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      UPDATE events e SET search_text = moa_event_search_text(e.id, e.name, e.location_name, e.event_tag, e.club_id, e.category_id)
      WHERE e.club_id = NEW.id
        AND e.search_text IS DISTINCT FROM moa_event_search_text(e.id, e.name, e.location_name, e.event_tag, e.club_id, e.category_id);
      RETURN NULL;
    END $$
    """,
)

TEXT_TRIGGERS = (
    r"""
    CREATE TRIGGER trg_events_search_text BEFORE INSERT OR UPDATE OF name, location_name, event_tag, club_id, category_id ON events
    FOR EACH ROW EXECUTE FUNCTION moa_events_search_text()
    """,
    r"""
    CREATE TRIGGER trg_clubs_search_text BEFORE INSERT OR UPDATE OF name, tagline, location_name ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_clubs_search_text()
    """,
    r"""
    CREATE TRIGGER trg_users_search_text BEFORE INSERT OR UPDATE OF full_name, username, email ON users
    FOR EACH ROW EXECUTE FUNCTION moa_users_search_text()
    """,
    r"""
    CREATE TRIGGER trg_event_interests_search AFTER INSERT OR DELETE OR UPDATE OF event_id, interest_id, is_deleted ON event_interests_link
    FOR EACH ROW EXECUTE FUNCTION moa_event_links_search()
    """,
    r"""
    CREATE TRIGGER trg_club_interests_search AFTER INSERT OR DELETE OR UPDATE OF club_id, interest_id, is_deleted ON clubs_interests_link
    FOR EACH ROW EXECUTE FUNCTION moa_club_links_search()
    """,
    r"""
    CREATE TRIGGER trg_clubs_search_events AFTER UPDATE OF name, location_name ON clubs
    FOR EACH ROW WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.location_name IS DISTINCT FROM NEW.location_name)
    EXECUTE FUNCTION moa_club_events_search()
    """,
)

OUTBOX_FUNCTIONS = (
    r"""
    CREATE OR REPLACE FUNCTION moa_search_outbox_event() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF current_setting('moa.quiet_outbox', true) = 'on' THEN
        RETURN NULL;
      END IF;
      INSERT INTO search_outbox (kind, ref_id) VALUES ('event', CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END);
      RETURN NULL;
    END $$
    """,
    r"""
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
    r"""
    CREATE OR REPLACE FUNCTION moa_search_outbox_taxonomy() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      INSERT INTO search_outbox (kind, ref_id) VALUES (TG_ARGV[0], NEW.id);
      RETURN NULL;
    END $$
    """,
)

OUTBOX_TRIGGERS = (
    r"""
    CREATE TRIGGER trg_events_search_outbox AFTER INSERT OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION moa_search_outbox_event()
    """,
    r"""
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
    r"""
    CREATE TRIGGER trg_clubs_search_outbox AFTER INSERT OR DELETE ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_search_outbox_club()
    """,
    r"""
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
    r"""
    CREATE TRIGGER trg_event_categories_search_outbox AFTER UPDATE OF name, is_deleted ON event_categories
    FOR EACH ROW WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted)
    EXECUTE FUNCTION moa_search_outbox_taxonomy('category')
    """,
    r"""
    CREATE TRIGGER trg_interests_search_outbox AFTER UPDATE OF name, category_id, is_deleted ON interests
    FOR EACH ROW WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.category_id IS DISTINCT FROM NEW.category_id OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted)
    EXECUTE FUNCTION moa_search_outbox_taxonomy('interest')
    """,
    r"""
    CREATE TRIGGER trg_interest_categories_search_outbox AFTER UPDATE OF name, is_deleted ON interest_categories
    FOR EACH ROW WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted)
    EXECUTE FUNCTION moa_search_outbox_taxonomy('topic')
    """,
)

CELL_SQL = (
    r"""
    CREATE OR REPLACE FUNCTION moa_geocell(lat double precision, lng double precision) RETURNS integer
    LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
      SELECT CASE WHEN lat IS NULL OR lng IS NULL OR lat < -90 OR lat > 90 OR lng < -180 OR lng > 180 THEN NULL
        ELSE least(greatest(floor((lat + 90.0) * 2)::integer, 0), 359) * 720
           + least(greatest(floor((lng + 180.0) * 2)::integer, 0), 719) END
    $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_events_geocell() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.geocell := CASE WHEN coalesce(NEW.is_online, false) THEN NULL ELSE moa_geocell(NEW.lat, NEW.lng) END;
      RETURN NEW;
    END $$
    """,
    r"""
    CREATE OR REPLACE FUNCTION moa_clubs_geocell() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.geocell := moa_geocell(NEW.place_lat, NEW.place_lng);
      RETURN NEW;
    END $$
    """,
    r"""
    CREATE TRIGGER trg_events_geocell BEFORE INSERT OR UPDATE OF lat, lng, is_online ON events
    FOR EACH ROW EXECUTE FUNCTION moa_events_geocell()
    """,
    r"""
    CREATE TRIGGER trg_clubs_place_zcell BEFORE INSERT OR UPDATE OF lat, lng, place_lat, place_lng ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_clubs_geocell()
    """,
)

INDEXES = (
    # (name, definition after ON)
    ("ix_events_search_trgm", "events USING gin (search_text gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_events_shown_search_trgm", "events USING gin (search_text gin_trgm_ops) WHERE NOT is_deleted AND status IN ('published', 'live')"),
    ("ix_clubs_search_trgm", "clubs USING gin (search_text gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_users_search_trgm", "users USING gin (search_text gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_events_shown_cell", "events (geocell, event_datetime, id) WHERE NOT is_deleted AND status IN ('published', 'live') AND geocell IS NOT NULL"),
    ("ix_clubs_listed_cell", "clubs (geocell) WHERE listed AND geocell IS NOT NULL"),
)

#: What the new ones replace: put back by the downgrade.
DROPPED = (
    ("ix_events_location_trgm", "events USING gin (location_name gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_events_shown_name_trgm", "events USING gin (name gin_trgm_ops) WHERE NOT is_deleted AND status IN ('published', 'live')"),
    ("ix_events_shown_location_trgm", "events USING gin (location_name gin_trgm_ops) WHERE NOT is_deleted AND status IN ('published', 'live')"),
    ("ix_clubs_tagline_trgm", "clubs USING gin (tagline gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_clubs_location_trgm", "clubs USING gin (location_name gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_users_full_name_trgm", "users USING gin (full_name gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_users_email_trgm", "users USING gin (email gin_trgm_ops) WHERE NOT is_deleted"),
    ("ix_users_username_trgm", "users USING gin (username gin_trgm_ops) WHERE NOT is_deleted"),
)

TRIGGERS = (
    ("trg_events_search_text", "events"), ("trg_clubs_search_text", "clubs"), ("trg_users_search_text", "users"),
    ("trg_event_interests_search", "event_interests_link"), ("trg_club_interests_search", "clubs_interests_link"),
    ("trg_clubs_search_events", "clubs"),
    ("trg_events_search_outbox", "events"), ("trg_events_search_outbox_update", "events"),
    ("trg_clubs_search_outbox", "clubs"), ("trg_clubs_search_outbox_update", "clubs"),
    ("trg_event_categories_search_outbox", "event_categories"), ("trg_interests_search_outbox", "interests"),
    ("trg_interest_categories_search_outbox", "interest_categories"),
    ("trg_events_geocell", "events"), ("trg_clubs_place_zcell", "clubs"),
)

FUNCTIONS = (
    "moa_users_search_text()", "moa_clubs_search_text()", "moa_events_search_text()", "moa_event_links_search()",
    "moa_club_links_search()", "moa_club_events_search()", "moa_refresh_event_search(integer)", "moa_refresh_club_search(integer)",
    "moa_user_search_text(text, text, text)", "moa_club_search_text(integer, text, text, text)",
    "moa_event_search_text(integer, text, text, text, integer, integer)", "moa_search_norm(text)",
    "moa_search_outbox_event()", "moa_search_outbox_club()", "moa_search_outbox_taxonomy()",
    "moa_events_geocell()", "moa_clubs_geocell()", "moa_geocell(double precision, double precision)",
)

#: Per table: what a row's columns should be, and which rows still need it.
BACKFILL = (
    (
        "events",
        "search_text = moa_event_search_text(t.id, t.name, t.location_name, t.event_tag, t.club_id, t.category_id), "
        "geocell = CASE WHEN coalesce(t.is_online, false) THEN NULL ELSE moa_geocell(t.lat, t.lng) END",
        "search_text IS NULL OR (geocell IS NULL AND lat IS NOT NULL AND lng IS NOT NULL AND NOT coalesce(is_online, false))",
    ),
    (
        "clubs",
        "search_text = moa_club_search_text(t.id, t.name, t.tagline, t.location_name), geocell = moa_geocell(t.place_lat, t.place_lng)",
        "search_text IS NULL OR (geocell IS NULL AND place_lat IS NOT NULL AND place_lng IS NOT NULL)",
    ),
    ("users", "search_text = moa_user_search_text(t.full_name, t.username, t.email)", "search_text IS NULL"),
)


def _state(name: str):
    """None when there is no such index, else whether it is valid."""
    row = op.get_bind().execute(
        sa.text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :name AND c.relnamespace = current_schema()::regnamespace"
        ),
        {"name": name},
    ).first()
    return None if row is None else bool(row[0])


def _build(name: str, definition: str) -> None:
    """CREATE INDEX CONCURRENTLY, once: a valid one is left alone, an invalid
    one (a build that failed) is dropped and built again."""
    state = _state(name)
    if state:
        return
    if state is False:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {definition}")


def _sql(statement: str) -> str:
    """The SQL of a definition above as one statement (the dump keeps its indentation)."""
    return statement.strip()


def _backfill(bind) -> None:
    """Every row's new columns, a batch of rows per statement, each committed
    (with synchronous_commit off: a batch lost to a crash is simply done
    again, its rows still need it), each sized to take about
    BACKFILL_SECONDS; rows held by another writer skipped and tried again
    on a later pass."""
    for table, assign, needs in BACKFILL:
        batch = BACKFILL_BATCH
        left = 0
        for attempt in range(BACKFILL_PASSES):
            last = 0
            while True:
                ids = [r[0] for r in bind.execute(
                    sa.text(f"SELECT id FROM {table} WHERE id > :last AND ({needs}) ORDER BY id LIMIT :n"), {"last": last, "n": batch},
                )]
                if not ids:
                    break
                last = ids[-1]
                started = time.monotonic()
                bind.execute(sa.text("BEGIN"))
                bind.execute(sa.text(LOCK_TIMEOUT))
                bind.execute(sa.text("SET LOCAL synchronous_commit = off"))
                bind.execute(sa.text("SET LOCAL moa.quiet_outbox = 'on'"))
                bind.execute(
                    sa.text(
                        f"UPDATE {table} t SET {assign} WHERE t.id IN ("
                        f"SELECT id FROM {table} WHERE id = ANY(:ids) AND ({needs}) FOR UPDATE SKIP LOCKED)"
                    ),
                    {"ids": ids},
                )
                bind.execute(sa.text("COMMIT"))
                took = max(time.monotonic() - started, 0.001)
                batch = int(min(max(batch * BACKFILL_SECONDS / took, batch / 2, BACKFILL_MIN), batch * 2, BACKFILL_MAX))
            left = bind.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {needs}")).scalar()
            if not left:
                break
            time.sleep(1.0 * (attempt + 1))
        if left:
            print(f"w2_search: {left} {table} rows were held by other writers throughout; the six-hourly search check fills them")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(LOCK_TIMEOUT)
    for table, column, type_ in COLUMNS:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {type_}")
    op.execute(
        "CREATE TABLE IF NOT EXISTS search_outbox ("
        "id bigserial PRIMARY KEY, kind varchar(16) NOT NULL, ref_id integer NOT NULL, "
        "created_at timestamptz NOT NULL DEFAULT now())"
    )
    for sql in TEXT_FUNCTIONS + OUTBOX_FUNCTIONS + CELL_SQL[:3]:
        op.execute(_sql(sql))
    for name, table in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    for sql in TEXT_TRIGGERS + OUTBOX_TRIGGERS + CELL_SQL[3:]:
        op.execute(_sql(sql))

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        _backfill(bind)
        # The planner's statistics for the new columns, so the new indexes are
        # chosen from the first request (ANALYZE samples, and blocks no one).
        for table, columns in (("events", "search_text, geocell"), ("clubs", "search_text, geocell"), ("users", "search_text")):
            op.execute(f"ANALYZE {table} ({columns})")
        for name, definition in INDEXES:
            _build(name, definition)
        for name, _definition in DROPPED:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, definition in DROPPED:
            _build(name, definition)
        for name, _definition in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute(LOCK_TIMEOUT)
    for name, table in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP TABLE IF EXISTS search_outbox")
    for table, column, _type in reversed(COLUMNS):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
