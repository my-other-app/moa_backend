"""Where each club is, for "clubs near": kept on the club, not worked out on
every request.

A club's place is its own point (lat, lng) when it has one, else the middle
(the mean latitude and longitude) of its in-person events' points: events
not deleted, not drafts, not online, with a point, past ones included. That
was computed per request by a lateral average over every club's events, which
took 63 s at a lakh of clubs (docs/SCALING.md, fix 4). Now it is stored in
clubs.place_lat / place_lng and kept by triggers, in the writer's own
transaction, as the going and follower counters are:

- on clubs, BEFORE INSERT OR UPDATE OF lat, lng: the club's own point, or its
  events' middle when it has none;
- on events, AFTER INSERT, DELETE, or UPDATE of a column the middle depends
  on (lat, lng, is_online, is_deleted, status into or out of draft,
  club_id), only when the event has a point before or after: the club's
  middle again, from its events through ix_events_club_start, and written
  only when the club has no point of its own and the middle moved.

A trigger rather than application code because points are written by the
dashboard, the map-link resolver, the geocoder's backfill, imports, the
trash's restore and raw SQL; a trigger sees each of them.

Anything written with triggers off (a data-only restore, the benchmark's
loader) is put right by `recompute` (POST /admin/discover/recompute-club-places,
or scripts/recompute_club_places.py), which only writes the clubs whose
stored place disagrees with their rows.

The searches then use the place like the events' distance search uses an
event's point: a latitude/longitude box on ix_clubs_place, then the exact
haversine (geo.km_sql).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import DDL, and_, event as sa_event, or_, select, text

from app.api.clubs.models import Clubs
from app.api.discover import geo
from app.api.events.models import Events

# ─── The triggers ─────────────────────────────────────────────────────────
#
# Installed with the events table (create_all, in the tests) and by the
# migration discover_scale; both tables exist by then. PL/pgSQL, so the
# bodies are resolved when they run, not when they are created.

PLACE_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_club_events_place(cid integer, OUT lat double precision, OUT lng double precision)
    LANGUAGE plpgsql STABLE AS $$
    BEGIN
      SELECT avg(e.lat), avg(e.lng) INTO lat, lng FROM events e
      WHERE e.club_id = cid AND e.lat IS NOT NULL AND e.lng IS NOT NULL AND NOT e.is_deleted
        AND e.status <> 'draft' AND NOT coalesce(e.is_online, false);
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_refresh_club_place(cid integer) RETURNS void LANGUAGE plpgsql AS $$
    BEGIN
      IF cid IS NULL THEN
        RETURN;
      END IF;
      UPDATE clubs c SET place_lat = p.lat, place_lng = p.lng
      FROM moa_club_events_place(cid) p
      WHERE c.id = cid AND (c.lat IS NULL OR c.lng IS NULL)
        AND (c.place_lat IS DISTINCT FROM p.lat OR c.place_lng IS DISTINCT FROM p.lng);
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_club_place() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.lat IS NOT NULL AND NEW.lng IS NOT NULL THEN
        NEW.place_lat := NEW.lat;
        NEW.place_lng := NEW.lng;
      ELSE
        SELECT p.lat, p.lng INTO NEW.place_lat, NEW.place_lng FROM moa_club_events_place(NEW.id) p;
      END IF;
      RETURN NEW;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_club_place_from_events() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM moa_refresh_club_place(OLD.club_id);
      END IF;
      IF TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND NEW.club_id IS DISTINCT FROM OLD.club_id) THEN
        PERFORM moa_refresh_club_place(NEW.club_id);
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_clubs_place BEFORE INSERT OR UPDATE OF lat, lng ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_club_place()
    """,
    """
    CREATE TRIGGER trg_events_club_place_insert AFTER INSERT ON events
    FOR EACH ROW WHEN (NEW.lat IS NOT NULL AND NEW.lng IS NOT NULL)
    EXECUTE FUNCTION moa_club_place_from_events()
    """,
    """
    CREATE TRIGGER trg_events_club_place_delete AFTER DELETE ON events
    FOR EACH ROW WHEN (OLD.lat IS NOT NULL AND OLD.lng IS NOT NULL)
    EXECUTE FUNCTION moa_club_place_from_events()
    """,
    """
    CREATE TRIGGER trg_events_club_place_update AFTER UPDATE OF lat, lng, is_online, is_deleted, status, club_id ON events
    FOR EACH ROW WHEN (
      ((OLD.lat IS NOT NULL AND OLD.lng IS NOT NULL) OR (NEW.lat IS NOT NULL AND NEW.lng IS NOT NULL))
      AND (OLD.lat IS DISTINCT FROM NEW.lat OR OLD.lng IS DISTINCT FROM NEW.lng
           OR coalesce(OLD.is_online, false) IS DISTINCT FROM coalesce(NEW.is_online, false)
           OR OLD.is_deleted IS DISTINCT FROM NEW.is_deleted
           OR (OLD.status = 'draft') IS DISTINCT FROM (NEW.status = 'draft')
           OR OLD.club_id IS DISTINCT FROM NEW.club_id)
    )
    EXECUTE FUNCTION moa_club_place_from_events()
    """,
)

DROP_SQL = (
    "DROP TRIGGER IF EXISTS trg_events_club_place_update ON events",
    "DROP TRIGGER IF EXISTS trg_events_club_place_delete ON events",
    "DROP TRIGGER IF EXISTS trg_events_club_place_insert ON events",
    "DROP TRIGGER IF EXISTS trg_clubs_place ON clubs",
    "DROP FUNCTION IF EXISTS moa_club_place_from_events()",
    "DROP FUNCTION IF EXISTS moa_club_place()",
    "DROP FUNCTION IF EXISTS moa_refresh_club_place(integer)",
    "DROP FUNCTION IF EXISTS moa_club_events_place(integer)",
)

for _sql in PLACE_SQL:
    sa_event.listen(Events.__table__, "after_create", DDL(_sql).execute_if(dialect="postgresql"))


# ─── Putting it right ─────────────────────────────────────────────────────

# Every club's place from its rows, the same rule as the triggers, in one pass.
_TRUE_PLACES = """
SELECT k.id,
       CASE WHEN k.lat IS NOT NULL AND k.lng IS NOT NULL THEN k.lat ELSE ev.lat END AS lat,
       CASE WHEN k.lat IS NOT NULL AND k.lng IS NOT NULL THEN k.lng ELSE ev.lng END AS lng
FROM clubs k
LEFT JOIN (
  SELECT club_id, avg(lat) AS lat, avg(lng) AS lng FROM events
  WHERE lat IS NOT NULL AND lng IS NOT NULL AND NOT is_deleted AND status <> 'draft' AND NOT coalesce(is_online, false)
  GROUP BY club_id
) ev ON ev.club_id = k.id
"""

# A stored place differs from the true one: one is missing, or they are more
# than about a millimetre apart. (A mean summed in another order can differ in
# its last bits; that is not a fault.)
_DIFFERS = """
((c.place_lat IS NULL) <> (p.lat IS NULL) OR (c.place_lng IS NULL) <> (p.lng IS NULL)
 OR abs(c.place_lat - p.lat) > 1e-8 OR abs(c.place_lng - p.lng) > 1e-8)
"""

RECOMPUTE_SQL = f"""
UPDATE clubs c SET place_lat = p.lat, place_lng = p.lng
FROM ({_TRUE_PLACES}) p
WHERE p.id = c.id AND {_DIFFERS}
"""

_DRIFT_SQL = f"SELECT c.id FROM clubs c JOIN ({_TRUE_PLACES}) p ON p.id = c.id WHERE {_DIFFERS} ORDER BY c.id"


async def drift(session) -> list[int]:
    """The clubs whose stored place disagrees with their rows (normally none)."""
    return list((await session.execute(text(_DRIFT_SQL))).scalars().all())


async def recompute(session) -> int:
    """Store every club's place again where it disagrees with its rows; returns how many were put right."""
    result = await session.execute(text(RECOMPUTE_SQL))
    await session.commit()
    return int(result.rowcount or 0)


# ─── Finding clubs by place ───────────────────────────────────────────────


def _listed() -> list:
    return [Clubs.is_verified == True, Clubs.is_deleted == False]  # noqa: E712


def placed():
    """The club has a place."""
    return and_(Clubs.place_lat.isnot(None), Clubs.place_lng.isnot(None))


def km_from(here: tuple[float, float]):
    """The haversine from `here` to the club's place, as SQL (null without one)."""
    return geo.km_sql(here[0], here[1], Clubs.place_lat, Clubs.place_lng)


def within(here: tuple[float, float], radius_km: float):
    """A club with no place cannot be ruled out; one with a place must be
    inside the radius. The box goes first so the index finds the candidates
    and the exact distance is only worked out for them."""
    box = geo.bbox(here[0], here[1], radius_km)
    return or_(
        Clubs.place_lat.is_(None), Clubs.place_lng.is_(None),
        and_(geo.in_box_sql(box, Clubs.place_lat, Clubs.place_lng), km_from(here) <= radius_km),
    )


#: The circles "the k nearest" widens through, in km; after the last, everywhere.
RINGS_KM = (100.0, 1000.0)


async def nearest(session, centre: tuple[float, float], k: int) -> tuple[Optional[float], list[tuple[int, float, float, int]]]:
    """(how far the k-th nearest listed club with a place is from `centre`
    in km, []); or (None, every listed club with a place) when fewer than k
    have one at all.

    Tries circles of RINGS_KM, then everywhere: when a circle holds k clubs,
    the k nearest anywhere are those (anything nearer is inside it too)."""
    d = km_from(centre)
    got: list = []
    for r in RINGS_KM + (None,):
        conds = [*_listed(), placed()]
        if r is not None:
            conds += [geo.in_box_sql(geo.bbox(centre[0], centre[1], r), Clubs.place_lat, Clubs.place_lng), d <= r]
        got = (await session.execute(
            select(Clubs.id, Clubs.place_lat, Clubs.place_lng, Clubs.follower_count, d).where(*conds).order_by(d).limit(k)
        )).all()
        if len(got) >= k:
            return float(got[-1][4]), []
    return None, [(i, float(la), float(ln), int(n or 0)) for i, la, ln, n, _ in got]


async def around(session, centre: tuple[float, float], radius_km: Optional[float]) -> list[tuple[int, float, float, int]]:
    """(id, lat, lng, followers) of every listed club placed within
    `radius_km` of `centre`; every placed one when it is None."""
    conds = [*_listed(), placed()]
    if radius_km is not None:
        conds += [geo.in_box_sql(geo.bbox(centre[0], centre[1], radius_km), Clubs.place_lat, Clubs.place_lng), km_from(centre) <= radius_km]
    rows = await session.execute(select(Clubs.id, Clubs.place_lat, Clubs.place_lng, Clubs.follower_count).where(*conds))
    return [(i, float(la), float(ln), int(n or 0)) for i, la, ln, n in rows.all()]


async def unplaced(session, k: int) -> list[tuple[int, None, None, int]]:
    """The k most followed listed clubs with no place (then by id)."""
    rows = await session.execute(
        select(Clubs.id, Clubs.follower_count)
        .where(*_listed(), or_(Clubs.place_lat.is_(None), Clubs.place_lng.is_(None)))
        .order_by(Clubs.follower_count.desc(), Clubs.id).limit(k)
    )
    return [(i, None, None, int(n or 0)) for i, n in rows.all()]
