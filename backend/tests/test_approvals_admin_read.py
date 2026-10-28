"""Platform admins and an event's registrations under review: they read the
queue and export it for any event, read-only, through the same rule as an
event's attendees (ClubAuth admits an admin, who needs VIEW to read and
EXPORT_DATA for the whole list as a file; app/api/events/access.py lets them
past the ownership check). Confirming, declining and putting back stay the
club's (approvals.router._actor)."""
import uuid

from sqlalchemy import func, select

from app.api.approvals.models import RegistrationRequests as R
from app.api.users.models import AdminRoles
from tests.conftest import auth_header
from tests.test_approvals import API, _regs, _shop, ask, decline, mail  # noqa: F401 - mail is a fixture


def _requests(event_id: int) -> str:
    return f"{API}/events/registration/{event_id}/requests"


async def _queue(client, session, make_club, make_event, mint_token, tag):
    """An event that reviews registrations: three people registered, the
    club declined the third."""
    w = await _shop(session, make_club, make_event, mint_token, tag=tag)
    ids = []
    for n in (1, 2, 3):
        r = await ask(client, w.event, n)
        assert r.status_code == 200, r.text
        ids.append(r.json()["approval"]["id"])
    r = await decline(client, w, ids=[ids[2]], notify=False)
    assert r.status_code == 200, r.text
    w.ids = ids
    return w


async def _statuses(session, event_id) -> dict:
    rows = (await session.execute(
        select(R.id, R.status).where(R.event_id == event_id).execution_options(populate_existing=True)
    )).all()
    return {str(i): s for i, s in rows}


async def test_an_admin_reads_the_list_and_the_export_of_any_event(client, session, make_club, make_event, make_admin, mint_token, mail):
    w = await _queue(client, session, make_club, make_event, mint_token, "adm1")
    h = auth_header(await mint_token(await make_admin()))

    pending = await client.get(_requests(w.event.id), headers=h)
    assert pending.status_code == 200, pending.text
    body = pending.json()
    # The queue, oldest first, with every status's count.
    assert [i["full_name"] for i in body["items"]] == ["Asker Number 1", "Asker Number 2"]
    assert body["counts"]["pending"] == 2 and body["counts"]["declined"] == 1 and body["counts"]["total"] == 3
    first = body["items"][0]
    assert (first["email"], first["phone"], first["ticket_type"]["name"]) == ("asker1@example.com", "9876500001", "General admission")
    assert first["submitted_at"]

    every = await client.get(_requests(w.event.id), params={"status": "all"}, headers=h)
    assert every.status_code == 200 and len(every.json()["items"]) == 3
    found = await client.get(_requests(w.event.id), params={"status": "all", "q": "asker3"}, headers=h)
    assert [i["status"] for i in found.json()["items"]] == ["declined"]
    # Pages by the server's cursor, as the club's do.
    page = await client.get(_requests(w.event.id), params={"limit": 1}, headers=h)
    assert page.json()["next"]
    nxt = await client.get(_requests(w.event.id), params={"limit": 1, "after": page.json()["next"]}, headers=h)
    assert [i["full_name"] for i in nxt.json()["items"]] == ["Asker Number 2"]

    export = await client.get(f"{_requests(w.event.id)}/export", headers=h)
    assert export.status_code == 200, export.text
    assert export.headers["content-type"].startswith("text/csv")
    assert export.headers["cache-control"] == "no-store"
    assert "attachment" in export.headers["content-disposition"]
    text = export.text
    for n in (1, 2, 3):
        assert f"asker{n}@example.com" in text
    # Reading changed nothing.
    assert set((await _statuses(session, w.event.id)).values()) == {"pending", "declined"}


async def test_an_admin_without_export_data_reads_the_list_but_not_the_file(client, session, make_club, make_event, make_admin, mint_token, mail):
    """The attendees' export asks an admin for EXPORT_DATA (one file carries
    every guest's name, email, phone and answers); so does this one. VIEW,
    which every admin role has, reads the list."""
    w = await _queue(client, session, make_club, make_event, mint_token, "adm2")
    for role in (AdminRoles.viewer, AdminRoles.support):
        h = auth_header(await mint_token(await make_admin(email=f"{role.value}-adm2@example.com", role=role)))
        listed = await client.get(_requests(w.event.id), headers=h)
        assert listed.status_code == 200, listed.text
        for path in (f"{_requests(w.event.id)}/export", f"{API}/events/registration/{w.event.id}/export"):
            refused = await client.get(path, headers=h)
            assert refused.status_code == 403, (role, path, refused.text)
            assert refused.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"


async def test_an_admin_cannot_confirm_decline_or_put_back(client, session, make_club, make_event, make_admin, mint_token, mail):
    w = await _queue(client, session, make_club, make_event, mint_token, "adm3")
    h = auth_header(await mint_token(await make_admin()))
    before = await _statuses(session, w.event.id)

    for path, body in (
        ("approve", {"ids": [w.ids[0]]}),
        ("approve", {"all_pending": True}),
        ("decline", {"ids": [w.ids[1]], "notify": False}),
        ("decline", {"all_pending": True, "notify": False}),
        ("reopen", {"ids": [w.ids[2]]}),
    ):
        r = await client.post(f"{_requests(w.event.id)}/{path}", json=body, headers=h)
        assert r.status_code == 403, (path, body, r.text)
        assert r.json()["error_code"] == "INSUFFICIENT_PERMISSIONS"

    assert await _statuses(session, w.event.id) == before
    assert await _regs(session, w.event.id) == 0
    # The club still decides.
    r = await client.post(f"{_requests(w.event.id)}/approve", json={"ids": [w.ids[0]]}, headers=w.h)
    assert r.status_code == 200 and [a["id"] for a in r.json()["approved"]] == [w.ids[0]], r.text


async def test_another_club_cannot_read_the_requests(client, session, make_club, make_event, mint_token, mail):
    w = await _queue(client, session, make_club, make_event, mint_token, "adm4")
    intruder, _ = await make_club(name="Nosy Club", email="nosy-adm4@example.com")
    h = auth_header(await mint_token(intruder))
    for path in (_requests(w.event.id), f"{_requests(w.event.id)}/export"):
        r = await client.get(path, headers=h)
        assert r.status_code == 403, (path, r.text)
    # The host club still reads it.
    own = await client.get(_requests(w.event.id), headers=w.h)
    assert own.status_code == 200 and own.json()["counts"]["total"] == 3


async def test_an_ordinary_user_is_refused(client, session, make_club, make_event, make_user, mint_token, mail):
    w = await _queue(client, session, make_club, make_event, mint_token, "adm5")
    h = auth_header(await mint_token(await make_user(email="someone-adm5@example.com")))
    for path in (_requests(w.event.id), f"{_requests(w.event.id)}/export"):
        r = await client.get(path, headers=h)
        assert r.status_code == 403, (path, r.text)
        signed_out = await client.get(path)
        assert signed_out.status_code == 401, (path, signed_out.text)
    total = await session.scalar(select(func.count()).select_from(R).where(R.event_id == w.event.id))
    assert total == 3


async def test_an_unknown_event_is_not_found_for_an_admin(client, make_admin, mint_token):
    h = auth_header(await mint_token(await make_admin(email="lost-admin@example.com")))
    r = await client.get(_requests(10_000_000), headers=h)
    assert r.status_code == 404, r.text
    # And an id that isn't one is the router's 422, not a leak.
    assert (await client.get(f"{API}/events/registration/{uuid.uuid4()}/requests", headers=h)).status_code == 422
