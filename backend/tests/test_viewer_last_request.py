"""The event page's word on a registration under review once it is no longer
open (app/api/approvals viewer_last_request): declined (with the club's
note, and no registering again), cancelled by them or expired (they may
register again). `viewer_request` keeps meaning "open", for apps already out."""
from tests.conftest import auth_header
from tests.test_approvals import API, ask, decline, mail, pushed, shop  # noqa: F401


def tickets(client, w, h):
    return client.get(f"{API}/events/{w.event.id}/ticket-types", headers=h)


async def test_a_declined_registration_says_so_and_not_register_again(client, session, shop, make_user, mint_token):
    user = await make_user(email="declined.viewer@example.com", username="declinedviewer")
    await session.commit()
    h = auth_header(await mint_token(user))

    # Nothing asked yet: neither field says anything.
    before = (await tickets(client, shop, h)).json()
    assert before["viewer_request"] is None and before["viewer_last_request"] is None

    asked = (await ask(client, shop.event, 70, email="declined.viewer@example.com", headers=h)).json()["approval"]
    open_ = (await tickets(client, shop, h)).json()
    assert open_["viewer_request"]["status"] == "pending" and open_["viewer_last_request"] is None

    await decline(client, shop, ids=[asked["id"]], notify=False, note="We are full, sorry")
    out = (await tickets(client, shop, h)).json()
    assert out["viewer_request"] is None  # unchanged meaning: open only
    last = out["viewer_last_request"]
    assert (last["id"], last["status"], last["decline_note"], last["can_register_again"]) == (
        asked["id"], "declined", "We are full, sorry", False,
    )
    assert last["token"] and last["status_url"].endswith(last["token"]) and last["decided_at"]
    # Registering again is refused, as the page now says.
    again = await ask(client, shop.event, 70, email="declined.viewer@example.com", headers=h)
    assert again.status_code == 409 and again.json()["error_code"] == "REQUEST_DECLINED"


async def test_a_cancelled_registration_may_register_again(client, session, shop, make_user, mint_token):
    user = await make_user(email="withdrawn.viewer@example.com", username="withdrawnviewer")
    await session.commit()
    h = auth_header(await mint_token(user))
    asked = (await ask(client, shop.event, 71, email="withdrawn.viewer@example.com", headers=h)).json()["approval"]
    assert (await client.post(f"{API}/registration-requests/{asked['token']}/withdraw")).status_code == 200

    last = (await tickets(client, shop, h)).json()["viewer_last_request"]
    assert (last["status"], last["decline_note"], last["can_register_again"]) == ("withdrawn", None, True)

    # Asked again: the new one is open, and the old one is no longer shown.
    (await ask(client, shop.event, 71, email="withdrawn.viewer@example.com", headers=h)).json()
    out = (await tickets(client, shop, h)).json()
    assert out["viewer_request"]["status"] == "pending" and out["viewer_last_request"] is None


async def test_a_stranger_sees_nothing_of_anyone_elses(client, session, shop, make_user, mint_token):
    asked = (await ask(client, shop.event, 72)).json()["approval"]
    await decline(client, shop, ids=[asked["id"]], notify=False)
    other = await make_user(email="someone.else@example.com", username="someoneelse")
    await session.commit()
    out = (await tickets(client, shop, auth_header(await mint_token(other)))).json()
    assert out["viewer_request"] is None and out["viewer_last_request"] is None
    anonymous = (await client.get(f"{API}/events/{shop.event.id}/ticket-types")).json()
    assert anonymous.get("viewer_last_request") is None
