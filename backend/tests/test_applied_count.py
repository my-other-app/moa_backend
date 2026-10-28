"""The public "N applied" on events that review registrations: `applied_count`
on every public event shape (the discover cards wherever they are, and the
event's page), beside `going`.

- Every registration ever made counts, whatever became of it (under review,
  confirmed, declined, cancelled, expired, closed), and so do registrations
  made without a request (the desk, an import, before review was on); it
  never drops as the club decides, and it is exactly the dashboard's
  `total + added_directly`.
- Null on an event that doesn't review registrations.
- Counted for a whole page at once, in the statement that already reads the
  page's going counts: no statement more for a list, however many review
  events it has.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import update

from app.api.approvals import service as approvals
from app.api.approvals.models import RegistrationRequests as R
from app.api.events.models import EventRegistrationsLink, Events
from app.db.listeners import add_loader_criteria
from tests.conftest import auth_header
from tests.test_approvals import _req, approve, ask, decline

API = "/api/v1"
D = f"{API}/discover"
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _as_production(session):
    """The soft-delete filter production puts on every session: the count's
    outer join must keep the requests without a registration under it."""
    add_loader_criteria(session)


@pytest.fixture(autouse=True)
def _no_mail(monkeypatch):
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email", lambda **kw: None)
    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kw: None)


@pytest.fixture
def statements(session):
    seen: list[str] = []
    conn = session.bind.sync_connection

    def before(_conn, _cursor, statement, *_args):
        if "SAVEPOINT" not in statement:
            seen.append(statement)

    sa_event.listen(conn, "before_cursor_execute", before)
    yield seen
    sa_event.remove(conn, "before_cursor_execute", before)


async def _club(session, make_club, mint_token, tag):
    owner, club = await make_club(name=f"{tag.title()} Applied Club", email=f"{tag}-applied@example.com")
    return owner, club, auth_header(await mint_token(owner))


class _W:
    def __init__(self, owner, club, event, h):
        self.owner, self.club, self.event, self.h = owner, club, event, h


async def _page(client, event) -> dict:
    r = await client.get(f"{D}/events/{event.slug}")
    assert r.status_code == 200, r.text
    return r.json()


def _card(items, event) -> dict:
    return next(c for c in items if c["id"] == event.id)


async def test_every_registration_ever_made_counts_and_it_never_drops(client, session, make_club, make_event, make_user, mint_token):
    owner, club, h = await _club(session, make_club, mint_token, "devfest")
    event = await make_event(club=club, created_by=owner, name="DevFest Applied", event_datetime=NOW + timedelta(days=6), requires_approval=True, max_participants=50)
    # Someone registered before the club switched review on: no request.
    early = await make_user(email="before-review@example.com")
    session.add(EventRegistrationsLink(event_id=event.id, user_id=early.id, full_name="Before Review", email=early.email, ticket_id=f"T{uuid.uuid4().hex[:10]}"))
    await session.commit()
    w = _W(owner, club, event, h)

    ids = [(await ask(client, event, 700 + i)).json()["approval"]["id"] for i in range(7)]
    assert len((await approve(client, w, ids=ids[:2])).json()["approved"]) == 2
    await decline(client, w, ids=[ids[2]], notify=False)
    assert (await client.post(f"{API}/registration-requests/{(await _req(session, ids[3])).token}/withdraw")).status_code == 200
    # Expired (confirmed, not paid in time) and closed, as the sweeps leave them.
    await session.execute(update(R).where(R.id == uuid.UUID(ids[4])).values(status="lapsed"))
    await session.execute(update(R).where(R.id == uuid.UUID(ids[5])).values(status="closed", closed_reason="event_ended"))
    await session.commit()
    # ids[6] is still under review. And one added at the desk: no request.
    added = await client.post(f"{API}/events/registration/{event.id}/attendees", json={"full_name": "Desk Person", "email": "desk-applied@example.com"}, headers=h)
    assert added.status_code == 200, added.text

    # 7 registrations under review or decided, and 2 made directly; 4 going.
    page = (await _page(client, event))["event"]
    assert (page["going"], page["applied_count"], page["requires_approval"]) == (4, 9, True)
    listed = _card((await client.get(f"{D}/events")).json()["items"], event)
    assert (listed["going"], listed["applied_count"]) == (4, 9)
    club_page = await client.get(f"{D}/clubs/{club.slug}")
    assert _card(club_page.json()["upcoming"], event)["applied_count"] == 9
    club_events = await client.get(f"{D}/clubs/{club.slug}/events", params={"when": "upcoming"})
    assert _card(club_events.json()["items"], event)["applied_count"] == 9
    searched = await client.get(f"{D}/events", params={"q": "devfest"})
    assert _card(searched.json()["items"], event)["applied_count"] == 9
    home = (await client.get(f"{D}/home")).json()
    assert _card(home["this_week"], event)["applied_count"] == 9

    # Exactly the dashboard's figure: its requests' total and those added directly.
    summary = (await client.get(f"{API}/events/{event.id}/summary", headers=h)).json()["requests"]
    assert summary["total"] + summary["added_directly"] == 9

    # A confirmed person cancels their ticket: fewer going, as many applied.
    gone = (await _req(session, ids[0])).registration_id
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.id == gone).values(is_deleted=True))
    await session.commit()
    page = (await _page(client, event))["event"]
    assert (page["going"], page["applied_count"]) == (3, 9)
    # Reopened, re-declined, withdrawn and asked again: each registration counts once more, never less.
    again = (await ask(client, event, 703)).json()["approval"]
    assert again["status"] == "pending"
    assert (await approvals.applied_counts(session, [event.id])) == {event.id: 10}
    summary = (await client.get(f"{API}/events/{event.id}/summary", headers=h)).json()["requests"]
    assert summary["total"] + summary["added_directly"] == 10
    # Never fewer than are going.
    assert page["applied_count"] >= page["going"]


async def test_a_new_review_event_says_none_applied_and_others_say_null(client, session, make_club, make_event, mint_token, make_registration, make_user):
    owner, club, h = await _club(session, make_club, mint_token, "nullish")
    fresh = await make_event(club=club, created_by=owner, name="Fresh Review", event_datetime=NOW + timedelta(days=3), requires_approval=True)
    plain = await make_event(club=club, created_by=owner, name="Plain Night", event_datetime=NOW + timedelta(days=4))
    switched_off = await make_event(club=club, created_by=owner, name="Was Reviewed", event_datetime=NOW + timedelta(days=5), requires_approval=True)
    await make_registration(event=plain, user=await make_user(email="plain-goer@example.com"))
    await session.commit()
    await ask(client, switched_off, 801)
    await session.execute(update(Events).where(Events.id == switched_off.id).values(requires_approval=False))
    await session.commit()

    items = (await client.get(f"{D}/events")).json()["items"]
    assert (_card(items, fresh)["going"], _card(items, fresh)["applied_count"]) == (0, 0)
    assert (_card(items, plain)["going"], _card(items, plain)["applied_count"]) == (1, None)
    # Review switched off: a normal event again, whatever requests it kept.
    assert _card(items, switched_off)["applied_count"] is None
    assert (await _page(client, fresh))["event"]["applied_count"] == 0
    for e in (plain, switched_off):
        detail = (await _page(client, e))["event"]
        assert detail["applied_count"] is None and detail["requires_approval"] is False
    assert await approvals.applied_counts(session, [fresh.id, plain.id, switched_off.id]) == {fresh.id: 0}


async def test_a_list_counts_applied_in_the_statement_it_already_runs(client, session, make_club, make_event, mint_token, statements):
    owner, club, h = await _club(session, make_club, mint_token, "budget")
    events = [
        await make_event(club=club, created_by=owner, name=f"Budget Night {i}", event_datetime=NOW + timedelta(days=2 + i))
        for i in range(3)
    ]
    await session.commit()
    paths = [f"{D}/events", f"{D}/clubs/{club.slug}", f"{D}/clubs/{club.slug}/events", f"{D}/home"]

    async def count():
        out = []
        for path in paths:
            statements.clear()
            r = await client.get(path)
            assert r.status_code == 200, r.text
            out.append(len(statements))
        return out

    before = await count()
    # The same events reviewing registrations, with registrations to count.
    await session.execute(update(Events).where(Events.id.in_([e.id for e in events])).values(requires_approval=True))
    await session.commit()
    for i, e in enumerate(events):
        for n in range(i + 1):
            await ask(client, e, 900 + 10 * i + n)
    after = await count()
    assert after == before, list(zip(paths, before, after))
    # One grouped count of the page's requests, inside the cards' statement.
    statements.clear()
    items = (await client.get(f"{D}/events")).json()["items"]
    assert [_card(items, e)["applied_count"] for e in events] == [1, 2, 3]
    asked = [s for s in statements if "registration_requests" in s]
    assert len(asked) == 1 and "GROUP BY registration_requests.event_id" in asked[0] and "interest" in asked[0].lower()

    # The event's own page: one statement more on an event that reviews registrations, none on any other.
    plain = await make_event(club=club, created_by=owner, name="Budget Plain", event_datetime=NOW + timedelta(days=9))
    await session.commit()
    statements.clear()
    await _page(client, plain)
    plain_n = len(statements)
    statements.clear()
    await _page(client, events[0])
    assert len(statements) <= plain_n + 1
