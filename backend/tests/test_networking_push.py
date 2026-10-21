"""The check-in push and People Like You: one push, after the check-in is
kept, that opens the list when the event has it and it is open -- and the
plain welcome otherwise. Nothing about the push can undo a check-in."""
import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.api.checkins import after
from app.api.checkins import service as checkins
from app.api.checkins.gate_auth import GateActor
from app.api.notifications.models import Notifications
from app.config import settings
from app.core import jobs

NOW = datetime.now(timezone.utc)


@pytest.fixture
def pushes(monkeypatch):
    """Every push the check-in hooks send, as (title, body, data); the
    guest has one device."""
    from app.core.notifications import service as push

    sent: list = []

    async def token(_session, _user_id):
        return "tok-guest"

    monkeypatch.setattr(push, "get_fcm_token_for_user", token)
    monkeypatch.setattr(after, "_send_blocking", lambda tok, title, body, data, dead: sent.append((title, body, dict(data))) or True)
    return sent


@pytest.fixture
async def door(session, make_club, make_user, make_event, make_registration):
    owner, club = await make_club(name="Door Club", email="door-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Mixer Night", event_datetime=NOW - timedelta(minutes=30), duration=3.0)
    guest = await make_user(email="door-guest@example.com")
    reg = await make_registration(event=event, user=guest)
    await session.commit()
    actor = GateActor(kind="user", name="Door Owner", user=owner, full=True)
    return dict(owner=owner, club=club, event=event, guest=guest, reg=reg, actor=actor)


async def _check_in(session, door) -> dict:
    out = await checkins.check_in_by_hand(session, door["event"], door["reg"].id, "Desk", door["actor"])
    await jobs.settle()
    return out


async def _note(session, user_id):
    return await session.scalar(select(Notifications).where(Notifications.user_id == user_id, Notifications.type == "event_checkin"))


async def test_a_check_in_sends_one_push_that_opens_people_like_you(session, door, pushes):
    out = await _check_in(session, door)
    assert out["outcome"] == "CHECKED_IN"
    assert pushes == [(
        "✅ You're checked in",
        "Welcome to Mixer Night. See who here shares your interests.",
        {"type": "event_checkin", "event_id": str(door["event"].id), "open": "people"},
    )]
    note = await _note(session, door["guest"].id)
    assert note.title == "✅ You're checked in" and note.data["open"] == "people"

    # Scanned again (and the job run again): still the one push.
    again = await checkins.scan(session, door["event"], code=door["reg"].ticket_id, door_label="Desk", client_ref="again-1",
                                scanned_at=None, actor=door["actor"])
    await jobs.settle()
    assert again["outcome"] == "ALREADY_CHECKED_IN" and len(pushes) == 1


async def test_a_push_that_fails_never_fails_the_check_in(session, door, monkeypatch):
    from app.core.notifications import service as push

    async def token(_session, _user_id):
        return "tok-guest"

    def broken(*_args, **_kwargs):
        raise RuntimeError("FCM is down")

    monkeypatch.setattr(push, "get_fcm_token_for_user", token)
    monkeypatch.setattr(after, "_send_blocking", broken)
    out = await _check_in(session, door)
    assert out["outcome"] == "CHECKED_IN"
    await session.refresh(door["reg"])
    assert door["reg"].is_attended is True and await checkins.check_in_of(session, door["reg"].id) is not None


async def test_a_doubt_about_the_list_is_the_plain_welcome(session, door, pushes, monkeypatch):
    from app.api.subscriptions import gates

    async def broken(*_args, **_kwargs):
        raise RuntimeError("plans unreadable")

    monkeypatch.setattr(gates, "allows", broken)
    out = await _check_in(session, door)
    assert out["outcome"] == "CHECKED_IN"
    assert [(t, d) for t, _b, d in pushes] == [("✅ Checked in successfully!", {"type": "event_checkin", "event_id": str(door["event"].id)})]


async def test_no_people_push_once_the_window_has_closed(session, door, pushes):
    door["event"].event_datetime = NOW - timedelta(hours=6)  # 3h event, its hour after over two hours ago
    await session.commit()
    await after.run(after.Admitted(user_id=door["guest"].id, event_id=door["event"].id, event_name="Mixer Night", club_id=door["club"].id))
    assert [(t, b, d) for t, b, d in pushes] == [(
        "✅ Checked in successfully!", "Welcome to Mixer Night. Enjoy the event!",
        {"type": "event_checkin", "event_id": str(door["event"].id)},
    )]
    assert "open" not in ((await _note(session, door["guest"].id)).data or {})


def _migration(name):
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_no_people_push_when_the_plan_lacks_networking(session, door, pushes, monkeypatch):
    from app.api.subscriptions import gates

    # Enforced, with the plans production has: a club on no plan is on Free,
    # which has no networking.
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", datetime(2020, 1, 1, tzinfo=timezone.utc))
    gates.reset()
    plans = _migration("club_plans")
    connection = await session.connection()

    def seed(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            plans.seed_plans()

    await connection.run_sync(seed)
    await session.commit()
    assert await gates.allows(session, door["club"].id, "networking", event=door["event"]) is False

    out = await _check_in(session, door)
    assert out["outcome"] == "CHECKED_IN"
    assert [d for _t, _b, d in pushes] == [{"type": "event_checkin", "event_id": str(door["event"].id)}]
    gates.reset()
