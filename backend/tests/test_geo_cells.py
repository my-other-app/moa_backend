"""Grid cells for "near you": the cell a point is in, the cells that cover a
circle, and the lists that read them.

- Python's cell and the database's (moa_geocell) agree, to the bit, at
  cell edges, at the poles and on the antimeridian.
- A cover holds every point within the radius: points on the circle itself
  (a hair inside, checked by the exact haversine), around the poles, across
  the antimeridian, at radii from a hundred metres to most of the Earth.
- The cell columns follow their rows: an event's point and whether it is
  online, a club's own point and the middle of its events (its place).
- The lists that read the cells answer exactly what the haversine says:
  events within a radius, sort=distance's placed part, the front page's
  "near you" (walked cell by cell), clubs near a point -- around the poles,
  across the antimeridian, and at the radius's edge, metres either side.
"""
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.api.closeout.models import EventStatus
from app.api.clubs.models import Clubs
from app.api.discover import cells, geo, places, service, upcoming
from app.api.events.models import EventCategories, Events
from app.db.listeners import add_loader_criteria


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


def _dest(lat: float, lng: float, km: float, bearing: float) -> tuple[float, float]:
    """The point `km` from (lat, lng) along `bearing` (degrees) on the sphere geo.km measures."""
    d = km / geo.EARTH_KM
    p1, l1, b = math.radians(lat), math.radians(lng), math.radians(bearing)
    p2 = math.asin(max(-1.0, min(1.0, math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    lng2 = (math.degrees(l2) + 540.0) % 360.0 - 180.0
    return math.degrees(p2), lng2


CENTRES = [
    (12.9716, 77.5946),     # Bengaluru
    (0.0, 0.0),
    (-18.1416, 178.4419),   # Suva, by the antimeridian
    (-16.85, -179.95),      # just east of it
    (0.2, 179.99),
    (78.2232, 15.6267),     # Longyearbyen
    (89.9, 10.0),           # by the north pole
    (-89.95, -120.0),       # by the south pole
    (90.0, 0.0),
    (60.0, -179.7),
]
RADII = [0.1, 1.0, 10.0, 50.0, 70.3, 300.0, 2000.0, 12000.0, 19500.0]


def test_the_cell_is_the_same_here_and_in_the_database_sql_text():
    # (the SQL is checked against these in test_cells_agree_with_moa_geocell)
    assert cells.cell(-90.0, -180.0) == 0
    assert cells.cell(90.0, 180.0) == cells.ROWS * cells.COLS - 1
    assert cells.cell(12.5, 77.5) == cells.row_of(12.5) * cells.COLS + cells.col_of(77.5)
    assert cells.cell(None, 77.5) is None and cells.cell(91.0, 0.0) is None


async def test_cells_agree_with_moa_geocell(session):
    rng = random.Random(3)
    points = [(-90.0, -180.0), (90.0, 180.0), (0.0, 0.0), (12.5, 77.5), (12.4999999, 77.4999999), (-0.0000001, -0.0000001),
              (45.0, 180.0), (45.0, -180.0), (89.9999999, 179.9999999)]
    points += [(rng.uniform(-90, 90), rng.uniform(-180, 180)) for _ in range(300)]
    points += [(round(rng.uniform(-90, 90) * 2) / 2, round(rng.uniform(-180, 180) * 2) / 2) for _ in range(100)]  # on cell edges
    got = (await session.execute(
        text("SELECT moa_geocell(p.lat, p.lng) FROM unnest(CAST(:lats AS float8[]), CAST(:lngs AS float8[])) WITH ORDINALITY AS p(lat, lng, n) ORDER BY n"),
        {"lats": [p[0] for p in points], "lngs": [p[1] for p in points]},
    )).scalars().all()
    assert list(got) == [cells.cell(*p) for p in points]


@pytest.mark.parametrize("centre", CENTRES)
@pytest.mark.parametrize("radius", RADII)
def test_a_cover_holds_every_point_within_the_radius(centre, radius):
    cover = cells.cover(*centre, radius)
    if cover is None:
        assert radius / geo.EARTH_KM >= math.pi
        return
    inside = set(cells.ids(cover))
    rng = random.Random(hash((centre, radius)) & 0xFFFF)
    for bearing in [b * 7.5 for b in range(48)] + [rng.uniform(0, 360) for _ in range(150)]:
        for frac in (1 - 1e-9, 0.999, 0.75, 0.5, 0.1, 0.0):
            p = _dest(*centre, radius * frac, bearing)
            assert geo.km(*centre, *p) <= radius * (1 + 1e-12) + 1e-9  # the exact check: the point is within
            assert cells.cell(*p) in inside, (centre, radius, frac, bearing, p)
    # Both names of the antimeridian, when the circle reaches it.
    for lng in (180.0, -180.0):
        lat = max(-90.0, min(90.0, centre[0]))
        if geo.km(*centre, lat, lng) <= radius:
            assert cells.cell(lat, lng) in inside


def test_a_cover_is_a_handful_of_cells_near_you_and_ranges_further_out():
    near = cells.cover(12.9716, 77.5946, 50.0)
    assert 4 <= cells.size(near) <= 16 and cells.listed(near) is not None
    sql = str(cells.cover_sql(Events.geocell, near).compile(compile_kwargs={"literal_binds": True}))
    assert "geocell IN (" in sql and "IS NOT NULL" in sql
    wide = cells.cover(12.9716, 77.5946, 600.0)
    assert cells.size(wide) > cells.MAX_IN and len(wide) <= cells.MAX_RANGES
    assert "BETWEEN" in str(cells.cover_sql(Events.geocell, wide).compile(compile_kwargs={"literal_binds": True}))
    # A ring by the pole is whole rows of 720 cells: a range or two, not hundreds of ids.
    polar = cells.cover(89.9, 10.0, 50.0)
    assert len(polar) <= 2 and cells.listed(polar) is None
    assert cells.cover(0.0, 0.0, 20016.0) is None  # the whole sphere: nothing to rule out


# ─── The columns follow their rows ────────────────────────────────────────


async def _cell(session, table: str, row_id: int):
    return await session.scalar(text(f"SELECT geocell FROM {table} WHERE id = :id"), {"id": row_id})


async def test_the_cell_columns_follow_their_rows(session, make_club):
    _, club = await make_club(name=f"Cell Club {uuid.uuid4().hex[:6]}", email=f"cells-{uuid.uuid4().hex[:6]}@example.com")
    cat = EventCategories(name="Cells")
    session.add(cat)
    await session.flush()
    e = Events(slug=f"c-{uuid.uuid4().hex[:8]}", name="Cell Event", images=[], event_datetime=datetime.now(timezone.utc) + timedelta(days=2),
               duration=1.0, club_id=club.id, category_id=cat.id, lat=12.97, lng=77.59, lifecycle=EventStatus.published)
    session.add(e)
    await session.commit()
    assert await _cell(session, "events", e.id) == cells.cell(12.97, 77.59)
    # The club has no point of its own: its place is its events' middle, and its cell that place's.
    assert await _cell(session, "clubs", club.id) == cells.cell(12.97, 77.59)
    e.lat, e.lng = -18.14, 178.44
    await session.commit()
    assert await _cell(session, "events", e.id) == cells.cell(-18.14, 178.44) == await _cell(session, "clubs", club.id)
    e.is_online = True
    await session.commit()
    assert await _cell(session, "events", e.id) is None  # online: anywhere
    await session.execute(text("UPDATE events SET is_online = false, lat = 9.93, lng = 76.26 WHERE id = :id"), {"id": e.id})
    await session.commit()
    assert await _cell(session, "events", e.id) == cells.cell(9.93, 76.26)
    club.lat, club.lng = 28.61, 77.21  # a point of its own wins
    await session.commit()
    assert await _cell(session, "clubs", club.id) == cells.cell(28.61, 77.21)
    await session.execute(text("UPDATE clubs SET lat = NULL, lng = NULL WHERE id = :id"), {"id": club.id})
    await session.commit()
    assert await _cell(session, "clubs", club.id) == cells.cell(9.93, 76.26)
    assert await cells.drift(session) == {"events": [], "clubs": []}
    await session.execute(text("UPDATE events SET geocell = 7 WHERE id = :id"), {"id": e.id})
    assert (await cells.drift(session))["events"] == [e.id]
    assert (await cells.recompute(session))["events"] == 1
    assert await cells.drift(session) == {"events": [], "clubs": []}


# ─── The lists that read them ─────────────────────────────────────────────

EDGE_CENTRES = [(12.9716, 77.5946), (-18.1416, 178.4419), (-16.85, -179.95), (78.2232, 15.6267), (89.9, 10.0), (-89.95, -120.0)]


@pytest.fixture
async def placed_world(session, make_club):
    """Events and clubs scattered around the awkward centres, and some exactly
    at the radius's edge, 5 m inside and 5 m outside."""
    rng = random.Random(11)
    tag = uuid.uuid4().hex[:6]
    cat = EventCategories(name=f"Placed {tag}")
    session.add(cat)
    await session.flush()
    _, host = await make_club(name=f"Placed Host {tag}", email=f"placed-host-{tag}@example.com")
    now = datetime.now(timezone.utc)
    events, clubs = [], []
    for ci, centre in enumerate(EDGE_CENTRES):
        points = [_dest(*centre, rng.uniform(0, 140), rng.uniform(0, 360)) for _ in range(14)]
        points += [_dest(*centre, 50.0 + d, b) for d in (-0.005, 0.005) for b in (0, 90, 181, 270)]
        for n, (la, ln) in enumerate(points):
            e = Events(slug=f"p{ci}-{n}-{tag}", name=f"Placed {ci}-{n}", images=[], event_datetime=now + timedelta(hours=1 + n),
                       duration=1.0, club_id=host.id, category_id=cat.id, lat=round(la, 7), lng=round(ln, 7), is_online=(n % 9 == 0),
                       lifecycle=EventStatus.published)
            events.append(e)
            if n % 3 == 0:
                c = Clubs(slug=f"pc{ci}-{n}-{tag}", name=f"Placed Club {ci}-{n}", user_id=host.user_id, is_verified=True,
                          lat=round(la, 7), lng=round(ln, 7))
                clubs.append(c)
    session.add_all(events + clubs)
    await session.commit()
    return dict(events=events, clubs=clubs, host=host, now=now, tag=tag)


def _exact(rows, centre, radius):
    return {r.id for r in rows if r.lat is not None and geo.km(*centre, r.lat, r.lng) <= radius}


@pytest.mark.parametrize("centre", EDGE_CENTRES)
async def test_within_a_radius_is_exactly_the_haversine(session, placed_world, centre):
    now = placed_world["now"]
    ids = [e.id for e in placed_world["events"]]
    for radius in (50.0, 120.0):
        got = set((await session.execute(
            select(Events.id).where(Events.id.in_(ids), Events.is_online == False, service._within(centre, radius))  # noqa: E712
        )).scalars().all())
        assert got == _exact([e for e in placed_world["events"] if not e.is_online], centre, radius), (centre, radius)
        # sort=distance's placed part reads the cells: the same rows, nearest first.
        since = await upcoming.since(session, now)
        conds = service._conditions(now, since) + [Events.id.in_(ids), service._within(centre, radius)]
        page, _ = await service._distance_page(session, conds, centre, radius, 0, 100, 100, False, None)
        placed = [e for e in page if not e.is_online]
        assert {e.id for e in placed} == _exact([e for e in placed_world["events"] if not e.is_online], centre, radius)
        far = [geo.km(*centre, e.lat, e.lng) for e in placed]
        assert all(a <= b + 1e-9 for a, b in zip(far, far[1:]))  # nearest first (points the same distance away in either order)


@pytest.mark.parametrize("centre", EDGE_CENTRES)
async def test_near_you_walks_the_cells_and_finds_what_the_haversine_finds(session, placed_world, centre, monkeypatch):
    monkeypatch.setattr(service, "NEAR_CHUNK", 3)  # several chunks, so the walk's cursor is used
    now = placed_world["now"]
    since = await upcoming.since(session, now)
    pool = await service._near_pool(session, now, since, centre, 0.0)
    want = [e for e in sorted(placed_world["events"], key=lambda e: (e.event_datetime, e.id))
            if not e.is_online and geo.km(*centre, e.lat, e.lng) <= service.NEAR_KM + service._SLACK_KM]
    got = [i for i, _, _ in pool["events"]]
    ours = {e.id for e in placed_world["events"]}
    assert [i for i in got if i in ours] == [e.id for e in want][:len([i for i in got if i in ours])]
    # It stopped only once settled, or at the end: never short of what is sure.
    assert len([i for i in got if i in ours]) >= min(len(want), service.NEAR_COUNT)


@pytest.mark.parametrize("centre", EDGE_CENTRES)
async def test_clubs_near_a_point_are_exactly_the_haversine(session, placed_world, centre):
    ids = [c.id for c in placed_world["clubs"]]
    for radius in (50.0, 120.0):
        got = set((await session.execute(
            select(Clubs.id).where(Clubs.id.in_(ids), places.placed(), places.within(centre, radius))
        )).scalars().all())
        assert got == _exact(placed_world["clubs"], centre, radius)
        around = {i for i, *_ in await places.around(session, centre, radius) if i in set(ids)}
        assert around == got
