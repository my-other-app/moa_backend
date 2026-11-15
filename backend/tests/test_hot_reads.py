"""The reads the dashboards poll, made cheap without changing an answer.

- The Announcements page groups pushes in SQL and pages there: the same
  sends, figures and pages as the Python grouping it replaces (kept below
  as `_old_*`, word for word), in a fixed number of statements however many
  people each push reached. Its figures are kept 30 s and forgotten when the
  club sends something new.
- The old push history and the admin's "every announcement" answer as before.
- The console summary and the club dashboard are kept a few seconds, the
  deliveries log's total and tally 3 s: a warm read runs no aggregate.
- The app's event page reads its ticket types once, and the viewer's rating
  with the event.
"""
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.clubs import announcements_page as page
from app.api.discover import cache
from app.api.notifications.models import NotificationStatus, Notifications
from tests.conftest import auth_header
from tests.racing import statements as _all_statements

API = "/api/v1"
NOW = datetime.now(timezone.utc)
#: What a signed-in, registered guest's view of the event page runs, at most
#: (sign-in included). It was 27: the ticket types were read three times and
#: the viewer's rating on its own.
STATEMENTS_PER_VIEW = 24


class statements:
    """The statements run meanwhile, without the savepoints the test's session wraps around them."""

    def __init__(self, session):
        self._cm = _all_statements(session)

    def __enter__(self):
        self._seen = self._cm.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)

    @property
    def run(self) -> list[str]:
        return [s for s in self._seen if not s.lstrip().upper().startswith(("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT"))]

    def __len__(self):
        return len(self.run)

    def __iter__(self):
        return iter(self.run)

    def __eq__(self, other):
        return self.run == other


@pytest.fixture
def warm():
    """The few-second stores switched on for this test (the suite runs with them off)."""
    cache.reset(enabled=True)
    yield
    cache.reset(enabled=False)


# ─── The old grouping, as it was (the reference) ─────────────────────────


async def _old_sends(session, club_id: int) -> list[dict]:
    from app.api.events.models import Events

    events = {e.id: e for e in (await session.scalars(select(Events).where(Events.club_id == club_id, Events.is_deleted == False))).all()}  # noqa: E712
    if not events:
        return []
    rows = (await session.scalars(
        select(Notifications).where(Notifications.event_id.in_(list(events)), Notifications.type.in_(list(page.KINDS)), Notifications.is_deleted == False).order_by(Notifications.created_at.desc())  # noqa: E712
    )).all()
    groups: dict = {}
    for n in rows:
        minute = n.created_at.replace(second=0, microsecond=0)
        key = (n.type, n.title, n.description, n.event_id, minute)
        g = groups.get(key)
        if g is None:
            g = groups[key] = dict(
                id=hashlib.sha1(f"{n.type}|{n.title}|{n.event_id}|{minute.isoformat()}".encode()).hexdigest()[:16],
                kind=page.KINDS[n.type], title=n.title, body=n.description, event_id=n.event_id, created_at=n.created_at,
                audience=(n.data or {}).get("audience") if isinstance(n.data, dict) else None, sent=0, read=0, read_quickly=0,
            )
        g["sent"] += 1
        if n.status == NotificationStatus.read:
            g["read"] += 1
            if n.updated_at and n.updated_at - n.created_at <= page.QUICK:
                g["read_quickly"] += 1
    out = []
    for g in groups.values():
        e = events.get(g["event_id"])
        g["event"] = dict(id=e.id, name=e.name, starts_at=e.event_datetime) if e else None
        g["read_pct"] = round(100 * g["read"] / g["sent"]) if g["sent"] else 0
        g["slot"] = page._slot(g["created_at"])
        out.append(g)
    out.sort(key=lambda g: g["created_at"], reverse=True)
    return out


def _old_matches(g: dict, term: str) -> bool:
    t = term.lower()
    return t in (g["title"] or "").lower() or t in (g["body"] or "").lower() or t in ((g["event"] or {}).get("name") or "").lower()


def _old_history(sends, *, scope="all", search=None, limit=20, offset=0) -> dict:
    counts = dict(all=len(sends), announcements=sum(1 for g in sends if g["kind"] == "announcement"), live_updates=sum(1 for g in sends if g["kind"] == "live_update"))
    if scope == "announcements":
        sends = [g for g in sends if g["kind"] == "announcement"]
    elif scope == "live_updates":
        sends = [g for g in sends if g["kind"] == "live_update"]
    if search and search.strip():
        sends = [g for g in sends if _old_matches(g, search.strip())]
    return dict(items=sends[offset: offset + limit], total=len(sends), limit=limit, offset=offset, counts=counts)


async def _old_legacy(session, club_id: int, limit: int, offset: int) -> dict:
    """GET /clubs/notifications/history as it was: grouped by title, event and clock hour."""
    from app.api.events.models import Events

    events = {e.id: e for e in (await session.scalars(select(Events).where(Events.club_id == club_id, Events.is_deleted == False))).all()}  # noqa: E712
    rows = (await session.scalars(
        select(Notifications).where(Notifications.event_id.in_(list(events)), Notifications.type == "club_announcement", Notifications.is_deleted == False).order_by(Notifications.created_at.desc())  # noqa: E712
    )).all()
    groups: dict = {}
    for n in rows:
        key = (n.title, n.event_id, n.created_at.replace(minute=0, second=0, microsecond=0))
        if key not in groups:
            groups[key] = dict(title=n.title, description=n.description, event_id=n.event_id, created_at=n.created_at, sent_count=0, read_count=0)
        groups[key]["sent_count"] += 1
        if n.status == NotificationStatus.read:
            groups[key]["read_count"] += 1
    grouped = sorted(groups.values(), key=lambda x: x["created_at"], reverse=True)
    items = []
    for idx, g in enumerate(grouped[offset: offset + limit]):
        e = events.get(g["event_id"])
        sent, read = g["sent_count"], g["read_count"]
        items.append(dict(
            id=f"{g['event_id']}-{offset + idx}", title=g["title"], description=g["description"], event_id=g["event_id"],
            event_name=e.name if e else "Unknown Event", event_poster=e.poster if e else None, sent_count=sent, read_count=read,
            read_percentage=round((read / sent * 100), 1) if sent > 0 else 0, created_at=g["created_at"],
        ))
    return dict(items=items, total=len(grouped))


# ─── A club that has pushed a few things ─────────────────────────────────


@pytest.fixture
async def pushes(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Circuit Society", email="hot-ann@example.com")
    other_owner, other_club = await make_club(name="Elsewhere", email="hot-else@example.com")
    e1 = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=NOW + timedelta(days=3), duration=3.0)
    e2 = await make_event(club=club, created_by=owner, name="Demo Day", event_datetime=NOW + timedelta(days=9), duration=2.0)
    gone = await make_event(club=club, created_by=owner, name="Called Off", event_datetime=NOW + timedelta(days=4), duration=1.0, is_deleted=True)
    theirs = await make_event(club=other_club, created_by=other_owner, name="Not Ours", event_datetime=NOW + timedelta(days=5), duration=1.0)
    people = [await make_user(email=f"hot-p{i}@example.com") for i in range(5)]
    for u in people[:3]:
        await make_registration(event=e1, user=u, full_name=u.email)

    base = (NOW - timedelta(days=2)).replace(second=5, microsecond=0)
    shuttle = (NOW - timedelta(days=3)).replace(hour=10, minute=20, second=0, microsecond=0)  # 15:50 in India

    def push(event, title, body, when, n, *, read=0, quick=0, kind="club_announcement", audience="all", deleted=False):
        for i in range(n):
            is_read = i < read
            session.add(Notifications(
                user_id=people[i].id, title=title, description=body, type=kind, event_id=event.id, from_club_id=event.club_id,
                status=NotificationStatus.read if is_read else NotificationStatus.unread,
                data={"audience": audience} if kind == "club_announcement" else {"type": kind, "event_id": str(event.id)},
                created_at=when, updated_at=when + (timedelta(minutes=10) if i < quick else timedelta(hours=3)) if is_read else when,
                is_deleted=deleted,
            ))

    push(e1, "Doors open", "Gate A", base, 4, read=2, quick=1)
    push(e1, "Doors open", "Gate A", base + timedelta(seconds=30), 1, read=1, quick=1)  # the same send, finished in its minute
    push(e1, "Doors open", "Gate B", base + timedelta(seconds=20), 2)  # same minute, other words: a send of its own
    push(e1, "Doors open", "Gate A", base + timedelta(seconds=60), 2, read=2)  # the next minute: another send
    push(e2, "Demo Day", "Demos move to Lab 3B", base + timedelta(hours=1), 3, read=1, quick=1, kind="event_live_update")
    push(e2, "Venue change", "Now at 100% capacity_hall", base + timedelta(hours=2), 2, read=2, audience="attendees")
    push(e2, "Shuttle", "From the station", shuttle, 2, read=1)
    push(e2, "Shuttle", "From the station", shuttle + timedelta(minutes=20), 1)  # 16:10 in India: another clock hour
    push(e1, "Early bird", "Ends tonight", NOW - timedelta(days=40), 3, read=3, quick=2)  # the 30 days before
    push(e1, "Save the date", "Hack Night is coming", NOW - timedelta(days=120), 2, read=1)
    push(e1, "Withdrawn", "Never mind", base + timedelta(hours=3), 2, deleted=True)
    push(e1, "Welcome", "You're a volunteer", base + timedelta(hours=4), 1, kind="volunteer_added")
    push(gone, "Called off", "Sorry", base + timedelta(hours=5), 2)
    push(theirs, "Not ours", "Elsewhere", base + timedelta(hours=6), 2)
    await session.commit()
    return {"club": club, "other": other_club, "e1": e1, "e2": e2, "h": auth_header(await mint_token(owner)), "people": people}


async def test_the_sends_are_the_ones_python_grouped(session, pushes):
    club = pushes["club"]
    old = await _old_sends(session, club.id)
    new = await page._every_send(session, club.id)
    assert len(old) == 9
    assert new == old


@pytest.mark.parametrize("scope,search", [
    ("all", None), ("announcements", None), ("live_updates", None),
    ("all", "doors"), ("all", "GATE b"), ("all", "hack"), ("announcements", "demo"), ("live_updates", "lab 3b"),
    ("all", "100%"), ("all", "%"), ("all", "_"), ("all", "  "), ("all", "nothing like it"),
])
async def test_each_page_is_the_page_python_cut(session, pushes, scope, search):
    club = pushes["club"]
    sends = await _old_sends(session, club.id)
    for limit, offset in ((20, 0), (2, 0), (2, 2), (2, 4), (2, 8), (3, 30)):
        want = _old_history(sends, scope=scope, search=search, limit=limit, offset=offset)
        got = await page.history(session, club.id, scope=scope, search=search, limit=limit, offset=offset)
        assert got == want, (limit, offset)


async def test_the_figures_are_the_ones_python_worked_out(session, pushes, monkeypatch):
    club = pushes["club"]
    sends = await _old_sends(session, club.id)

    async def old(_session, _club_id):
        return sends

    want = None
    with monkeypatch.context() as m:
        m.setattr(page, "_sends", old)
        want = await page.insights(session, club.id)
    got = await page.insights(session, club.id)
    want.pop("generated_at"), got.pop("generated_at")
    assert got == want
    assert got["all_time"] == {"sends": 9, "reached": 22, "opened": 13}


async def test_a_page_is_two_statements_however_many_were_reached(session, pushes):
    club = pushes["club"]
    with statements(session) as seen:
        await page.history(session, club.id, limit=2)
    assert len(seen) == 2  # the kinds counted, and the page with its total
    with statements(session) as seen:
        await page._every_send(session, club.id)
    assert len(seen) == 1
    # A thousand more people on one send changes nothing.
    more = [Notifications(user_id=pushes["people"][i % 5].id, title="Doors open", description="Gate A", type="club_announcement",
                          event_id=pushes["e1"].id, status=NotificationStatus.unread, data={"audience": "all"},
                          created_at=NOW - timedelta(hours=1), updated_at=NOW - timedelta(hours=1)) for i in range(1000)]
    session.add_all(more)
    await session.commit()
    with statements(session) as seen:
        got = await page.history(session, club.id, limit=2)
    assert len(seen) == 2 and got["items"][0]["sent"] == 1000


async def test_the_old_history_is_the_one_python_grouped(client, session, pushes):
    club = pushes["club"]
    every = (await client.get(f"{API}/clubs/notifications/history", headers=pushes["h"])).json()["items"]
    assert len([i for i in every if i["title"] == "Shuttle"]) == 2  # two clock hours in India, though one in UTC
    for limit, offset in ((50, 0), (2, 0), (2, 2), (2, 6), (5, 40)):
        want = await _old_legacy(session, club.id, limit, offset)
        r = await client.get(f"{API}/clubs/notifications/history", params={"limit": limit, "offset": offset}, headers=pushes["h"])
        assert r.status_code == 200
        got = r.json()
        assert got["total"] == want["total"] >= 6, (limit, offset)
        assert [{**i, "created_at": datetime.fromisoformat(i["created_at"])} for i in got["items"]] == want["items"], (limit, offset)


async def test_the_admins_every_announcement_looks_back_90_days_in_one_statement(client, session, pushes, make_admin, mint_token):
    from sqlalchemy import func

    from app.api.audit import notifications as audit

    async def old_total(days):
        # As it was counted: the groups of a second statement.
        hour = func.date_trunc("hour", Notifications.created_at)
        N = Notifications
        grouped = select(N.title).where(N.type == "club_announcement", N.is_deleted == False, N.created_at >= NOW - timedelta(days=days)).group_by(  # noqa: E712
            N.title, N.description, N.event_id, N.from_club_id, hour)
        return await session.scalar(select(func.count()).select_from(grouped.subquery()))

    admin = await make_admin(email="hot-admin@example.com")
    h = auth_header(await mint_token(admin))
    with statements(session) as seen:
        items, total = await audit.list_sent_announcements(session, limit=3, offset=0)
    assert len(seen) == 3  # the page with its total, then the page's events and clubs by id
    assert total == await old_total(90) and len(items) == 3
    _, everything = await audit.list_sent_announcements(session, limit=3, offset=0, days=365)
    assert everything == await old_total(365) == total + 1  # the one from 120 days ago
    last = total - 1
    body = (await client.get(f"{API}/admin/notifications", params={"limit": 3, "offset": last}, headers=h)).json()
    assert body.get("total") == total and len(body["items"]) == 1 and body["next"] is None, body
    past = (await client.get(f"{API}/admin/notifications", params={"limit": 3, "offset": 30}, headers=h)).json()
    assert past["total"] == total and past["items"] == []


async def test_the_figures_are_kept_until_the_club_sends_something(client, session, pushes, warm, monkeypatch):
    import app.core.notifications.service as push_mod

    async def fake_push(*args, **kwargs):
        user_ids = kwargs.get("user_ids") or (args[1] if len(args) > 1 else [])
        return len(user_ids)

    monkeypatch.setattr(push_mod, "send_notification_to_users", fake_push)
    monkeypatch.setattr("app.core.notifications.triggers.send_notification_to_users", fake_push)
    h, e1 = pushes["h"], pushes["e1"]
    first = (await client.get(f"{API}/clubs/notifications/insights", headers=h)).json()
    assert first["all_time"]["sends"] == 9
    # Written behind the page's back: the figures keep their answer meanwhile.
    session.add(Notifications(user_id=pushes["people"][4].id, title="Quiet", description="Sneaked in", type="club_announcement", event_id=e1.id,
                              status=NotificationStatus.unread, data={"audience": "all"}, created_at=NOW, updated_at=NOW))
    await session.commit()
    with statements(session) as seen:
        again = (await client.get(f"{API}/clubs/notifications/insights", headers=h)).json()
    assert again["all_time"]["sends"] == 9
    assert not any("FROM notifications" in s for s in seen)
    # A send forgets them.
    r = await client.post(f"{API}/clubs/notifications/events/{e1.id}/send", json={"title": "Doors open at 5", "body": "Come early", "audience": "all"}, headers=h)
    assert r.status_code == 200 and r.json()["success"] is True
    assert (await client.get(f"{API}/clubs/notifications/insights", headers=h)).json()["all_time"]["sends"] == 11
    # So does a live update from the room.
    r = await client.post(f"{API}/events/{e1.id}/live/updates", json={"kind": "room", "text": "Demos move to Lab 3B", "audience": "registered"}, headers=h)
    assert r.status_code == 201, r.text
    assert (await client.get(f"{API}/clubs/notifications/insights", headers=h)).json()["all_time"]["sends"] == 12
    # The list itself is never kept.
    assert (await client.get(f"{API}/clubs/notifications/page", headers=h)).json()["counts"]["all"] == 12


# ─── The console, the club's home, the deliveries log ────────────────────


@pytest.fixture
async def door(client, session, make_club, make_event, make_user, make_registration, mint_token):
    from app.api.closeout.models import EventStatus
    from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryStatus

    start = NOW.replace(microsecond=0) - timedelta(hours=1)
    owner, club = await make_club(name="Yukthi Hot", email="hot-door@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Day", event_datetime=start, duration=4.0, max_participants=50, lifecycle=EventStatus.live)
    regs = []
    for i in range(4):
        u = await make_user(email=f"hot-g{i}@example.com")
        regs.append(await make_registration(event=event, user=u, full_name=f"Guest {i}"))
    for r, status in zip(regs, (DeliveryStatus.sent, DeliveryStatus.sent, DeliveryStatus.failed, DeliveryStatus.pending)):
        session.add(Deliveries(event_id=event.id, registration_id=r.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email, status=status, design_version=0))
    await session.commit()
    return {"club": club, "event": event, "regs": regs, "h": auth_header(await mint_token(owner))}


def _without_clock(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "generated_at"}


async def test_the_console_summary_is_shared_for_five_seconds(session, door, warm, make_user, make_registration):
    from app.api.analytics import service

    event = door["event"]
    first = await service.summary(session, event)
    assert _without_clock(first) == _without_clock(await service._summary(session, event, None))
    with statements(session) as seen:
        again = await service.summary(session, event)
    assert seen == [] and again is first
    # Someone registers: the kept answer stands until it expires; an explicit moment is always fresh.
    await make_registration(event=event, user=await make_user(email="hot-late@example.com"), full_name="Late")
    await session.commit()
    assert (await service.summary(session, event))["registered"] == 4
    assert (await service.summary(session, event, now=NOW))["registered"] == 5


async def test_the_club_home_is_shared_for_ten_seconds(client, session, door, warm):
    from app.api.analytics import service

    club = door["club"]
    first = await service.dashboard(session, club)
    assert _without_clock(first) == _without_clock(await service._dashboard(session, club, None))
    with statements(session) as seen:
        again = await service.dashboard(session, club)
    assert seen == [] and again is first
    a = (await client.get(f"{API}/clubs/dashboard", headers=door["h"])).json()
    with statements(session) as seen:
        b = (await client.get(f"{API}/clubs/dashboard", headers=door["h"])).json()
    assert a == b and not any("event_registrations_link" in s for s in seen)  # only the sign-in's own reads


async def test_the_deliveries_log_counts_once_and_keeps_its_tally(client, session, door, warm):
    from app.api.deliveries import service

    event = door["event"]
    cache.reset(enabled=False)
    cold = await service.log(session, event, page_size=50)
    with statements(session) as seen:
        await service.log(session, event, page_size=50)
    assert len(seen) == 2  # the page (its own total: shorter than a page) and the tally
    assert cold["total"] == 4 and cold["tally"]["sent"] == 2 and cold["tally"]["failed"] == 1 and cold["tally"]["pending"] == 1
    # A second page needs the total; with the store on it and the tally are kept.
    cache.reset(enabled=True)
    paged = await service.log(session, event, page=2, page_size=2)
    assert paged["total"] == 4 and paged["tally"] == cold["tally"] and len(paged["items"]) == 2
    with statements(session) as seen:
        again = await service.log(session, event, page=2, page_size=2)
    assert len(seen) == 1 and again["total"] == 4 and again["tally"] == cold["tally"]
    # The summary the page polls beside it.
    s1 = (await client.get(f"{API}/events/{event.id}/deliveries/summary", headers=door["h"])).json()
    with statements(session) as seen:
        s2 = (await client.get(f"{API}/events/{event.id}/deliveries/summary", headers=door["h"])).json()
    assert s1 == s2 and not any("FROM deliveries" in s for s in seen)


# ─── The app's event page ────────────────────────────────────────────────


async def test_the_event_page_reads_its_types_once_and_the_rating_with_the_event(client, session, door, mint_token):
    from app.api.events.models import EventRatingsLink
    from app.api.users.models import Users

    event, reg = door["event"], door["regs"][0]
    guest = await session.get(Users, reg.user_id)
    session.add(EventRatingsLink(event_id=event.id, user_id=guest.id, rating=4.0))
    await session.commit()
    h = auth_header(await mint_token(guest))
    await client.get(f"{API}/events/info/{event.id}", headers=h)  # a first view makes the default ticket type
    with statements(session) as seen:
        r = await client.get(f"{API}/events/info/{event.id}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["user_rating"] == 4.0 and body["is_registered"] is True and body["ticket_id"] == reg.ticket_id
    public = (await client.get(f"{API}/events/{event.id}/ticket-types")).json()
    assert body["ticketing"]["types"] == public["types"]
    assert len([s for s in seen if "FROM ticket_types" in s and "ticket_types.event_id =" in s]) == 1
    assert not any("FROM event_ratings_link" in s and "event_ratings_link.user_id =" in s and "FROM events" not in s for s in seen)
    assert len(seen) <= STATEMENTS_PER_VIEW, len(seen)
