"""Which ticket type a guest holds, where the website's and the app's ticket
screens read it: the ticket (GET /events/tickets/{id}) and My events
(GET /events/list?is_registered=true). The ticket also says its event's
status, and why it was cancelled, as the event page does."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import event as sa_event

from app.api.club_ops.models import EventCancellations
from app.api.closeout.models import EventStatus
from app.api.ticketing.models import TicketTypes
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


async def _typed_registration(session, make_registration, event, user, name="VIP"):
    vip = TicketTypes(event_id=event.id, name=name, price=0)
    session.add(vip)
    await session.flush()
    reg = await make_registration(event=event, user=user, ticket_type_id=vip.id, ticket_label=name)
    return vip, reg


async def test_the_ticket_says_its_type_and_its_events_status(
    client, session, make_user, make_club, make_event, make_registration, mint_token
):
    owner, club = await make_club(name="Label Club", email="label-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Gala", event_datetime=NOW + timedelta(days=3))
    guest = await make_user(email="label-guest@example.com")
    vip, reg = await _typed_registration(session, make_registration, event, guest)
    await session.commit()

    r = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}", headers=auth_header(await mint_token(guest)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket_type_id"] == vip.id and body["ticket_label"] == "VIP"
    assert body["event"]["status"] == "published" and body["event"]["cancellation"] is None
    # What was there before is unchanged.
    assert body["ticket_id"] == reg.ticket_id and body["event"]["id"] == event.id


async def test_a_ticket_without_a_type_says_none(
    client, session, make_user, make_club, make_event, make_registration, mint_token
):
    owner, club = await make_club(name="Plain Club", email="plain-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Plain Night", lifecycle=EventStatus.live)
    guest = await make_user(email="plain-guest@example.com")
    reg = await make_registration(event=event, user=guest)
    await session.commit()

    body = (await client.get(f"/api/v1/events/tickets/{reg.ticket_id}", headers=auth_header(await mint_token(guest)))).json()
    assert body["ticket_type_id"] is None and body["ticket_label"] is None
    assert body["event"]["status"] == "live" and body["event"]["cancellation"] is None


async def test_a_cancelled_events_ticket_says_why(
    client, session, make_user, make_club, make_event, make_registration, mint_token
):
    owner, club = await make_club(name="Called Off", email="called-off@example.com")
    event = await make_event(club=club, created_by=owner, name="Rained Out", lifecycle=EventStatus.cancelled, status_clock=False)
    guest = await make_user(email="called-off-guest@example.com")
    reg = await make_registration(event=event, user=guest)
    when = NOW - timedelta(hours=2)
    session.add(EventCancellations(event_id=event.id, club_id=club.id, reason="The venue flooded.", cancelled_at=when))
    await session.commit()

    body = (await client.get(f"/api/v1/events/tickets/{reg.ticket_id}", headers=auth_header(await mint_token(guest)))).json()
    assert body["event"]["status"] == "cancelled"
    c = body["event"]["cancellation"]
    assert c["reason"] == "The venue flooded."
    assert datetime.fromisoformat(c["cancelled_at"]) == when


async def test_my_events_say_each_tickets_type_in_one_query(
    client, session, _engine, make_user, make_club, make_event, make_registration, mint_token
):
    owner, club = await make_club(name="Many Types", email="many-types@example.com")
    typed = await make_event(club=club, created_by=owner, name="Typed Night", event_datetime=NOW + timedelta(days=2))
    plain = await make_event(club=club, created_by=owner, name="Plain Day", event_datetime=NOW + timedelta(days=4))
    await make_event(club=club, created_by=owner, name="Elsewhere", event_datetime=NOW + timedelta(days=6))
    guest = await make_user(email="many-types-guest@example.com")
    await _typed_registration(session, make_registration, typed, guest, name="Student")
    await make_registration(event=plain, user=guest)
    # Someone else's ticket for the same event is not the caller's.
    other = await make_user(email="many-types-other@example.com")
    await make_registration(event=plain, user=other, ticket_label="Not yours")
    await session.commit()
    token = await mint_token(guest)

    # The registrations of the page are read once, however many rows it has.
    reads: list[str] = []

    def count(conn, cursor, statement, *args):
        if "FROM event_registrations_link" in statement and "event_registrations_link.event_id IN" in statement:
            reads.append(statement)

    engine = _engine.sync_engine
    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        r = await client.get("/api/v1/events/list?is_registered=true", headers=auth_header(token))
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    assert r.status_code == 200, r.text
    rows = {row["name"]: row for row in r.json()["items"]}
    assert set(rows) == {"Typed Night", "Plain Day"}
    assert rows["Typed Night"]["ticket_label"] == "Student"
    assert rows["Plain Day"]["ticket_label"] is None
    assert len(reads) == 1, reads

    # An event the caller is not registered for has no label.
    browse = (await client.get("/api/v1/events/list?is_registered=false", headers=auth_header(token))).json()["items"]
    assert [(row["name"], row["ticket_label"]) for row in browse] == [("Elsewhere", None)]

    # Each caller sees their own ticket's label.
    theirs = (await client.get("/api/v1/events/list?is_registered=true", headers=auth_header(await mint_token(other)))).json()["items"]
    assert [(row["name"], row["ticket_label"]) for row in theirs] == [("Plain Day", "Not yours")]
