"""Grid cells: "near you" as an index lookup of a handful of cells.

A latitude/longitude box on a B-tree over (lat, lng) can only range on the
latitude: for 50 km around Bengaluru it read every event in the strip from
12.5° to 13.4° north across the whole country (Chennai, Mangaluru, Vellore
and the rest), and a visitor somewhere with nothing near read every event
still to come before giving up (docs/SCALING.md, "Filters/geo: distance
O(lat strip)").

So every event held in person with a point, and every club with a place,
carries the cell its point falls in (events.geocell, clubs.geocell), kept by
triggers. The grid is 0.5° by 0.5° (about 55 km by 54 km in Bengaluru,
narrower in longitude further north): a cell's id is

    row * 720 + column,  row = floor((lat + 90) * 2), column = floor((lng + 180) * 2)

with the last row and column taking in 90° and 180°. (Times two rather than
over 0.5: the same IEEE arithmetic in Python and in SQL, to the bit.)

A circle of radius r is covered by the cells its bounding box touches
(geo.bbox, which already handles the antimeridian and the poles): a
superset of the circle, since every point within r is inside the box and so
inside one of the box's cells. "Near you" then reads those cells -- a B-tree
IN over a handful (2x2 to 4x4 for 50 km) -- and the exact haversine decides
(geo.km_sql, unchanged). Where the cover would be large (a radius of
hundreds of kilometres, or a pole's ring of 720 cells), it is a few ranges
of ids instead (a row's cells are consecutive ids), and past that the box.

No PostGIS: production's Postgres image does not carry it.
"""
from __future__ import annotations

import math
from typing import Optional

from sqlalchemy import DDL, Integer, and_, bindparam, event as sa_event, or_, text

from app.api.discover import geo
from app.db.base import AbstractSQLModel

#: Cells per degree, each way.
PER_DEG = 2
ROWS = 180 * PER_DEG
COLS = 360 * PER_DEG
#: Up to this many cells, the cover is an IN list (each an index probe, the
#: event index's date bound inside it); more, ranges of ids.
MAX_IN = 64
#: Up to this many ranges; more, and the cover is no better than the box.
MAX_RANGES = 48


def row_of(lat: float) -> int:
    return min(max(math.floor((lat + 90.0) * PER_DEG), 0), ROWS - 1)


def col_of(lng: float) -> int:
    return min(max(math.floor((lng + 180.0) * PER_DEG), 0), COLS - 1)


def cell(lat: Optional[float], lng: Optional[float]) -> Optional[int]:
    """The cell a point falls in (what the triggers store); None without a point."""
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return row_of(lat) * COLS + col_of(lng)


Cover = list[tuple[int, int]]


def cover(lat: float, lng: float, radius_km: float) -> Optional[Cover]:
    """The cells within reach of `radius_km` from (lat, lng), as inclusive
    ranges of ids, sorted and merged; None when the circle takes in the
    whole sphere (nothing to rule out)."""
    box = geo.bbox(lat, lng, radius_km)
    if box is None:
        return None
    south, north, lngs = box
    spans: list[tuple[int, int]] = []
    for r in range(row_of(max(south, -90.0)), row_of(min(north, 90.0)) + 1):
        for west, east in lngs:
            spans.append((r * COLS + col_of(max(west, -180.0)), r * COLS + col_of(min(east, 180.0))))
            # 180° and -180° are one meridian, kept in the last column and
            # the first: a range that reaches either takes the other too.
            if east >= 180.0:
                spans.append((r * COLS, r * COLS))
            if west <= -180.0:
                spans.append((r * COLS + COLS - 1, r * COLS + COLS - 1))
    spans.sort()
    merged: Cover = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def size(c: Cover) -> int:
    return sum(hi - lo + 1 for lo, hi in c)


def ids(c: Cover) -> list[int]:
    return [i for lo, hi in c for i in range(lo, hi + 1)]


def listed(c: Optional[Cover]) -> Optional[list[int]]:
    """The cover's cells one by one, when it is small enough to read that way."""
    if c is None or size(c) > MAX_IN:
        return None
    return ids(c)


def cover_sql(column, c: Optional[Cover]):
    """SQL for "the row's cell is in the cover": an IN list or ranges, with
    `column IS NOT NULL` spelled out for the partial indexes' predicates.
    None when the cover is too wide to help (or there is none): the caller
    keeps the box."""
    if c is None:
        return None
    if size(c) <= MAX_IN:
        return and_(column.isnot(None), column.in_(bindparam(None, ids(c), expanding=True, literal_execute=True, type_=Integer)))
    if len(c) <= MAX_RANGES:
        return and_(column.isnot(None), or_(*[column.between(lo, hi) for lo, hi in c]))
    return None


def near_sql(here: tuple[float, float], radius_km: float, cell_col, lat_col, lng_col):
    """A placed row within `radius_km` of `here`: its cell in the cover (the
    index finds the candidates), then the exact haversine. Where the cover
    is too wide to help, the box and the haversine, as before."""
    km = geo.km_sql(here[0], here[1], lat_col, lng_col) <= radius_km
    by_cell = cover_sql(cell_col, cover(here[0], here[1], radius_km))
    if by_cell is not None:
        return and_(by_cell, km)
    return and_(geo.in_box_sql(geo.bbox(here[0], here[1], radius_km), lat_col, lng_col), km)


# ─── Kept by the database ─────────────────────────────────────────────────

GEOCELL_SQL = (
    f"""
    CREATE OR REPLACE FUNCTION moa_geocell(lat double precision, lng double precision) RETURNS integer
    LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
      SELECT CASE WHEN lat IS NULL OR lng IS NULL OR lat < -90 OR lat > 90 OR lng < -180 OR lng > 180 THEN NULL
        ELSE least(greatest(floor((lat + 90.0) * {PER_DEG})::integer, 0), {ROWS - 1}) * {COLS}
           + least(greatest(floor((lng + 180.0) * {PER_DEG})::integer, 0), {COLS - 1}) END
    $$
    """,
    # An event's cell: held in person with a point; online events are anywhere.
    """
    CREATE OR REPLACE FUNCTION moa_events_geocell() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.geocell := CASE WHEN coalesce(NEW.is_online, false) THEN NULL ELSE moa_geocell(NEW.lat, NEW.lng) END;
      RETURN NEW;
    END $$
    """,
    # A club's cell is its place's (places.py). Named to fire after
    # trg_clubs_place, which sets the place: BEFORE triggers run in name order.
    """
    CREATE OR REPLACE FUNCTION moa_clubs_geocell() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.geocell := moa_geocell(NEW.place_lat, NEW.place_lng);
      RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER trg_events_geocell BEFORE INSERT OR UPDATE OF lat, lng, is_online ON events
    FOR EACH ROW EXECUTE FUNCTION moa_events_geocell()
    """,
    """
    CREATE TRIGGER trg_clubs_place_zcell BEFORE INSERT OR UPDATE OF lat, lng, place_lat, place_lng ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_clubs_geocell()
    """,
)


#: What each kept cell should be, per table (the triggers' rule).
TRUE_CELL = {
    "events": "CASE WHEN coalesce(is_online, false) THEN NULL ELSE moa_geocell(lat, lng) END",
    "clubs": "moa_geocell(place_lat, place_lng)",
}


async def drift(session) -> dict[str, list[int]]:
    """Per table, the ids whose stored cell is not their point's (normally
    none: a write with triggers off, such as the benchmark's loader)."""
    out = {}
    for table, true_cell in TRUE_CELL.items():
        out[table] = list((await session.execute(
            text(f"SELECT id FROM {table} WHERE geocell IS DISTINCT FROM ({true_cell}) ORDER BY id")
        )).scalars().all())
    return out


async def recompute(session, *, batch: int = 2000) -> dict[str, int]:
    """Store every cell that `drift` finds wrong; how many per table. In
    batches that commit, rows someone else holds skipped (the next check
    finds them), and no outbox rows (their documents are unchanged)."""
    found = await drift(session)
    await session.commit()
    fixed = {}
    for table, ids in found.items():
        n = 0
        for i in range(0, len(ids), batch):
            await session.execute(text("SET LOCAL moa.quiet_outbox = 'on'"))
            result = await session.execute(text(
                f"UPDATE {table} SET geocell = {TRUE_CELL[table]} "
                f"WHERE id IN (SELECT id FROM {table} WHERE id = ANY(:ids) FOR UPDATE SKIP LOCKED)"
            ), {"ids": ids[i:i + batch]})
            await session.commit()
            n += int(result.rowcount or 0)
        fixed[table] = n
    return fixed


def _install(target, connection, **kw) -> None:
    if connection.dialect.name != "postgresql":
        return
    for sql in GEOCELL_SQL:
        connection.execute(DDL(sql))


# With create_all (the suite), after every table; the migration w2_search in production.
sa_event.listen(AbstractSQLModel.metadata, "after_create", _install)
