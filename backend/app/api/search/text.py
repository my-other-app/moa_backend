"""search_text: everything a row is found by, in one normalised column.

A search used to test each word against four columns of three tables (the
event's name and place, its club's name, its category's name: an OR of
ILIKEs and two semi-joins per word), and could not find an event by its
topics or interests at all. Now each searchable row carries one column,
lowercased and unaccented, that holds all of it:

- events: name, place, tag; the club's name and place (its city); the
  category's name; the names of its interests and of their topics;
- clubs: name, tagline, place; the names of its interests and their topics;
- users (the admin's search): full name, username and address, with a
  separator between them that no search term spans.

A word is then one LIKE on one column, served by one trigram index
(ix_events_search_trgm, ix_events_shown_search_trgm, ix_clubs_search_trgm,
ix_users_search_trgm), and a typo one `%>` on the same index.

Kept by the database, in the writer's transaction, as the counters and the
club places are: a trigger sees the dashboard, the app, imports, the trash's
restore and raw SQL alike.

- BEFORE INSERT or an UPDATE of a column the text is built from, on events,
  clubs and users: the row's own text, worked out again.
- AFTER a change to an event's or a club's interest links: that one row's.
- AFTER a club's name or place changes: the text of each of its events (a
  club's events are a bounded set).
- A category, interest or topic renamed or deleted can touch a lakh of
  events, too many for one transaction to rewrite under its row locks: the
  change goes into the search outbox (outbox.py) and the drain rewrites the
  rows it touches in batches that commit (sync.rebuild_taxonomy), seconds
  later.

`drift` lists rows whose text is not what their rows make it (normally none:
a write with triggers off, or a taxonomy change not drained yet), and
`recompute` puts them right; the six-hourly check runs both (sync.py).

Normalised the same way on both sides: `moa_search_norm` (lower, unaccent,
spaces collapsed). A query of plain ASCII is normalised in Python, which is
the same thing for ASCII; anything else asks the database (`normalise`), so
the two can never disagree.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import DDL, event as sa_event, text

from app.db.base import AbstractSQLModel

#: Between the fields of an account's text: no search term spans it (a term
#: is one substring of one field, as it was when each field was searched alone).
USER_SEPARATOR = " ‖ "

FUNCTIONS_SQL = (
    # unaccent's one-argument form, STABLE: it reads its dictionary. Only
    # triggers and statements call it, never an index expression.
    r"""
    CREATE OR REPLACE FUNCTION moa_search_norm(t text) RETURNS text
    LANGUAGE sql STABLE PARALLEL SAFE AS $$
      SELECT nullif(btrim(regexp_replace(lower(unaccent(coalesce(t, ''))), '[ \t\n\r\f\v]+', ' ', 'g')), '')
    $$
    """,
    """
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
    """
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
    f"""
    CREATE OR REPLACE FUNCTION moa_user_search_text(fname text, uname text, mail text)
    RETURNS text LANGUAGE sql STABLE PARALLEL SAFE AS $$
      SELECT concat_ws('{USER_SEPARATOR}', moa_search_norm(fname), moa_search_norm(uname), moa_search_norm(mail))
    $$
    """,
    # One event's or club's text again, written only when it changed.
    """
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
    """
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
    # The trigger functions.
    """
    CREATE OR REPLACE FUNCTION moa_events_search_text() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.search_text := moa_event_search_text(NEW.id, NEW.name, NEW.location_name, NEW.event_tag, NEW.club_id, NEW.category_id);
      RETURN NEW;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_clubs_search_text() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.search_text := moa_club_search_text(NEW.id, NEW.name, NEW.tagline, NEW.location_name);
      RETURN NEW;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_users_search_text() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.search_text := moa_user_search_text(NEW.full_name, NEW.username, NEW.email);
      RETURN NEW;
    END $$
    """,
    # An event's interest links changed: its text, and its search document
    # (its interests and topics are filters there even when no name changed).
    """
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
    """
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
    # A club's name or place is part of each of its events' text.
    """
    CREATE OR REPLACE FUNCTION moa_club_events_search() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      UPDATE events e SET search_text = moa_event_search_text(e.id, e.name, e.location_name, e.event_tag, e.club_id, e.category_id)
      WHERE e.club_id = NEW.id
        AND e.search_text IS DISTINCT FROM moa_event_search_text(e.id, e.name, e.location_name, e.event_tag, e.club_id, e.category_id);
      RETURN NULL;
    END $$
    """,
)

TRIGGERS_SQL = (
    """
    CREATE TRIGGER trg_events_search_text BEFORE INSERT OR UPDATE OF name, location_name, event_tag, club_id, category_id ON events
    FOR EACH ROW EXECUTE FUNCTION moa_events_search_text()
    """,
    """
    CREATE TRIGGER trg_clubs_search_text BEFORE INSERT OR UPDATE OF name, tagline, location_name ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_clubs_search_text()
    """,
    """
    CREATE TRIGGER trg_users_search_text BEFORE INSERT OR UPDATE OF full_name, username, email ON users
    FOR EACH ROW EXECUTE FUNCTION moa_users_search_text()
    """,
    """
    CREATE TRIGGER trg_event_interests_search AFTER INSERT OR DELETE OR UPDATE OF event_id, interest_id, is_deleted ON event_interests_link
    FOR EACH ROW EXECUTE FUNCTION moa_event_links_search()
    """,
    """
    CREATE TRIGGER trg_club_interests_search AFTER INSERT OR DELETE OR UPDATE OF club_id, interest_id, is_deleted ON clubs_interests_link
    FOR EACH ROW EXECUTE FUNCTION moa_club_links_search()
    """,
    """
    CREATE TRIGGER trg_clubs_search_events AFTER UPDATE OF name, location_name ON clubs
    FOR EACH ROW WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.location_name IS DISTINCT FROM NEW.location_name)
    EXECUTE FUNCTION moa_club_events_search()
    """,
)


def _install(target, connection, **kw) -> None:
    if connection.dialect.name != "postgresql":
        return
    for sql in FUNCTIONS_SQL + TRIGGERS_SQL:
        connection.execute(DDL(sql))


# The functions and triggers go in with create_all (the test suite), after
# every table, since they read several. The migration w2_search installs the
# same in production.
sa_event.listen(AbstractSQLModel.metadata, "after_create", _install)


# ─── The query's side ─────────────────────────────────────────────────────

_SPACES = re.compile(r"[ \t\n\r\f\v]+")


def norm_ascii(q: str) -> Optional[str]:
    """`moa_search_norm(q)` worked out here, for a query of plain ASCII (where
    unaccent changes nothing and lower() is the same everywhere); None for
    anything else, which the database must normalise."""
    if not q.isascii():
        return None
    return _SPACES.sub(" ", q.lower()).strip()


async def normalise(session, q: Optional[str]) -> str:
    """A query as search_text is written: lowercased, unaccented, spaces
    collapsed. ASCII here; anything else by the database's own function, so
    "Café" finds "cafe" and the two sides agree to the letter."""
    if not q:
        return ""
    done = norm_ascii(q)
    if done is not None:
        return done
    return (await session.scalar(text("SELECT moa_search_norm(:q)"), {"q": q})) or ""


# ─── Drift and repair ─────────────────────────────────────────────────────

_EVENT_TRUE = "moa_event_search_text(e.id, e.name, e.location_name, e.event_tag, e.club_id, e.category_id)"
_CLUB_TRUE = "moa_club_search_text(c.id, c.name, c.tagline, c.location_name)"
_USER_TRUE = "moa_user_search_text(u.full_name, u.username, u.email)"

#: (table, alias, what the text should be) for each kept column.
KEPT = (("events", "e", _EVENT_TRUE), ("clubs", "c", _CLUB_TRUE), ("users", "u", _USER_TRUE))

#: Rows compared per statement, so no check holds one long scan.
SLICE = 5000


async def drift(session, *, slice_: int = SLICE, pause: float = 0.0) -> dict[str, list[int]]:
    """Per table, the ids whose search_text is not what their rows make it.
    `pause` seconds between slices, so a check never holds the database busy."""
    import asyncio

    out: dict[str, list[int]] = {}
    for table, a, true_text in KEPT:
        found: list[int] = []
        last = 0
        while True:
            ids = list((await session.execute(
                text(f"SELECT id FROM {table} WHERE id > :last ORDER BY id LIMIT :n"), {"last": last, "n": slice_},
            )).scalars().all())
            if not ids:
                break
            found += list((await session.execute(
                text(f"SELECT {a}.id FROM {table} {a} WHERE {a}.id = ANY(:ids) AND {a}.search_text IS DISTINCT FROM {true_text} ORDER BY {a}.id"),
                {"ids": ids},
            )).scalars().all())
            last = ids[-1]
            await session.commit()
            if pause:
                await asyncio.sleep(pause)
        out[table] = found
    return out


async def recompute(session, found: Optional[dict[str, list[int]]] = None, *, pause: float = 0.0) -> dict[str, int]:
    """Write the text again where `drift` (or `found`) says it is wrong; how many per table.
    Each table's rows in their own statement, committed."""
    found = found if found is not None else await drift(session, pause=pause)
    fixed: dict[str, int] = {}
    for table, a, true_text in KEPT:
        ids = found.get(table) or []
        for i in range(0, len(ids), SLICE):
            await session.execute(
                text(f"UPDATE {table} {a} SET search_text = {true_text} WHERE {a}.id = ANY(:ids)"), {"ids": ids[i:i + SLICE]},
            )
            await session.commit()
        fixed[table] = len(ids)
    return fixed


# ─── Filling it in, without holding anyone up ─────────────────────────────

#: Rows written per statement by a backfill.
BACKFILL_BATCH = 2000


async def backfill(session, table: str, *, batch: int = BACKFILL_BATCH, passes: int = 5) -> dict:
    """Fill search_text wherever it is still empty, `batch` rows a statement,
    each committed: never one long transaction, and never a wait on a row
    someone else holds -- rows locked by another writer are skipped (SKIP
    LOCKED) and tried again on the next pass. Returns {"written", "left"}:
    what is left is rows locked through every pass (run it again).

    The triggers keep every row written meanwhile right, so a backfill only
    ever has old rows to do."""
    a, true_text = next((a, t) for name, a, t in KEPT if name == table)
    written = 0
    for _ in range(passes):
        skipped = False
        last = 0
        while True:
            ids = list((await session.execute(
                text(f"SELECT id FROM {table} WHERE id > :last AND search_text IS NULL ORDER BY id LIMIT :n"),
                {"last": last, "n": batch},
            )).scalars().all())
            await session.commit()
            if not ids:
                break
            last = ids[-1]
            await session.execute(text("SET LOCAL moa.quiet_outbox = 'on'"))
            result = await session.execute(text(
                f"UPDATE {table} {a} SET search_text = {true_text} "
                f"WHERE {a}.id IN (SELECT id FROM {table} WHERE id = ANY(:ids) AND search_text IS NULL FOR UPDATE SKIP LOCKED)"
            ), {"ids": ids})
            await session.commit()
            n = int(result.rowcount or 0)
            written += n
            skipped = skipped or n < len(ids)
        if not skipped:
            break
    left = int(await session.scalar(text(f"SELECT count(*) FROM {table} WHERE search_text IS NULL")) or 0)
    await session.commit()
    return dict(written=written, left=left)
