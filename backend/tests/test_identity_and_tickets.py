"""Whose record is it: a registration found by address, a registration's id at
the door, and a certificate's public address.

- A registration made with an address is handed back only to its own
  account, or to an account that has shown the address is its own (Google,
  Apple, or the code typed back: users.service.merges_into). A guest
  session -- which anyone gets by registering somewhere with the address --
  or a password account never confirmed gets the "already registered"
  answer, and nothing of the registration: not its ticket id, phone or
  answers, and no say over its price, its order or its WhatsApp consent.
  The waiting list's "me" follows the same rule.
- The door takes a ticket id and nothing else. A registration's id is no
  entry pass, and the competition view no longer hands other guests' out.
- A certificate's public page, links and number say nothing of its ticket:
  it is reached by a public id of its own, drawn when it is issued.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.events.models import EventRegistrationsLink
from app.api.ticketing.models import WaitlistEntries
from app.api.users.models import SigninProviders, UserTypes
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import certificate_starter
from app.core.mail import links
from app.core.utils import keys
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc)
VICTIM = "victim-id@example.com"


def _register(client, event_id, email, *, headers=None, name="Vidya Raman", **extra):
    body = {"full_name": name, "email": email, "phone": "9876500011", **extra}
    return client.post(f"{API}/events/registration/{event_id}/register", json=body, headers=headers or {})


async def _reg(session, reg_id) -> EventRegistrationsLink:
    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == reg_id).execution_options(populate_existing=True)
    )


@pytest.fixture
async def events(session, make_club, make_event):
    owner, club = await make_club(name="Identity Club", email="identity-club@example.com")
    free = await make_event(club=club, created_by=owner, name="Free Talk", event_datetime=NOW + timedelta(days=4))
    target = await make_event(club=club, created_by=owner, name="Target Night", event_datetime=NOW + timedelta(days=6))
    paid = await make_event(club=club, created_by=owner, name="Paid Night", event_datetime=NOW + timedelta(days=8), has_fee=True, reg_fee=200)
    await session.commit()
    return {"owner": owner, "club": club, "free": free, "target": target, "paid": paid}


async def _guest_token_for(client, event, email):
    """What anyone can get: register at a free event with the address."""
    r = await _register(client, event.id, email, name="Someone Else")
    assert r.status_code == 200, r.text
    return auth_header(r.json()["auth_token"]["access_token"])


def _nothing_of(response, reg: EventRegistrationsLink):
    text = response.text
    for secret in (reg.ticket_id, str(reg.id), reg.phone or "never-a-phone", reg.certificate_no or "never-a-number"):
        assert secret not in text, secret


# ─── 1. A registration found by its address ─────────────────────────────────


async def test_a_guest_token_with_someones_address_gets_nothing_of_their_registration(client, session, events):
    victim = await _register(client, events["target"].id, VICTIM, organization="Victim Org")
    assert victim.status_code == 200, victim.text
    theirs = await _reg(session, victim.json()["event_registration_id"])

    token = await _guest_token_for(client, events["free"], VICTIM)
    r = await _register(client, events["target"].id, VICTIM, headers=token, name="Attacker")
    assert r.status_code == 409, r.text
    assert r.json()["error_code"] == "ALREADY_REGISTERED"
    _nothing_of(r, theirs)
    assert "Victim Org" not in r.text
    # And no second registration was made for the address either.
    count = len(list(await session.scalars(
        select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == events["target"].id)
    )))
    assert count == 1


async def test_back_to_pay_is_the_holders_alone(client, session, events):
    """The branch that re-prices, changes the order and sets WhatsApp consent."""
    victim = await _register(client, events["paid"].id, VICTIM)
    assert victim.status_code == 200 and victim.json()["pay_amount"] == 200.0, victim.text
    theirs = await _reg(session, victim.json()["event_registration_id"])
    before = (theirs.ticket_type_id, theirs.actual_amount, theirs.whatsapp_opt_in)

    token = await _guest_token_for(client, events["free"], VICTIM)
    r = await _register(
        client, events["paid"].id, VICTIM, headers=token, whatsapp_opt_in=True, store=[], discount_code="ANYTHING",
    )
    assert r.status_code == 409 and r.json()["error_code"] == "ALREADY_REGISTERED", r.text
    _nothing_of(r, theirs)
    after = await _reg(session, theirs.id)
    assert (after.ticket_type_id, after.actual_amount, after.whatsapp_opt_in) == before
    assert after.whatsapp_opt_in_at is None


async def test_an_unconfirmed_password_account_gets_nothing_either(client, session, events, make_user, mint_token):
    victim = await _register(client, events["paid"].id, VICTIM)
    theirs = await _reg(session, victim.json()["event_registration_id"])
    typed = await make_user(email=VICTIM, username="typed-victim")  # /user/register: the address only typed
    assert typed.email_verified_at is None and not typed.email_verified
    await session.commit()
    r = await _register(client, events["paid"].id, VICTIM, headers=auth_header(await mint_token(typed)))
    assert r.status_code == 409 and r.json()["error_code"] == "ALREADY_REGISTERED", r.text
    _nothing_of(r, theirs)


async def test_a_guest_back_in_the_same_browser_still_reaches_their_own(client, session, events):
    first = await _register(client, events["paid"].id, VICTIM)
    token = auth_header(first.json()["auth_token"]["access_token"])
    again = await _register(client, events["paid"].id, VICTIM, headers=token)
    assert again.status_code == 200, again.text
    assert again.json()["event_registration_id"] == first.json()["event_registration_id"]
    assert again.json()["ticket_id"] == first.json()["ticket_id"]
    # On a free event, their own is the 400 the app has always had.
    free = await _register(client, events["free"].id, "free-once@example.com")
    twice = await _register(client, events["free"].id, "free-once@example.com", headers=auth_header(free.json()["auth_token"]["access_token"]))
    assert twice.status_code == 400 and twice.json()["error_code"] == "ALREADY_REGISTERED"


@pytest.mark.parametrize("vouched", ["google", "code"])
async def test_an_account_that_proved_the_address_takes_up_the_guests_registration(
    client, session, events, make_user, mint_token, vouched
):
    guest = await _register(client, events["paid"].id, VICTIM)
    owner = await make_user(email=VICTIM, username=f"owner-{vouched}")
    if vouched == "google":
        owner.provider = SigninProviders.google
    else:
        owner.email_verified_at = NOW
    await session.commit()
    r = await _register(client, events["paid"].id, VICTIM, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 200, r.text
    assert r.json()["event_registration_id"] == guest.json()["event_registration_id"]
    assert r.json()["ticket_id"] == guest.json()["ticket_id"]


async def test_even_a_proven_account_never_takes_another_accounts_registration(
    client, session, events, make_user, make_registration, mint_token
):
    """The merge folds guests only; a registration held by an app account
    under an address it was edited to stays that account's."""
    other = await make_user(email="someone-else@example.com", username="someone-else")
    held = await make_registration(event=events["paid"], user=other, email=VICTIM, actual_amount=200)
    owner = await make_user(email=VICTIM, username="owner-google")
    owner.provider = SigninProviders.google
    await session.commit()
    r = await _register(client, events["paid"].id, VICTIM, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 409 and r.json()["error_code"] == "ALREADY_REGISTERED", r.text
    _nothing_of(r, held)


async def test_the_organiser_adding_someone_is_told_as_before(client, session, events, mint_token):
    """Staff paths (by hand, import, the door) are not the self-service rule."""
    await _register(client, events["free"].id, VICTIM)
    h = auth_header(await mint_token(events["owner"]))
    r = await client.post(
        f"{API}/events/registration/{events['free'].id}/attendees",
        json={"full_name": "Vidya Raman", "email": VICTIM, "role": "attendee"}, headers=h,
    )
    assert r.status_code == 400 and r.json()["error_code"] == "ALREADY_REGISTERED", r.text


# ─── The waiting list's "me" ────────────────────────────────────────────────


@pytest.fixture
async def line(client, session, events, mint_token):
    from types import SimpleNamespace

    from tests.test_ticketing import set_types

    event = events["target"]
    h = auth_header(await mint_token(events["owner"]))
    types = await set_types(client, SimpleNamespace(h=h), event, [{"name": "Seat", "capacity": 1}])
    r = await client.put(f"{API}/events/{event.id}/ticketing/waitlist/settings", json={"enabled": True}, headers=h)
    assert r.status_code == 200, r.text
    assert (await _register(client, event.id, "first-in@example.com", ticket_type_id=types["Seat"]["id"])).status_code == 200
    joined = await client.post(
        f"{API}/events/{event.id}/waitlist", json={"full_name": "Vidya Raman", "email": VICTIM, "ticket_type_id": types["Seat"]["id"]},
    )
    assert joined.status_code == 200, joined.text
    return SimpleNamespace(event=event, entry_id=joined.json()["id"])


async def test_a_guest_token_with_the_address_neither_sees_nor_leaves_their_place(client, session, events, line):
    token = await _guest_token_for(client, events["free"], VICTIM)
    url = f"{API}/events/{line.event.id}/waitlist/me"
    assert (await client.get(url, headers=token)).json() == []
    assert (await client.delete(url, headers=token)).json() == {"left": 0}
    info = (await client.get(f"{API}/events/info/{line.event.id}", headers=token)).json()
    assert info["ticketing"]["viewer_waitlist"] == []
    entry = await session.scalar(select(WaitlistEntries).where(WaitlistEntries.id == uuid.UUID(line.entry_id)).execution_options(populate_existing=True))
    assert entry.status == "waiting"


async def test_the_addresss_proven_owner_still_sees_and_leaves_it(client, session, line, make_user, mint_token):
    owner = await make_user(email=VICTIM, username="line-owner")
    owner.provider = SigninProviders.google
    await session.commit()
    h = auth_header(await mint_token(owner))
    url = f"{API}/events/{line.event.id}/waitlist/me"
    assert [e["id"] for e in (await client.get(url, headers=h)).json()] == [line.entry_id]
    assert (await client.delete(url, headers=h)).json() == {"left": 1}


# ─── 2. Only a ticket id opens the door ─────────────────────────────────────


@pytest.fixture
async def door(client, session, make_club, make_event, make_user, make_registration, mint_token):
    from app.api.events.volunteer.models import Volunteer

    owner, club = await make_club(name="Door Club", email="door-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Door Night", event_datetime=NOW, duration=3.0)
    a = await make_user(email="door-a@example.com")
    b = await make_user(email="door-b@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Asha Door")
    r_b = await make_registration(event=event, user=b, full_name="Bimal Door")
    vol = await make_user(email="door-vol@example.com")
    session.add(Volunteer(email=vol.email, event_id=event.id, user_id=vol.id, is_approved=True))
    await session.commit()
    return {
        "event": event, "a": r_a, "b": r_b, "club": auth_header(await mint_token(owner)),
        "vol": auth_header(await mint_token(vol)),
    }


def _scan(code):
    return {"code": code, "door": "Gate A", "client_ref": uuid.uuid4().hex}


async def test_a_registration_id_is_refused_as_a_scan_code(client, session, door):
    url = f"{API}/events/{door['event'].id}/gate/scan"
    for code in (str(door["a"].id), str(door["a"].id).upper(), f" {door['a'].id} "):
        r = await client.post(url, json=_scan(code), headers=door["club"])
        assert r.status_code == 200 and r.json()["outcome"] == "INVALID", (code, r.text)
    assert (await _reg(session, door["a"].id)).is_attended is False
    # The offline queue is the same door.
    batch = await client.post(f"{API}/events/{door['event'].id}/gate/scan-batch", json={"scans": [_scan(str(door["a"].id))]}, headers=door["club"])
    assert batch.status_code == 200, batch.text
    assert batch.json()["results"][0]["response"]["outcome"] == "INVALID"
    # The volunteer app's scanner and its validator too.
    vol = await client.post(f"{API}/events/volunteer/checkin/{door['event'].id}", json={"ticket_id": str(door["a"].id)}, headers=door["vol"])
    assert vol.status_code == 400 and vol.json()["error_code"] == "REGISTRATION_NOT_FOUND", vol.text
    valid = await client.post(f"{API}/events/volunteer/validate/{door['event'].id}", json={"ticket_id": str(door["a"].id)}, headers=door["vol"])
    assert valid.status_code == 400 and valid.json()["error_code"] == "REGISTRATION_NOT_FOUND", valid.text
    assert (await _reg(session, door["a"].id)).is_attended is False


async def test_the_ticket_id_still_admits_and_a_person_still_checks_someone_in_by_hand(client, session, door):
    url = f"{API}/events/{door['event'].id}/gate"
    ok = await client.post(f"{url}/scan", json=_scan(door["a"].ticket_id.lower()), headers=door["club"])
    assert ok.json()["outcome"] == "CHECKED_IN", ok.text
    # Picked out in the search and tapped: an authenticated, explicit check-in by id.
    by_hand = await client.post(f"{url}/check-in/{door['b'].id}", json={"door": "Desk"}, headers=door["club"])
    assert by_hand.status_code == 200 and by_hand.json()["outcome"] == "CHECKED_IN", by_hand.text
    assert (await _reg(session, door["b"].id)).is_attended is True
    # The attendees page's tick, by id, as before.
    await client.post(f"{url}/check-in/{door['a'].id}", json={}, headers=door["club"])  # already in: answered, not admitted twice
    undo = await client.delete(f"{url}/check-in/{door['b'].id}", headers=door["club"])
    assert undo.status_code == 200, undo.text
    page = await client.post(
        f"{API}/events/registration/{door['event'].id}/attendance/{door['b'].id}", json={"is_attended": True}, headers=door["club"],
    )
    assert page.status_code == 200, page.text
    assert (await _reg(session, door["b"].id)).is_attended is True


async def test_the_competition_view_names_other_guests_without_their_ids(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    owner, club = await make_club(name="Comp Club", email="comp-ids@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Ids", event_datetime=NOW + timedelta(days=2), duration=8.0)
    users = [await make_user(email=f"comp-ids{i}@example.com") for i in range(3)]
    regs = [await make_registration(event=event, user=u, full_name=n) for u, n in zip(users, ("Aarav K", "Diya K", "Kabir M"))]
    await session.commit()
    h = auth_header(await mint_token(owner))
    u = [auth_header(await mint_token(x)) for x in users]
    base = f"{API}/events/{event.id}/competition"
    assert (await client.patch(base, json={"enabled": True, "team_max": 3}, headers=h)).status_code == 200
    team = (await client.post(f"{base}/me/team", json={"name": "Byte Benders"}, headers=u[0])).json()
    await client.post(f"{base}/me/team/join/{team['team']['id']}", headers=u[1])
    await client.put(f"{base}/me/seeking", json={"on": True, "skills": ["PCB"]}, headers=u[2])

    me = await client.get(f"{base}/me", headers=u[0])
    assert me.status_code == 200, me.text
    body = me.json()
    assert [m["name"] for m in body["team"]["members"]] == ["Aarav K", "Diya K"]
    assert body["team"]["members"] == [{"name": "Aarav K"}, {"name": "Diya K"}]
    assert body["seekers"] == [{"name": "Kabir M", "skills": ["PCB"], "note": None}]
    assert body["registration_id"] == str(regs[0].id)  # their own
    for other in regs[1:]:
        assert str(other.id) not in me.text
    seeker = (await client.get(f"{base}/me", headers=u[2])).json()
    assert seeker["seeking"] == {"name": "Kabir M", "skills": ["PCB"], "note": None}
    for other in regs[:2]:
        assert str(other.id) not in str(seeker)
    # The organiser's own view keeps the ids it manages teams by.
    assert str(regs[1].id) in (await client.get(base, headers=h)).text


# ─── 3. A certificate says nothing of its ticket ────────────────────────────


@pytest.fixture
async def cert(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Cert Club", email="cert-ids@example.com")
    event = await make_event(club=club, created_by=owner, name="Cert Night", event_datetime=NOW - timedelta(days=1), duration=3.0)
    a = await make_user(email="cert-a@example.com")
    b = await make_user(email="cert-b@example.com")
    old = await make_user(email="cert-old@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Asha Cert", is_attended=True)
    r_b = await make_registration(event=event, user=b, full_name="Bimal Cert")
    r_old = await make_registration(event=event, user=old, full_name="Old Cert", is_attended=True, ticket_id="MOA-2BCEBA9-PFZG4")
    r_old.certificate_no = None  # numbered from its ticket, before numbers were drawn on their own
    await session.commit()
    h = auth_header(await mint_token(owner))
    st = certificate_starter("classic")
    r = await client.put(
        f"{API}/events/{event.id}/designs/certificate/layout",
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=h,
    )
    assert r.status_code == 200, r.text
    issued = await client.post(f"{API}/events/{event.id}/certificates/issue", json={"audience": "checked_in"}, headers=h)
    assert issued.status_code == 200 and issued.json()["issued"] == 2, issued.text
    for reg in (r_a, r_b, r_old):
        await session.refresh(reg)
    return {
        "event": event, "a": r_a, "b": r_b, "old": r_old, "club": h,
        "as_a": auth_header(await mint_token(a)), "as_b": auth_header(await mint_token(b)),
    }


def _body(ticket_id: str) -> str:
    return ticket_id.split("-", 1)[1]


async def test_issuing_draws_a_random_public_id_and_numbers_the_old_ones_afresh(cert):
    a, old, b = cert["a"], cert["old"], cert["b"]
    for reg in (a, old):
        assert keys.CERTIFICATE_PUBLIC_ID.match(reg.certificate_public_id), reg.certificate_public_id
        assert reg.ticket_id not in reg.certificate_public_id and _body(reg.ticket_id) not in reg.certificate_public_id
        assert keys.CERTIFICATE_CODE.match(reg.certificate_no)
        assert _body(reg.ticket_id) not in reg.certificate_no
        for part in _body(reg.ticket_id).split("-"):
            assert part not in reg.certificate_no
    assert a.certificate_public_id != old.certificate_public_id
    assert b.certificate_public_id is None  # never issued


def test_a_public_id_is_24_url_safe_characters_from_the_csprng(monkeypatch):
    import secrets

    seen = []
    real = secrets.token_urlsafe
    monkeypatch.setattr(secrets, "token_urlsafe", lambda n: (seen.append(n), real(n))[1])
    value = keys.generate_certificate_public_id()
    assert seen == [18] and len(value) == 24 and re.fullmatch(r"[A-Za-z0-9_-]{24}", value)
    assert len({keys.generate_certificate_public_id() for _ in range(2000)}) == 2000


async def test_every_certificate_url_the_api_gives_is_by_public_id(client, cert):
    a = cert["a"]
    expected = f"https://myotherapp.in/certificates/{a.certificate_public_id}"
    row = next(
        e for e in (await client.get(f"{API}/events/list", params={"is_registered": "true"}, headers=cert["as_a"])).json()["items"]
        if e["id"] == cert["event"].id
    )
    info = (await client.get(f"{API}/events/info/{cert['event'].id}", headers=cert["as_a"])).json()
    ticket = (await client.get(f"{API}/events/tickets/{a.ticket_id}", headers=cert["as_a"])).json()
    listed = next(
        r for r in (await client.get(f"{API}/events/{cert['event'].id}/certificates", headers=cert["club"])).json()["items"]
        if r["name"] == "Asha Cert"
    )
    for shown in (row, info, ticket, listed):
        assert shown["certificate_url"] == expected
    for shown in (row, info, ticket):
        assert shown["certificate_no"] == a.certificate_no
    # Never issued: no address.
    b_info = (await client.get(f"{API}/events/info/{cert['event'].id}", headers=cert["as_b"])).json()
    assert b_info["certificate_url"] is None and b_info["certificate_no"] is None


async def test_the_public_page_is_reached_by_its_public_id(client, cert):
    a = cert["a"]
    pid = a.certificate_public_id
    v = (await client.get(f"{API}/verify/certificates/{pid}")).json()
    assert (v["valid"], v["number"], v["attendee_name"], v["event_name"]) == (True, a.certificate_no, "Asha Cert", "Cert Night")
    assert a.ticket_id not in str(v)
    for path in (f"verify/certificates/{pid}/image", f"verify/certificates/{pid}/pdf", f"events/certificates/{pid}/image", f"events/certificates/{pid}/pdf"):
        r = await client.get(f"{API}/{path}")  # no session at all
        assert r.status_code == 200, (path, r.text[:200])
    pdf = await client.get(f"{API}/verify/certificates/{pid}/pdf")
    assert pdf.headers["content-type"] == "application/pdf" and a.ticket_id not in pdf.headers["content-disposition"]
    assert (await client.get(f"{API}/events/certificates/{pid}/style")).status_code == 200
    # Case matters: a public id is read exactly as its link carries it.
    if pid.swapcase() != pid:
        assert (await client.get(f"{API}/verify/certificates/{pid.swapcase()}")).json()["reason"] == "not_found"


async def test_no_public_certificate_route_answers_to_a_ticket_id(client, cert):
    a, old = cert["a"], cert["old"]
    for reg in (a, old):
        for code in (reg.ticket_id, f"certificate:{reg.ticket_id}", f"CERT-{_body(reg.ticket_id)}", reg.ticket_id.lower()):
            assert (await client.get(f"{API}/verify/certificates/{code}")).json()["reason"] == "not_found", code
            for route in ("image", "pdf"):
                assert (await client.get(f"{API}/verify/certificates/{code}/{route}")).status_code == 404, (code, route)
            assert (await client.get(f"{API}/verify/certificates/{code}/files")).json()["available"] is False
        # The site's own-certificate routes by ticket id: nothing for a stranger.
        for route in ("image", "pdf", "style"):
            assert (await client.get(f"{API}/events/certificates/{reg.ticket_id}/{route}")).status_code == 404, route


async def test_by_ticket_id_only_the_holder_their_club_or_an_admin(client, cert, make_admin, mint_token):
    a = cert["a"]
    for route in ("image", "pdf", "style"):
        url = f"{API}/events/certificates/{a.ticket_id}/{route}"
        assert (await client.get(url)).status_code == 404, route
        # Signed in as someone else: 403, as the ticket itself answers (never a 401).
        other = await client.get(url, headers=cert["as_b"])
        assert other.status_code == 403, (route, other.text)
        assert (await client.get(url, headers=cert["as_a"])).status_code == 200, route
        assert (await client.get(url, headers=cert["club"])).status_code == 200, route
    admin = auth_header(await mint_token(await make_admin(email="cert-admin@example.com")))
    assert (await client.get(f"{API}/events/certificates/{a.ticket_id}/pdf", headers=admin)).status_code == 200
    # A guest token that holds a different ticket: 403, the token kept.
    assert (await client.get(f"{API}/events/tickets/{a.ticket_id}", headers=cert["as_b"])).status_code == 403
    # Kept private: a shared cache never holds the holder's copy.
    r = await client.get(f"{API}/events/certificates/{a.ticket_id}/image", headers=cert["as_a"])
    assert r.headers["cache-control"].startswith("private")


async def test_the_emailed_link_is_by_public_id_and_an_old_ticket_link_opens_nothing(client, cert):
    a = cert["a"]
    pid = a.certificate_public_id
    fresh = await client.get(f"{API}/files/certificates/{pid}.pdf?s={links.file_token('certificate', pid)}")
    assert fresh.status_code == 200 and fresh.headers["content-type"] == "application/pdf"
    assert a.ticket_id not in links.file_url("certificate", pid)
    old = await client.get(f"{API}/files/certificates/{a.ticket_id}.pdf?s={links.file_token('certificate', a.ticket_id)}")
    assert old.status_code == 404
    forged = await client.get(f"{API}/files/certificates/{pid}.pdf?s=nope")
    assert forged.status_code == 403


async def test_the_guests_email_carries_the_public_link_and_the_number(session, cert):
    from sqlalchemy.orm import joinedload

    from app.api.deliveries import service as deliveries
    from app.api.emails import service as email_service
    from app.api.events.models import Events

    reg = await session.scalar(
        select(EventRegistrationsLink)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club))
        .where(EventRegistrationsLink.id == cert["a"].id).execution_options(populate_existing=True)
    )
    values = email_service.guest_values(reg.event, reg)
    assert cert["a"].certificate_public_id in values["certificate_url"]
    assert reg.ticket_id not in values["certificate_url"]
    copy = deliveries.copy_for(deliveries.DeliveryKind.certificate, reg, reg.event, "Cert Club")
    assert reg.certificate_no in copy["text"] and reg.ticket_id not in copy["text"] and reg.ticket_id not in copy["html"]


async def test_revoking_and_reinstating_keep_the_public_id(client, session, cert):
    a = cert["a"]
    pid = a.certificate_public_id
    base = f"{API}/events/{cert['event'].id}/certificates"
    await client.post(f"{base}/{a.id}/revoke", headers=cert["club"])
    assert (await client.get(f"{API}/verify/certificates/{pid}")).json()["reason"] == "revoked"
    assert (await client.get(f"{API}/verify/certificates/{pid}/pdf")).status_code == 404
    await client.post(f"{base}/{a.id}/reinstate", headers=cert["club"])
    await client.post(f"{base}/issue", json={"audience": "everyone"}, headers=cert["club"])
    assert (await _reg(session, a.id)).certificate_public_id == pid
    assert (await client.get(f"{API}/verify/certificates/{pid}")).json()["valid"] is True
