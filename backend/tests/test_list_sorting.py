"""Sorting the club's people lists (the guest list, the checked-in list and
the waiting list), the waiting list's search, and correcting someone's place
in line."""
from sqlalchemy import update

from app.api.events.models import EventRegistrationsLink
from tests.test_ticketing import register
from tests.test_waitlist import API, enable, entry, join, line, mail  # noqa: F401


async def _add(client, w, name, email):
    r = await client.post(
        f"{API}/events/registration/{w.event.id}/attendees", json={"full_name": name, "email": email}, headers=w.h,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _guests(client, w, path="list", **params):
    return client.get(f"{API}/events/registration/{w.event.id}/{path}", params={"limit": 50, **params}, headers=w.h)


def _names(body) -> list[str]:
    rows = body.get("data") or body.get("items") or body.get("results") or []
    return [r["full_name"] for r in rows]


async def test_the_guest_list_sorts_by_name_email_registered_and_check_in(client, session, make_club, make_event, mint_token):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from tests.conftest import auth_header

    owner, club = await make_club(name="Sort Club", email="sort-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Sort Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=3))
    await session.commit()
    line = SimpleNamespace(event=event, h=auth_header(await mint_token(owner)))
    people = [("Zara Quinn", "b@example.com"), ("adam Lee", "e@example.com"), ("Maya Iyer", "a@example.com")]
    added = [await _add(client, line, n, e) for n, e in people]

    body = (await _guests(client, line)).json()
    by_name = sorted((p[0] for p in people), key=str.lower)
    assert _names(body) == by_name  # unchanged default: names A to Z
    assert _names((await _guests(client, line, order="desc")).json()) == by_name[::-1]
    assert _names((await _guests(client, line, sort="email")).json()) == ["Maya Iyer", "Zara Quinn", "adam Lee"]
    # Registered: newest first unasked, oldest first asked.
    assert _names((await _guests(client, line, sort="registered")).json()) == [p[0] for p in people][::-1]
    assert _names((await _guests(client, line, sort="registered", order="asc")).json()) == [p[0] for p in people]

    # Checked in: latest first, the not yet checked in last.
    ids = [a.get("id") or a.get("registration_id") or a["data"]["id"] for a in added]
    for i, rid in ((0, ids[0]), (2, ids[2])):
        r = await client.post(f"{API}/events/registration/{line.event.id}/attendance/{rid}", json={"is_attended": True}, headers=line.h)
        assert r.status_code == 200, r.text
    # Make the order of the two check-ins certain.
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.full_name == "Zara Quinn")
                          .values(attended_on=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await session.commit()
    assert _names((await _guests(client, line, sort="checked_in")).json()) == ["Maya Iyer", "Zara Quinn", "adam Lee"]
    # The checked-in list takes the same orders.
    assert _names((await _guests(client, line, path="attendees", sort="name")).json()) == ["Maya Iyer", "Zara Quinn"]
    assert _names((await _guests(client, line, path="attendees", sort="name", order="desc")).json()) == ["Zara Quinn", "Maya Iyer"]

    assert (await _guests(client, line, sort="phone")).status_code == 422
    assert (await _guests(client, line, order="sideways")).status_code == 422


async def test_the_waiting_list_sorts_searches_and_corrects(client, session, line):
    await enable(client, line)
    assert (await register(client, line.event, ticket_type_id=line.seat)).status_code == 200
    first = (await join(client, line, 1, full_name="Zed Last", email="zz@example.com")).json()
    second = (await join(client, line, 2, full_name="amy First", email="mm@example.com")).json()
    (await join(client, line, 3, full_name="Kim Middle", email="aa@example.com")).json()
    path = f"{API}/events/{line.event.id}/ticketing/waitlist"

    def names(**params):
        return client.get(path, params=params, headers=line.h)

    assert [e["full_name"] for e in (await names()).json()["entries"]] == ["Zed Last", "amy First", "Kim Middle"]
    assert [e["full_name"] for e in (await names(sort="name")).json()["entries"]] == ["amy First", "Kim Middle", "Zed Last"]
    assert [e["full_name"] for e in (await names(sort="name", order="desc")).json()["entries"]] == ["Zed Last", "Kim Middle", "amy First"]
    assert [e["full_name"] for e in (await names(sort="email")).json()["entries"]] == ["Kim Middle", "amy First", "Zed Last"]
    assert [e["full_name"] for e in (await names(sort="joined")).json()["entries"]] == ["Kim Middle", "amy First", "Zed Last"]
    found = (await names(q="mm@")).json()
    assert [e["full_name"] for e in found["entries"]] == ["amy First"] and found["total"] == 1
    assert (await names(sort="height")).status_code == 422

    # Correcting: the name and email, their place kept, nothing sent.
    sent_before = len(line.mail)
    fixed = await client.patch(f"{path}/{first['id']}", json={"full_name": "Zed Corrected", "email": "zed@example.com"}, headers=line.h)
    assert fixed.status_code == 200, fixed.text
    assert (fixed.json()["full_name"], fixed.json()["email"], fixed.json()["position"]) == ("Zed Corrected", "zed@example.com", 1)
    assert len(line.mail) == sent_before
    row = await entry(session, first["id"])
    assert (row.full_name, row.email, row.status) == ("Zed Corrected", "zed@example.com", "waiting")

    # Another open place in the same line already uses that address.
    clash = await client.patch(f"{path}/{first['id']}", json={"email": "MM@example.com"}, headers=line.h)
    assert clash.status_code == 409
    # Name can't be blank; an unknown entry is not found.
    assert (await client.patch(f"{path}/{second['id']}", json={"full_name": ""}, headers=line.h)).status_code == 422
    import uuid

    assert (await client.patch(f"{path}/{uuid.uuid4()}", json={"full_name": "X"}, headers=line.h)).status_code == 404

    from app.api.club_access import policy

    assert not policy.allows("door", "PATCH", f"/api/v1/events/{line.event.id}/ticketing/waitlist/{first['id']}")
    assert policy.allows("editor", "PATCH", f"/api/v1/events/{line.event.id}/ticketing/waitlist/{first['id']}")


def test_the_order_builders_for_contacts_certificates_and_store_orders():
    """Each list's ORDER BY: the column asked for, its direction, and a
    tie-break, so pages never overlap."""
    from sqlalchemy.dialects import postgresql

    from app.api.certificates.service import _certificate_order
    from app.api.contacts.service import contact_order
    from app.api.store.service import _orders_order

    def sql(parts) -> str:
        return ", ".join(str(p.compile(dialect=postgresql.dialect())) for p in parts)

    assert "created_at DESC" in sql(contact_order(None, None))
    assert "lower(archived_contacts.name) ASC" in sql(contact_order("name", None))
    assert "lower(archived_contacts.event_name) DESC" in sql(contact_order("event", "desc"))
    assert "ticket_id ASC" in sql(_certificate_order(None, None))
    assert "certificate_issued_at DESC" in sql(_certificate_order("issued", None))
    assert "lower(event_registrations_link.full_name) ASC" in sql(_certificate_order("name", "asc"))
    assert "event_store_orders.created_at DESC" in sql(_orders_order(None, None))
    assert "event_store_orders.total ASC" in sql(_orders_order("total", "asc"))
    assert "lower(event_registrations_link.full_name) ASC" in sql(_orders_order("name", None))
