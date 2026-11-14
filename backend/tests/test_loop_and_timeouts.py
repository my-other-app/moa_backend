"""The event loop's thread pools, and outbound calls that give up in time:
Firebase is set up once with a timeout, a hung FCM send does not hold its
caller for FCM's own 120 s, no pooled connection waits on FCM, and Razorpay
calls wait in threads of their own."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from firebase_admin import messaging

from app.api.payments import rzp
from app.api.users.models import UserDeviceTokens
from app.config import settings
from app.core import threads
from app.core.notifications import service as push


# ─── No connection waits on FCM ─────────────────────────────────────────────


async def test_no_transaction_is_open_while_fcm_answers(session, make_user, monkeypatch):
    one = await make_user(email="push-tx-1@example.com", username="push-tx-1")
    two = await make_user(email="push-tx-2@example.com", username="push-tx-2")
    session.add_all([
        UserDeviceTokens(user_id=one.id, fcm_token="tok-tx-1", platform="android"),
        UserDeviceTokens(user_id=two.id, fcm_token="tok-tx-2", platform="ios"),
    ])
    await session.commit()
    open_during_send = []

    async def single(token, *a, **k):
        open_during_send.append(session.in_transaction())
        return True

    async def many(tokens, *a, **k):
        open_during_send.append(session.in_transaction())
        return len(tokens)

    monkeypatch.setattr(push, "_send_to_token", single)
    monkeypatch.setattr(push, "_send_to_tokens", many)
    assert await push.send_notification_to_user(session, one.id, "Hi", "One") is True
    assert await push.send_notification_to_users(session, [one.id, two.id], "Hi", "Both") == 2
    assert open_during_send == [False, False]


# ─── Thread pools ───────────────────────────────────────────────────────────


def test_each_process_gets_a_default_pool_of_the_configured_size(monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_THREADS", 24)
    loop = asyncio.new_event_loop()
    try:
        pool = threads.install_default_executor(loop)
        assert loop._default_executor is pool and pool._max_workers == 24

        async def where():
            return await asyncio.to_thread(lambda: threading.current_thread().name)

        assert loop.run_until_complete(where()).startswith("default")
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


async def test_razorpay_calls_wait_in_their_own_threads():
    seen = []

    def sdk_call(order_id):
        seen.append(threading.current_thread().name)
        return {"id": order_id}

    assert await rzp.call(sdk_call, "order_1") == {"id": "order_1"}
    assert seen[0].startswith("razorpay")
    assert threads.razorpay_pool()._max_workers == settings.RAZORPAY_THREADS


async def test_a_razorpay_call_past_its_timeout_is_given_up_on():
    def hung():
        time.sleep(0.5)

    with pytest.raises(rzp.RazorpayTimeout):
        await rzp.call(hung, timeout=0.05)


# ─── Firebase ───────────────────────────────────────────────────────────────


def test_firebase_is_set_up_once_across_threads_with_a_timeout(monkeypatch):
    made = []

    def initialize_app(cred, options=None):
        time.sleep(0.05)  # long enough for every thread to arrive meanwhile
        made.append(options)
        return SimpleNamespace(project_id="moa-test")

    monkeypatch.setattr(push, "_firebase_app", None)
    monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_PATH", "")
    monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_BASE64", "e30=")  # {}
    monkeypatch.setattr(push.credentials, "Certificate", lambda info: object())
    monkeypatch.setattr(push.firebase_admin, "initialize_app", initialize_app)

    apps = []
    workers = [threading.Thread(target=lambda: apps.append(push.initialize_firebase())) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert made == [{"httpTimeout": settings.FIREBASE_TIMEOUT}]
    assert len(apps) == 8 and all(a is apps[0] for a in apps) and apps[0].project_id == "moa-test"


def test_an_app_made_elsewhere_in_the_process_is_used(monkeypatch):
    existing = SimpleNamespace(project_id="made-before")

    def initialize_app(cred, options=None):
        raise ValueError("The default Firebase app already exists.")

    monkeypatch.setattr(push, "_firebase_app", None)
    monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_PATH", "")
    monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_BASE64", "e30=")
    monkeypatch.setattr(push.credentials, "Certificate", lambda info: object())
    monkeypatch.setattr(push.firebase_admin, "initialize_app", initialize_app)
    monkeypatch.setattr(push.firebase_admin, "get_app", lambda: existing)
    assert push.initialize_firebase() is existing


def test_a_send_waits_at_most_three_http_timeouts(monkeypatch):
    monkeypatch.setattr(settings, "FIREBASE_TIMEOUT", 10.0)
    assert push.http_timeout() == 10.0 and push.send_wait() == 30.0
    # Under a job's 120 s: a slow push never times out the job that sent its email first.
    assert push.send_wait() < 120


@pytest.fixture
def hung_fcm(monkeypatch):
    """FCM that does not answer: each call blocks well past the (shortened) wait."""
    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(push, "send_wait", lambda: 0.05)

    def hang(*_a, **_k):
        time.sleep(0.5)

    for name in ("send", "send_each_for_multicast", "send_each"):
        monkeypatch.setattr(messaging, name, hang)


async def test_a_hung_fcm_send_is_given_up_on(hung_fcm):
    started = time.monotonic()
    assert await push._send_to_token("tok-1", "Hi", "There") is False
    assert await push._send_to_tokens(["tok-1", "tok-2"], "Hi", "There") == 0
    assert time.monotonic() - started < 0.45


async def test_a_hung_topic_send_is_retried_by_its_job(hung_fcm):
    with pytest.raises(push.TopicSendFailed):
        await push.send_to_topic(topic="club_1", title="Hi", body="There")
