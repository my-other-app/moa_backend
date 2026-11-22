"""The leaderboards (SPEC §7): one per game, ties to whoever got there first.

    cards  distinct heroes held, then the earlier time that count was reached
    hunt   total points, then the earlier time that total was reached

(a last tie-break by registration id keeps the order the same on every read).
A row is `{rank, name, score, reached_at, move}`: the name is first name and
last initial, or "Player #N" for an opt-out; `move` is places gained since a
snapshot of the board a minute or more earlier (positive up, negative down, 0
level, null when there is none to compare with yet).
"""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.events.models import EventRegistrationsLink
from app.api.games import common
from app.api.games.models import CARDS, HUNT, GameCardScores, GameFeed, GameHuntScores, GamePlayers, GameSnapshots
from app.response import CustomHTTPException

SNAPSHOT_EVERY = 60
SNAPSHOT_TOP = 50
MAX_LIMIT = 50


def _tables(game: str):
    if game == CARDS:
        return GameCardScores, GameCardScores.distinct_heroes
    if game == HUNT:
        return GameHuntScores, GameHuntScores.total
    raise CustomHTTPException(422, message="game must be cards or hunt", error_code="BAD_GAME")


def _order(table, score):
    return (score.desc(), table.reached_at.asc(), table.registration_id.asc())


async def _top(session, event_id: int, game: str, limit: int) -> list[tuple]:
    table, score = _tables(game)
    return (
        await session.execute(
            select(table.registration_id, score, table.reached_at, EventRegistrationsLink.full_name, GamePlayers.public, GamePlayers.player_no)
            .join(GamePlayers, GamePlayers.registration_id == table.registration_id)
            .join(EventRegistrationsLink, EventRegistrationsLink.id == table.registration_id)
            .where(table.event_id == event_id, *( [score > 0] if game == HUNT else [] ))
            .order_by(*_order(table, score))
            .limit(limit)
        )
    ).all()


async def count_of(session, event_id: int, game: str) -> int:
    table, score = _tables(game)
    return int(await session.scalar(select(func.count()).select_from(table).where(table.event_id == event_id, *( [score > 0] if game == HUNT else [] ))) or 0)


async def rank_of(session, event_id: int, game: str, registration_id) -> Optional[dict]:
    """`{rank, of, score}` for one player, or None when they are not on the board."""
    table, score = _tables(game)
    mine = (await session.execute(select(score, table.reached_at).where(table.registration_id == registration_id))).first()
    if mine is None or (game == HUNT and mine[0] <= 0):
        return None
    better = await session.scalar(
        select(func.count()).select_from(table).where(
            table.event_id == event_id,
            or_(
                score > mine[0],
                and_(score == mine[0], table.reached_at < mine[1]),
                and_(score == mine[0], table.reached_at == mine[1], table.registration_id < registration_id),
            ),
            *( [score > 0] if game == HUNT else [] ),
        )
    )
    return {"rank": int(better) + 1, "of": await count_of(session, event_id, game), "score": int(mine[0])}


async def _baseline(session, event_id: int, game: str, ranks: dict) -> Optional[dict]:
    """The ranks a minute or more ago; takes a new snapshot when the last is that old."""
    now = common.now()
    cutoff = now - timedelta(seconds=SNAPSHOT_EVERY)
    rows = (
        await session.execute(
            select(GameSnapshots.taken_at, GameSnapshots.ranks).where(GameSnapshots.event_id == event_id, GameSnapshots.game == game).order_by(GameSnapshots.taken_at.desc()).limit(4)
        )
    ).all()
    if not rows or rows[0][0] <= cutoff:
        await session.execute(
            pg_insert(GameSnapshots)
            .values(event_id=event_id, game=game, bucket=int(now.timestamp()) // SNAPSHOT_EVERY, ranks=ranks, taken_at=now)
            .on_conflict_do_nothing(constraint="uq_game_snapshots_bucket")
        )
        await session.commit()
    for taken, r in rows:
        if taken <= cutoff:
            return r
    return None


async def board(session, event_id: int, game: str, limit: int = 10, me: Optional[object] = None) -> dict:
    """The top `limit` of one board, and the caller's own place when `me` (a registration id) is given."""
    limit = max(1, min(int(limit or 10), MAX_LIMIT))
    top = await _top(session, event_id, game, max(limit, SNAPSHOT_TOP))
    current = {str(r[0]): i + 1 for i, r in enumerate(top)}
    before = await _baseline(session, event_id, game, current)
    rows = []
    for i, (reg_id, score, reached, full_name, public, number) in enumerate(top[:limit]):
        was = (before or {}).get(str(reg_id))
        rows.append(
            {
                "rank": i + 1,
                "name": common.display_name(full_name, public, number),
                "score": int(score),
                "reached_at": reached,
                "move": (was - (i + 1)) if was is not None else (None if before is None else 0),
                "me": bool(me is not None and reg_id == me),
            }
        )
    out = {"game": game, "rows": rows, "of": await count_of(session, event_id, game), "me": None}
    if me is not None:
        out["me"] = await rank_of(session, event_id, game, me)
    return out


async def feed(session, event_id: int, limit: int = 3, *, include_hidden: bool = False) -> list[dict]:
    q = select(GameFeed).where(GameFeed.event_id == event_id).order_by(GameFeed.created_at.desc(), GameFeed.id.desc()).limit(limit)
    if not include_hidden:
        q = q.where(GameFeed.hidden == False)  # noqa: E712
    return [
        {"id": f.id, "game": f.game, "type": f.type, "message": f.message, "created_at": f.created_at, **({"hidden": f.hidden} if include_hidden else {})}
        for f in (await session.scalars(q)).all()
    ]


async def csv_of(session, event_id: int, game: str, everyone: bool) -> bytes:
    """The board as a spreadsheet for prizes: the top 20, or all with `everyone`. Full names: it is the organiser's."""
    table, score = _tables(game)
    q = (
        select(table.registration_id, score, table.reached_at, EventRegistrationsLink.full_name, EventRegistrationsLink.email, GamePlayers.player_no)
        .join(GamePlayers, GamePlayers.registration_id == table.registration_id)
        .join(EventRegistrationsLink, EventRegistrationsLink.id == table.registration_id)
        .where(table.event_id == event_id, *( [score > 0] if game == HUNT else [] ))
        .order_by(*_order(table, score))
    )
    if not everyone:
        q = q.limit(20)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["rank", "player", "name", "email", "distinct heroes" if game == CARDS else "points", "reached at"])
    for i, (_, s, at, name, email, number) in enumerate((await session.execute(q)).all()):
        w.writerow([i + 1, number, _safe(name), _safe(email), int(s), at.isoformat() if at else ""])
    return out.getvalue().encode("utf-8-sig")


def _safe(value) -> str:
    """A cell that a spreadsheet will not run as a formula."""
    text = str(value or "")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
