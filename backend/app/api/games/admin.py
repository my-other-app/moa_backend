"""The admin portal's games overview: GET /api/v1/admin/games/overview."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.clubs.models import Clubs
from app.api.events.models import Events
from app.api.games import common
from app.api.games.models import (
    GameCardScores,
    GameHuntClaims,
    GameHuntScores,
    GameMeetings,
    GamePlayers,
    GameSettings,
)
from app.api.subscriptions.models import ClubSubscriptions, PlanFeatures, Plans, SubscriptionStatus
from app.core.auth.dependencies import AdminAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/admin/games", tags=["admin"])

CONNECTED_WITHIN = timedelta(minutes=5)


def _by_event(session, column, table, *where):
    return session.execute(select(table.event_id, func.count()).where(*where).group_by(table.event_id))


@router.get("/overview", summary="Where the games are on, and how they are played")
async def overview(
    session: SessionDep, user: AdminAuth, date_from: Optional[datetime] = Query(None, alias="from"), date_to: Optional[datetime] = Query(None, alias="to")
) -> dict:
    now = common.now()
    q = (
        select(GameSettings, Events.name, Events.slug, Events.event_datetime, Events.club_id, Clubs.name)
        .join(Events, Events.id == GameSettings.event_id)
        .outerjoin(Clubs, Clubs.id == Events.club_id)
        .where(Events.is_deleted == False, (GameSettings.cards_on == True) | (GameSettings.hunt_on == True))  # noqa: E712
    )
    if date_from is not None:
        q = q.where(Events.event_datetime >= date_from)
    if date_to is not None:
        q = q.where(Events.event_datetime <= date_to)
    base = (await session.execute(q.order_by(Events.event_datetime.desc()))).all()
    ids = [r[0].event_id for r in base]

    async def per_event(table, *where):
        if not ids:
            return {}
        rows = await session.execute(select(table.event_id, func.count()).where(table.event_id.in_(ids), *where).group_by(table.event_id))
        return {e: int(n) for e, n in rows.all()}

    from app.api.games.models import GameReveals

    players = await per_event(GamePlayers)
    connected = await per_event(GamePlayers, GamePlayers.last_seen_at > now - CONNECTED_WITHIN)
    reveals = await per_event(GameReveals)
    collects = await per_event(GameMeetings, GameMeetings.scanned == True)  # noqa: E712
    claims = await per_event(GameHuntClaims)
    decks = await per_event(GameCardScores, GameCardScores.completed_at.isnot(None))
    hunts = await per_event(GameHuntScores, GameHuntScores.completed_at.isnot(None))
    rows = []
    for s, name, slug, at, club_id, club_name in base:
        e = s.event_id
        rows.append(
            {
                "event_id": e,
                "event": name,
                "event_name": name,
                "club_name": club_name,
                "slug": slug,
                "event_datetime": at,
                "club_id": club_id,
                "club": club_name,
                "cards_on": s.cards_on,
                "hunt_on": s.hunt_on,
                "players": players.get(e, 0),
                "connected_now": connected.get(e, 0),
                "reveals": reveals.get(e, 0),
                "collects": collects.get(e, 0),
                "hunt_claims": claims.get(e, 0),
                "decks_completed": decks.get(e, 0),
                "hunts_completed": hunts.get(e, 0),
            }
        )
    def total(key, only=None):
        return sum(r[key] for r in rows if only is None or r[only])

    totals = {
        "cards": {"events": sum(1 for r in rows if r["cards_on"]), "players": total("players", "cards_on"), "reveals": total("reveals"), "collects": total("collects"), "decks_completed": total("decks_completed")},
        "hunt": {"events": sum(1 for r in rows if r["hunt_on"]), "players": total("players", "hunt_on"), "claims": total("hunt_claims"), "hunts_completed": total("hunts_completed")},
        "events": len(rows),
        "players": total("players"),
        "connected_now": total("connected_now"),
    }
    # Clubs with each feature, per plan: the plans a platform admin ticked them on.
    plan_rows = (
        await session.execute(
            select(Plans.key, Plans.name, PlanFeatures.game_cards, PlanFeatures.game_hunt, func.count(func.distinct(ClubSubscriptions.club_id)))
            .join(PlanFeatures, PlanFeatures.plan_id == Plans.id)
            .outerjoin(
                ClubSubscriptions,
                (ClubSubscriptions.plan_id == Plans.id)
                & (ClubSubscriptions.is_deleted == False)  # noqa: E712
                & (ClubSubscriptions.status.in_([SubscriptionStatus.active, SubscriptionStatus.trial])),
            )
            .where(Plans.is_deleted == False)  # noqa: E712
            .group_by(Plans.key, Plans.name, Plans.sort_order, PlanFeatures.game_cards, PlanFeatures.game_hunt)
            .order_by(Plans.sort_order)
        )
    ).all()
    plans = [{"plan": k, "name": n, "game_cards": bool(c), "game_hunt": bool(h), "clubs": int(clubs)} for k, n, c, h, clubs in plan_rows]
    return {
        "from": date_from,
        "to": date_to,
        "totals": totals,
        "rows": rows,
        "plans": plans,
        "clubs_with": {
            "game_cards": sum(p["clubs"] for p in plans if p["game_cards"]),
            "game_hunt": sum(p["clubs"] for p in plans if p["game_hunt"]),
        },
    }
