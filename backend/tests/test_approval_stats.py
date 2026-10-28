"""Confirm-each events: the full numbers, and the loopholes closed
(docs/APPROVALS.md §7, contract approval-stats).

- The event summary's `requests` and `places`, on a free and a paid event.
- Places with holds; the public `seats_left`/`full` agreeing with the ticket picker.
- The lists' request figures, with no query a row.
- The waiting list off on confirm-each events.
- A request settled when its person registers another way (the form, the
  desk, an import, by hand), and when a confirmation finds them registered.
- A lapsed hold's unpaid registration retired.
- Deleting an event, and an event ending: the requests close, and the
  `request_unanswered` email goes once each.
- The requests' CSV export, the closeout, finance, sales, analytics and admin
  figures, and the "High demand" label.
"""
import csv
import io
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy import func, insert, select, update

from app.api.approvals import service as approvals
from app.api.approvals.models import RegistrationRequests as R
from app.api.events.models import EventRegistrationsLink, Events
from app.api.payments import service as payments
from app.api.ticketing import sweep, waitlist
from app.api.ticketing.models import WaitlistEntries, WaitlistSettings
from app.core import jobs
from tests.conftest import auth_header
from tests.test_approvals import _req, approve, ask, decline, listing, pushed  # noqa: F401
from tests.test_money_integrity import FakeRazorpay, rzp_fake  # noqa: F401
from tests.test_ticketing import register

API = "/api/v1"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def mail(monkeypatch):
    from app.core.email import email as email_mod

    sent = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    return sent


@pytest.fixture(autouse=True)
def _deliveries_in_this_session(monkeypatch, session):
    """The ticket sends a desk walk-up sets off run on the test's session."""
    from contextlib import asynccontextmanager

    from app.api.deliveries import service as deliveries

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(deliveries, "session_scope", scope)


async def _club_event(session, make_club, make_event, mint_token, *, tag: str, **values):
    owner, club = await make_club(name=f"{tag.title()} Club", email=f"{tag}-owner@example.com")
    values = {"event_datetime": NOW + timedelta(days=5), "requires_approval": True, **values}
    event = await make_event(club=club, created_by=owner, name=f"{tag.title()} Night", **values)
    await session.commit()
    return SimpleNamespace(owner=owner, club=club, event=event, h=auth_header(await mint_token(owner)))


def _sent_to(mail, email) -> list:
    return [m for m in mail if m["recipients"] == [email]]


async def _summary(client, w) -> dict:
    r = await client.get(f"{API}/events/{w.event.id}/summary", headers=w.h)
    assert r.status_code == 200, r.text
    return r.json()


async def _count(session, model, *where) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _token_of(session, rid) -> str:
    return (await _req(session, rid)).token


async def _claim(client, session, rid) -> dict:
    """The confirmed paid request's claim: its unpaid registration."""
    view = (await client.get(f"{API}/registration-requests/{await _token_of(session, rid)}")).json()
    r = await client.post(f"{API}/waitlist/offers/{view['pay']['claim_token']}/claim", json={})
    assert r.status_code == 200, r.text
    return r.json()


async def _pay(client, session, rzp_fake, w, claimed: dict) -> None:
    order = (await client.post(
        f"{API}/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": w.event.id, "event_registration_id": claimed["event_registration_id"]}},
        headers=auth_header(claimed["auth_token"]["access_token"]),
    )).json()
    p = rzp_fake.pay(order["razorpay_order_id"])
    await payments.verify_razorpay_payment(session, order["razorpay_order_id"], p["id"], payment_details=p, expand_payment_details=False)


# ─── The summary's numbers ───────────────────────────────────────────────


async def test_every_summary_number_on_a_free_event(client, session, make_club, make_event, mint_token, mail):
    w = await _club_event(session, make_club, make_event, mint_token, tag="freesum", max_participants=10)
    ids = [(await ask(client, w.event, 300 + i)).json()["approval"]["id"] for i in range(5)]
    out = (await approve(client, w, ids=ids[:2])).json()
    assert [a["id"] for a in out["approved"]] == ids[:2]
    await decline(client, w, ids=[ids[2]], notify=False)
    assert (await client.post(f"{API}/registration-requests/{await _token_of(session, ids[3])}/withdraw")).status_code == 200
    # Added by hand: a registration that came through no request.
    added = await client.post(
        f"{API}/events/registration/{w.event.id}/attendees", json={"full_name": "Direct Person", "email": "direct-free@example.com"},
        headers=w.h,
    )
    assert added.status_code == 200, added.text
    # One confirmed person was let in.
    first_reg = (await _req(session, ids[0])).registration_id
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.id == first_reg).values(is_attended=True))
    await session.commit()

    s = await _summary(client, w)
    req = s["requests"]
    oldest = (await _req(session, ids[4])).submitted_at
    assert req["enabled"] is True
    assert {k: req[k] for k in ("total", "pending", "approved", "declined", "withdrawn", "lapsed", "closed")} == {
        "total": 5, "pending": 1, "approved": 2, "declined": 1, "withdrawn": 1, "lapsed": 0, "closed": 0,
    }
    assert (req["approved_paid"], req["approved_awaiting_payment"], req["awaiting_payment_amount"]) == (0, 0, 0.0)
    assert req["earliest_hold_expires_at"] is None
    assert req["checked_in"] == 1
    assert datetime.fromisoformat(req["oldest_pending_at"]) == oldest
    assert req["avg_decision_hours"] is not None and req["avg_decision_hours"] >= 0
    # Asked (approved) + added directly = registered: nothing counted twice.
    assert req["added_directly"] == 1 and s["registered"] == 3 == req["approved"] + req["added_directly"]
    assert s["requests_pending"] == 1
    assert s["places"] == {"capacity": 10, "taken": 3, "held": 0, "left": 7}

    # An event that never confirmed each place, with no requests: no block.
    plain = await _club_event(session, make_club, make_event, mint_token, tag="plainsum", requires_approval=False)
    p = await _summary(client, plain)
    assert p["requests"] is None and p["places"] == {"capacity": None, "taken": 0, "held": 0, "left": None}
    # Switched off later, its requests still count.
    await session.execute(update(Events).where(Events.id == w.event.id).values(requires_approval=False))
    await session.commit()
    off = (await _summary(client, w))["requests"]
    assert off["enabled"] is False and off["total"] == 5


async def test_every_summary_number_on_a_paid_event_with_holds(client, session, make_club, make_event, mint_token, mail, rzp_fake, pushed):
    w = await _club_event(
        session, make_club, make_event, mint_token, tag="paidsum", has_fee=True, reg_fee=Decimal("199.00"), max_participants=5,
    )
    ids = [(await ask(client, w.event, 320 + i)).json()["approval"]["id"] for i in range(4)]
    out = (await approve(client, w, ids=ids[:3])).json()
    assert len(out["approved"]) == 3 and all(a["pay_by"] for a in out["approved"])
    paid = await _claim(client, session, ids[0])
    await _pay(client, session, rzp_fake, w, paid)
    await _claim(client, session, ids[1])  # claimed, not paid: an unpaid registration

    s = await _summary(client, w)
    req = s["requests"]
    assert (req["total"], req["pending"], req["approved"]) == (4, 1, 3)
    assert (req["approved_paid"], req["approved_awaiting_payment"], req["awaiting_payment_amount"]) == (1, 2, 398.0)
    expiries = []
    for i in ids[1:3]:
        expiries.append((await session.get(WaitlistEntries, (await _req(session, i)).waitlist_entry_id)).offer_expires_at)
    first_expiry = min(expiries)
    assert datetime.fromisoformat(req["earliest_hold_expires_at"]) == first_expiry
    # Two registrations, both from requests (one paid, one claimed and owing).
    assert s["registered"] == 2 and req["added_directly"] == 0
    # The cap counts paid tickets; the two holds hold theirs; the pending one holds nothing.
    assert s["places"] == {"capacity": 5, "taken": 1, "held": 2, "left": 2}

    # Sales by type: the holds are `held`, never the waiting list's `offered`.
    sales = (await client.get(f"{API}/events/{w.event.id}/ticketing/sales", headers=w.h)).json()
    assert (sales["totals"]["held"], sales["totals"]["offered"]) == (2, 0)
    assert sum(t["held"] for t in sales["types"]) == 2

    # Finance: the unclaimed hold is expected; the claimed one is owed. Never both.
    fin = (await client.get(f"{API}/events/{w.event.id}/finance", headers=w.h)).json()
    f = fin["figures"]
    assert (f["expected_from_holds"], f["expected_from_holds_count"]) == (199.0, 1)
    assert (f["outstanding"], f["outstanding_count"]) == (199.0, 1)
    assert f["expected_from_holds"] + f["outstanding"] == req["awaiting_payment_amount"]
    club_fin = (await client.get(f"{API}/clubs/finance", headers=w.h)).json()
    row = next(r for r in club_fin["by_event"] if r["id"] == w.event.id) if club_fin["by_event"] else None
    if row is not None:
        assert row["expected_from_holds"] == 199.0


# ─── Places and the public pages ─────────────────────────────────────────


async def test_places_count_holds_and_the_public_seats_left_agree_with_the_picker(
    client, session, make_club, make_event, mint_token, mail, pushed,
):
    w = await _club_event(
        session, make_club, make_event, mint_token, tag="seats", has_fee=True, reg_fee=Decimal("99.00"), max_participants=3,
    )
    ids = [(await ask(client, w.event, 340 + i)).json()["approval"]["id"] for i in range(3)]
    await approve(client, w, ids=ids[:2])

    async def picker() -> int:
        return (await client.get(f"{API}/events/{w.event.id}/ticket-types")).json()["available"]

    async def page() -> dict:
        r = await client.get(f"{API}/discover/events/{w.event.slug}")
        assert r.status_code == 200, r.text
        return r.json()["event"]

    assert await picker() == 1
    e = await page()
    assert (e["seats_left"], e["status"], e["registration_open"]) == (1, "upcoming", True)
    assert (e["requires_approval"], e["high_demand"]) == (True, False)
    assert (await listing(client, w)).json()["places_left"] == 1
    await approve(client, w, ids=ids[2:])
    assert await picker() == 0
    e = await page()
    assert (e["seats_left"], e["status"], e["registration_open"]) == (0, "full", False)
    # The club's page's cards say so too.
    club_page = (await client.get(f"{API}/discover/clubs/{w.club.slug}")).json()
    card = next(c for c in club_page["upcoming"] if c["id"] == w.event.id)
    assert (card["status"], card["requires_approval"], card["high_demand"]) == ("full", True, False)

    # A paid event's cap counts paid tickets: two unpaid registrations of a
    # cap of two leave it open, as the picker says (going_count said full).
    open_ = await _club_event(
        session, make_club, make_event, mint_token, tag="unpaidcap", has_fee=True, reg_fee=Decimal("50.00"), max_participants=2,
        requires_approval=False,
    )
    for n in (360, 361):
        assert (await register(client, open_.event, n)).status_code == 200
    assert (await session.scalar(select(Events.going_count).where(Events.id == open_.event.id).execution_options(populate_existing=True))) == 2
    e = (await client.get(f"{API}/discover/events/{open_.event.slug}")).json()["event"]
    picker2 = (await client.get(f"{API}/events/{open_.event.id}/ticket-types")).json()["available"]
    assert (e["seats_left"], e["status"]) == (picker2, "upcoming") and picker2 == 2
    found = (await client.get(f"{API}/discover/events", params={"q": "Unpaidcap"})).json()["items"]
    assert [c["status"] for c in found if c["id"] == open_.event.id] == ["upcoming"]


# ─── The lists: fields, and no query a row ───────────────────────────────


class _Statements:
    def __init__(self, engine):
        self.engine = engine.sync_engine
        self.n = 0

    def _count(self, *_a, **_k):
        self.n += 1

    def __enter__(self):
        sa_event.listen(self.engine, "before_cursor_execute", self._count)
        return self

    def __exit__(self, *exc):
        sa_event.remove(self.engine, "before_cursor_execute", self._count)


async def _bulk(session, event, n: int, *, tag: str, status: str = "pending") -> None:
    now = datetime.now(timezone.utc)
    await session.execute(insert(R), [
        dict(
            id=uuid.uuid4(), event_id=event.id, full_name=f"Bulk {tag} {i}", email=f"bulk-{tag}-{i}@example.com",
            status=status, token=approvals.new_token(), submitted_at=now + timedelta(microseconds=i),
            whatsapp_opt_in=False, club_updates_whatsapp=False, created_at=now, updated_at=now,
        )
        for i in range(n)
    ])


async def test_the_lists_carry_request_figures_without_a_query_a_row(
    client, session, _engine, make_club, make_event, make_admin, mint_token,
):
    w = await _club_event(session, make_club, make_event, mint_token, tag="listed")
    await _bulk(session, w.event, 3, tag="l0")
    await _bulk(session, w.event, 2, tag="l0d", status="declined")
    await session.commit()

    async def club_list() -> tuple[dict, int]:
        with _Statements(_engine) as seen:
            r = await client.get(f"{API}/clubs/events/list", params={"limit": 50}, headers=w.h)
        assert r.status_code == 200, r.text
        return {i["id"]: i for i in r.json()["items"]}, seen.n

    rows, few = await club_list()
    row = rows[w.event.id]
    assert (row["requires_approval"], row["requests_pending"], row["requests_total"]) == (True, 3, 5)
    # Many more events, each with requests: the same number of statements.
    for i in range(8):
        e = await make_event(club=w.club, created_by=w.owner, name=f"Listed {i}", event_datetime=NOW + timedelta(days=6 + i), requires_approval=True)
        await session.flush()
        await _bulk(session, e, 2, tag=f"l{i + 1}")
    await session.commit()
    rows, many = await club_list()
    assert len(rows) == 9 and all(r["requests_total"] == 2 for k, r in rows.items() if k != w.event.id)
    assert many == few, (few, many)

    figures = (await client.get(f"{API}/clubs/events/figures", headers=w.h)).json()
    assert (figures["requires_approval"], figures["requests_pending"], figures["requests_total"]) == (True, 3 + 16, 5 + 16)

    overview = (await client.get(f"{API}/clubs/overview", headers=w.h)).json()
    nxt = overview["next_event"]
    assert nxt["id"] == w.event.id and (nxt["requests_total"], nxt["requests_pending"], nxt["requires_approval"]) == (5, 3, True)

    admin = auth_header(await mint_token(await make_admin(email="apstats-admin@example.com")))

    async def admin_list() -> tuple[dict, int]:
        with _Statements(_engine) as seen:
            r = await client.get(f"{API}/events/admin/list", params={"club_id": w.club.id, "limit": 50}, headers=admin)
        assert r.status_code == 200, r.text
        return {i["id"]: i for i in r.json()["items"]}, seen.n

    arows, a_many = await admin_list()
    assert (arows[w.event.id]["requires_approval"], arows[w.event.id]["requests_pending"], arows[w.event.id]["requests_total"]) == (True, 3, 5)
    assert all(arows[k]["requests_total"] == 2 for k in arows if k != w.event.id)
    # Fewer rows on the admin's page: the same statements.
    with _Statements(_engine) as seen:
        r = await client.get(f"{API}/events/admin/list", params={"club_id": w.club.id, "limit": 2}, headers=admin)
    assert r.status_code == 200 and len(r.json()["items"]) == 2 and seen.n == a_many

    # The admin's event detail reads the summary: the requests block is there.
    s = (await client.get(f"{API}/events/{w.event.id}/summary", headers=admin)).json()
    assert s["requests"]["total"] == 5 and s["requests_pending"] == 3
    from app.api import admin_overview

    admin_overview.reset_cache()
    ov = (await client.get(f"{API}/admin/overview", headers=admin)).json()
    assert ov["requests"]["received"] >= 21 and ov["requests"]["waiting"] >= 19


# ─── The waiting list is off on confirm-each events ──────────────────────


async def test_the_waiting_list_is_off_on_confirm_each_events(client, session, make_club, make_event, mint_token, mail):
    w = await _club_event(session, make_club, make_event, mint_token, tag="nowl", requires_approval=False, max_participants=1)
    on = await client.put(f"{API}/events/{w.event.id}/ticketing/waitlist/settings", json={"enabled": True}, headers=w.h)
    assert on.status_code == 200, on.text
    assert (await register(client, w.event, 380)).status_code == 200
    # Before the switch: sold out, the list on, one person in line.
    joined = await client.post(f"{API}/events/{w.event.id}/waitlist", json={"full_name": "Line Person", "email": "line-380@example.com"})
    assert joined.status_code == 200, joined.text
    entry_id = joined.json()["id"]

    await session.execute(update(Events).where(Events.id == w.event.id).values(requires_approval=True))
    await session.commit()
    page = (await client.get(f"{API}/events/{w.event.id}/ticket-types")).json()
    assert page["waitlist"]["enabled"] is False and all(t["waitlist"] is False for t in page["types"])
    assert page["types"][0]["status"] == "sold_out"
    refused = await client.post(f"{API}/events/{w.event.id}/waitlist", json={"full_name": "Late Person", "email": "late-381@example.com"})
    assert refused.status_code == 409 and refused.json()["error_code"] == "WAITLIST_OFF_ON_APPROVAL_EVENTS"
    sales = (await client.get(f"{API}/events/{w.event.id}/ticketing/sales", headers=w.h)).json()
    assert sales["waitlist_enabled"] is False
    # The setting itself is kept, for when confirming goes off again.
    assert (await session.get(WaitlistSettings, w.event.id)).enabled is True

    # The person already in line stays, and is offered nothing: not when a place frees...
    reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == w.event.id))
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.id == reg.id).values(is_deleted=True))
    await session.commit()
    assert await waitlist.fill(session, w.event.id) == []
    await sweep.run_sweep(session)
    entry = await session.scalar(select(WaitlistEntries).where(WaitlistEntries.id == uuid.UUID(entry_id)).execution_options(populate_existing=True))
    assert entry.status == "waiting" and entry.offer_token is None
    # ...nor by the club's hand.
    by_hand = await client.post(f"{API}/events/{w.event.id}/ticketing/waitlist/{entry_id}/offer", headers=w.h)
    assert by_hand.status_code == 409 and by_hand.json()["error_code"] == "WAITLIST_OFF_ON_APPROVAL_EVENTS"
    # And they hold no place ahead of the club's confirmations.
    rid = (await ask(client, w.event, 382)).json()["approval"]["id"]
    assert (await listing(client, w)).json()["places_left"] == 1
    out = (await approve(client, w, ids=[rid])).json()
    assert [a["id"] for a in out["approved"]] == [rid]


# ─── Registered another way: the request is settled ──────────────────────


async def test_a_request_is_settled_by_a_registration_made_any_other_way(
    client, session, make_club, make_event, mint_token, make_user, make_registration, mail, pushed,
):
    w = await _club_event(session, make_club, make_event, mint_token, tag="settle", event_datetime=NOW + timedelta(hours=2))
    ids = {who: (await ask(client, w.event, n)).json()["approval"]["id"] for who, n in (("form", 400), ("desk", 401), ("import", 402), ("hand", 403), ("race", 404), ("kept", 405))}
    mail.clear()

    # The form, with the setting off: they register directly.
    await session.execute(update(Events).where(Events.id == w.event.id).values(requires_approval=False))
    await session.commit()
    r = await client.post(
        f"{API}/events/registration/{w.event.id}/register",
        json={"full_name": "Asker Number 400", "email": "ASKER400@example.com ", "phone": "9876500400"},
    )
    assert r.status_code == 200, r.text
    await session.execute(update(Events).where(Events.id == w.event.id).values(requires_approval=True))
    await session.commit()

    # The desk: a walk-up at the door.
    walk = await client.post(
        f"{API}/events/{w.event.id}/gate/walkup",
        json={"name": "Asker Number 401", "email": "asker401@example.com", "client_ref": uuid.uuid4().hex}, headers=w.h,
    )
    assert walk.status_code == 200 and walk.json()["outcome"] == "REGISTERED", walk.text

    # An import.
    sheet = "full_name,email\nAsker Number 402,asker402@example.com\n"
    imp = await client.post(
        f"{API}/events/registration/{w.event.id}/import", files={"file": ("guests.csv", sheet.encode(), "text/csv")}, headers=w.h,
    )
    assert imp.status_code == 200, imp.text
    await jobs.settle()

    # By hand.
    hand = await client.post(
        f"{API}/events/registration/{w.event.id}/attendees", json={"full_name": "Asker Number 403", "email": "asker403@example.com"}, headers=w.h,
    )
    assert hand.status_code == 200, hand.text

    for who in ("form", "desk", "import", "hand"):
        req = await _req(session, ids[who])
        assert (who, req.status, req.closed_reason, req.registration_id) == (who, "closed", "registered_directly", None)
    # Nobody was told they were declined, or anything else about the request.
    assert not [m for m in mail if "request" in m["subject"].lower() or "sorry" in m.get("html", "").lower()]
    assert (await _req(session, ids["kept"])).status == "pending"
    assert (await listing(client, w)).json()["counts"]["closed"] == 4

    # Registered behind everyone's back (a row written straight in): the
    # confirmation finds them, skips, and settles the request.
    from app.api.users.models import Users

    guest = await session.get(Users, (await _req(session, ids["race"])).user_id)
    await make_registration(event=w.event, user=guest, full_name="Asker Number 404", email="asker404@example.com")
    await session.commit()
    out = (await approve(client, w, ids=[ids["race"], ids["kept"]])).json()
    assert out["skipped"] == [{"id": ids["race"], "reason": "already_registered"}]
    assert [a["id"] for a in out["approved"]] == [ids["kept"]]
    race = await _req(session, ids["race"])
    assert (race.status, race.closed_reason) == ("closed", "registered_directly")
    # The confirmed one is the request's own registration: never settled by it.
    kept = await _req(session, ids["kept"])
    assert kept.status == "approved" and kept.registration_id is not None
    view = (await client.get(f"{API}/registration-requests/{race.token}")).json()
    assert (view["status"], view["closed_reason"]) == ("closed", "registered_directly")


async def test_a_paid_hold_is_given_back_when_its_person_is_added_directly(client, session, make_club, make_event, mint_token, mail, pushed):
    w = await _club_event(
        session, make_club, make_event, mint_token, tag="heldsettle", has_fee=True, reg_fee=Decimal("150.00"), max_participants=2,
    )
    rid = (await ask(client, w.event, 410)).json()["approval"]["id"]
    await approve(client, w, ids=[rid])
    entry_id = (await _req(session, rid)).waitlist_entry_id
    assert (await _summary(client, w))["places"]["held"] == 1
    added = await client.post(
        f"{API}/events/registration/{w.event.id}/attendees", json={"full_name": "Asker Number 410", "email": "asker410@example.com"}, headers=w.h,
    )
    assert added.status_code == 200, added.text
    req = await _req(session, rid)
    assert (req.status, req.closed_reason) == ("closed", "registered_directly")
    entry = await session.scalar(select(WaitlistEntries).where(WaitlistEntries.id == entry_id).execution_options(populate_existing=True))
    assert entry.status == "left" and entry.offer_token is None
    # The hold no longer holds a place: never counted with the registration too.
    assert (await _summary(client, w))["places"]["held"] == 0


# ─── A lapsed hold's unpaid registration ─────────────────────────────────


async def test_a_lapsed_hold_retires_its_unpaid_registration(client, session, make_club, make_event, mint_token, mail, pushed, rzp_fake):
    w = await _club_event(
        session, make_club, make_event, mint_token, tag="retire", has_fee=True, reg_fee=Decimal("120.00"), max_participants=3,
    )
    ids = [(await ask(client, w.event, 420 + i)).json()["approval"]["id"] for i in range(2)]
    await approve(client, w, ids=ids)
    gone = await _claim(client, session, ids[0])
    paying = await _claim(client, session, ids[1])
    # The second opened a checkout just now: its payment may still land.
    order = await client.post(
        f"{API}/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": w.event.id, "event_registration_id": paying["event_registration_id"]}},
        headers=auth_header(paying["auth_token"]["access_token"]),
    )
    assert order.status_code in (200, 201), order.text
    fin = (await client.get(f"{API}/events/{w.event.id}/finance", headers=w.h)).json()["figures"]
    assert fin["outstanding_count"] == 2
    entries = [(await _req(session, i)).waitlist_entry_id for i in ids]
    await session.execute(update(WaitlistEntries).where(WaitlistEntries.id.in_(entries)).values(offer_expires_at=NOW - timedelta(minutes=1)))
    await session.commit()
    await sweep.run_sweep(session)
    await jobs.settle()

    assert {(await _req(session, i)).status for i in ids} == {"lapsed"}

    async def retired(reg_id) -> bool:
        return await session.scalar(
            select(EventRegistrationsLink.is_deleted).where(EventRegistrationsLink.id == uuid.UUID(reg_id))
            .execution_options(include_deleted=True)
        )

    assert await retired(gone["event_registration_id"]) is True
    assert await retired(paying["event_registration_id"]) is False
    # Looked at again once the checkout's window has passed.
    later = await session.scalar(
        select(func.count()).select_from(jobs.Jobs).where(jobs.Jobs.name == approvals.RETIRE_JOB, jobs.Jobs.run_at > NOW + timedelta(minutes=20))
    )
    assert later == 1
    assert await approvals.retire_lapsed(session, ids[1]) == "later"
    assert await approvals.retire_lapsed(session, ids[1], now=datetime.now(timezone.utc) + timedelta(hours=1)) == "retired"
    assert await retired(paying["event_registration_id"]) is True
    s = await _summary(client, w)
    assert s["registered"] == 0 and s["requests"]["lapsed"] == 2 and s["places"]["held"] == 0
    fin = (await client.get(f"{API}/events/{w.event.id}/finance", headers=w.h)).json()["figures"]
    assert (fin["outstanding_count"], fin["expected_from_holds_count"]) == (0, 0)
    # Never twice, never a paid one.
    assert await approvals.retire_lapsed(session, ids[0]) == "nothing"


# ─── Deleting, ending: closed, and the unanswered email once ─────────────


async def test_deleting_an_event_closes_its_requests_and_tells_them(client, session, make_club, make_event, mint_token, mail):
    w = await _club_event(session, make_club, make_event, mint_token, tag="deleted")
    ids = [(await ask(client, w.event, 440 + i)).json()["approval"]["id"] for i in range(2)]
    declined = (await ask(client, w.event, 442)).json()["approval"]["id"]
    await decline(client, w, ids=[declined], notify=False)
    # What the dashboard reads before deleting says who waits.
    row = next(i for i in (await client.get(f"{API}/clubs/events/list", headers=w.h)).json()["items"] if i["id"] == w.event.id)
    assert row["requests_pending"] == 2
    mail.clear()
    r = await client.delete(f"{API}/events/delete/{w.event.id}", headers=w.h)
    assert r.status_code == 200, r.text
    await jobs.settle()
    for rid in ids:
        req = await _req(session, rid)
        assert (req.status, req.closed_reason) == ("closed", "event_deleted")
    assert (await _req(session, declined)).status == "declined"
    told = [m for m in mail if m["subject"].startswith("Your registration for")]
    assert sorted(m["recipients"][0] for m in told) == ["asker440@example.com", "asker441@example.com"]
    assert "able to confirm your registration" in told[0]["html"] and "Deleted Club" in told[0]["html"]
    # The sweep finds nothing more to do, and nobody hears twice.
    await sweep.run_sweep(session)
    await jobs.settle()
    assert len([m for m in mail if m["subject"].startswith("Your registration for")]) == 2


async def test_an_event_ending_tells_each_unanswered_person_once(client, session, make_club, make_event, mint_token, mail, pushed):
    w = await _club_event(session, make_club, make_event, mint_token, tag="ended", has_fee=True, reg_fee=Decimal("80.00"))
    ids = [(await ask(client, w.event, 450 + i)).json()["approval"]["id"] for i in range(2)]
    held = (await ask(client, w.event, 452)).json()["approval"]["id"]
    await approve(client, w, ids=[held])
    mail.clear()
    await session.execute(update(Events).where(Events.id == w.event.id).values(event_datetime=NOW - timedelta(days=1)))
    await session.commit()
    for _ in range(2):
        await sweep.run_sweep(session)
        await jobs.settle()
    for rid in ids:
        req = await _req(session, rid)
        assert (req.status, req.closed_reason) == ("closed", "event_ended")
    hold = await _req(session, held)
    assert (hold.status, hold.closed_reason) == ("closed", "event_ended")
    told = [m for m in mail if m["subject"].startswith("Your registration for")]
    # Once each, and only to those nobody answered (the club did confirm the hold).
    assert sorted(m["recipients"][0] for m in told) == ["asker450@example.com", "asker451@example.com"]
    # The email library holds it, for the club to edit.
    lib = (await client.get(f"{API}/clubs/me/emails/overview", headers=w.h)).json()
    assert "request_unanswered" in {t.get("key") for t in lib["templates"]}

    # A cancellation keeps its own email: no unanswered one.
    c = await _club_event(session, make_club, make_event, mint_token, tag="cancelled")
    rid = (await ask(client, c.event, 455)).json()["approval"]["id"]
    mail.clear()
    assert (await client.post(f"{API}/events/{c.event.id}/cancel", json={"reason": "The hall flooded"}, headers=c.h)).status_code == 200
    await jobs.settle()
    req = await _req(session, rid)
    assert (req.status, req.closed_reason) == ("closed", "event_cancelled")
    subjects = [m["subject"] for m in _sent_to(mail, "asker455@example.com")]
    assert any(s.startswith("Cancelled") for s in subjects) and not any(s.startswith("Your registration for") for s in subjects)


# ─── The export ──────────────────────────────────────────────────────────


async def test_the_requests_export(client, session, make_club, make_event, mint_token, mail, pushed):
    from app.api.club_access import policy

    w = await _club_event(session, make_club, make_event, mint_token, tag="export", has_fee=True, reg_fee=Decimal("60.00"))
    a = (await ask(client, w.event, 460, additional_details={"college": "=CET"})).json()["approval"]["id"]
    b = (await ask(client, w.event, 461)).json()["approval"]["id"]
    c = (await ask(client, w.event, 462)).json()["approval"]["id"]
    await approve(client, w, ids=[a])
    await decline(client, w, ids=[b], note="Full up", notify=False)

    r = await client.get(f"{API}/events/registration/{w.event.id}/requests/export", headers=w.h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    head = list(csv.reader(io.StringIO(r.text)))[0]
    assert head == approvals.EXPORT_LEAD + ["college"] + approvals.EXPORT_TAIL
    by_email = {row["Email"]: row for row in rows}
    assert len(rows) == 3 and set(by_email) == {"asker460@example.com", "asker461@example.com", "asker462@example.com"}
    first = by_email["asker460@example.com"]
    # The club's words for each status (the dashboard's).
    assert (first["Status"], first["Payment"], first["Amount"]) == ("Confirmed", "awaiting payment", "60.0")
    assert first["Pay by"] and first["Reviewed at"] and first["Registered at"] and first["college"] == "'=CET"
    second = by_email["asker461@example.com"]
    assert (second["Status"], second["Decline note"], second["Payment"]) == ("Declined", "Full up", "")
    assert by_email["asker462@example.com"]["Status"] == "Under review"
    assert (await _req(session, c)).status == "pending"

    # Whoever may read the queue may export it: the same rule, the same answers.
    for role in ("admin", "editor", "door"):
        assert policy.allows(role, "GET", f"/api/v1/events/registration/{w.event.id}/requests/export") == policy.allows(
            role, "GET", f"/api/v1/events/registration/{w.event.id}/requests"
        ) is True
    other = await _club_event(session, make_club, make_event, mint_token, tag="exportother")
    seen = {}
    for path in ("requests", "requests/export"):
        theirs = await client.get(f"{API}/events/registration/{w.event.id}/{path}", headers=other.h)
        anon = await client.get(f"{API}/events/registration/{w.event.id}/{path}")
        seen[path] = (theirs.status_code, anon.status_code)
    assert seen["requests/export"] == seen["requests"] and seen["requests"][0] in (403, 404) and seen["requests"][1] == 401


# ─── Closeout, analytics, MCP ────────────────────────────────────────────


async def test_closeout_analytics_and_mcp_carry_the_requests(client, session, make_club, make_event, mint_token, mail, pushed, monkeypatch):
    from app.api.closeout import report as closeout_report
    from app.api.closeout import service as closeout

    w = await _club_event(session, make_club, make_event, mint_token, tag="closefig")
    ids = [(await ask(client, w.event, 470 + i)).json()["approval"]["id"] for i in range(3)]
    await approve(client, w, ids=ids[:2])
    await session.execute(update(Events).where(Events.id == w.event.id).values(page_views=30))
    await session.commit()

    figs = (await client.get(f"{API}/events/{w.event.id}/closeout/figures", headers=w.h)).json()
    assert (figs["requests"]["total"], figs["requests"]["approved"], figs["requests"]["pending"]) == (3, 2, 1)
    assert isinstance(figs["requests"]["oldest_pending_at"], str)
    event = await session.get(Events, w.event.id)
    f = await closeout.figures(session, event)
    pdf = closeout_report.report_pdf(f, datetime.now(timezone.utc), "Asia/Kolkata")
    assert pdf.startswith(b"%PDF")

    a = (await client.get(f"{API}/events/registration/{w.event.id}/analytics", headers=w.h)).json()
    assert a["requests_total"] == 3 and a["requests_per_view"] == 0.1
    assert sum(d["count"] for d in a["requests_by_day"]) == 3 and all(len(d["date"]) == 10 for d in a["requests_by_day"])

    dash = (await client.get(f"{API}/clubs/dashboard", headers=w.h)).json()
    assert dash["attention"][0]["kind"] == "REQUESTS_PENDING"

    from app.api.mcp import tools

    call = SimpleNamespace(session=session, money_allowed=lambda: False)

    async def _event(_call, event_id, load=()):
        return await session.get(Events, event_id)

    monkeypatch.setattr(tools, "_event", _event)
    out = await tools.event_stats(call, {"event_id": w.event.id})
    assert out["requires_approval"] is True and (out["requests"]["total"], out["requests"]["pending"]) == (3, 1)


# ─── High demand ─────────────────────────────────────────────────────────


async def test_the_high_demand_label_round_trips_and_shows_publicly(client, session, make_club, mint_token):
    from app.api.events.models import EventCategories

    owner, club = await make_club(name="Demand Club", email="demand-owner@example.com")
    category = EventCategories(name="Demand things")
    session.add(category)
    await session.commit()
    h = auth_header(await mint_token(owner))
    form = {
        "name": "Demand Night", "event_datetime": (NOW + timedelta(days=9)).isoformat(), "duration": "2",
        "has_fee": "false", "status": "published", "category_id": str(category.id),
    }
    made = await client.post(f"{API}/events/create", data={**form, "requires_approval": "true", "high_demand_label": "true"}, headers=h)
    assert made.status_code in (200, 201), made.text
    assert made.json()["high_demand_label"] is True
    event_id, slug = made.json()["id"], made.json()["slug"]
    info = (await client.get(f"{API}/events/info/{event_id}")).json()
    assert (info["high_demand_label"], info["high_demand"], info["requires_approval"]) == (True, True, True)
    page = (await client.get(f"{API}/discover/events/{slug}")).json()["event"]
    assert (page["high_demand"], page["requires_approval"]) == (True, True)
    card = next(c for c in (await client.get(f"{API}/discover/clubs/{club.slug}")).json()["upcoming"] if c["id"] == event_id)
    assert (card["high_demand"], card["requires_approval"]) == (True, True)
    listed = next(i for i in (await client.get(f"{API}/clubs/events/list", headers=h)).json()["items"] if i["id"] == event_id)
    assert listed["high_demand_label"] is True
    # Left out of an edit: as it was. Then off.
    kept = await client.put(f"{API}/events/update/{event_id}", data=form, headers=h)
    assert kept.status_code == 200 and kept.json()["high_demand_label"] is True
    off = await client.put(f"{API}/events/update/{event_id}", data={**form, "high_demand_label": "false"}, headers=h)
    assert off.json()["high_demand_label"] is False
    assert (await client.get(f"{API}/discover/events/{slug}")).json()["event"]["high_demand"] is False
    assert (await client.get(f"{API}/events/info/{event_id}")).json()["high_demand"] is False
    # A new event starts without it.
    plain = await client.post(f"{API}/events/create", data={**form, "name": "Plain Demand"}, headers=h)
    assert plain.json()["high_demand_label"] is False
