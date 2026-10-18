"""Push tokens: a phone that signs out stops getting the account's pushes,
a token belongs to the one account signed in on that device, and tokens FCM
says are dead are removed as they are found. Plus PROFILE_EXISTS."""
from types import SimpleNamespace

import pytest
from firebase_admin import exceptions as firebase_exceptions, messaging
from sqlalchemy import select

from app.api.users.models import UserDeviceTokens
from app.core.notifications import service as push
from tests.conftest import auth_header

API = "/api/v1"


async def _tokens(session, user_id=None) -> list[tuple[int, str]]:
    q = select(UserDeviceTokens.user_id, UserDeviceTokens.fcm_token).execution_options(include_deleted=True)
    if user_id is not None:
        q = q.where(UserDeviceTokens.user_id == user_id)
    return sorted((await session.execute(q.order_by(UserDeviceTokens.id))).all())


@pytest.fixture
async def people(session, make_user, mint_token):
    asha = await make_user(email="asha-tokens@example.com", username="asha-tokens")
    ravi = await make_user(email="ravi-tokens@example.com", username="ravi-tokens")
    await session.commit()
    return SimpleNamespace(asha=asha, ravi=ravi, asha_h=auth_header(await mint_token(asha)), ravi_h=auth_header(await mint_token(ravi)))


def _register(client, headers, token, platform="android"):
    return client.post(f"{API}/user/fcm-token", json={"fcm_token": token, "platform": platform}, headers=headers)


def _remove(client, headers, token):
    return client.request("DELETE", f"{API}/user/fcm-token", json={"token": token}, headers=headers)


async def test_signing_out_removes_the_devices_token(client, session, people):
    assert (await _register(client, people.asha_h, "tok-asha-phone")).status_code == 200
    assert (await _register(client, people.ravi_h, "tok-ravi-phone", platform="ios")).status_code == 200
    assert await _tokens(session, people.asha.id) == [(people.asha.id, "tok-asha-phone")]

    # Someone else's token is not theirs to remove.
    assert (await _remove(client, people.ravi_h, "tok-asha-phone")).status_code == 204
    assert await _tokens(session, people.asha.id) == [(people.asha.id, "tok-asha-phone")]

    r = await _remove(client, people.asha_h, "tok-asha-phone")
    assert r.status_code == 204 and r.content == b""
    assert await _tokens(session, people.asha.id) == []
    # Again, or a token never seen: still 204.
    assert (await _remove(client, people.asha_h, "tok-asha-phone")).status_code == 204
    assert (await _remove(client, people.asha_h, "tok-never-seen")).status_code == 204
    assert await _tokens(session, people.ravi.id) == [(people.ravi.id, "tok-ravi-phone")]

    assert (await _remove(client, {}, "tok-ravi-phone")).status_code == 401
    assert (await _remove(client, people.asha_h, "")).status_code == 422
    # Signing in again on the same phone brings the token back.
    assert (await _register(client, people.asha_h, "tok-asha-phone")).status_code == 200
    assert await _tokens(session, people.asha.id) == [(people.asha.id, "tok-asha-phone")]


async def test_a_token_belongs_to_the_account_signed_in_on_the_device(client, session, people):
    """A phone handed over (or signed out offline, never reaching DELETE):
    the next account to register its token takes it, and the first stops
    getting pushes there."""
    await _register(client, people.asha_h, "tok-shared-phone")
    await _register(client, people.ravi_h, "tok-shared-phone")
    assert await _tokens(session) == [(people.ravi.id, "tok-shared-phone")]
    assert await push.get_fcm_tokens_for_users(session, [people.asha.id]) == []


async def test_a_removed_token_row_is_revived_not_duplicated(client, session, people):
    await _register(client, people.asha_h, "tok-old")
    row = await session.scalar(select(UserDeviceTokens).where(UserDeviceTokens.user_id == people.asha.id))
    row.is_deleted = True
    await session.commit()
    # A token is unique whether or not its row is deleted: it is reused.
    assert (await _register(client, people.asha_h, "tok-old")).status_code == 200
    rows = list((await session.scalars(select(UserDeviceTokens).where(UserDeviceTokens.user_id == people.asha.id).execution_options(include_deleted=True, populate_existing=True))).all())
    assert len(rows) == 1 and rows[0].fcm_token == "tok-old" and rows[0].is_deleted is False


async def test_a_second_phone_keeps_the_first(client, session, people, monkeypatch):
    """Tokens were unique per (user, platform): a second Android phone took
    the first one's place and only one of a person's devices got pushes."""
    await _register(client, people.asha_h, "tok-phone-1")
    await _register(client, people.asha_h, "tok-phone-2")
    await _register(client, people.asha_h, "tok-ipad", platform="ios")
    assert await _tokens(session, people.asha.id) == sorted([(people.asha.id, "tok-phone-1"), (people.asha.id, "tok-phone-2"), (people.asha.id, "tok-ipad")])

    sent = []

    def fake_multicast(message):
        sent.append(list(message.tokens))
        return SimpleNamespace(success_count=len(message.tokens), failure_count=0, responses=[])

    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake_multicast)
    assert await push.send_notification_to_user(session, people.asha.id, "Hello", "World") is True
    assert sent == [["tok-phone-1", "tok-phone-2", "tok-ipad"]]


async def test_an_account_keeps_its_newest_ten_devices(client, session, people):
    from app.api.users import service as users_service

    for i in range(users_service.MAX_DEVICES_PER_USER + 2):
        assert (await _register(client, people.asha_h, f"tok-many-{i:02d}")).status_code == 200
    kept = sorted(t for _, t in await _tokens(session, people.asha.id))
    assert kept == [f"tok-many-{i:02d}" for i in range(2, users_service.MAX_DEVICES_PER_USER + 2)]


# ─── Dead tokens ────────────────────────────────────────────────────────


def _bad_token():
    return firebase_exceptions.InvalidArgumentError("The registration token is not a valid FCM registration token")


async def test_tokens_fcm_calls_dead_are_removed_after_a_send(session, make_user, monkeypatch):
    users = [await make_user(email=f"push{i}-tokens@example.com", username=f"push{i}-tokens") for i in range(5)]
    for u, token in zip(users, ["tok-live", "tok-unregistered", "tok-invalid", "tok-bad-message", "tok-quota"]):
        session.add(UserDeviceTokens(user_id=u.id, fcm_token=token, platform="android"))
    await session.commit()

    outcomes = {
        "tok-live": None,
        "tok-unregistered": messaging.UnregisteredError("Requested entity was not found."),
        "tok-invalid": _bad_token(),
        # A bad message fails every token the same way: no token is to blame.
        "tok-bad-message": firebase_exceptions.InvalidArgumentError("Request contains an invalid argument: android.notification.image"),
        "tok-quota": messaging.QuotaExceededError("Quota exceeded"),
    }

    def fake_multicast(message):
        return SimpleNamespace(
            success_count=sum(1 for t in message.tokens if outcomes[t] is None),
            failure_count=sum(1 for t in message.tokens if outcomes[t] is not None),
            responses=[SimpleNamespace(success=outcomes[t] is None, exception=outcomes[t]) for t in message.tokens],
        )

    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake_multicast)
    sent = await push.send_notification_to_users(session, [u.id for u in users], "Hello", "World")
    assert sent == 1
    left = {t for _, t in await _tokens(session)}
    assert left == {"tok-live", "tok-bad-message", "tok-quota"}


async def test_a_single_send_to_a_dead_token_removes_it(session, make_user, monkeypatch):
    u = await make_user(email="single-tokens@example.com", username="single-tokens")
    session.add(UserDeviceTokens(user_id=u.id, fcm_token="tok-gone", platform="ios"))
    await session.commit()

    def unregistered(message):
        raise messaging.UnregisteredError("Requested entity was not found.")

    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send", unregistered)
    assert await push.send_notification_to_user(session, u.id, "Hello", "World") is False
    assert await _tokens(session, u.id) == []

    # A failure that says nothing about the token keeps it.
    session.add(UserDeviceTokens(user_id=u.id, fcm_token="tok-kept", platform="ios"))
    await session.commit()

    def unavailable(message):
        raise firebase_exceptions.UnavailableError("FCM is down")

    monkeypatch.setattr(messaging, "send", unavailable)
    assert await push.send_notification_to_user(session, u.id, "Hello", "World") is False
    assert await _tokens(session, u.id) == [(u.id, "tok-kept")]


# ─── PROFILE_EXISTS ─────────────────────────────────────────────────────


async def test_a_second_profile_is_refused_with_a_code(client, people):
    first = await client.post(f"{API}/user/profile/create", data={"full_name": "Asha Menon"}, headers=people.asha_h)
    assert first.status_code == 200, first.text
    again = await client.post(f"{API}/user/profile/create", data={"full_name": "Asha Menon"}, headers=people.asha_h)
    assert again.status_code == 400
    assert again.json()["message"] == "Profile already exists" and again.json()["error_code"] == "PROFILE_EXISTS"
