"""The public site's newer lists: a club's events a page at a time, events
between two calendar days, and the calendar of a month.

Days are India's: an event touches a day when any of it falls between that
day's midnight and the next (an event of no length, where it starts). What
counts is what the public may see: on the calendar's past days, what was
held, as a club page's past list shows it."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event as sa_event, text

from app.api.closeout.models import EventStatus
from app.api.discover import cache, service
from app.api.events.models import EventCategories
from app.db.listeners import add_loader_criteria

API = "/api/v1/discover"
IST = ZoneInfo("Asia/Kolkata")
BLR = (12.9716, 77.5946)
DELHI = (28.6139, 77.2090)


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


@pytest.fixture
def statements(session):
    seen: list[tuple[str, object]] = []
    conn = session.bind.sync_connection

    def before(_conn, _cursor, statement, parameters, *_args):
        # The test session's own SAVEPOINTs (conftest) are not the code's statements.
        if "SAVEPOINT" not in statement:
            seen.append((statement, parameters))

    sa_event.listen(conn, "before_cursor_execute", before)
    yield seen
    sa_event.remove(conn, "before_cursor_execute", before)


def at(d: date, hour: int = 0, minute: int = 0) -> datetime:
    """A moment in India."""
    return datetime.combine(d, time(hour, minute), tzinfo=IST)


def month_of(d: date, shift: int) -> date:
    """The first day of the month `shift` months from `d`'s."""
    n = d.year * 12 + d.month - 1 + shift
    return date(n // 12, n % 12 + 1, 1)


def ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _names(items):
    return [x["name"] for x in items]


@pytest.fixture
async def scene(session, make_club, make_event):
    now = datetime.now(timezone.utc)
    today = now.astimezone(IST).date()
    tech, music = EventCategories(name="Tech"), EventCategories(name="Music")
    session.add_all([tech, music])
    await session.flush()
    owner, club = await make_club(name="Polish Society", email="polish-a@example.com")
    other_owner, other = await make_club(name="Second Circle", email="polish-b@example.com")
    ghost_owner, ghost = await make_club(name="Ghost Polish", email="polish-c@example.com", is_verified=False)

    async def ev(name, start, hours=2.0, c=None, o=None, category=None, **kw):
        kw.setdefault("lifecycle", EventStatus.published)
        return await make_event(club=c or club, created_by=o or owner, name=name, event_datetime=start, duration=hours, category_id=(category or tech).id, **kw)

    # The club's past: nine held (newest first: Past 1 … Past 9), and what never shows.
    statuses = [EventStatus.closed, EventStatus.archived, EventStatus.published, EventStatus.live]
    past = [await ev(f"Past {i}", now - timedelta(days=i, hours=3), lifecycle=statuses[i % 4]) for i in range(1, 10)]
    await ev("Past Draft", now - timedelta(days=2), lifecycle=EventStatus.draft)
    await ev("Past Deleted", now - timedelta(days=2), is_deleted=True)
    # Its upcoming: one on now, three to come, and what never shows.
    on_now = await ev("On Now", now - timedelta(hours=1), hours=3.0, lifecycle=EventStatus.live)
    coming = [await ev(f"Coming {i}", now + timedelta(days=i)) for i in (1, 2, 3)]
    await ev("Coming Draft", now + timedelta(days=1), lifecycle=EventStatus.draft)
    await ev("Coming Deleted", now + timedelta(days=1), is_deleted=True)
    await ev("Ghost Coming", now + timedelta(days=1), c=ghost, o=ghost_owner)

    # A month two ahead, every day of it to come.
    m = month_of(today, 2)
    last = month_of(today, 3) - timedelta(days=1)
    d5, d6, d10 = m + timedelta(days=4), m + timedelta(days=5), m + timedelta(days=9)
    month = dict(
        e1=await ev("Morning Build", at(d5, 10), lat=BLR[0], lng=BLR[1], location_name="Bengaluru Lab"),
        # 18:00 for 30 hours: until midnight two days on, so days 5 and 6 only.
        e2=await ev("Two Day Summit", at(d5, 18), hours=30.0, lat=DELHI[0], lng=DELHI[1]),
        e3=await ev("Midnight Instant", at(d10), hours=0.0),
        e4=await ev("Month End", at(last, 23)),
        # From the last evening of the month before into this one's first day.
        e5=await ev("Overnight", at(m - timedelta(days=1), 23), hours=3.0),
        online=await ev("Remote Talk", at(d5, 12), is_online=True),
        paid=await ev("Robotics Jam", at(d6, 15), c=other, o=other_owner, category=music, has_fee=True, reg_fee=100),
        # Never counted:
        draft=await ev("Draft Day", at(d5, 9), lifecycle=EventStatus.draft),
        deleted=await ev("Deleted Day", at(d5, 9), is_deleted=True),
        ghost=await ev("Ghost Day", at(d5, 9), c=ghost, o=ghost_owner),
        # Ends exactly as day 5 begins: it does not touch it.
        edge=await ev("Edge Evening", at(d5 - timedelta(days=1), 22), hours=2.0),
    )

    # Two months back (clear of the club's last nine days): what was held there.
    p = month_of(today, -2)
    held_by = dict(c=other, o=other_owner)
    held = dict(
        closed=await ev("Held Closed", at(p + timedelta(days=2), 10), lifecycle=EventStatus.closed, **held_by),
        archived=await ev("Held Archived", at(p + timedelta(days=3), 10), lifecycle=EventStatus.archived, **held_by),
        clock_off=await ev("Held Published", at(p + timedelta(days=3), 15), lifecycle=EventStatus.published, **held_by),
        draft=await ev("Held Draft", at(p + timedelta(days=3), 15), lifecycle=EventStatus.draft, **held_by),
        deleted=await ev("Held Deleted", at(p + timedelta(days=3), 15), lifecycle=EventStatus.closed, is_deleted=True, **held_by),
        ghost=await ev("Held Ghost", at(p + timedelta(days=3), 15), lifecycle=EventStatus.closed, c=ghost, o=ghost_owner),
    )
    await session.commit()
    return dict(
        now=now, today=today, club=club, other=other, ghost=ghost, past=past, on_now=on_now, coming=coming,
        m=m, last=last, d5=d5, d6=d6, d10=d10, month=month, p=p, held=held, tech=tech, music=music,
    )


# ─── A club's events ─────────────────────────────────────────────────────


async def test_a_clubs_past_events_page_through_the_club_pages_rule(client, scene):
    slug = scene["club"].slug
    r = await client.get(f"{API}/clubs/{slug}/events", params={"when": "past", "limit": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["total"], body["offset"], body["limit"]) == (9, 0, 4)
    assert _names(body["items"]) == ["Past 1", "Past 2", "Past 3", "Past 4"]
    assert all(c["status"] == "ended" and c["club"]["slug"] == slug for c in body["items"])

    seen = list(body["items"])
    for offset in (4, 8):
        page = (await client.get(f"{API}/clubs/{slug}/events", params={"when": "past", "offset": offset, "limit": 4})).json()
        assert page["total"] == 9
        seen += page["items"]
    assert _names(seen) == [f"Past {i}" for i in range(1, 10)]
    beyond = (await client.get(f"{API}/clubs/{slug}/events", params={"when": "past", "offset": 40})).json()
    assert (beyond["items"], beyond["total"]) == ([], 9)

    # The club page shows the first six of exactly these.
    club_page = (await client.get(f"{API}/clubs/{slug}")).json()
    assert club_page["past"] == seen[:6]
    assert club_page["club"]["events_held"] == 9


async def test_a_clubs_upcoming_events_are_what_the_search_shows_soonest_first(client, scene):
    slug = scene["club"].slug
    r = await client.get(f"{API}/clubs/{slug}/events", params={"when": "upcoming"})
    assert r.status_code == 200, r.text
    body = r.json()
    names = _names(body["items"])
    assert names[:4] == ["On Now", "Coming 1", "Coming 2", "Coming 3"]
    assert "Coming Draft" not in names and "Coming Deleted" not in names and not any(n.startswith("Past") for n in names)
    assert body["total"] == len(names) and body["limit"] == 12
    assert body["items"][0]["status"] == "live"
    # The default is what is coming, and it matches the club page's list.
    assert (await client.get(f"{API}/clubs/{slug}/events")).json() == body
    assert (await client.get(f"{API}/clubs/{slug}")).json()["upcoming"] == body["items"]
    # Every one is in the public search too.
    searched = (await client.get(f"{API}/events", params={"limit": 60})).json()["items"]
    assert set(names) <= set(_names(searched))


async def test_a_club_the_public_cannot_see_has_no_events_page(client, session, scene):
    for slug in (scene["ghost"].slug, "no-such-club"):
        r = await client.get(f"{API}/clubs/{slug}/events", params={"when": "past"})
        assert r.status_code == 404, r.text
    scene["other"].is_deleted = True
    await session.commit()
    assert (await client.get(f"{API}/clubs/{scene['other'].slug}/events")).status_code == 404


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 49}, {"offset": -1}, {"when": "soon"}])
async def test_a_clubs_events_page_checks_its_paging(client, scene, params):
    r = await client.get(f"{API}/clubs/{scene['club'].slug}/events", params=params)
    assert r.status_code == 422, r.text


async def test_a_clubs_events_take_a_few_statements_however_many_it_has(client, scene, statements):
    for when in ("past", "upcoming"):
        statements.clear()
        r = await client.get(f"{API}/clubs/{scene['club'].slug}/events", params={"when": when, "limit": 48})
        assert r.status_code == 200
        # The club, the count, the page, the counters; and for what is to come
        # the floor of what can be on (discover.upcoming), which production
        # keeps for the minute and the suite, with the cache off, looks up.
        assert len(statements) <= (4 if when == "past" else 5), [s for s, _ in statements]


# ─── Between two days ────────────────────────────────────────────────────


async def _range(client, **params):
    r = await client.get(f"{API}/events", params={"limit": 60, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_from_and_to_keep_what_touches_those_days_in_india(client, scene):
    d5, d6, d10 = scene["d5"], scene["d6"], scene["d10"]
    one = await _range(client, **{"from": d5.isoformat(), "to": d5.isoformat()})
    assert set(_names(one["items"])) == {"Morning Build", "Two Day Summit", "Remote Talk"}
    assert one["total"] == 3
    # The summit runs into day 6; the jam is on it.
    assert set(_names((await _range(client, **{"from": d6.isoformat(), "to": d6.isoformat()}))["items"])) == {"Two Day Summit", "Robotics Jam"}
    # An event of no length counts on the day it starts.
    assert _names((await _range(client, **{"from": d10.isoformat(), "to": d10.isoformat()}))["items"]) == ["Midnight Instant"]
    # The day before day 5 holds the evening that ends as day 5 begins.
    before = await _range(client, **{"from": (d5 - timedelta(days=1)).isoformat(), "to": (d5 - timedelta(days=1)).isoformat()})
    assert _names(before["items"]) == ["Edge Evening"]
    # An overnight event touches both days.
    m = scene["m"]
    assert "Overnight" in _names((await _range(client, **{"from": m.isoformat(), "to": m.isoformat()}))["items"])
    assert "Overnight" in _names((await _range(client, **{"from": (m - timedelta(days=1)).isoformat(), "to": (m - timedelta(days=1)).isoformat()}))["items"])
    # Several days, soonest first.
    span = await _range(client, **{"from": d5.isoformat(), "to": d6.isoformat()})
    assert _names(span["items"]) == ["Morning Build", "Remote Talk", "Two Day Summit", "Robotics Jam"]


async def test_from_and_to_replace_when_and_work_alone_and_with_the_other_filters(client, scene):
    d5, d6 = scene["d5"], scene["d6"]
    both = await _range(client, **{"from": d5.isoformat(), "to": d6.isoformat()})
    # `when` is ignored once a day is given.
    assert await _range(client, when="today", **{"from": d5.isoformat(), "to": d6.isoformat()}) == both
    # From alone: from that day on; to alone: up to the end of that day.
    after = _names((await _range(client, **{"from": d6.isoformat()}))["items"])
    assert "Two Day Summit" in after and "Midnight Instant" in after and "Morning Build" not in after and "Coming 1" not in after
    upto = _names((await _range(client, to=(d5 - timedelta(days=1)).isoformat()))["items"])
    assert "Coming 1" in upto and "Edge Evening" in upto and "Morning Build" not in upto
    # The usual filters narrow it.
    narrowed = await _range(client, **{"from": d5.isoformat(), "to": d6.isoformat(), "q": "robotics"})
    assert _names(narrowed["items"]) == ["Robotics Jam"]
    assert _names((await _range(client, **{"from": d5.isoformat(), "to": d6.isoformat(), "type": "online"}))["items"]) == ["Remote Talk"]
    assert _names((await _range(client, **{"from": d5.isoformat(), "to": d6.isoformat(), "price": "paid"}))["items"]) == ["Robotics Jam"]
    near = await _range(client, **{"from": d5.isoformat(), "to": d6.isoformat(), "lat": BLR[0], "lng": BLR[1], "sort": "distance"})
    assert _names(near["items"])[0] == "Morning Build" and "Two Day Summit" not in _names(near["items"])
    # Still only what is on: what is over is not in the search, whatever the days.
    past_days = await _range(client, **{"from": scene["p"].isoformat(), "to": (scene["p"] + timedelta(days=27)).isoformat()})
    assert past_days["total"] == 0


@pytest.mark.parametrize("params, field", [
    ({"from": "2030-01-10", "to": "2030-01-09"}, "to"),
    ({"from": "2030-01-01", "to": "2031-01-02"}, "to"),  # 367 days, both ends counted
    ({"from": "2030-02-30"}, "from"),
    ({"to": "tomorrow"}, "to"),
])
async def test_a_bad_range_is_refused(client, params, field):
    r = await client.get(f"{API}/events", params=params)
    assert r.status_code == 422, r.text
    assert field in (r.json()["errors"] or {}), r.json()


async def test_a_range_of_366_days_is_fine(client):
    r = await client.get(f"{API}/events", params={"from": "2030-01-01", "to": "2031-01-01"})
    assert r.status_code == 200, r.text


def test_days_are_counted_in_india():
    start, end = service.date_range(date(2030, 3, 9), date(2030, 3, 10))
    assert start == datetime(2030, 3, 8, 18, 30, tzinfo=timezone.utc)
    assert end == datetime(2030, 3, 10, 18, 30, tzinfo=timezone.utc)
    assert service.date_range(None, None) == (None, None)


# ─── The calendar ────────────────────────────────────────────────────────


async def _month(client, month: str, **params) -> dict:
    r = await client.get(f"{API}/calendar", params={"month": month, **params})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["month"] == month
    return body["days"]


async def test_the_calendar_counts_each_day_an_event_touches(client, scene):
    m, d5, d6, d10, last = scene["m"], scene["d5"], scene["d6"], scene["d10"], scene["last"]
    days = await _month(client, ym(m))
    assert days == {
        m.isoformat(): 1,        # the overnight one from the month before
        (d5 - timedelta(days=1)).isoformat(): 1,  # the evening that ends as day 5 begins
        d5.isoformat(): 3,       # the morning build, the summit, the online talk
        d6.isoformat(): 2,       # the summit again, the jam
        d10.isoformat(): 1,      # the instant at midnight
        last.isoformat(): 1,     # the month's last evening (its tail is next month's)
    }


async def test_the_calendar_agrees_with_the_search_day_by_day(client, scene):
    m = scene["m"]
    days = await _month(client, ym(m))
    d = m
    while d.month == m.month:
        found = await _range(client, **{"from": d.isoformat(), "to": d.isoformat()})
        assert found["total"] == days.get(d.isoformat(), 0), d
        d += timedelta(days=1)


async def test_past_days_count_what_was_held(client, scene):
    p = scene["p"]
    days = await _month(client, ym(p))
    assert days[(p + timedelta(days=2)).isoformat()] == 1          # closed
    assert days[(p + timedelta(days=3)).isoformat()] == 2          # archived, and published with its clock off
    assert sum(days.values()) == 3                                 # never the draft or the deleted one


async def test_the_calendar_takes_the_searchs_filters(client, scene):
    m, d5, d6 = scene["m"], scene["d5"], scene["d6"]
    assert await _month(client, ym(m), q="robotics") == {d6.isoformat(): 1}
    # A typo is forgiven, as the search forgives it.
    assert await _month(client, ym(m), q="robtics") == {d6.isoformat(): 1}
    assert await _month(client, ym(m), q="zzzzqqq") == {}
    assert await _month(client, ym(m), category=scene["music"].id) == {d6.isoformat(): 1}
    assert await _month(client, ym(m), type="online") == {d5.isoformat(): 1}
    assert await _month(client, ym(m), price="paid") == {d6.isoformat(): 1}
    in_person = await _month(client, ym(m), type="in_person")
    assert in_person[d5.isoformat()] == 2
    # Near Bengaluru: the Delhi summit is out; online and unplaced ones cannot be ruled out.
    near = await _month(client, ym(m), lat=BLR[0], lng=BLR[1], radius_km=50)
    assert near[d5.isoformat()] == 2 and near[d6.isoformat()] == 1
    everywhere = await _month(client, ym(m), lat=BLR[0], lng=BLR[1], radius_km=0)
    assert everywhere == await _month(client, ym(m))


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "26-01", "2026-00", "", "2026-01-01"])
async def test_a_month_must_be_a_month(client, month):
    r = await client.get(f"{API}/calendar", params={"month": month})
    assert r.status_code == 422, r.text


async def test_the_calendar_answers_two_years_either_way(client, scene):
    today = scene["today"]
    for shift in (-24, 24):
        assert (await client.get(f"{API}/calendar", params={"month": ym(month_of(today, shift))})).status_code == 200
    for shift in (-25, 25):
        r = await client.get(f"{API}/calendar", params={"month": ym(month_of(today, shift))})
        assert r.status_code == 422, r.text
        assert "month" in r.json()["errors"]


async def test_the_calendar_is_one_statement_and_kept_for_the_minute(client, scene, statements):
    month = ym(scene["m"])
    statements.clear()
    first = await _month(client, month)
    assert len(statements) == 1, [s for s, _ in statements]

    cache.reset(enabled=True)
    try:
        assert await _month(client, month) == first
        statements.clear()
        assert await _month(client, month) == first
        assert statements == []
        # Another filter is another answer.
        await _month(client, month, type="online")
        assert len(statements) == 1
    finally:
        cache.reset(enabled=False)


async def test_the_calendar_and_the_day_range_can_probe_the_span_index(client, session, scene, statements):
    """Both queries carry ix_events_span's expression, word for word, so the
    planner may use the index for them (which it does is its choice, on the
    statistics: RESULTS.md); and that expression alone is served by it."""
    from sqlalchemy import func, literal_column, select
    from sqlalchemy.dialects import postgresql

    from app.api.events.models import Events, span_sql

    expression = str(span_sql(Events).compile(dialect=postgresql.dialect()))
    statements.clear()
    await _month(client, ym(scene["m"]))
    [(calendar_sql, _)] = statements
    statements.clear()
    d5 = scene["d5"].isoformat()
    await _range(client, **{"from": d5, "to": d5})
    count_sql = next(s for s, _ in statements if "count(" in s.lower())
    for sql in (calendar_sql, count_sql):
        assert f"{expression} && tstzrange(" in sql, sql

    probe = select(func.count()).select_from(Events).where(
        span_sql(Events).op("&&")(func.tstzrange(service._instant(service.day_start(scene["d5"])), service._instant(service.day_start(scene["d6"])), literal_column("'[)'")))
    )
    conn = await session.connection()
    await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
    compiled = probe.compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True})
    plan = "\n".join(r[0] for r in (await conn.exec_driver_sql("EXPLAIN " + str(compiled))).all())
    await conn.execute(text("RESET enable_seqscan"))
    assert "ix_events_span" in plan, plan
