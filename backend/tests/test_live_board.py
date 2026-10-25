"""The quiz's board in Redis (app/api/live/board.py): ranks and the top ten
from a sorted set equal what the snapshot works out from every player --
ties, late joiners, answers -- and the set is built again from Postgres when
it is missing or a write to it failed. Against the local Redis the platform
tests use, skipped when none answers."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.api.live import board, room
from app.api.live.models import LiveQuizPlayers, LiveQuizzes
from app.config import settings
from app.core import redis
from tests.live_harness import REDIS_URL, _join, _quiz, _u, with_redis, world  # noqa: F401 (fixtures)
from tests.test_redis import _wipe, needs_redis


async def _scored_quiz(client, session, world, scores: list[int]) -> tuple[dict, list[dict]]:
    """A running quiz whose players have these scores, joined in this order."""
    code = await _quiz(client, world, questions=2)
    players = [await _join(client, code, i) for i in range(len(scores))]
    for p, score in zip(players, scores):
        await session.execute(update(LiveQuizPlayers).where(LiveQuizPlayers.id == p["player_id"]).values(score=score))
    await session.commit()
    # Scores written past the board (as no code does): its set is made again.
    if redis.client() is not None:
        await _wipe()
    await client.post(_u(world, "/quiz/host/next"), headers=world["h"])
    return players


async def _in_process(session, world):
    """The snapshot as it is without Redis: every player read and sorted."""
    saved = settings.REDIS_URL
    settings.REDIS_URL = ""
    redis.reset_for_tests()
    try:
        return await room._build_quiz(session, world["event"].id)
    finally:
        settings.REDIS_URL = saved
        redis.reset_for_tests()


@needs_redis
async def test_the_board_in_redis_ranks_and_tops_as_the_snapshot_does(client, session, world, with_redis):
    # Ties at the top, in the middle and at the bottom, and around tenth place.
    scores = [900, 1800, 900, 0, 1800, 450, 900, 0, 450, 1800, 900, 120, 0, 1350]
    players = await _scored_quiz(client, session, world, scores)
    expected = await _in_process(session, world)
    assert expected.board_key is None

    view = await room._build_quiz(session, world["event"].id)
    assert view.board_key is not None and view.scores is None
    assert view.board == expected.board  # positions, names, scores: ties to the earlier joiner
    met = [room.Player(id=p.id, name=p.name, score=-1, created_at=p.created_at) for p in expected.players.values()]
    got = await room.standings(session, view, met)
    assert {pid: r for pid, (_, r) in got.items()} == {p.id: expected.rank_of(p.score) for p in expected.players.values()}
    assert {pid: s for pid, (s, _) in got.items()} == {p.id: p.score for p in expected.players.values()}

    # A late joiner, after the set was built: at 0, ranked with the others at 0.
    code = (await session.scalar(select(LiveQuizzes.join_code).where(LiveQuizzes.event_id == world["event"].id)))
    late = await _join(client, code, 99)
    expected = await _in_process(session, world)
    view = await room._build_quiz(session, world["event"].id)
    late_p = expected.players[late["player_key"]]
    assert (await room.standings(session, view, [late_p]))[late_p.id] == (0, expected.rank_of(0))
    assert view.board == expected.board

    # Answers through the API raise the set's scores; it agrees with Postgres.
    for p in players[:3] + [late]:
        r = await client.post(_u(world, "/quiz/answer"), json={"option_key": "a"}, headers={"X-Live-Player": p["player_key"]})
        assert r.status_code == 200, r.text
    expected = await _in_process(session, world)
    view = await room._build_quiz(session, world["event"].id)
    assert view.board == expected.board
    everyone = list(expected.players.values())
    got = await room.standings(session, view, everyone)
    assert got == {p.id: (p.score, expected.rank_of(p.score)) for p in everyone}
    # And a player's poll says the same with and without Redis, once the
    # question's answers have closed (until then no score moves, no rank or board shows).
    await session.execute(update(LiveQuizzes).where(LiveQuizzes.event_id == world["event"].id).values(
        question_opened_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await session.commit()
    room.quizzes.clear()
    polled = (await client.get(_u(world, "/quiz/play"), headers={"X-Live-Player": late["player_key"]})).json()
    assert (polled["score"], polled["rank"], polled["leaderboard"]) == (expected.players[late["player_key"]].score, expected.rank_of(expected.players[late["player_key"]].score), expected.board)

    # Gone from Redis (evicted, expired): built again from Postgres, the same.
    import redis.asyncio as aioredis

    c = aioredis.from_url(REDIS_URL)
    await c.delete(view.board_key)
    await c.aclose()
    again = await room._build_quiz(session, world["event"].id)
    assert again.board_key == view.board_key and again.board == expected.board
    assert await room.standings(session, again, everyone) == got


@needs_redis
async def test_a_board_write_that_fails_is_repaired_from_postgres(client, session, world, with_redis, monkeypatch):
    players = await _scored_quiz(client, session, world, [100, 200, 300])
    view = await room._build_quiz(session, world["event"].id)
    assert view.board_key is not None
    # Redis refuses the answer's write: the set is marked, and not read until rebuilt.
    c = redis.client()

    async def refuse(*a, **k):
        raise ConnectionError("Redis went away")

    monkeypatch.setattr(c, "zadd", refuse)
    r = await client.post(_u(world, "/quiz/answer"), json={"option_key": "a"}, headers={"X-Live-Player": players[0]["player_key"]})
    assert r.status_code == 200
    assert view.board_key in board._dirty
    assert (await room._build_quiz(session, world["event"].id)).board_key is None  # meanwhile: the snapshot's own
    monkeypatch.undo()
    monkeypatch.setattr(settings, "REDIS_URL", REDIS_URL)
    redis._down_until = 0.0  # Redis answers again
    expected = await _in_process(session, world)
    rebuilt = await room._build_quiz(session, world["event"].id)
    assert view.board_key not in board._dirty and rebuilt.board == expected.board
    p0 = expected.players[players[0]["player_key"]]
    assert (await room.standings(session, rebuilt, [p0]))[p0.id] == (p0.score, expected.rank_of(p0.score))
