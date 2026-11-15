"""Door staff and the club-access rules beside them.

- Door staff (a Door member, a gate link, a volunteer at the gate) read the
  guest list, the requests, the waiting list and the gate's search with every
  row and field kept and the contacts and ticket ids masked; the organiser
  reads them whole. Their searches match a name, or a whole email, number or
  ticket id -- never a part of one.
- The gate's search needs three characters (a whole admission number: any),
  and is limited per door link or account.
- A volunteer whose "can check in" switch is off writes no phone at the gate
  and checks nobody in on the app's old scanner.
- What tickets cost, and hiding a type, are money: not an editor's.
- A club's followers are the club's own (and the platform's admins').
- A card prints (and counts as printed) only when asked to; the door key is
  read from the header only. The waiting list's file and the batch of cards
  are data exports for an admin.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.club_access import door
from app.api.events.volunteer.models import Volunteer
from app.core.email import email as email_mod
from app.core.rate_limit import limiter
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
BULLET = "•"


@pytest.fixture
async def hall(session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    from app.api.approvals.models import RegistrationRequests
    from app.api.ticketing import types as types_mod
    from app.api.ticketing.models import OFFERED, WaitlistEntries

    monkeypatch.setattr(limiter, "enabled", False)
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    owner, club = await make_club(name="Circuit Door", email="door-club@example.com")
    other_owner, other_club = await make_club(name="Robotics Door", email="door-other@example.com")
    event = await make_event(club=club, created_by=owner, name="DevFest Hall", event_datetime=NOW - timedelta(hours=1), duration=8.0, max_participants=100)
    guests = []
    for i, (name, email, phone) in enumerate((
        ("Ananya Iyer", "ananya.iyer@gmail.com", "+91 98765 44321"),
        ("Bala Krishnan", "bala.k@college.ac.in", "9876500002"),
        ("Ananya Rao", "ananya.rao@gmail.com", None),
    )):
        u = await make_user(email=email)
        guests.append(await make_registration(event=event, user=u, full_name=name, email=email, phone=phone, admission_no=f"A{i + 1}"))
    ttype = (await types_mod.ensure(session, event))[0]
    session.add(WaitlistEntries(event_id=event.id, ticket_type_id=ttype.id, full_name="Chitra Nair", email="chitra@gmail.com", phone="9000011111",
                                status=OFFERED, offer_token=uuid.uuid4().hex, offered_at=NOW, offer_expires_at=NOW + timedelta(hours=24)))
    session.add(RegistrationRequests(event_id=event.id, full_name="Dev Menon", email="dev.menon@gmail.com", phone="9000022222", token=uuid.uuid4().hex, answers={"college": "CET"}))
    await session.commit()
    world = dict(owner=owner, club=club, other=other_club, event=event, guests=guests, sent=sent,
                 h=auth_header(await mint_token(owner)), other_h=auth_header(await mint_token(other_owner)))
    for role in ("door", "editor", "admin"):
        world[f"{role}_h"] = await _member_header(session, owner, club, role)
    return world


async def _member_header(session, owner, club, role: str) -> dict:
    """A team member's session, as signing in with their own password makes
    one: it acts as the club's account, and names the member (club_access)."""
    from app.api.auth.schemas import AuthTokenData
    from app.api.club_access.models import ClubMembers
    from app.core.auth.jwt import create_access_token

    member = ClubMembers(club_id=club.id, email=f"door-{role}@example.com", full_name=role.title(), role=role, status="active")
    session.add(member)
    await session.commit()
    await session.refresh(owner)
    token = create_access_token(data=AuthTokenData(user_id=owner.id, token_type="access_token", token_version=owner.token_version, member_id=member.id).model_dump())
    return auth_header(token)


def _url(hall, path):
    return f"{API}/events/{hall['event'].id}{path}"


def _is_masked_email(v):
    return v is None or v == "" or re.fullmatch(r".•••@.+", v) is not None


async def test_the_guest_list_keeps_every_field_and_masks_the_contacts_for_door_staff(client, hall):
    url = f"{API}/events/registration/{hall['event'].id}/list"
    whole = (await client.get(url, headers=hall["h"])).json()
    masked = (await client.get(url, headers=hall["door_h"])).json()
    assert masked["total"] == whole["total"] == 3
    by_id = {r["id"]: r for r in whole["items"]}
    for row in masked["items"]:
        full = by_id[row["id"]]
        assert set(row) == set(full)  # every field the console and the app parse
        assert row["full_name"] == full["full_name"] and row["admission_no"] == full["admission_no"] and row["is_paid"] == full["is_paid"]
        assert row["email"] == door.mask_email(full["email"]) and _is_masked_email(row["email"])
        assert row["phone"] == door.mask_phone(full["phone"])
        assert row["ticket_id"] == door.mask_ticket(full["ticket_id"]) and row["ticket_id"].startswith(BULLET * 4)
        assert row["user"]["email"] == door.mask_email(full["user"]["email"])
    ananya = next(r for r in masked["items"] if r["full_name"] == "Ananya Iyer")
    assert ananya["email"] == "a•••@gmail.com" and ananya["phone"] == "•••• 4321"
    # The checked-in list too.
    attendees = await client.get(f"{API}/events/registration/{hall['event'].id}/attendees", headers=hall["door_h"])
    assert attendees.status_code == 200


async def test_door_staff_find_a_guest_by_name_or_a_whole_code_never_a_part(client, hall):
    url = f"{API}/events/registration/{hall['event'].id}/list"
    ticket = hall["guests"][0].ticket_id

    async def found(q, h):
        return sorted(r["full_name"] for r in (await client.get(url, params={"search": q}, headers=h)).json()["items"])

    assert await found("ananya", hall["door_h"]) == ["Ananya Iyer", "Ananya Rao"]
    assert await found("gmail", hall["door_h"]) == []
    assert await found("gmail", hall["h"]) == ["Ananya Iyer", "Ananya Rao"]
    assert await found("ANANYA.IYER@gmail.com", hall["door_h"]) == ["Ananya Iyer"]
    assert await found(ticket[:6], hall["door_h"]) == []
    assert await found(ticket.lower(), hall["door_h"]) == ["Ananya Iyer"]
    assert await found("98765", hall["door_h"]) == []


async def test_requests_and_the_waiting_list_are_masked_for_door_staff(client, hall):
    req_url = f"{API}/events/registration/{hall['event'].id}/requests"
    whole = (await client.get(req_url, headers=hall["h"])).json()["items"][0]
    masked = (await client.get(req_url, headers=hall["door_h"])).json()["items"][0]
    assert set(masked) == set(whole) and masked["full_name"] == "Dev Menon"
    assert masked["email"] == "d•••@gmail.com" and masked["phone"] == "•••• 2222" and whole["email"] == "dev.menon@gmail.com"
    assert (await client.get(req_url, params={"q": "menon@"}, headers=hall["door_h"])).json()["items"] == []
    assert len((await client.get(req_url, params={"q": "dev.menon@gmail.com"}, headers=hall["door_h"])).json()["items"]) == 1

    wl_url = _url(hall, "/ticketing/waitlist")
    whole = (await client.get(wl_url, headers=hall["h"])).json()["entries"][0]
    masked = (await client.get(wl_url, headers=hall["door_h"])).json()["entries"][0]
    assert set(masked) == set(whole)
    assert masked["email"] == "c•••@gmail.com" and masked["phone"] == "•••• 1111"
    assert whole["offer_url"] and masked["offer_url"] is None  # a claim link is a place, not a contact
    assert (await client.get(wl_url, params={"q": "chitra@"}, headers=hall["door_h"])).json()["entries"] == []


async def _gate_key(client, hall) -> str:
    made = await client.post(_url(hall, "/doors"), json={"name": "Main gate", "kind": "door"}, headers=hall["h"])
    return (await client.post(_url(hall, f"/doors/{made.json()['id']}/link"), json={"can_walkup": False}, headers=hall["h"])).json()["key"]


async def test_the_gate_search_wants_three_characters_and_masks_for_a_gate_link(client, hall):
    key = {"X-Gate-Key": await _gate_key(client, hall)}
    search = _url(hall, "/gate/search")
    assert (await client.get(search, params={"q": "an"}, headers=key)).json() == []
    # An admission number typed whole is found however short.
    short = (await client.get(search, params={"q": "a2"}, headers=key)).json()
    assert [r["name"] for r in short] == ["Bala Krishnan"]
    rows = (await client.get(search, params={"q": "anan"}, headers=key)).json()
    assert sorted(r["name"] for r in rows) == ["Ananya Iyer", "Ananya Rao"]
    for r in rows:
        assert r["ticket_id"].startswith(BULLET * 4) and _is_masked_email(r["email"])
    # A part of a ticket or address finds nobody at a masked door; the whole one does.
    ticket = hall["guests"][0].ticket_id
    assert (await client.get(search, params={"q": ticket[:8]}, headers=key)).json() == []
    assert [r["name"] for r in (await client.get(search, params={"q": ticket}, headers=key)).json()] == ["Ananya Iyer"]
    # The organiser sees the rows whole and finds by any part.
    whole = (await client.get(search, params={"q": ticket[:8]}, headers=hall["h"])).json()
    assert [r["ticket_id"] for r in whole] == [ticket]
    # A Door member, signed in as the club, reads it masked too.
    member = (await client.get(search, params={"q": "anan"}, headers=hall["door_h"])).json()
    assert all(r["ticket_id"].startswith(BULLET) for r in member)


async def test_the_gate_search_is_limited_per_door_link(client, hall, monkeypatch):
    from app.api.checkins import router as checkins_router

    key = {"X-Gate-Key": await _gate_key(client, hall)}
    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    n = int(checkins_router.GATE_SEARCH_LIMIT.split("/")[0])
    for _ in range(n):
        assert (await client.get(_url(hall, "/gate/search"), params={"q": "bala"}, headers=key)).status_code == 200
    assert (await client.get(_url(hall, "/gate/search"), params={"q": "bala"}, headers=key)).status_code == 429
    # Another caller has a budget of its own.
    assert (await client.get(_url(hall, "/gate/search"), params={"q": "bala"}, headers=hall["h"])).status_code == 200
    limiter.reset()


async def test_a_volunteer_with_check_in_off_writes_no_phone_and_admits_no_one(client, session, hall, make_user, mint_token):
    on_user = await make_user(email="door-vol-on@example.com")
    off_user = await make_user(email="door-vol-off@example.com")
    session.add(Volunteer(email=on_user.email, event_id=hall["event"].id, user_id=on_user.id, is_approved=True, can_check_in=True))
    session.add(Volunteer(email=off_user.email, event_id=hall["event"].id, user_id=off_user.id, is_approved=True, can_check_in=False))
    await session.commit()
    on, off = auth_header(await mint_token(on_user)), auth_header(await mint_token(off_user))
    guest = hall["guests"][2]
    capture = _url(hall, f"/gate/capture/{guest.id}")
    refused = await client.post(capture, json={"phone": "9000033333"}, headers=off)
    assert refused.status_code == 403
    assert (await client.post(capture, json={"phone": "9000033333"}, headers=on)).status_code == 200
    # The app's old scanner and its check.
    for path in ("/validate", "/checkin"):
        r = await client.post(f"{API}/events/volunteer{path}/{hall['event'].id}", json={"ticket_id": hall["guests"][1].ticket_id}, headers=off)
        assert r.status_code == 403 and r.json()["error_code"] == "CHECK_IN_NOT_ALLOWED", r.text
    ok = await client.post(f"{API}/events/volunteer/checkin/{hall['event'].id}", json={"ticket_id": hall["guests"][1].ticket_id}, headers=on)
    assert ok.status_code == 200, ok.text
    # A volunteer at the gate reads the search masked.
    rows = (await client.get(_url(hall, "/gate/search"), params={"q": "bala"}, headers=on)).json()
    assert rows and rows[0]["ticket_id"].startswith(BULLET)


async def test_what_tickets_cost_is_not_an_editors(client, session, hall):
    ed, admin = hall["editor_h"], hall["admin_h"]
    types = (await client.get(_url(hall, "/ticketing/types"), headers=hall["h"])).json()["types"]
    t = types[0]
    patch = _url(hall, f"/ticketing/types/{t['id']}")
    assert (await client.patch(patch, json={"name": "Hall pass"}, headers=ed)).status_code == 200
    for body in ({"price": 0 if t["price"] else 499}, {"is_hidden": True}):
        r = await client.patch(patch, json=body, headers=ed)
        assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN", body
    # The event form's whole list: saved when it changes no price and hides nothing.
    item = {k: t[k] for k in ("id", "name", "description", "price", "capacity", "sales_start", "sales_end", "is_hidden", "per_person_limit")} | {"name": "Hall pass"}
    assert (await client.put(_url(hall, "/ticketing/types"), json={"types": [item]}, headers=ed)).status_code == 200, "unchanged"
    two = [item, {"name": "Speakers", "price": 0, "is_hidden": True}]
    assert (await client.put(_url(hall, "/ticketing/types"), json={"types": two}, headers=ed)).status_code == 403
    assert (await client.post(_url(hall, "/ticketing/types"), json={"name": "Comp", "price": 0, "is_hidden": True}, headers=ed)).status_code == 403
    # An admin may, and so may the club.
    assert (await client.post(_url(hall, "/ticketing/types"), json={"name": "Comp", "price": 0, "is_hidden": True}, headers=admin)).status_code == 201
    paid = await client.post(_url(hall, "/ticketing/types"), json={"name": "VIP", "price": 999}, headers=hall["h"])
    assert paid.status_code == 201
    # A priced type is not an editor's to take away, nor a free one to add beside it.
    assert (await client.delete(_url(hall, f"/ticketing/types/{paid.json()['id']}"), headers=ed)).status_code == 403
    assert (await client.post(_url(hall, "/ticketing/types"), json={"name": "Free", "price": 0}, headers=ed)).status_code == 403


async def test_the_events_own_fee_with_one_type_is_not_an_editors(client, session, make_event, hall):
    from app.api.ticketing import types as types_mod

    e = await make_event(club=hall["club"], created_by=hall["owner"], name="Workshop", event_datetime=NOW + timedelta(days=3), duration=2.0, has_fee=True, reg_fee=200)
    await types_mod.ensure(session, e)
    await session.commit()

    def form(fee):
        return {
            "name": "Workshop", "event_datetime": e.event_datetime.isoformat(), "duration": "2.0", "category_id": str(e.category_id),
            "has_fee": "true" if fee else "false", **({"reg_fee": str(fee)} if fee else {}),
        }

    url = f"{API}/events/update/{e.id}"
    r = await client.put(url, data=form(0), headers=hall["editor_h"])
    assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN", r.text
    assert (await client.put(url, data=form(200), headers=hall["editor_h"])).status_code == 200  # the same fee: saved
    assert (await client.put(url, data=form(0), headers=hall["admin_h"])).status_code == 200


async def test_a_clubs_followers_are_its_own(client, hall, make_admin, mint_token):
    url = f"{API}/clubs/followers/{hall['club'].id}"
    assert (await client.get(url, headers=hall["h"])).status_code == 200
    assert (await client.get(url, headers=hall["door_h"])).status_code == 200
    assert (await client.get(url, headers=hall["other_h"])).status_code == 403
    admin = await make_admin(email="door-platform@example.com")
    assert (await client.get(url, headers=auth_header(await mint_token(admin)))).status_code == 200


async def test_a_card_counts_as_printed_only_when_asked_and_the_key_is_a_header(client, session, hall, monkeypatch):
    from app.api.idcards.models import PrintJobs
    from tests.doc_designs_fake import install

    install(monkeypatch)
    guest = hall["guests"][0]
    card = _url(hall, f"/idcards/{guest.id}.pdf")

    async def jobs():
        return len((await session.scalars(select(PrintJobs).where(PrintJobs.registration_id == guest.id))).all())

    assert (await client.get(card, headers=hall["h"])).status_code == 200
    assert await jobs() == 0  # a link opened is a preview
    assert (await client.get(card, params={"record": "true"}, headers=hall["h"])).status_code == 200
    assert await jobs() == 1
    key = await _gate_key(client, hall)
    assert (await client.get(card, params={"key": key, "record": "true"})).status_code == 401
    assert (await client.get(card, params={"record": "true"}, headers={"X-Gate-Key": key})).status_code == 200
    assert await jobs() == 2


async def test_the_waiting_list_file_and_the_card_batch_are_exports(client, hall, make_admin, mint_token):
    from app.core.auth.permissions import AdminRoles

    viewer = await make_admin(email="door-viewer@example.com", role=AdminRoles.viewer)
    h = auth_header(await mint_token(viewer))
    for path in ("/ticketing/waitlist/export", "/idcards/batch.pdf"):
        r = await client.get(_url(hall, path), headers=h)
        assert r.status_code == 403, (path, r.text)


def test_the_masks():
    assert door.mask_email("ananya.iyer@gmail.com") == "a•••@gmail.com"
    assert door.mask_email("x@college.ac.in") == "x•••@college.ac.in"
    assert door.mask_email("no-at-sign") == "•••"
    assert door.mask_email("") == "" and door.mask_email(None) is None
    assert door.mask_phone("+91 98765 44321") == "•••• 4321"
    assert door.mask_phone("123") == "••••"
    assert door.mask_phone(None) is None and door.mask_phone("") == ""
    assert door.mask_ticket("MOA-7KQ2M-X4F2K") == "••••4F2K"
    assert door.mask_ticket("AB12") == "••••"
    assert door.mask_contact("a@b.co") == "a•••@b.co" and door.mask_contact("9876543210") == "•••• 3210"
    # At any depth; numbers stay numbers (the console's contact counts).
    assert door.masked({
        "email": "a@b.co", "contacts": {"email": 3, "phone": 1},
        "items": [{"ticket_id": "MOA-AAAAA-BBBBB", "offer_url": "https://x.test/o/1", "user": {"email": "u@v.in", "phone": None}}],
    }) == {
        "email": "a•••@b.co", "contacts": {"email": 3, "phone": 1},
        "items": [{"ticket_id": "••••BBBB", "offer_url": None, "user": {"email": "u•••@v.in", "phone": None}}],
    }
