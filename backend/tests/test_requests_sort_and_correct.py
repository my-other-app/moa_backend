"""The review queue's basics (app/api/approvals): sorting the club's list by
when they asked, name or email, either way, page by page; and correcting a
request -- a name misspelt, an email typo -- without deciding or emailing,
on its registration too once it is confirmed."""
import uuid

from sqlalchemy import select

from app.api.approvals.models import RegistrationRequests as R
from app.api.events.models import EventRegistrationsLink
from tests.test_approvals import API, _req, approve, ask, decline, listing, mail, pushed, shop  # noqa: F401


def correct(client, w, rid, **body):
    return client.patch(f"{API}/events/registration/{w.event.id}/requests/{rid}", json=body, headers=w.h)


def _names(page) -> list[str]:
    return [i["full_name"] for i in page["items"]]


async def _walk(client, w, **params) -> list[str]:
    """Every name, page by page, following `next`."""
    out, after = [], None
    while True:
        page = (await listing(client, w, limit=2, **({"after": after} if after else {}), **params)).json()
        out += _names(page)
        after = page["next"]
        if not after:
            return out


async def test_the_queue_sorts_by_name_and_email_either_way_page_by_page(client, session, shop):
    people = [("Zara Quinn", "b@example.com"), ("adam Lee", "e@example.com"), ("Maya Iyer", "a@example.com"),
              ("Bilal Khan", "d@example.com"), ("Chitra Nair", "c@example.com")]
    for n, (name, email) in enumerate(people):
        assert (await ask(client, shop.event, 40 + n, full_name=name, email=email)).status_code in (200, 201)

    # The default is unchanged: the queue, oldest first.
    assert await _walk(client, shop) == [p[0] for p in people]
    # Names A to Z, case ignored, and Z to A -- across pages.
    by_name = sorted((p[0] for p in people), key=str.lower)
    assert await _walk(client, shop, sort="name") == by_name
    assert await _walk(client, shop, sort="name", order="desc") == by_name[::-1]
    # Email, and when they asked turned round.
    assert await _walk(client, shop, sort="email") == [p[0] for p in sorted(people, key=lambda p: p[1])]
    assert await _walk(client, shop, sort="submitted", order="desc") == [p[0] for p in people][::-1]

    # A page link only works for the order it came from.
    named = (await listing(client, shop, limit=2, sort="name")).json()["next"]
    plain = (await listing(client, shop, limit=2)).json()["next"]
    assert (await listing(client, shop, after=named)).status_code == 422
    assert (await listing(client, shop, after=plain, sort="name")).status_code == 422
    assert (await listing(client, shop, after=named, sort="email")).status_code == 422
    assert (await listing(client, shop, sort="phone")).status_code == 422
    assert (await listing(client, shop, order="up")).status_code == 422


async def test_a_waiting_request_is_corrected_and_nothing_is_decided_or_sent(client, session, shop):
    rid = (await ask(client, shop.event, 60, full_name="Anu Mathw", email="anu.typo@example.com")).json()["approval"]["id"]
    sent_before = len(shop.mail)

    r = await correct(client, shop, rid, full_name="Anu Mathew", email="anu@example.com", phone="9876500060",
                      answers={"organisation": "DUK"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["full_name"], body["email"], body["phone"], body["status"]) == ("Anu Mathew", "anu@example.com", "9876500060", "pending")
    assert body["answers"]["organisation"] == "DUK"

    row = await _req(session, rid)
    assert (row.full_name, row.email, row.status, row.decided_at) == ("Anu Mathew", "anu@example.com", "pending", None)
    assert len(shop.mail) == sent_before  # nothing emailed
    # Found by the new address, not the old one.
    assert _names((await listing(client, shop, q="anu@example")).json()) == ["Anu Mathew"]
    assert _names((await listing(client, shop, q="anu.typo")).json()) == []

    # Only what's sent changes; a blank name is refused; so is a bad email.
    assert (await correct(client, shop, rid, phone="9876500061")).json()["full_name"] == "Anu Mathew"
    assert (await correct(client, shop, rid, full_name="")).status_code == 422
    assert (await correct(client, shop, rid, email="not-an-email")).status_code == 422


async def test_a_declined_request_can_be_corrected_too(client, session, shop):
    rid = (await ask(client, shop.event, 61)).json()["approval"]["id"]
    await decline(client, shop, ids=[rid], notify=False)
    r = await correct(client, shop, rid, full_name="Renamed Person")
    assert r.status_code == 200 and r.json()["status"] == "declined" and r.json()["full_name"] == "Renamed Person"


async def test_an_email_another_request_or_a_registration_uses_is_refused(client, session, shop):
    a = (await ask(client, shop.event, 62)).json()["approval"]["id"]
    (await ask(client, shop.event, 63)).json()
    taken = await correct(client, shop, a, email="ASKER63@example.com")
    assert taken.status_code == 409 and "request" in taken.json()["message"]

    # A confirmed request is still open: its address is a request's too.
    c = (await ask(client, shop.event, 64)).json()["approval"]["id"]
    assert (await approve(client, shop, ids=[c])).status_code == 200
    assert (await correct(client, shop, a, email="asker64@example.com")).status_code == 409

    # A guest added by hand at the desk holds an address with no request.
    added = await client.post(
        f"{API}/events/registration/{shop.event.id}/attendees",
        json={"full_name": "Desk Guest", "email": "desk.guest@example.com"}, headers=shop.h,
    )
    assert added.status_code in (200, 201), added.text
    held = await correct(client, shop, a, email="Desk.Guest@example.com")
    assert held.status_code == 409 and "registered" in held.json()["message"]

    # Their own address again, in other case, is no change and no clash.
    assert (await correct(client, shop, a, email="Asker62@example.com")).status_code == 200


async def test_a_confirmed_request_is_corrected_on_its_registration_too(client, session, shop):
    rid = (await ask(client, shop.event, 65)).json()["approval"]["id"]
    assert (await approve(client, shop, ids=[rid])).status_code == 200
    req = await _req(session, rid)
    assert req.status == "approved" and req.registration_id is not None

    r = await correct(client, shop, rid, full_name="Fixed Name", email="fixed@example.com")
    assert r.status_code == 200, r.text
    reg = await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == req.registration_id)
        .execution_options(populate_existing=True)
    )
    assert (reg.full_name, reg.email) == ("Fixed Name", "fixed@example.com")
    assert (await _req(session, rid)).email == "fixed@example.com"


async def test_unknown_or_another_events_request_is_not_found_and_door_staff_cannot_correct(client, session, shop, make_club, make_event, mint_token):
    assert (await correct(client, shop, uuid.uuid4(), full_name="X")).status_code == 404
    assert (await correct(client, shop, "not-a-uuid", full_name="X")).status_code == 404

    from tests.test_approvals import _shop

    other = await _shop(session, make_club, make_event, mint_token, tag="otherev")
    theirs = (await ask(client, other.event, 66)).json()["approval"]["id"]
    assert (await correct(client, shop, theirs, full_name="X")).status_code == 404
    # Another club's owner can't reach this event's queue at all.
    mine = (await ask(client, shop.event, 67)).json()["approval"]["id"]
    assert (await client.patch(f"{API}/events/registration/{shop.event.id}/requests/{mine}", json={"full_name": "X"}, headers=other.h)).status_code in (403, 404)
    assert (await session.scalar(select(R.full_name).where(R.id == uuid.UUID(mine)))) == "Asker Number 67"

    from app.api.club_access import policy

    path = f"/api/v1/events/registration/{shop.event.id}/requests/{mine}"
    assert not policy.allows("door", "PATCH", path)
    assert policy.allows("editor", "PATCH", path) and policy.allows("admin", "PATCH", path)
