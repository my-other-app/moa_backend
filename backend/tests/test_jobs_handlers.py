"""The jobs the heavy work became: each runs once from the caller's view, and
run again (the queue is at least once) changes nothing it already did.

On the test's session: a job a test's commit sets off runs when the test
waits for it (`jobs.settle`, or after each client request).
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.api.notifications.models import Notifications
from app.core import jobs


async def _jobs(session, name):
    return list((await session.scalars(select(jobs.Jobs).where(jobs.Jobs.name == name).order_by(jobs.Jobs.id))).all())


async def _run_again(session, row) -> None:
    """The same job once more, as a runner whose lease ran out would."""
    await jobs.enqueue(session, row.name, row.payload)
    await session.commit()
    await jobs.settle()


async def test_check_in_hooks_leave_one_note_however_often_they_run(session, make_club, make_event, make_user, monkeypatch):
    from app.api.checkins import after

    pushed: list = []
    monkeypatch.setattr(after, "_send_blocking", lambda *a, **k: pushed.append(a) or True)
    from app.core.notifications import service as push

    monkeypatch.setattr(push, "get_fcm_token_for_user", lambda *a, **k: _token())
    owner, club = await make_club(name="Door Club", email="door-jobs@example.com")
    ev = await make_event(club=club, created_by=owner, name="Door Night")
    guest = await make_user(email="guest-jobs@example.com")
    await after.enqueue(session, after.Admitted(user_id=guest.id, event_id=ev.id, event_name=ev.name, club_id=club.id))
    await session.commit()
    await jobs.settle()
    [row] = await _jobs(session, "checkins.after")
    assert row.finished_at is not None
    await _run_again(session, row)
    notes = await session.scalar(select(func.count()).select_from(Notifications).where(Notifications.user_id == guest.id, Notifications.type == "event_checkin"))
    assert notes == 1 and len(pushed) == 1


async def _token():
    return "tok-door"


async def test_a_payment_receipt_is_drawn_and_sent_in_a_job_once(session, make_user, monkeypatch):
    from app.api.payments import service as payments
    from app.api.payments.models import PaymentOrders

    sent: list = []
    monkeypatch.setattr(payments, "send_payment_confirmation_email", lambda **kw: sent.append(kw))
    user = await make_user(email="payer-jobs@example.com")
    order = PaymentOrders(id=uuid.uuid4(), receipt="rcpt-1", razorpay_order_id=f"order_{uuid.uuid4().hex[:10]}", user_id=user.id, amount=499, source="event", payload={})
    session.add(order)
    await session.flush()
    await payments._send_receipt(session, order.id, "pay_123", None)
    await payments._send_receipt(session, order.id, "pay_123", None)  # a webhook and a verify, at once
    assert sent == []  # not in the request
    await jobs.settle()
    assert [m["recipients"] for m in sent] == [["payer-jobs@example.com"]]
    assert sent[0]["payload"]["receipt_id"] == "rcpt-1"


async def test_an_import_that_finished_is_not_run_again(session, make_club, make_event, monkeypatch):
    import pandas as pd

    from app.api.events.registration import service as registration
    from app.api.models import BackgroundTaskLogs
    from app.api.service import create_background_task_log

    owner, club = await make_club(name="Import Club", email="import-jobs@example.com")
    ev = await make_event(club=club, created_by=owner, name="Import Night")
    log = await create_background_task_log(session, "Import guests.csv", "event_registrations_bulk_import")
    df = pd.DataFrame([{"full_name": "Asha Rao", "email": "asha-jobs@example.com", "phone": None, "role": float("nan")}])
    await registration.enqueue_import(session, ev.id, df, log, notify=False)
    await session.commit()
    await jobs.settle()
    log = await session.get(BackgroundTaskLogs, log.id, populate_existing=True)
    assert log.status == "COMPLETED" and len(log.logs) == 1
    [row] = await _jobs(session, "registrations.import")
    assert row.payload["rows"][0]["role"] is None  # NaN travels as null
    await _run_again(session, row)
    log = await session.get(BackgroundTaskLogs, log.id, populate_existing=True)
    assert len(log.logs) == 1  # the second run saw a finished log and left it


async def test_a_push_to_every_device_is_one_job_a_batch_each_device_once(session, monkeypatch):
    from app.api.users.models import UserDeviceTokens
    from app.core.notifications import fanout
    from app.core.notifications import service as push

    batches: list = []
    sent_badges: dict = {}

    async def send(tokens, title, body, data, image_url, dead=None, badges=None):
        batches.append(list(tokens))
        sent_badges.update(badges or {})
        return len(tokens)

    monkeypatch.setattr(push, "FCM_BATCH", 2)
    monkeypatch.setattr(push, "_send_to_tokens", send)
    before = set((await session.scalars(select(UserDeviceTokens.fcm_token).where(UserDeviceTokens.is_deleted == False))).all())  # noqa: E712
    from tests.test_accounts_integrity import _bulk_users

    await _bulk_users(session, 5, "bcastjob", tokens=True)
    await fanout.enqueue_broadcast(session, "b1", "Hello", "Everyone", None)
    await session.commit()
    await jobs.settle()
    mine = [t for b in batches for t in b if t not in before]
    assert len(mine) == 5 and len(set(mine)) == 5
    assert all(len(b) <= 2 for b in batches)
    # No inbox line goes with it: each device's badge is its person's count
    # as it was, nothing unread here -- 0, never a made-up 1.
    assert [sent_badges[t] for t in mine] == [0] * 5
    assert all(j.finished_at is not None for j in await _jobs(session, "notifications.broadcast"))


@pytest.mark.parametrize("worker", [False, True])
async def test_with_a_worker_the_request_only_writes_the_job(client, session, make_club, make_user, mint_token, monkeypatch, worker):
    """APP_JOBS_WORKER: a thread post answers with the followers' job written
    and not run; whoever runs the queue (here, a worker's runner) tells them."""
    from app.api.threads import service as threads
    from app.config import settings
    from app.core.rate_limit import limiter
    from tests.conftest import auth_header

    monkeypatch.setattr(limiter, "enabled", False)
    told: list = []

    async def tell(club_id, thread_id):
        told.append(str(thread_id))
        return 1

    monkeypatch.setattr(threads, "tell_followers", tell)
    monkeypatch.setattr(settings, "JOBS_WORKER", worker)
    if worker:
        jobs.use(None)  # the API process has no runner of its own
    owner, club = await make_club(name="Worker Club", email="worker-jobs@example.com")
    r = await client.post("/api/v1/clubs/threads", data={"body": "Lab open till 9", "notify_followers": "true"}, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 201, r.text
    [row] = await _jobs(session, "threads.tell_followers")
    if worker:
        assert told == [] and row.finished_at is None
        await jobs.run_due(ids=[row.id], me="worker:test")
    assert told == [r.json()["id"]]
    row = (await session.scalars(select(jobs.Jobs).where(jobs.Jobs.id == row.id).execution_options(populate_existing=True))).one()
    assert row.finished_at is not None and row.attempts == 1
    assert datetime.now(timezone.utc) >= row.finished_at
