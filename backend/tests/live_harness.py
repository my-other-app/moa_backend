"""What the live room's tests share (tests/test_live_stream.py,
tests/test_live_board.py): a room in a club, its paths and keys, a quiz to
play, Redis for a test (the local one the platform tests use)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.api.live import board
from app.config import settings
from app.core import redis
from tests.conftest import auth_header
from tests.test_redis import URL as REDIS_URL, _wipe

START = datetime.now(timezone.utc) + timedelta(hours=1)
HALL = "49.36.113.7"


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Stream Society", email="stream@example.com")
    event = await make_event(club=club, created_by=owner, name="Stream Night", event_datetime=START, duration=3.0, location_name="Main Hall")
    guests = []
    for i in range(3):
        u = await make_user(email=f"stream-guest-{i}@example.com")
        guests.append(await make_registration(event=event, user=u, full_name=f"Guest {i}"))
    other_owner, _ = await make_club(name="Other Society", email="other@example.com")
    await session.commit()
    return {
        "event": event, "club": club, "owner": owner, "guests": guests,
        "h": auth_header(await mint_token(owner)), "other": auth_header(await mint_token(other_owner)),
    }


def _u(world, path=""):
    return f"/api/v1/events/{world['event'].id}/live{path}"


def _key(i: int) -> dict:
    return {"x-forwarded-for": HALL, "X-Live-Key": f"browser-{i:08d}"}


def _no_clock(body):
    """A body with its question's clock taken out (counted when written)."""
    body = json.loads(json.dumps(body))
    for holder in (body, body.get("quiz") if isinstance(body, dict) else None):
        if isinstance(holder, dict) and isinstance(holder.get("question"), dict):
            holder["question"]["time_left"] = None
    return body


async def _quiz(client, world, questions=1, seconds=30) -> str:
    for n in range(questions):
        q = {"prompt": f"Question {n}?", "options": [{"key": "a", "label": "Yes"}, {"key": "b", "label": "No"}], "correct_key": "a", "seconds": seconds}
        assert (await client.post(_u(world, "/quiz/questions"), json=q, headers=world["h"])).status_code == 201
    started = await client.post(_u(world, "/quiz/host/start"), headers=world["h"])
    assert started.status_code == 200, started.text
    return started.json()["join_code"]


async def _join(client, code, i) -> dict:
    r = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": f"Player {i}"}, headers=_key(i))
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
async def with_redis(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", REDIS_URL)
    redis.reset_for_tests()
    board.reset_for_tests()
    await _wipe()
    yield REDIS_URL
    redis.reset_for_tests()
    board.reset_for_tests()
    await _wipe()
