"""Stations and the swag desk at the door console (CONTRACT §7).

A scan, an offline scan and a check-in by hand all reach checkins.service.scan.
Before anything else it asks `door_at`: the door the scan is at (the label
sent, else the door link's -- the rule admissions use) when that door is a
station or a swag desk. Only then does the scan come here, and here nobody
is ever admitted. An entrance never gets this far: its scan is exactly
what it was.

Consoles that know about quests say so (`X-Gate-Features: quests`) and get
STATION_VISIT / SWAG_DESK. Others -- the console deployed before, the app
until its next release -- get a known refusal: INVALID with a hint, final
for their offline queues, never an admission.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.checkins import doors as doors_mod
from app.api.checkins.models import CheckInVia, Doors
from app.api.quests import engine, stock
from app.api.quests import evaluators as ev
from app.api.quests import kinds as K
from app.api.quests.models import (
    COLLECTED,
    PURPOSES,
    RELEASED,
    RESERVED,
    STATION,
    SWAG_DESK,
    QuestAudit,
    QuestClaims,
    QuestMilestones,
    QuestRewards,
    QuestStationVisits,
    QuestTasks,
)
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

FEATURES_HEADER = "X-Gate-Features"
FEATURE = "quests"
OFFLINE_WINDOW = timedelta(hours=24)

OLD_CLIENT_ERROR = {
    STATION: "This door is a station, not an entrance. Reload the console or update the app to record visits here.",
    SWAG_DESK: "This door is a swag desk, not an entrance. Reload the console or update the app to hand rewards over here.",
}
OLD_CLIENT_HINT = {STATION: "QUEST_STATION", SWAG_DESK: "QUEST_SWAG_DESK"}


def features_of(header: Optional[str]) -> frozenset:
    return frozenset(p.strip().lower() for p in (header or "").split(",") if p.strip())


def aware(actor) -> bool:
    return FEATURE in getattr(actor, "features", frozenset())


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def door_at(session, event, actor, door_label: Optional[str]) -> Optional[Doors]:
    """The station or swag desk this scan is at, or None (an entrance)."""
    name = doors_mod.normalize(door_label or "")
    door = await doors_mod.by_name(session, event.id, name) if name else actor.door
    if door is not None and door.purpose in PURPOSES:
        return door
    return None


def masked(reg) -> dict:
    """What station and desk staff see: the name, never a contact or the ticket id."""
    role = getattr(reg.role, "value", reg.role) or "attendee"
    return {
        "id": reg.id,
        "name": reg.full_name or "",
        "ticket_id": "",
        "role": str(role),
        "email": None,
        "phone": None,
        "has_contact": False,
        "is_paid": True,
        "checked_in_at": None,
        "badge_printed_at": None,
        "admission_no": None,
        "ticket_type": None,
        "missing_answers": [],
    }


async def scan_at(
    session, event, door: Doors, *, code: Optional[str], registration, client_ref: Optional[str], scanned_at: Optional[datetime], actor, via
) -> dict:
    from app.api.checkins import service as cs

    reg = registration if registration is not None else await cs.lookup(session, event.id, code or "")
    if reg is None:
        other = await cs.elsewhere(session, code or "")
        if other is not None and other.event_id != event.id and other.event is not None:
            return cs._not_admitted("WRONG_EVENT", other_event={"name": other.event.name, "starts_at": other.event.event_datetime})
        return cs._not_admitted("INVALID")
    if reg.ticket_revoked_at is not None:
        return cs._not_admitted("REVOKED", attendee=masked(reg), revoked_at=reg.ticket_revoked_at)
    if via in (CheckInVia.kiosk, CheckInVia.phone):
        # A guest scanning themselves is no proof: stations and the desk are staff's.
        return cs._not_admitted("INVALID", hint=OLD_CLIENT_HINT[door.purpose], error="Stations and the swag desk are run by staff, not by a kiosk.")
    if door.purpose == STATION:
        station = await record_visit(session, event, door, reg, actor=actor, via=via, client_ref=client_ref, scanned_at=scanned_at)
        if not aware(actor):
            return cs._not_admitted("INVALID", hint=OLD_CLIENT_HINT[STATION], error=OLD_CLIENT_ERROR[STATION])
        replayed = station.pop("replayed", False)
        return {**cs._not_admitted("STATION_VISIT", attendee=masked(reg)), "replayed": replayed, "station": station}
    if not aware(actor):
        return cs._not_admitted("INVALID", hint=OLD_CLIENT_HINT[SWAG_DESK], error=OLD_CLIENT_ERROR[SWAG_DESK])
    return {**cs._not_admitted("SWAG_DESK", attendee=masked(reg)), "swag": await swag_block(session, event, door, reg)}


def _visit_time(via, scanned_at: Optional[datetime], now: datetime) -> datetime:
    if scanned_at is not None and scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)
    if via is CheckInVia.offline_sync and scanned_at is not None and scanned_at <= now and now - scanned_at < OFFLINE_WINDOW:
        return scanned_at
    return now


def _via_name(via) -> str:
    return {CheckInVia.offline_sync: "offline_sync", CheckInVia.desk: "desk"}.get(via, "gate")


async def record_visit(session, event, door: Doors, reg, *, actor, via, client_ref: Optional[str], scanned_at: Optional[datetime]) -> dict:
    """One visit per station and guest; a replay of a scan answers as it did."""
    V = QuestStationVisits
    now = _now()
    out = {"door_id": door.id, "name": door.name, "result": "VISITED", "reason": None, "visited_at": None, "task": None, "replayed": False}
    if client_ref:
        same = await session.scalar(select(V).where(V.client_ref == client_ref))
        if same is not None and same.door_id == door.id and same.registration_id == reg.id:
            out.update(visited_at=same.visited_at, replayed=True)
            await _describe_credit(session, event, door, reg, out, fresh=False)
            return out
        if same is not None:
            client_ref = None  # a reference used for another scan: recorded without it
    when = _visit_time(via, scanned_at, now)
    got = await session.scalar(
        pg_insert(V)
        .values(
            id=uuid.uuid4(),
            event_id=event.id,
            door_id=door.id,
            registration_id=reg.id,
            visited_at=when,
            scanned_at=scanned_at,
            recorded_by=actor.user_id,
            via=_via_name(via),
            client_ref=client_ref,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(V.id)
    )
    if got is None:
        first = await session.scalar(select(V).where(V.door_id == door.id, V.registration_id == reg.id))
        out.update(result="ALREADY_VISITED", visited_at=first.visited_at if first is not None else when)
        await _describe_credit(session, event, door, reg, out, fresh=False)
        await session.commit()
        return out
    out["visited_at"] = when
    await _describe_credit(session, event, door, reg, out, fresh=True)
    await session.commit()
    return out


async def _describe_credit(session, event, door: Doors, reg, out: dict, *, fresh: bool) -> None:
    """The station's task, and (for a new visit) its tick."""
    quest = await engine.quest_of(session, event.id)
    tasks = []
    if quest is not None:
        tasks = list(
            (
                await session.scalars(
                    select(QuestTasks)
                    .where(QuestTasks.quest_id == quest.id, QuestTasks.door_id == door.id, QuestTasks.kind == K.STATION, QuestTasks.archived_at.is_(None))
                    .order_by(QuestTasks.sort_order)
                )
            ).all()
        )
    if tasks:
        t = tasks[0]
        out["task"] = {"id": t.id, "title": t.title, "points": t.points}
    if not fresh:
        return
    if quest is None or not quest.enabled:
        out.update(result="NOT_COUNTED", reason="QUESTS_OFF")
        return
    if not tasks:
        out.update(result="NOT_COUNTED", reason="NO_TASK")
        return
    ctx = ev.Ctx(event=event, quest=quest, now=out["visited_at"])
    ticked, inside = False, False
    for t in tasks:
        if ev.open_now(ctx, t):
            inside = True
            ticked |= await engine.tick(session, quest, t, reg.id, source="station")
    if not inside:
        out.update(result="NOT_COUNTED", reason="OUTSIDE_WINDOW")
        return
    if ticked:
        d = await engine.definition(session, quest)
        await engine.refresh(session, d, event, [ev.Guest(reg.id, reg.user_id)], commit=False)


# ─── The swag desk ─────────────────────────────────────────────────────


async def _claims_of(session, event, reg) -> list:
    from app.api.merch.models import MerchItems, MerchVariants

    rows = (
        await session.execute(
            select(QuestClaims, QuestRewards, QuestMilestones, MerchItems)
            .join(QuestRewards, QuestRewards.id == QuestClaims.reward_id)
            .join(QuestMilestones, QuestMilestones.id == QuestClaims.milestone_id)
            .join(MerchItems, MerchItems.id == QuestClaims.merch_item_id)
            .where(QuestClaims.registration_id == reg.id, QuestRewards.event_id == event.id)
            .order_by(QuestMilestones.threshold, QuestRewards.sort_order, QuestRewards.created_at)
        )
    ).all()
    item_ids = {m.id for _, _, _, m in rows}
    variants: dict = {}
    if item_ids:
        for v in (
            await session.scalars(select(MerchVariants).where(MerchVariants.item_id.in_(item_ids)).order_by(MerchVariants.created_at, MerchVariants.id))
        ).all():
            variants.setdefault(v.item_id, []).append(v)
    return [(c, r, ms, m, variants.get(m.id, [])) for c, r, ms, m in rows]


async def _collection_brief(session, claim) -> dict:
    from app.api.merch.models import MerchCollections
    from app.api.users.models import Users

    if claim.collection_id is None:
        return {"collected_at": claim.collected_at, "collected_by": None, "door": None}
    row = (
        await session.execute(
            select(MerchCollections, Users).outerjoin(Users, Users.id == MerchCollections.collected_by).where(MerchCollections.id == claim.collection_id)
        )
    ).first()
    if row is None:
        return {"collected_at": claim.collected_at, "collected_by": None, "door": None}
    c, user = row
    return {"collected_at": c.collected_at, "collected_by": (user.full_name or user.email) if user else (c.door_label or None), "door": c.door_label}


async def claim_row(session, claim, reward, milestone, item, variants) -> dict:
    from app.core.storage import objects

    st = stock.Stock(item, variants)
    v = st.variant(claim.variant_id) if claim.variant_id is not None else None
    needs_size = claim.status == RESERVED and st.sized and v is None
    row = {
        "claim_id": claim.id,
        "reward_id": reward.id,
        "milestone": milestone.title,
        "milestone_id": milestone.id,
        "item_id": item.id,
        "name": item.name,
        "photo_url": objects.photo_url(item.photo_key),
        "status": claim.status,
        "needs_size": needs_size,
        "variant_label": v.label if v is not None else None,
        "choices": st.choices() if (claim.status == RESERVED and st.sized) else [],
        "collected_at": None,
        "collected_by": None,
        "door": None,
    }
    if claim.status == COLLECTED:
        row.update(await _collection_brief(session, claim))
    return row


async def swag_block(session, event, door: Optional[Doors], reg) -> dict:
    from app.api.quests.models import QuestEntries

    quest = await engine.quest_of(session, event.id)
    entry = await session.scalar(select(QuestEntries).where(QuestEntries.quest_id == quest.id, QuestEntries.registration_id == reg.id)) if quest else None
    rewards = [await claim_row(session, *r) for r in await _claims_of(session, event, reg) if r[0].status != RELEASED]
    return {
        "door_id": door.id if door is not None else None,
        "name": door.name if door is not None else None,
        "enabled": bool(quest and quest.enabled),
        "points": entry.points if entry else 0,
        "tasks_done": entry.tasks_done if entry else 0,
        "rewards": rewards,
    }


async def guest_view(session, event, reg_id) -> dict:
    """GET /gate/quests/{registration_id}: the desk's view after a search."""
    from app.api.checkins.service import registration_of

    reg = await registration_of(session, event, reg_id)
    visits = (
        await session.execute(
            select(QuestStationVisits.door_id, Doors.name, QuestStationVisits.visited_at)
            .join(Doors, Doors.id == QuestStationVisits.door_id)
            .where(QuestStationVisits.registration_id == reg.id)
            .order_by(QuestStationVisits.visited_at)
        )
    ).all()
    return {
        "attendee": masked(reg),
        "swag": await swag_block(session, event, None, reg),
        "stations": [{"door_id": d, "name": n, "visited_at": at} for d, n, at in visits],
    }


def may_hand_over(actor) -> bool:
    if actor.permissions.get("merch"):
        return True
    return actor.door is not None and actor.door.purpose == SWAG_DESK


async def handover(session, event, claim_id, actor, *, variant_label: Optional[str] = None, door_label: Optional[str] = None) -> dict:
    from app.api.events.models import EventRegistrationsLink
    from app.api.merch.models import MerchItems, MerchVariants

    if not may_hand_over(actor):
        raise CustomHTTPException(403, message="You may not hand rewards over at this door.", error_code="QUEST_HANDOVER_NOT_ALLOWED")
    first = await session.scalar(
        select(QuestClaims).join(QuestRewards, QuestRewards.id == QuestClaims.reward_id).where(QuestClaims.id == claim_id, QuestRewards.event_id == event.id)
    )
    if first is None:
        raise CustomHTTPException(404, message="That reward could not be found.")
    st = await stock.lock(session, first.merch_item_id)
    claim = await session.scalar(select(QuestClaims).where(QuestClaims.id == claim_id).with_for_update().execution_options(populate_existing=True))
    reg = await session.get(EventRegistrationsLink, claim.registration_id)
    reward = await session.get(QuestRewards, claim.reward_id)
    milestone = await session.get(QuestMilestones, claim.milestone_id)

    async def answer(result: str) -> dict:
        await session.commit()
        item = await session.get(MerchItems, claim.merch_item_id)
        variants = list(
            (await session.scalars(select(MerchVariants).where(MerchVariants.item_id == item.id).order_by(MerchVariants.created_at, MerchVariants.id))).all()
        )
        return {
            "result": result,
            "claim": await claim_row(session, claim, reward, milestone, item, variants),
            "remaining": stock.Stock(item, variants).remaining(claim.variant_id),
        }

    if claim.status == COLLECTED:
        return await answer("ALREADY_COLLECTED")
    if claim.status != RESERVED:
        return await answer("NOT_RESERVED")
    if st.sized:
        if variant_label:
            try:
                stock.choose_size(st, claim, variant_label, reg, event)
            except LookupError:
                raise CustomHTTPException(422, message="That is not one of the sizes set up for this item.")
            except stock.SizeGone:
                return await answer("SIZE_GONE")
        elif claim.variant_id is None:
            return await answer("NEEDS_SIZE")
    name = doors_mod.normalize(door_label or "")
    door = (await doors_mod.by_name(session, event.id, name) if name else None) or actor.door
    collection = await stock.hand_over(session, st, claim, reg, event, actor_user_id=actor.user_id, door=door, door_label=door_label)
    if collection is None:
        return await answer("ALREADY_COLLECTED")
    session.add(
        QuestAudit(
            quest_id=claim.quest_id,
            registration_id=reg.id,
            reward_id=reward.id,
            action="handover",
            by_user_id=actor.user_id,
            by_name=(actor.name or "")[:120],
            detail={"door": door.name if door else door_label},
        )
    )
    return await answer("HANDED_OVER")


async def context_block(session, event, actor) -> Optional[dict]:
    """`quests` in /gate/context for a console that knows about them."""
    quest = await engine.quest_of(session, event.id)
    door = actor.door
    if door is None and actor.volunteer is not None and actor.volunteer.door_id is not None:
        door = await session.get(Doors, actor.volunteer.door_id)
    if quest is None and (door is None or door.purpose is None):
        return None
    return {
        "enabled": bool(quest and quest.enabled),
        "door_purpose": door.purpose if door is not None else None,
        "door_name": door.name if door is not None else None,
        "can_hand_over": may_hand_over(actor),
    }
