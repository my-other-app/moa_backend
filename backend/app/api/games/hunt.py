"""The Scavenger Hunt (SPEC §5).

States: draft -> scheduled -> live <-> paused -> ended. They move by the
organiser's buttons and, with no loop, by the clock: a scheduled hunt is live
once its `starts_at` has come and a live one is ended once `ends_at` and the
grace have passed -- whichever request notices first writes it (`tick`).

A scan is checked in this order: (1) a genuine QR-point token of this event,
(2) the hunt is live and before `ends_at` (+2 s grace), (3) the player has not
claimed it, (4) in ordered mode it is their current clue, (5) the claim, its
rank and the score, in one transaction with the point's row locked so ranks
are one after another.

Points = base + early bonus (the first N claimers of that point) - hints
(charged when bought). Clue order: ordered mode starts player k at clue
(k mod points) and wraps; free mode lists every clue.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.api.games import boards, common, tokens
from app.api.games.common import err
from app.api.games.models import (
    DRAFT,
    ENDED,
    HUNT,
    LIVE,
    ORDERED,
    PAUSED,
    SCHEDULED,
    GameHunts,
    GameHuntClaims,
    GameHuntHints,
    GameHuntPoints,
    GameHuntScores,
)

#: Scans are accepted this long after `ends_at`.
GRACE = timedelta(seconds=2)
#: One scan in this many seconds per player.
SCAN_GAP = 3.0
RECENT = 5


def _aware(value):
    return value.replace(tzinfo=common.now().tzinfo) if value is not None and value.tzinfo is None else value


async def hunt_of(session, event_id: int) -> Optional[GameHunts]:
    return await session.scalar(select(GameHunts).where(GameHunts.event_id == event_id))


async def ensure_hunt(session, event_id: int) -> GameHunts:
    hunt = await hunt_of(session, event_id)
    if hunt is None:
        await session.execute(pg_insert(GameHunts).values(id=uuid.uuid4(), event_id=event_id).on_conflict_do_nothing(index_elements=["event_id"]))
        await session.flush()
        hunt = await hunt_of(session, event_id)
    return hunt


async def tick(session, hunt: GameHunts) -> GameHunts:
    """Move the hunt along with the clock, once, whoever notices first."""
    now = common.now()
    moved = False
    if hunt.status == SCHEDULED and hunt.starts_at is not None and _aware(hunt.starts_at) <= now:
        got = await session.execute(update(GameHunts).where(GameHunts.id == hunt.id, GameHunts.status == SCHEDULED).values(status=LIVE, updated_at=now))
        moved = got.rowcount > 0
        hunt.status = LIVE
    if hunt.status == LIVE and hunt.ends_at is not None and _aware(hunt.ends_at) + GRACE < now:
        got = await session.execute(update(GameHunts).where(GameHunts.id == hunt.id, GameHunts.status == LIVE).values(status=ENDED, updated_at=now))
        moved = moved or got.rowcount > 0
        hunt.status = ENDED
    if moved:
        await session.commit()
        common.told(hunt.event_id, "hunt", "board")
    return hunt


async def points_of(session, event_id: int) -> list[GameHuntPoints]:
    return list((await session.scalars(select(GameHuntPoints).where(GameHuntPoints.event_id == event_id).order_by(GameHuntPoints.number))).all())


def order_for(points: list, player_no: int) -> list:
    """The player's clue order: starts at clue (player_no mod points) and wraps."""
    n = len(points)
    return [points[(player_no + i) % n] for i in range(n)] if n else []


def hunt_out(h: GameHunts) -> dict:
    return {
        "status": h.status,
        "starts_at": h.starts_at,
        "ends_at": h.ends_at,
        "duration_minutes": h.duration_minutes,
        "mode": h.mode,
        "bonus_n": h.bonus_n,
        "bonus_pts": h.bonus_pts,
        "hint_cost": h.hint_cost,
        "paused_at": h.paused_at,
        "server_now": common.now(),
    }


# ─── What a player sees ────────────────────────────────────────────────


def _clue(p: GameHuntPoints, h: GameHunts, *, claimed, hint_bought: bool) -> dict:
    """A clue as the player reads it: never the label or the code."""
    return {
        "point_id": p.id,
        "number": p.number,
        "clue": p.clue,
        "zone": p.zone,
        "points": p.base_points,
        "has_hint": bool(p.hint),
        "hint": p.hint if hint_bought else None,
        "hint_cost": h.hint_cost,
        "found": claimed is not None,
        "found_at": claimed.created_at if claimed else None,
        "earned": claimed.points_awarded if claimed else None,
    }


async def view(session, ctx) -> dict:
    event, reg = ctx.event, ctx.reg
    await common.require_active(session, event, ctx.settings, HUNT)
    h = await hunt_of(session, event.id)
    if h is None:
        raise err(404, "GAME_NOT_ON", "This game is not set up for this event.")
    h = await tick(session, h)
    points = await points_of(session, event.id)
    claims = {c.point_id: c for c in (await session.scalars(select(GameHuntClaims).where(GameHuntClaims.registration_id == reg.id))).all()}
    hints = {x.point_id for x in (await session.scalars(select(GameHuntHints).where(GameHuntHints.registration_id == reg.id))).all()}
    score = await session.scalar(select(GameHuntScores).where(GameHuntScores.registration_id == reg.id))
    out = {
        **hunt_out(h),
        "name": ctx.settings.hunt_name,
        "points": int(score.total) if score else 0,
        "found": int(score.found) if score else 0,
        "total_points": len(points),
        "complete": bool(score is not None and score.completed_at is not None),
        "rank": await boards.rank_of(session, event.id, HUNT, reg.id),
        "current": None,
        "clues": None,
        "recent": await recent_finds(session, event.id),
    }
    if h.status in (DRAFT, SCHEDULED):
        return out
    if h.mode == ORDERED:
        order = order_for(points, ctx.player.player_no)
        nxt = next((p for p in order if p.id not in claims), None)
        out["current"] = _clue(nxt, h, claimed=None, hint_bought=nxt.id in hints) if nxt else None
        out["clues"] = [{"number": p.number, "found": True, "earned": claims[p.id].points_awarded} for p in order if p.id in claims]
    else:
        out["clues"] = [_clue(p, h, claimed=claims.get(p.id), hint_bought=p.id in hints) for p in points]
    return out


async def recent_finds(session, event_id: int) -> list[dict]:
    from app.api.events.models import EventRegistrationsLink
    from app.api.games.models import GamePlayers

    rows = (
        await session.execute(
            select(GameHuntClaims.created_at, EventRegistrationsLink.full_name, GamePlayers.public, GamePlayers.player_no)
            .join(GamePlayers, GamePlayers.registration_id == GameHuntClaims.registration_id)
            .join(EventRegistrationsLink, EventRegistrationsLink.id == GameHuntClaims.registration_id)
            .where(GameHuntClaims.event_id == event_id)
            .order_by(GameHuntClaims.created_at.desc())
            .limit(RECENT)
        )
    ).all()
    return [{"name": common.display_name(n, pub, no), "at": at} for at, n, pub, no in rows]


# ─── Scan ──────────────────────────────────────────────────────────────


async def _gap(session, ctx) -> None:
    """One scan in SCAN_GAP seconds per player: the time is taken and kept before anything else is judged."""
    now = common.now()
    got = await session.execute(
        update(common.GamePlayers)
        .where(
            common.GamePlayers.id == ctx.player.id,
            (common.GamePlayers.last_scan_at.is_(None)) | (common.GamePlayers.last_scan_at < now - timedelta(seconds=SCAN_GAP)),
        )
        .values(last_scan_at=now)
    )
    if got.rowcount:
        await session.commit()
        return
    last = await session.scalar(select(common.GamePlayers.last_scan_at).where(common.GamePlayers.id == ctx.player.id))
    wait = SCAN_GAP - (now - last).total_seconds() if last else SCAN_GAP
    raise err(429, "RATE_LIMITED", "Slow down a moment.", retry_after=max(1, int(wait + 0.999)))


async def scan(session, ctx, token: str) -> dict:
    event, reg = ctx.event, ctx.reg
    await common.require_active(session, event, ctx.settings, HUNT)
    read = tokens.read_point(token)
    if read is None or read.event_id != event.id:
        raise err(422, "BAD_CODE", "That is not a code for this hunt.")
    point = await session.scalar(select(GameHuntPoints).where(GameHuntPoints.id == read.point_id, GameHuntPoints.event_id == event.id))
    if point is None:
        raise err(422, "BAD_CODE", "That code is not part of this hunt any more.")
    await _gap(session, ctx)
    h = await hunt_of(session, event.id)
    if h is None:
        raise err(409, "HUNT_NOT_LIVE", "The hunt has not started.")
    h = await tick(session, h)
    if h.status == PAUSED:
        raise err(409, "HUNT_PAUSED", "Hunt paused.")
    if h.status != LIVE:
        raise err(409, "HUNT_NOT_LIVE", "Hunt is over." if h.status == ENDED else "The hunt has not started.", hunt_status=h.status)
    claimed = {c.point_id: c for c in (await session.scalars(select(GameHuntClaims).where(GameHuntClaims.registration_id == reg.id))).all()}
    if point.id in claimed:
        raise err(409, "ALREADY_CLAIMED", "You already found this one.")
    points = await points_of(session, event.id)
    if h.mode == ORDERED:
        nxt = next((p for p in order_for(points, ctx.player.player_no) if p.id not in claimed), None)
        if nxt is None or nxt.id != point.id:
            raise err(409, "OUT_OF_ORDER", "Not this one yet. Follow your clue.")
    now = common.now()
    try:
        # The point's row is held while its rank is taken, so claimers of one point are ranked one after another.
        await session.execute(select(GameHuntPoints.id).where(GameHuntPoints.id == point.id).with_for_update())
        rank = int(await session.scalar(select(func.count()).select_from(GameHuntClaims).where(GameHuntClaims.point_id == point.id)) or 0) + 1
        bonus = h.bonus_pts if rank <= h.bonus_n else 0
        awarded = point.base_points + bonus
        session.add(GameHuntClaims(id=uuid.uuid4(), event_id=event.id, point_id=point.id, registration_id=reg.id, claim_rank=rank, points_awarded=awarded))
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise err(409, "ALREADY_CLAIMED", "You already found this one.")
    row = (
        await session.execute(
            pg_insert(GameHuntScores)
            .values(id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, total=awarded, found=1, reached_at=now)
            .on_conflict_do_update(
                constraint="uq_game_hunt_scores_registration",
                set_={"total": GameHuntScores.total + awarded, "found": GameHuntScores.found + 1, "reached_at": now},
            )
            .returning(GameHuntScores.total, GameHuntScores.found)
        )
    ).first()
    total, found = int(row[0]), int(row[1])
    complete = found >= len(points)
    if complete:
        await session.execute(update(GameHuntScores).where(GameHuntScores.registration_id == reg.id).values(completed_at=now))
        who = await common.player_label(session, reg.id)
        await common.add_feed(session, event.id, HUNT, "hunt_complete", f"{who} found every point in the hunt!", None)
    await _leader(session, event.id, reg.id)
    await session.commit()
    common.told(event.id, "board", "feed")
    return {
        "awarded": awarded,
        "base": point.base_points,
        "bonus": bonus,
        "claim_rank": rank,
        "total": total,
        "found": found,
        "total_points": len(points),
        "complete": complete,
        "rank": await boards.rank_of(session, event.id, HUNT, reg.id),
        "message": f"+{awarded} points" + (f" (including a +{bonus} early bonus)" if bonus else ""),
    }


async def _leader(session, event_id: int, mover) -> None:
    """Announce a new leader once, whoever's score just changed."""
    top = await boards._top(session, event_id, HUNT, 1)
    if not top:
        return
    leader = top[0][0]
    got = await session.execute(
        update(GameHunts).where(GameHunts.event_id == event_id, GameHunts.leader_registration_id.is_distinct_from(leader)).values(leader_registration_id=leader).returning(GameHunts.id)
    )
    if got.first() is not None:
        who = common.display_name(top[0][3], top[0][4], top[0][5])
        await common.add_feed(session, event_id, HUNT, "hunt_leader", f"{who} takes the lead with {int(top[0][1])} points!", None)


# ─── Hints ─────────────────────────────────────────────────────────────


async def hint(session, ctx, point_id: Optional[uuid.UUID]) -> dict:
    event, reg = ctx.event, ctx.reg
    await common.require_active(session, event, ctx.settings, HUNT)
    h = await hunt_of(session, event.id)
    if h is None:
        raise err(409, "HUNT_NOT_LIVE", "The hunt has not started.")
    h = await tick(session, h)
    if h.status == PAUSED:
        raise err(409, "HUNT_PAUSED", "Hunt paused.")
    if h.status != LIVE:
        raise err(409, "HUNT_NOT_LIVE", "Hunt is over." if h.status == ENDED else "The hunt has not started.", hunt_status=h.status)
    points = await points_of(session, event.id)
    claimed = set((await session.scalars(select(GameHuntClaims.point_id).where(GameHuntClaims.registration_id == reg.id))).all())
    if h.mode == ORDERED:
        current = next((p for p in order_for(points, ctx.player.player_no) if p.id not in claimed), None)
        if point_id is not None and (current is None or current.id != point_id):
            raise err(409, "OUT_OF_ORDER", "Not this one yet. Follow your clue.")
        point = current
    else:
        if point_id is None:
            raise err(422, "POINT_REQUIRED", "Say which clue you want a hint for.")
        point = next((p for p in points if p.id == point_id), None)
    if point is None:
        raise err(404, "POINT_NOT_FOUND", "There is no such clue.")
    if point.id in claimed:
        raise err(409, "ALREADY_CLAIMED", "You already found this one.")
    if not point.hint:
        raise err(404, "NO_HINT", "This clue has no hint.")
    cost = int(h.hint_cost)
    bought = await session.scalar(select(GameHuntHints).where(GameHuntHints.point_id == point.id, GameHuntHints.registration_id == reg.id))
    if bought is None:
        now = common.now()
        # Charged once; never below 0: the row is only changed when the points are there.
        charged = await session.execute(
            update(GameHuntScores).where(GameHuntScores.registration_id == reg.id, GameHuntScores.total >= cost).values(total=GameHuntScores.total - cost, reached_at=now).returning(GameHuntScores.total)
        )
        if charged.first() is None and cost > 0:
            await session.rollback()
            raise err(409, "NOT_ENOUGH_POINTS", "You need more points to buy this hint.", cost=cost)
        session.add(GameHuntHints(id=uuid.uuid4(), event_id=event.id, point_id=point.id, registration_id=reg.id, cost=cost))
        try:
            await session.commit()
        except IntegrityError:
            # A double tap: the other request bought it.
            await session.rollback()
        common.told(event.id, "board")
    score = await session.scalar(select(GameHuntScores.total).where(GameHuntScores.registration_id == reg.id))
    return {"point_id": point.id, "hint": point.hint, "cost": cost, "charged": bought is None, "points": int(score or 0)}


# ─── The organiser's side ──────────────────────────────────────────────


async def act(session, event, action: str, minutes: Optional[int] = None) -> GameHunts:
    """start | pause | resume | end | extend."""
    h = await hunt_of(session, event.id)
    if h is None:
        raise err(404, "GAME_NOT_ON", "The hunt is not set up for this event.")
    await session.execute(select(GameHunts.id).where(GameHunts.id == h.id).with_for_update())
    await session.refresh(h)
    h = await tick(session, h)
    now = common.now()
    if action == "start":
        if h.status not in (DRAFT, SCHEDULED):
            raise err(409, "BAD_STATE", f"The hunt is already {h.status}.")
        if not await points_of(session, event.id):
            raise err(422, "NO_POINTS", "Add at least one QR point first.")
        h.status, h.starts_at, h.ends_at, h.paused_at = LIVE, now, now + timedelta(minutes=h.duration_minutes), None
    elif action == "pause":
        if h.status != LIVE:
            raise err(409, "BAD_STATE", "Only a live hunt can be paused.")
        h.status, h.paused_at = PAUSED, now
    elif action == "resume":
        if h.status != PAUSED:
            raise err(409, "BAD_STATE", "Only a paused hunt can be resumed.")
        # The time it stood still is given back.
        h.ends_at = _aware(h.ends_at) + (now - _aware(h.paused_at))
        h.status, h.paused_at = LIVE, None
    elif action == "end":
        if h.status not in (LIVE, PAUSED):
            raise err(409, "BAD_STATE", "Only a running hunt can be ended.")
        h.status, h.ends_at, h.paused_at = ENDED, min(_aware(h.ends_at), now) if h.ends_at else now, None
    elif action == "extend":
        add = int(minutes or 0)
        if add < 1 or add > 240:
            raise err(422, "BAD_MINUTES", "Extend by 1 to 240 minutes.")
        if h.status == ENDED:
            # Extending reopens it: the new time counts from now (or from where it was, if later).
            h.ends_at = max(_aware(h.ends_at) or now, now) + timedelta(minutes=add)
            h.status = LIVE
        elif h.status in (LIVE, PAUSED):
            h.ends_at = _aware(h.ends_at) + timedelta(minutes=add)
        else:
            raise err(409, "BAD_STATE", "Start the hunt first.")
    else:
        raise err(404, "NOT_FOUND", "No such action.")
    h.updated_at = now
    await session.commit()
    common.told(event.id, "hunt", "board")
    return h


async def live_numbers(session, event) -> dict:
    """Scan counts per point, and players who claimed 3+ points in different zones within 60 seconds (SPEC §12)."""
    from app.api.events.models import EventRegistrationsLink

    points = await points_of(session, event.id)
    counts = dict((pid, int(n)) for pid, n in (await session.execute(select(GameHuntClaims.point_id, func.count()).where(GameHuntClaims.event_id == event.id).group_by(GameHuntClaims.point_id))).all())
    claims = (
        await session.execute(
            select(GameHuntClaims.registration_id, GameHuntClaims.point_id, GameHuntClaims.created_at, EventRegistrationsLink.full_name)
            .join(EventRegistrationsLink, EventRegistrationsLink.id == GameHuntClaims.registration_id)
            .where(GameHuntClaims.event_id == event.id)
            .order_by(GameHuntClaims.registration_id, GameHuntClaims.created_at)
        )
    ).all()
    zone = {p.id: (p.zone or "") for p in points}
    flagged: dict = {}
    by_player: dict = {}
    for reg_id, pid, at, name in claims:
        by_player.setdefault(reg_id, {"name": name, "claims": []})["claims"].append((at, zone.get(pid, "")))
    for reg_id, info in by_player.items():
        rows = info["claims"]
        for i in range(len(rows)):
            window = [r for r in rows[i:] if (r[0] - rows[i][0]).total_seconds() <= 60]
            if len(window) >= 3 and len({z for _, z in window}) >= 3:
                flagged[reg_id] = {"registration_id": reg_id, "name": info["name"], "claims": len(window), "zones": sorted({z for _, z in window}), "first_at": rows[i][0]}
                break
    h = await hunt_of(session, event.id)
    return {
        "status": h.status if h else None,
        "points": [{"id": p.id, "number": p.number, "label": p.label, "zone": p.zone, "scans": counts.get(p.id, 0)} for p in points],
        "scans": sum(counts.values()),
        "claims": sum(counts.values()),
        "players": int(await session.scalar(select(func.count()).select_from(GameHuntScores).where(GameHuntScores.event_id == event.id)) or 0),
        "recent": await recent_finds(session, event.id),
        "finishers": int(await session.scalar(select(func.count()).select_from(GameHuntScores).where(GameHuntScores.event_id == event.id, GameHuntScores.completed_at.isnot(None))) or 0),
        "flags": list(flagged.values()),
        "flagged": list(flagged.values()),
    }
