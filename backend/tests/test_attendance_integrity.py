"""The door, registration, ID cards, kiosks and ticket ids, held to account
(the attendance audit, migration attendance_integrity).

- The scan reads one event's registrations off unique indexes, never the
  whole table, and never admits on an admission number.
- What a check-in sets off runs after the answer, with FCM on a thread, and
  the club's badges read counters.
- Races (the legacy check-in, a one-card rule, the self check-in tries,
  door names) are settled by the database.
- Kiosk PINs are hashed and rate-limited; gate and kiosk keys come in headers.
- Registration: one lock order, a second removal, case-blind addresses,
  bounded and typed answers, the import's missing event, the attendee list
  on an index, analytics in SQL, the export off the event loop.
- Ticket ids: ten characters from a CSPRNG, retried on a clash; old ones work.

The concurrency tests run on connections of their own and commit, as
concurrent requests do; the rolled-back test session cannot show a race.
They clean up after themselves.
"""
import asyncio
import logging
import re
import threading
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, event as sa_event, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.checkins import after as after_mod
from app.api.checkins import doors as doors_mod
from app.api.checkins import service as checkins
from app.api.checkins.gate_auth import GateActor
from app.api.checkins.models import CheckIns, Doors
from app.api.clubs.models import Clubs
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.idcards import service as idcards
from app.api.idcards.models import PrintJobs, PrintSource, ReprintPolicy
from app.api.kiosk import self_service
from app.api.kiosk.models import KioskDevices, SelfCheckInSessions
from app.api.users.models import UserTypes, Users
from app.core.utils.keys import TICKET_ID, generate_ticket_id
from app.db.core import SESSION_KWARGS
from app.db.listeners import add_loader_criteria
from app.response import CustomHTTPException
from tests.conftest import _make_user, auth_header

START = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
API = "/api/v1"


# ─── helpers ──────────────────────────────────────────────────────────────


@contextmanager
def captured(session):
    """Every statement the test session's connection runs, with its parameters."""
    seen: list[tuple[str, object]] = []
    conn = session.bind.sync_connection

    def grab(_conn, _cursor, statement, parameters, _context, _executemany):
        seen.append((statement, parameters))

    sa_event.listen(conn, "before_cursor_execute", grab)
    try:
        yield seen
    finally:
        sa_event.remove(conn, "before_cursor_execute", grab)


async def plan_of(session, statement, parameters) -> str:
    conn = await session.connection()
    rows = (await conn.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
    return "\n".join(r[0] for r in rows)


def _scans_of(node, table):
    if node.get("Relation Name") == table:
        yield node
    for child in node.get("Plans", []):
        yield from _scans_of(child, table)


#: Every table a scan's statements read, with the check after it (the guest's
#: note and badges, the club's badges): ANALYZEd by a_platform_sized_table, so
#: no plan in these tests rests on what autovacuum last measured.
PLANNED = (
    "event_registrations_link", "events", "clubs", "users", "check_ins", "doors",
    "badge_definitions", "user_badges", "club_badges", "notifications",
)


async def _analyze(session) -> None:
    for table in PLANNED:
        await session.execute(text(f"ANALYZE {table}"))


@asynccontextmanager
async def a_platform_sized_table(session, make_club, make_event, rows: int = 1500, events: int = 300):
    """A platform's worth of rows around the test's world, every table the
    scan reads ANALYZEd, for a test that reads query plans.

    The plans of a world of a few rows are ties, and the planner broke them
    by whatever it had last been told, which the test did not decide:

    - On a few registrations a walk of a whole index that filters each row
      and a probe of the ticket's unique index both came to 8.15; the
      planner took either, by what autovacuum had last measured (#113 gave
      registrations their platform's size).
    - The check after a check-in reads registrations once more, joined to
      the events, to count the club's attended guests -- but only while the
      club has a badge still to earn (badges.service._get_club_metrics). So
      whether that read was there at all hung on the badge definitions in
      the database. When it was, the club held half the events there were,
      and the events table's statistics were autovacuum's: the planner read
      registrations through a bitmap -- by event, or over every live row.

    So the world here is decided by the test: 300 other events (the club
    holds 2 of 302), 1,500 other registrations spread over them, half of
    them attended, a club badge always still to earn, and every table in
    PLANNED ANALYZEd. Production's shape, where the probes win by far.

    The rows are the test's own (rolled back with it); ANALYZE's figures
    are not, so the tables are measured again without them before the test
    ends."""
    from app.api.badges.models import BadgeDefinitions, BadgeType

    owner, club = await make_club(name="Elsewhere", email="filler-club@example.com")
    first = await make_event(club=club, created_by=owner, name="Somewhere Else", event_datetime=START - timedelta(days=30))
    others = list((await session.scalars(
        insert(Events).returning(Events.id),
        [dict(slug=f"filler-night-{i}-{uuid.uuid4().hex[:8]}", name=f"Filler Night {i}", images=[], duration=2.0,
              event_datetime=START - timedelta(days=30 + i % 90), club_id=club.id, category_id=first.category_id) for i in range(1, events)],
    )).all())
    filler_events = [first.id, *others]
    users = list((await session.scalars(
        insert(Users).returning(Users.id),
        [dict(full_name=f"Filler {i}", username=f"filler-{i}", email=f"filler-{i}@example.com", user_type=UserTypes.app_user) for i in range(rows)],
    )).all())
    await session.execute(
        insert(EventRegistrationsLink),
        [dict(event_id=filler_events[i % len(filler_events)], user_id=u, full_name="Filler", email=f"filler-{u}@example.com",
              ticket_id=generate_ticket_id(), is_attended=i % 2 == 0) for i, u in enumerate(users)],
    )
    # A badge the world's club has still to earn, as clubs on a platform do:
    # the check after a check-in then always counts the club's guests.
    badge = BadgeDefinitions(slug=f"filler-crowd-{uuid.uuid4().hex[:8]}", name="Crowd", description="A thousand in", emoji="*",
                             badge_type=BadgeType.club, trigger_metric="total_attendees", threshold=1000)
    session.add(badge)
    await session.flush()
    await _analyze(session)
    try:
        yield
    finally:
        await session.execute(delete(EventRegistrationsLink).where(EventRegistrationsLink.event_id.in_(filler_events)))
        await session.execute(delete(Events).where(Events.id.in_(others)))
        await session.execute(delete(Users).where(Users.id.in_(users)))
        await session.execute(delete(BadgeDefinitions).where(BadgeDefinitions.id == badge.id))
        await _analyze(session)


KEYS = ("ticket_id", "(id =", "event_id", "user_id")


def _probes_by_key(scan, keys=KEYS) -> bool:
    """A read of a table by one of `keys` through an index: an index scan
    whose condition names one, or a bitmap whose every index scan does. Not
    a sequential scan, and not a walk of an index filtering every row."""
    if scan["Node Type"] in ("Index Scan", "Index Only Scan"):
        return any(k in scan.get("Index Cond", "") for k in keys)
    if scan["Node Type"] == "Bitmap Heap Scan":
        def index_scans(node):
            if node["Node Type"] == "Bitmap Index Scan":
                yield node
            for child in node.get("Plans", []):
                yield from index_scans(child)

        found = list(index_scans(scan))
        return bool(found) and all(any(k in n.get("Index Cond", "") for k in keys) for n in found)
    return False


async def probes_registrations_by_key(session, statements, keys=KEYS) -> int:
    """EXPLAIN every statement that reads registrations, with sequential
    scans priced out, and require each read of the table to be an index probe
    on the code looked for, the event, the guest or the row -- not a
    sequential scan, and not a walk of some other index (is_deleted's, say)
    filtering every row on the platform. Run on a_platform_sized_table: on a
    world of a few rows a walk and a probe cost the same. (The planner may
    take the event's index over the ticket's: both are one event's rows at
    most; the probe for another event's ticket has only the ticket's. The
    guest's badges, checked after the answer, read their own registrations
    by user; the club's, its attended guests event by event.)"""
    import json

    await session.execute(text("SET LOCAL enable_seqscan = off"))
    conn = await session.connection()
    probed = 0
    for statement, parameters in statements:
        if "event_registrations_link" not in statement or not statement.lstrip().upper().startswith("SELECT"):
            continue
        raw = (await conn.exec_driver_sql("EXPLAIN (FORMAT JSON) " + statement, parameters)).scalar()
        plan = (raw if isinstance(raw, list) else json.loads(raw))[0]["Plan"]
        for scan in _scans_of(plan, "event_registrations_link"):
            assert _probes_by_key(scan, keys), f"{statement}\n{json.dumps(scan, indent=1)}"
            probed += 1
    return probed


class Gate:
    """Lets `n` tasks through together, or after a timeout (so a test that
    would deadlock fails rather than hangs)."""

    def __init__(self, n: int, timeout: float = 3.0):
        self.n, self.timeout, self.arrived, self.go = n, timeout, 0, asyncio.Event()

    async def wait(self):
        self.arrived += 1
        if self.arrived >= self.n:
            self.go.set()
        try:
            await asyncio.wait_for(self.go.wait(), self.timeout)
        except asyncio.TimeoutError:
            pass


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="integrity@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0)
    other = await make_event(club=club, created_by=owner, name="Demo Day", event_datetime=START + timedelta(days=7), duration=2.0)
    a = await make_user(email="ananya-int@example.com")
    b = await make_user(email="bala-int@example.com")
    c = await make_user(email="chitra-int@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", admission_no=f"MOA-0001-E{event.id}")
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan", admission_no=f"MOA-0002-E{event.id}")
    r_c = await make_registration(event=other, user=c, full_name="Chitra Nair")
    event.admission_seq = 2
    await session.commit()
    return {"event": event, "other": other, "club": club, "owner": owner, "a": r_a, "b": r_b, "c": r_c, "h": auth_header(await mint_token(owner)), "users": (a, b, c)}


def _scan(code, ref=None):
    return {"code": code, "door": "Gate A", "client_ref": ref or uuid.uuid4().hex}


# ─── 1. The scan reads one event, off an index ────────────────────────────


async def test_a_scan_reads_registrations_only_through_an_index(client, session, world, make_club, make_event):
    url = f"{API}/events/{world['event'].id}/gate/scan"
    async with a_platform_sized_table(session, make_club, make_event):
        with captured(session) as hit:
            ok = await client.post(url, json=_scan(world["a"].ticket_id.lower()), headers=world["h"])
        assert ok.json()["outcome"] == "CHECKED_IN"
        # The ticket, then after the answer the guest's registrations and the club's attended guests.
        assert any("JOIN events" in st and "is_attended" in st for st, _ in hit), "the club's badges were not checked?"
        assert await probes_registrations_by_key(session, hit) >= 3, "the scan read no registrations?"
        # A miss on this event looks for the code elsewhere (WRONG_EVENT), on the same index.
        with captured(session) as hit:
            wrong = await client.post(url, json=_scan(world["c"].ticket_id), headers=world["h"])
            nothing = await client.post(url, json=_scan("MOA-NOPE0-00000"), headers=world["h"])
        assert wrong.json()["outcome"] == "WRONG_EVENT" and wrong.json()["other_event"]["name"] == "Demo Day"
        assert nothing.json()["outcome"] == "INVALID"
        assert await probes_registrations_by_key(session, hit) >= 4


async def test_an_admission_number_is_unique_on_its_event_in_any_case(session, world, make_user, make_registration):
    d = await make_user(email="dup-adm@example.com")
    e = await make_user(email="dup-adm2@example.com")
    number = world["a"].admission_no
    # The same number on another event is fine ("include code" off makes MOA-0042 everywhere).
    await make_registration(event=world["other"], user=d, admission_no=number)
    # A retired holder does not count.
    await make_registration(event=world["event"], user=d, admission_no=number.lower(), is_deleted=True)
    nested = await session.begin_nested()
    with pytest.raises(IntegrityError):
        await make_registration(event=world["event"], user=e, admission_no=number.lower())
    await nested.rollback()


async def test_an_admission_number_admits_nobody_at_a_kiosk_or_from_the_app(client, session, world, make_user, mint_token):
    from app.api.events.volunteer.models import Volunteer

    h = world["h"]
    paired = await client.post(f"{API}/events/{world['event'].id}/kiosks", json={"name": "Lobby", "shift_hours": 8}, headers=h)
    kiosk = {"X-Kiosk-Token": paired.json()["token"]}
    r = await client.post(f"{API}/kiosk/redeem", json={"code": world["a"].admission_no, "client_ref": uuid.uuid4().hex}, headers=kiosk)
    assert r.json()["outcome"] == "NO_MATCH"
    vol = await make_user(email="vol-int@example.com")
    session.add(Volunteer(email=vol.email, event_id=world["event"].id, user_id=vol.id, is_approved=True))
    await session.commit()
    v = auth_header(await mint_token(vol))
    app_scan = await client.post(f"{API}/events/volunteer/checkin/{world['event'].id}", json={"ticket_id": world["b"].admission_no}, headers=v)
    assert app_scan.status_code == 400 and app_scan.json()["error_code"] == "REGISTRATION_NOT_FOUND"
    assert (await client.post(f"{API}/events/volunteer/checkin/{world['event'].id}", json={"ticket_id": world["b"].ticket_id}, headers=v)).status_code == 200


async def test_tickets_minted_before_keep_working(client, session, world, make_user, make_registration):
    old = await make_registration(event=world["event"], user=await make_user(email="old-ticket@example.com"), full_name="Old Ticket", ticket_id="MOA-2BCEBA8-PFZG3")
    odd = await make_registration(event=world["event"], user=await make_user(email="odd-ticket@example.com"), full_name="Odd Ticket", ticket_id="moa-legacy-x1")
    await session.commit()
    url = f"{API}/events/{world['event'].id}/gate/scan"
    assert (await client.post(url, json=_scan("moa-2bceba8-pfzg3"), headers=world["h"])).json()["attendee"]["id"] == str(old.id)
    # One not minted upper case is still found as typed.
    assert (await client.post(url, json=_scan("moa-legacy-x1"), headers=world["h"])).json()["attendee"]["id"] == str(odd.id)
    from app.api.events.certificates import certificate_number, ticket_id_of_certificate

    assert ticket_id_of_certificate(certificate_number(old.ticket_id)) == old.ticket_id


# ─── 3. After the answer, off the loop; club badges off counters ─────────


async def test_the_scan_answers_before_the_check_in_hooks_and_fcm_runs_on_a_thread(client, session, world, monkeypatch):
    from app.api.notifications import service as notes
    from app.core.notifications import service as push

    order: list[str] = []
    loop_thread = threading.get_ident()
    sent_on: list[int] = []
    real_scan = checkins.scan

    async def scan(*args, **kwargs):
        out = await real_scan(*args, **kwargs)
        order.append("answered")
        return out

    async def create_notification(**kwargs):
        order.append("noted")

    async def token(_session, _user_id):
        return "fcm-token"

    async def send(token, title, body, data=None, dead=None):
        sent_on.append(threading.get_ident())
        return True

    monkeypatch.setattr(checkins, "scan", scan)
    monkeypatch.setattr(notes, "create_notification", create_notification)
    monkeypatch.setattr(push, "get_fcm_token_for_user", token)
    monkeypatch.setattr(push, "_send_to_token", send)
    r = await client.post(f"{API}/events/{world['event'].id}/gate/scan", json=_scan(world["a"].ticket_id), headers=world["h"])
    assert r.json()["outcome"] == "CHECKED_IN"
    assert order == ["answered", "noted"]
    assert sent_on and sent_on[0] != loop_thread


async def test_club_badges_read_the_follower_counter(session, world):
    from app.api.badges.models import BadgeDefinitions, BadgeType, ClubBadges
    from app.api.badges.service import check_and_award_club_badges

    club_id = world["club"].id
    session.add(BadgeDefinitions(slug="int-fans-50", name="Fans", description="50 followers", emoji="*", badge_type=BadgeType.club, trigger_metric="follower_count", threshold=50))
    session.add(BadgeDefinitions(slug="int-crowd-2", name="Crowd", description="2 in", emoji="*", badge_type=BadgeType.club, trigger_metric="total_attendees", threshold=2))
    await session.execute(update(Clubs).where(Clubs.id == club_id).values(follower_count=50))
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.event_id == world["event"].id).values(is_attended=True))
    await session.flush()
    session.expire_all()
    got = await check_and_award_club_badges(session, club_id)
    assert set(got) == {"int-fans-50", "int-crowd-2"}
    assert await session.scalar(select(func.count()).select_from(ClubBadges).where(ClubBadges.club_id == club_id)) == 2
    # Nothing left to earn: nothing is counted.
    assert await check_and_award_club_badges(session, club_id) == []


# ─── concurrency worlds ───────────────────────────────────────────────────


async def _committed_world(make, tag, *, guests=2, **event_values):
    async with make() as s:
        owner = await _make_user(s, email=f"{tag}-owner@example.com", user_type=UserTypes.club, username=f"{tag}owner")
        club = Clubs(slug=f"{tag}-club", name=f"{tag} Club", user_id=owner.id, is_verified=True)
        s.add(club)
        await s.flush()
        category = EventCategories(name=tag, created_by_id=owner.id)
        s.add(category)
        await s.flush()
        ev = Events(slug=f"{tag}-night", name=f"{tag} Night", images=[], duration=3.0, event_datetime=datetime.now(timezone.utc), club_id=club.id, category_id=category.id, **event_values)
        s.add(ev)
        await s.flush()
        users, regs = [], []
        for i in range(guests):
            u = await _make_user(s, email=f"{tag}-g{i}@example.com", username=f"{tag}g{i}")
            r = EventRegistrationsLink(event_id=ev.id, user_id=u.id, full_name=f"Guest {i}", email=u.email, ticket_id=generate_ticket_id())
            s.add(r)
            users.append(u.id)
            regs.append(r)
        await s.commit()
        return dict(owner=owner.id, club=club.id, category=category.id, event=ev.id, users=users, regs=[r.id for r in regs])


async def _drop(make, w):
    async with make() as s:
        regs = select(EventRegistrationsLink.id).where(EventRegistrationsLink.event_id == w["event"])
        await s.execute(delete(PrintJobs).where(PrintJobs.event_id == w["event"]))
        await s.execute(delete(CheckIns).where(CheckIns.event_id == w["event"]))
        await s.execute(delete(SelfCheckInSessions).where(SelfCheckInSessions.event_id == w["event"]))
        await s.execute(delete(KioskDevices).where(KioskDevices.event_id == w["event"]))
        await s.execute(delete(Doors).where(Doors.event_id == w["event"]))
        from app.api.competitions.models import Competitions

        await s.execute(delete(Competitions).where(Competitions.event_id == w["event"]))
        await s.execute(delete(EventRegistrationsLink).where(EventRegistrationsLink.id.in_(regs)))
        await s.execute(delete(Events).where(Events.id == w["event"]))
        await s.execute(delete(EventCategories).where(EventCategories.id == w["category"]))
        from app.api.subscriptions.models import ClubSubscriptions

        await s.execute(delete(ClubSubscriptions).where(ClubSubscriptions.club_id == w["club"]))
        await s.execute(delete(Clubs).where(Clubs.id == w["club"]))
        await s.execute(delete(Users).where(Users.id.in_([w["owner"], *w["users"]])))
        await s.commit()


@pytest.fixture
def make(_engine, monkeypatch):
    # The check-in hooks are not what these tests are about, and would leave
    # jobs (and, run, rows) for the committed guests behind.
    async def no_hooks(*_a, **_k):
        return None

    monkeypatch.setattr(after_mod, "enqueue", no_hooks)

    def factory():
        s = async_sessionmaker(bind=_engine, **SESSION_KWARGS)()
        add_loader_criteria(s)
        return s

    return factory


async def _event(s, event_id):
    from sqlalchemy.orm import joinedload

    return await s.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == event_id))


# ─── 4. Races settled by the database ─────────────────────────────────────


async def test_two_doors_scanning_a_guest_marked_present_by_hand_both_get_an_answer(make, monkeypatch):
    w = await _committed_world(make, "legacyrace", guests=1)
    try:
        async with make() as s:
            await s.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.id == w["regs"][0]).values(is_attended=True, attended_on=datetime.now(timezone.utc)))
            await s.commit()
        gate = Gate(2)
        real = checkins.check_in_of
        first: set = set()

        async def check_in_of(session, registration_id):
            row = await real(session, registration_id)
            if id(session) not in first:
                first.add(id(session))
                await gate.wait()
            return row

        monkeypatch.setattr(checkins, "check_in_of", check_in_of)

        async def door(name):
            async with make() as s:
                ev = await _event(s, w["event"])
                reg = await s.get(EventRegistrationsLink, w["regs"][0])
                return await checkins.scan(s, ev, code=reg.ticket_id, door_label=name, client_ref=uuid.uuid4().hex, scanned_at=None, actor=GateActor(kind="door", name=name, full=True))

        outcomes = await asyncio.gather(door("Gate A"), door("Gate B"), return_exceptions=True)
        assert [o["outcome"] if isinstance(o, dict) else repr(o) for o in outcomes] == ["ALREADY_CHECKED_IN", "ALREADY_CHECKED_IN"]
        async with make() as s:
            assert await s.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.event_id == w["event"])) == 1
    finally:
        await _drop(make, w)


async def test_a_one_card_rule_prints_one_card_when_two_doors_print_at_once(make, monkeypatch):
    w = await _committed_world(make, "cardrace", guests=1, reprint_policy=ReprintPolicy.never)
    try:
        gate = Gate(2)
        real = idcards.registration_of

        async def registration_of(session, event, registration_id):
            reg = await real(session, event, registration_id)
            await gate.wait()
            return reg

        monkeypatch.setattr(idcards, "registration_of", registration_of)

        async def door(name):
            async with make() as s:
                ev = await _event(s, w["event"])
                try:
                    await idcards.card_pdf(s, ev, w["regs"][0], actor=GateActor(kind="door", name=name), source=PrintSource.checkin, record=True)
                    return "printed"
                except CustomHTTPException as exc:
                    return exc.error_code

        assert sorted(await asyncio.gather(door("Gate A"), door("Gate B"))) == ["REPRINT_REFUSED", "printed"]
        async with make() as s:
            assert await s.scalar(select(func.count()).select_from(PrintJobs).where(PrintJobs.event_id == w["event"])) == 1
    finally:
        await _drop(make, w)


async def test_parallel_guesses_at_a_self_check_in_code_share_three_tries(make, monkeypatch):
    w = await _committed_world(make, "coderace", guests=1, self_check_in_enabled=True)
    try:
        sid = "s-" + uuid.uuid4().hex
        async with make() as s:
            kiosk = KioskDevices(id=uuid.uuid4(), event_id=w["event"], name="K", door_label="K", token_hash=uuid.uuid4().hex, token_prefix="kkkkkk", expires_at=datetime.now(timezone.utc) + timedelta(hours=2))
            s.add(kiosk)
            await s.flush()
            s.add(SelfCheckInSessions(
                id=sid, event_id=w["event"], kiosk_id=kiosk.id, registration_id=w["regs"][0], code_hash=self_service._code_hash("123456"),
                code_sent_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            ))
            await s.commit()
        gate = Gate(6)
        real = self_service._load

        async def load(session, session_id):
            got = await real(session, session_id)
            await gate.wait()
            return got

        monkeypatch.setattr(self_service, "_load", load)

        async def guess(code):
            async with make() as s:
                return await self_service.verify(s, sid, code)

        answers = await asyncio.gather(*(guess(f"00000{i}") for i in range(6)))
        assert sum(a["outcome"] == "WRONG_CODE" for a in answers) == 2
        assert sum(a["outcome"] == "LOCKED" for a in answers) == 4
        async with make() as s:
            assert await s.scalar(select(SelfCheckInSessions.code_attempts).where(SelfCheckInSessions.id == sid)) == 3
    finally:
        await _drop(make, w)


async def test_two_first_scans_at_a_new_door_spelled_two_ways_make_one_door(make, monkeypatch):
    w = await _committed_world(make, "doorrace", guests=0)
    try:
        gate = Gate(2)
        real = doors_mod.by_name
        seen: set = set()

        async def by_name(session, event_id, name):
            door = await real(session, event_id, name)
            if id(session) not in seen:
                seen.add(id(session))
                await gate.wait()
            return door

        monkeypatch.setattr(doors_mod, "by_name", by_name)

        async def resolve(label):
            async with make() as s:
                door = await doors_mod.resolve(s, w["event"], label)
                await s.commit()
                return door.id

        ids = await asyncio.gather(resolve("Gate X"), resolve("gate x"))
        assert ids[0] == ids[1]
        async with make() as s:
            assert await s.scalar(select(func.count()).select_from(Doors).where(Doors.event_id == w["event"])) == 1
    finally:
        await _drop(make, w)


async def test_a_door_name_is_unique_in_any_case(client, session, world):
    url = f"{API}/events/{world['event'].id}/doors"
    assert (await client.post(url, json={"name": "Gate A", "kind": "door"}, headers=world["h"])).status_code == 200
    nested = await session.begin_nested()
    session.add(Doors(id=uuid.uuid4(), event_id=world["event"].id, name="GATE A", kind="door"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await nested.rollback()


async def test_numbering_everyone_is_one_statement_and_one_bump(client, session, world, make_user, make_registration):
    for i in range(5):
        await make_registration(event=world["event"], user=await make_user(email=f"unnumbered{i}@example.com"), full_name=f"U {i}")
    await session.commit()
    with captured(session) as hit:
        done = await client.post(f"{API}/events/{world['event'].id}/admission/number-all", headers=world["h"])
    assert done.status_code == 200 and done.json()["numbered"] == 5
    writes = [s for s, _ in hit if s.lstrip().upper().startswith(("UPDATE", "WITH"))]
    assert len(writes) == 1, writes
    numbers = (await session.execute(select(EventRegistrationsLink.admission_no).where(EventRegistrationsLink.event_id == world["event"].id).order_by(EventRegistrationsLink.created_at, EventRegistrationsLink.id))).scalars().all()
    assert len(set(numbers)) == 7 and done.json()["first"] == f"MOA-0003-E{world['event'].id}" and done.json()["last"] == f"MOA-0007-E{world['event'].id}"


async def test_the_sql_admission_format_matches_in_the_database(session):
    from app.api.admission.service import SQL_NUMBER
    from app.core.layout.admission import ADMISSION_PREFIX_DEFAULT, admission_number

    for prefix, digits, include, seq in (("", 4, True, 7), ("GDDB", 3, False, 12345), ("X-Y", 2, True, 99)):
        got = await session.scalar(
            text(f"SELECT {SQL_NUMBER} FROM (SELECT CAST(:seq AS bigint) AS seq) s"),
            {"prefix": prefix, "default_prefix": ADMISSION_PREFIX_DEFAULT, "digits": digits, "include_code": include, "code": "E9", "seq": seq},
        )
        assert got == admission_number({"prefix": prefix or None, "digits": digits, "includeCode": include}, "E9", seq)


# ─── 5. Kiosk and gate credentials ────────────────────────────────────────


async def test_the_kiosk_pin_is_hashed_and_five_wrong_tries_lock_the_kiosk(client, session, world):
    h = world["h"]
    base = f"{API}/events/{world['event'].id}/kiosks"
    paired = await client.post(base, json={"name": "Lobby", "shift_hours": 8}, headers=h)
    k = {"X-Kiosk-Token": paired.json()["token"]}
    assert (await client.patch(f"{base}/settings", json={"kiosk_pin": "2468"}, headers=h)).json()["has_pin"] is True
    stored = await session.scalar(select(Events.kiosk_pin_hash).where(Events.id == world["event"].id))
    assert stored.startswith("pbkdf2_sha256$") and "2468" not in stored
    assert (await client.post(f"{API}/kiosk/staff", json={"pin": "2468"}, headers=k)).status_code == 200
    for _ in range(5):
        assert (await client.post(f"{API}/kiosk/staff", json={"pin": "0000"}, headers=k)).status_code == 403
    locked = await client.post(f"{API}/kiosk/staff", json={"pin": "2468"}, headers=k)
    assert locked.status_code == 429 and locked.json()["error_code"] == "PIN_LOCKED"
    # The window passes: the right PIN opens it again.
    await session.execute(update(KioskDevices).where(KioskDevices.id == uuid.UUID(paired.json()["device"]["id"])).values(pin_window_at=datetime.now(timezone.utc) - timedelta(minutes=16)))
    await session.flush()
    session.expire_all()
    assert (await client.post(f"{API}/kiosk/staff", json={"pin": "2468"}, headers=k)).status_code == 200


async def test_gate_keys_come_in_a_header_and_only_a_framed_card_may_carry_one(client, session, world):
    door = await client.post(f"{API}/events/{world['event'].id}/doors", json={"name": "Loading bay", "kind": "door"}, headers=world["h"])
    key = (await client.post(f"{API}/events/{world['event'].id}/doors/{door.json()['id']}/link", json={"can_walkup": False}, headers=world["h"])).json()["key"]
    ctx = f"{API}/events/{world['event'].id}/gate/context"
    assert (await client.get(ctx, headers={"X-Gate-Key": key})).status_code == 200
    assert (await client.get(ctx, params={"key": key})).status_code == 401
    assert (await client.post(f"{API}/events/{world['event'].id}/gate/scan", params={"key": key}, json=_scan(world["a"].ticket_id))).status_code == 401
    # The console's card frame, deprecated: still served while it prints that way.
    card = await client.get(f"{API}/events/{world['event'].id}/idcards/{world['a'].id}.pdf", params={"key": key, "record": "false"})
    assert card.status_code == 200 and card.content[:4] == b"%PDF"


async def test_a_ticket_check_that_misses_reads_and_logs_no_other_guests_ticket(client, session, world, make_user, mint_token, caplog):
    from app.api.events.volunteer.models import Volunteer

    vol = await make_user(email="vol-miss@example.com")
    session.add(Volunteer(email=vol.email, event_id=world["event"].id, user_id=vol.id, is_approved=True))
    await session.commit()
    v = auth_header(await mint_token(vol))
    with caplog.at_level(logging.INFO), captured(session) as hit:
        miss = await client.post(f"{API}/events/volunteer/validate/{world['event'].id}", json={"ticket_id": "MOA-ZZZZZ-ZZZZZ"}, headers=v)
    assert miss.json()["error_code"] == "REGISTRATION_NOT_FOUND"
    assert world["a"].ticket_id not in caplog.text and world["b"].ticket_id not in caplog.text
    # Every read of registrations named the code it looked for.
    for statement, _ in hit:
        if "event_registrations_link" in statement and statement.lstrip().upper().startswith("SELECT"):
            assert "ticket_id =" in statement or "event_registrations_link.id =" in statement, statement
    wrong = await client.post(f"{API}/events/volunteer/validate/{world['event'].id}", json={"ticket_id": world["c"].ticket_id}, headers=v)
    assert wrong.json()["error_code"] == "TICKET_WRONG_EVENT"


# ─── 6. Registration ──────────────────────────────────────────────────────


async def test_retiring_an_abandoned_registration_takes_the_address_lock_first(client, session, make_club, make_event):
    owner, club = await make_club(name="Lock Club", email="lock-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Paid Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=3), has_fee=True, reg_fee=100)
    guest = await _make_user(session, email="walked-away@example.com", user_type=UserTypes.guest)
    session.add(EventRegistrationsLink(
        event_id=event.id, user_id=guest.id, full_name="Walked Away", email=guest.email, ticket_id=generate_ticket_id(),
        actual_amount=100, created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))
    await session.commit()
    with captured(session) as hit:
        r = await client.post(f"{API}/events/registration/{event.id}/register", json={"full_name": "Walked Back", "email": "Walked-Away@example.com", "phone": "9876543210"})
    assert r.status_code == 200, r.text
    statements = [s for s, _ in hit]
    lock = next(i for i, s in enumerate(statements) if "pg_advisory_xact_lock" in s)
    retire = next(i for i, s in enumerate(statements) if s.lstrip().startswith("UPDATE event_registrations_link") and "is_deleted" in s)
    assert lock < retire


async def test_a_guest_can_be_taken_off_the_list_twice(client, session, world, make_user):
    h = world["h"]
    ev = world["event"]
    url = f"{API}/events/registration/{ev.id}"
    for _ in range(2):
        added = await client.post(f"{url}/attendees", json={"full_name": "Twice Removed", "email": "twice@example.com", "role": "attendee"}, headers=h)
        assert added.status_code == 200, added.text
        gone = await client.delete(f"{url}/info/{added.json()['id']}", params={"confirm_slug": ev.slug}, headers=h)
        assert gone.status_code == 200, gone.text


async def test_an_address_in_another_case_is_the_same_sign_up(session, world, make_user):
    from app.api.events.registration import service as registration

    first = await _make_user(session, email="case-a@example.com", user_type=UserTypes.guest)
    second = await _make_user(session, email="CASE-A@example.com", user_type=UserTypes.guest, username="case-a-2")
    await registration.register_event(session, full_name="Case A", email="Case-A@example.com", event_id=world["event"].id, user_id=first.id, notify=False, claim_existing=False)
    with pytest.raises(CustomHTTPException) as refused:
        await registration.register_event(session, full_name="Case A", email="case-a@EXAMPLE.com", event_id=world["event"].id, user_id=second.id, notify=False, claim_existing=False)
    assert refused.value.error_code == "ALREADY_REGISTERED"


@pytest.fixture
async def asks(session, world):
    ev = world["event"]
    ev.additional_details = [
        {"key": "age", "label": "Age", "field_type": "number", "required": False},
        {"key": "mail", "label": "Work email", "field_type": "email", "required": False},
        {"key": "site", "label": "Portfolio", "field_type": "url", "required": False},
        {"key": "dob", "label": "Date of birth", "field_type": "date", "required": False},
        {"key": "tracks", "label": "Tracks", "field_type": "checkbox", "required": False, "options": ["AI", "Web", "IoT"]},
        {"key": "agree", "label": "I agree", "field_type": "checkbox", "required": False, "options": None},
    ]
    await session.commit()
    return ev


async def _register(client, mint_token, make_user, ev, email, answers):
    u = await make_user(email=email)
    return await client.post(f"{API}/events/registration/{ev.id}/register", json={"full_name": "Asked Guest", "email": email, "phone": "9876543210", "additional_details": answers}, headers=auth_header(await mint_token(u)))


async def test_answers_are_checked_for_their_type_and_a_checkbox_takes_a_list(client, asks, make_user, mint_token):
    ok = await _register(client, mint_token, make_user, asks, "typed-ok@example.com", {"age": "21", "mail": "me@work.in", "site": "example.com", "dob": "18/10/2000", "tracks": ["AI", "IoT"], "agree": "Yes"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["additional_details"]["tracks"] == ["AI", "IoT"]
    bad = await _register(client, mint_token, make_user, asks, "typed-bad@example.com", {"age": "twenty", "mail": "nope", "site": "not a site", "dob": "31/02/2026", "tracks": ["AI", "Blockchain"]})
    assert bad.status_code == 400 and bad.json()["error_code"] == "INVALID_ANSWERS"
    assert set(bad.json()["errors"]) == {"age", "mail", "site", "dob", "tracks"}


async def test_answers_to_an_event_with_no_questions_are_bounded(client, world, make_user, mint_token):
    huge = {f"q{i}": "x" for i in range(80)}
    r = await _register(client, mint_token, make_user, world["event"], "bounded@example.com", huge)
    assert r.status_code == 400 and r.json()["error_code"] == "INVALID_ANSWERS"
    long = await _register(client, mint_token, make_user, world["event"], "bounded2@example.com", {"essay": "x" * 5000})
    assert long.status_code == 400
    fine = await _register(client, mint_token, make_user, world["event"], "bounded3@example.com", {"college": "STIST"})
    assert fine.status_code == 200 and fine.json()["additional_details"] == {"college": "STIST"}


async def test_an_import_into_an_event_that_is_gone_says_so(session):
    from app.api.events.registration import service as registration
    from app.api.models import BackgroundTaskLogs
    from app.api.service import create_background_task_log
    import pandas as pd

    log = await create_background_task_log(session, "Import", "event_registrations_bulk_import")
    await session.flush()
    await registration.bulk_import_event_registrations(session, 987654321, pd.DataFrame([{"full_name": "A", "email": "a@example.com"}]), log)
    row = await session.get(BackgroundTaskLogs, log.id)
    assert row.status == "FAILED" and any("987654321 not found" in e["message"] for e in row.logs), row.logs


async def test_an_import_keeps_numbers_and_skips_blank_cells_in_the_answers(session, world):
    from app.api.events.registration import service as registration
    from app.api.service import create_background_task_log
    import pandas as pd

    log = await create_background_task_log(session, "Import", "event_registrations_bulk_import")
    await session.flush()
    sheet = pd.DataFrame([
        {"full_name": "Sheet One", "email": "sheet1@example.com", "additional_details_fields": "age,city", "age": 21, "city": None},
        {"full_name": "Sheet Two", "email": "sheet2@example.com", "additional_details_fields": "age,city", "age": 34, "city": "Kochi"},
    ])
    await registration.bulk_import_event_registrations(session, world["event"].id, sheet, log, notify=False)
    rows = dict((await session.execute(select(EventRegistrationsLink.email, EventRegistrationsLink.additional_details).where(EventRegistrationsLink.event_id == world["event"].id, EventRegistrationsLink.email.like("sheet%")))).all())
    assert rows == {"sheet1@example.com": {"age": 21}, "sheet2@example.com": {"age": 34, "city": "Kochi"}}, rows


async def test_the_attendee_list_pages_through_an_index_by_name(client, session, world):
    await session.commit()
    with captured(session) as hit:
        page = await client.get(f"{API}/events/registration/{world['event'].id}/list", params={"limit": 1, "offset": 1}, headers=world["h"])
    assert page.status_code == 200 and [r["full_name"] for r in page.json()["items"]] == ["Bala Krishnan"]
    listing = next(s for s in hit if "ORDER BY lower(event_registrations_link.full_name)" in s[0])
    await session.execute(text("SET LOCAL enable_sort = off"))
    plan = await plan_of(session, *listing)
    assert "ix_registrations_event_name" in plan, plan


async def test_event_analytics_are_counted_by_the_database(client, session, world, make_user, make_registration):
    ev = world["event"]
    morning = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
    night = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)  # 22:30 IST
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.id == world["a"].id).values(created_at=morning, is_paid=True, paid_amount=150, is_attended=True, attended_on=datetime(2026, 11, 20, 13, 10, tzinfo=timezone.utc), additional_details={"College Name": "STIST"}))
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.id == world["b"].id).values(created_at=night, additional_details={"college": ""}))
    await session.flush()
    with captured(session) as hit:
        r = await client.get(f"{API}/events/registration/{ev.id}/analytics", headers=world["h"])
    body = r.json()
    assert body["total_registrations"] == 2 and body["total_revenue"] == 150.0
    assert body["payment_status"] == {"paid": 1, "unpaid": 1} and body["attendance_status"] == {"attended": 1, "absent": 1}
    assert body["registration_time"] == {"morning": 1, "afternoon": 0, "evening": 0, "night": 1}
    assert body["top_institutions"] == [{"name": "STIST", "value": 1}, {"name": "Unknown", "value": 1}] and body["total_institutions"] == 1
    assert body["attendance_over_time"] == [{"time": "06 PM", "count": 1}]
    # Counted, never loaded row by row.
    reads = [s for s, _ in hit if "FROM event_registrations_link" in s]
    assert reads and all(re.search(r"count\(|GROUP BY", s) for s in reads), reads


async def test_the_export_is_written_off_the_event_loop_and_answers_stay_text(client, session, world, monkeypatch):
    from io import BytesIO

    import openpyxl

    from app.api.events.registration import export

    loop_thread = threading.get_ident()
    wrote_on: list[int] = []
    real = export._Writer.append

    def append(self, rows):
        wrote_on.append(threading.get_ident())
        return real(self, rows)

    monkeypatch.setattr(export._Writer, "append", append)
    ev = world["event"]
    ev.additional_details = [{"key": "tracks", "label": "Tracks", "field_type": "checkbox", "required": False, "options": ["AI", "Web"]}]
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.id == world["a"].id).values(additional_details={"tracks": ["AI", "Web"], "note": "=HYPERLINK(\"http://x\")"}))
    await session.commit()
    r = await client.get(f"{API}/events/registration/{ev.id}/export", headers=world["h"])
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert wrote_on and all(t != loop_thread for t in wrote_on)
    sheet = openpyxl.load_workbook(BytesIO(r.content)).active
    rows = list(sheet.iter_rows(values_only=False))
    header = [c.value for c in rows[0]]
    assert header[:6] == ["Ticket ID", "Full Name", "Email", "Phone", "tracks", "note"] and header[-4] == "Registered At"
    # The ticket type and the discount code come last (app/api/ticketing).
    assert header[-3:] == ["Ticket Type", "Discount Code", "Discount"]
    ananya = next(row for row in rows[1:] if row[1].value == "Ananya Iyer")
    assert ananya[4].value == "AI, Web"
    assert ananya[5].value.startswith("=HYPERLINK") and ananya[5].data_type == "s"


# ─── 7. Ticket ids ────────────────────────────────────────────────────────


def test_ticket_ids_are_ten_random_characters(monkeypatch):
    import random

    monkeypatch.setattr(random, "choices", lambda *a, **k: (_ for _ in ()).throw(AssertionError("random is not a CSPRNG")))
    ids = {generate_ticket_id() for _ in range(2000)}
    assert len(ids) == 2000 and all(TICKET_ID.match(i) for i in ids)


async def test_a_ticket_id_that_is_taken_is_minted_again(session, world, monkeypatch):
    from app.api.events.registration import service as registration

    taken = world["a"].ticket_id
    minted = iter([taken, taken, "MOA-FRESH-AAAAA"])
    monkeypatch.setattr(registration, "generate_ticket_id", lambda: next(minted))
    u = await _make_user(session, email="clash@example.com")
    data = await registration.register_event(session, full_name="Clash Guest", email=u.email, event_id=world["event"].id, user_id=u.id, notify=False)
    assert data["ticket_id"] == "MOA-FRESH-AAAAA"
    assert await session.scalar(select(func.count()).select_from(EventRegistrationsLink).where(EventRegistrationsLink.event_id == world["event"].id)) == 3
