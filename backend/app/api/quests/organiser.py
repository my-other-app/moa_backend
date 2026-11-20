"""The organiser's side: settings, tasks, milestones, rewards, stations, the
live dashboard, guests, grants and revokes, the audit and the export
(CONTRACT §9)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.api.checkins import doors as doors_mod
from app.api.checkins.models import DoorKind, Doors
from app.api.quests import engine, signals, stock, switch
from app.api.quests import kinds as K
from app.api.quests.models import (
    CLAIM_STATUSES,
    COLLECTED,
    PURPOSES,
    RELEASED,
    RESERVED,
    STATION,
    QuestAudit,
    QuestClaims,
    QuestEntries,
    QuestMilestones,
    QuestProgress,
    QuestRewards,
    Quests,
    QuestStationVisits,
    QuestTasks,
    QuestUnlocks,
)
from app.response import CustomHTTPException


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure(session, event) -> Quests:
    """The event's quest, made (off) on first use."""
    quest = await engine.quest_of(session, event.id)
    if quest is None:
        await session.execute(pg_insert(Quests).values(id=uuid.uuid4(), event_id=event.id, enabled=False).on_conflict_do_nothing(index_elements=["event_id"]))
        await session.commit()
        quest = await engine.quest_of(session, event.id)
    return quest


def _changed(event_id: int) -> None:
    switch.forget(event_id)


# ─── The whole quest, for the editor ───────────────────────────────────


def settings_out(q: Quests) -> dict:
    return {
        "id": q.id,
        "event_id": q.event_id,
        "enabled": q.enabled,
        "title": q.title,
        "intro": q.intro,
        "mode": q.mode,
        "starts_at": q.starts_at,
        "ends_at": q.ends_at,
        "leaderboard": q.leaderboard,
        "leaderboard_names": q.leaderboard_names,
        "code_attempts_per_hour": q.code_attempts_per_hour,
        "updated_at": q.updated_at,
    }


async def _done_counts(session, quest) -> dict:
    P = QuestProgress
    rows = await session.execute(
        select(
            P.task_id,
            func.count().filter(P.done_at.isnot(None), P.revoked_at.is_(None)),
            func.count().filter(P.done_at.is_(None), P.revoked_at.is_(None), P.count > 0),
        )
        .where(P.quest_id == quest.id)
        .group_by(P.task_id)
    )
    return {t: (int(d), int(i)) for t, d, i in rows.all()}


def task_admin(t: QuestTasks, stations: dict, counts: dict) -> dict:
    kind = K.KINDS.get(t.kind)
    st = stations.get(t.door_id) if t.door_id else None
    done, doing = counts.get(t.id, (0, 0))
    return {
        "id": t.id,
        "kind": t.kind,
        "title": t.title,
        "description": t.description,
        "points": t.points,
        "required": t.required,
        "starts_at": t.starts_at,
        "ends_at": t.ends_at,
        "config": t.config or {},
        "sort_order": t.sort_order,
        "target": K.target(t.kind, t.config),
        "action": kind.action if kind else None,
        "station": {"door_id": st["id"], "name": st["name"]} if st else None,
        "done_count": done,
        "in_progress": doing,
        "archived": t.archived_at is not None,
    }


async def _claim_counts(session, quest) -> dict:
    C = QuestClaims
    out: dict = {}
    rows = await session.execute(
        select(C.reward_id, C.status, func.count(), func.count().filter(C.holds == True, C.variant_id.is_(None)))
        .where(C.quest_id == quest.id)
        .group_by(C.reward_id, C.status)  # noqa: E712
    )
    for reward_id, status, n, unsized in rows.all():
        row = out.setdefault(reward_id, {s: 0 for s in CLAIM_STATUSES} | {"needs_size": 0})
        row[status] = int(n)
        row["needs_size"] += int(unsized)
    return out


def stock_out(st: stock.Stock) -> dict:
    m = st.item
    if st.sized:
        total = sum(v.stock for v in st.variants)
        reserved = sum(v.reserved_count or 0 for v in st.variants) + (m.reserved_count or 0)
    else:
        total, reserved = (m.stock or 0), (m.reserved_count or 0)
    return {
        "total": total,
        "reserved": reserved,
        "collected": m.collected_count + (m.manual_handed_out or 0),
        "free": max(0, st.pool()),
        "variants": [
            {"id": v.id, "label": v.label, "total": v.stock, "reserved": v.reserved_count or 0, "collected": v.collected_count, "free": max(0, st.size_free(v))}
            for v in st.variants
        ],
    }


async def _station_rows(session, event) -> list:
    doors = list(
        (await session.scalars(select(Doors).where(Doors.event_id == event.id, Doors.purpose.isnot(None)).order_by(Doors.sort_order, Doors.created_at))).all()
    )
    visits = dict(
        (
            await session.execute(
                select(QuestStationVisits.door_id, func.count()).where(QuestStationVisits.event_id == event.id).group_by(QuestStationVisits.door_id)
            )
        ).all()
    )
    last = dict(
        (
            await session.execute(
                select(QuestStationVisits.door_id, func.max(QuestStationVisits.visited_at))
                .where(QuestStationVisits.event_id == event.id)
                .group_by(QuestStationVisits.door_id)
            )
        ).all()
    )
    return [
        {
            "id": d.id,
            "door_id": d.id,
            "name": d.name,
            "purpose": d.purpose,
            "active": d.active,
            "link": doors_mod._link_row(d),
            "visits": int(visits.get(d.id, 0)),
            "last_visit_at": last.get(d.id),
        }
        for d in doors
    ]


async def view(session, event) -> dict:
    from app.api.merch.models import MerchItems, MerchVariants
    from app.core.storage import objects

    quest = await ensure(session, event)
    d = await engine.definition(session, quest)
    stations = await _station_rows(session, event)
    by_door = {s["id"]: s for s in stations}
    counts = await _done_counts(session, quest)
    claims = await _claim_counts(session, quest)
    items = list((await session.scalars(select(MerchItems).where(MerchItems.event_id == event.id).order_by(MerchItems.created_at, MerchItems.id))).all())
    variants: dict = {}
    for v in (
        await session.scalars(
            select(MerchVariants).where(MerchVariants.item_id.in_([m.id for m in items] or [uuid.uuid4()])).order_by(MerchVariants.created_at, MerchVariants.id)
        )
    ).all():
        variants.setdefault(v.item_id, []).append(v)
    stocks = {m.id: stock.Stock(m, variants.get(m.id, [])) for m in items}
    used = {r.merch_item_id: r.id for rs in d.rewards.values() for r in rs}
    milestones = []
    for m in d.milestones:
        rewards = []
        for r in d.rewards.get(m.id, []):
            st = stocks.get(r.merch_item_id)
            if st is None:
                continue
            rewards.append(
                {
                    "id": r.id,
                    "merch_item_id": r.merch_item_id,
                    "name": st.item.name,
                    "photo_url": objects.photo_url(st.item.photo_key),
                    "sold_out_mode": r.sold_out_mode,
                    "sizes": st.sized,
                    "stock": stock_out(st),
                    "claims": claims.get(r.id, {s: 0 for s in CLAIM_STATUSES} | {"needs_size": 0}),
                }
            )
        milestones.append({"id": m.id, "title": m.title, "threshold": m.threshold, "sort_order": m.sort_order, "rewards": rewards})
    return {
        **settings_out(quest),
        "tasks": [task_admin(t, by_door, counts) for t in d.tasks],
        "milestones": milestones,
        "stations": stations,
        "swag_items": [
            {"id": m.id, "name": m.name, "photo_url": objects.photo_url(m.photo_key), "sizes": st.sized, "stock": stock_out(st), "reward_id": used.get(m.id)}
            for m, st in ((m, stocks[m.id]) for m in items)
        ],
    }


# ─── Settings ──────────────────────────────────────────────────────────


async def patch(session, event, body, user) -> dict:
    quest = await ensure(session, event)
    data = body.model_dump(exclude_unset=True)
    turning_on = data.get("enabled") is True and not quest.enabled
    starts, ends = data.get("starts_at", quest.starts_at), data.get("ends_at", quest.ends_at)
    if starts is not None and ends is not None and ends <= starts:
        raise CustomHTTPException(422, message="The quest must end after it starts.", errors={"fields": {"ends_at": "After the start."}})
    for key, value in data.items():
        if key in ("title", "intro") and value is not None:
            value = " ".join(value.split()) or None
        if key in ("mode", "leaderboard_names", "code_attempts_per_hour", "leaderboard", "enabled") and value is None:
            continue
        setattr(quest, key, value)
    quest.updated_by = getattr(user, "id", None)
    quest.updated_at = _now()
    rule_change = turning_on or "mode" in data or "starts_at" in data or "ends_at" in data
    if rule_change and quest.enabled:
        await signals.recompute(session, event.id)
    await session.commit()
    _changed(event.id)
    return await view(session, event)


# ─── Tasks ─────────────────────────────────────────────────────────────


async def _task(session, quest, task_id) -> QuestTasks:
    t = await session.scalar(select(QuestTasks).where(QuestTasks.id == task_id, QuestTasks.quest_id == quest.id, QuestTasks.archived_at.is_(None)))
    if t is None:
        raise CustomHTTPException(404, message="That task could not be found.")
    return t


async def _station_door(session, event, door_id) -> Doors:
    try:
        did = uuid.UUID(str(door_id))
    except ValueError:
        did = None
    door = await session.get(Doors, did) if did else None
    if door is None or door.event_id != event.id or door.purpose != STATION:
        raise CustomHTTPException(
            422, message="Pick one of the event's stations.", error_code="QUEST_STATION_NOT_FOUND", errors={"fields": {"door_id": "A station of this event."}}
        )
    return door


def _check_window(starts, ends) -> None:
    if starts is not None and ends is not None and ends <= starts:
        raise CustomHTTPException(422, message="A task must end after it starts.", errors={"fields": {"ends_at": "After the start."}})


async def _code_free(session, quest, code: Optional[str], task_id=None) -> None:
    if not code:
        return
    q = select(QuestTasks.id).where(QuestTasks.quest_id == quest.id, QuestTasks.code == code, QuestTasks.archived_at.is_(None))
    if task_id is not None:
        q = q.where(QuestTasks.id != task_id)
    if await session.scalar(q) is not None:
        raise CustomHTTPException(
            409, message="Another code task already uses that code.", error_code="QUEST_CODE_TAKEN", errors={"fields": {"code": "Pick another code."}}
        )


async def _save(session) -> None:
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise CustomHTTPException(
            409, message="Another code task already uses that code.", error_code="QUEST_CODE_TAKEN", errors={"fields": {"code": "Pick another code."}}
        )


async def create_task(session, event, body) -> dict:
    quest = await ensure(session, event)
    config = K.clean_config(body.kind, body.config)
    _check_window(body.starts_at, body.ends_at)
    door = await _station_door(session, event, config.get("door_id")) if body.kind == K.STATION else None
    code = K.normal_code(config.get("code")) if body.kind == K.CODE else None
    await _code_free(session, quest, code)
    count = await session.scalar(select(func.count()).select_from(QuestTasks).where(QuestTasks.quest_id == quest.id, QuestTasks.archived_at.is_(None))) or 0
    t = QuestTasks(
        id=uuid.uuid4(),
        quest_id=quest.id,
        event_id=event.id,
        kind=body.kind,
        title=(" ".join((body.title or "").split()) or K.default_title(body.kind, config, door.name if door else None))[:80],
        description=(" ".join((body.description or "").split()) or None),
        points=body.points,
        required=body.required,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        config=config,
        door_id=door.id if door else None,
        code=code,
        sort_order=count,
    )
    session.add(t)
    await _save(session)
    if quest.enabled and body.kind in K.AUTOMATIC:
        await signals.recompute(session, event.id)
    await session.commit()
    return task_admin(t, {s["id"]: s for s in await _station_rows(session, event)}, {})


async def update_task(session, event, task_id, body) -> dict:
    quest = await ensure(session, event)
    t = await _task(session, quest, task_id)
    data = body.model_dump(exclude_unset=True)
    rule = False
    if "config" in data and data["config"] is not None:
        config = K.clean_config(t.kind, data["config"])
        if t.kind == K.STATION:
            t.door_id = (await _station_door(session, event, config.get("door_id"))).id
        if t.kind == K.CODE:
            code = K.normal_code(config.get("code"))
            await _code_free(session, quest, code, t.id)
            t.code = code
        rule = config != (t.config or {})
        t.config = config
    starts, ends = data.get("starts_at", t.starts_at), data.get("ends_at", t.ends_at)
    _check_window(starts, ends)
    for key in ("starts_at", "ends_at"):
        if key in data:
            rule = rule or data[key] != getattr(t, key)
            setattr(t, key, data[key])
    for key in ("points", "required"):
        if data.get(key) is not None:
            rule = True
            setattr(t, key, data[key])
    if data.get("title"):
        t.title = " ".join(data["title"].split())[:80]
    if "description" in data:
        t.description = " ".join((data["description"] or "").split()) or None
    t.updated_at = _now()
    await _save(session)
    if rule and quest.enabled:
        await signals.recompute(session, event.id)
    await session.commit()
    return task_admin(t, {s["id"]: s for s in await _station_rows(session, event)}, await _done_counts(session, quest))


async def delete_task(session, event, task_id) -> None:
    quest = await ensure(session, event)
    t = await _task(session, quest, task_id)
    had = await session.scalar(select(func.count()).select_from(QuestProgress).where(QuestProgress.task_id == t.id)) or 0
    if had:
        t.archived_at = _now()
    else:
        await session.delete(t)
    if quest.enabled:
        await signals.recompute(session, event.id)
    await session.commit()


async def order_tasks(session, event, ids: list) -> list:
    quest = await ensure(session, event)
    d = await engine.definition(session, quest)
    known = {t.id: t for t in d.tasks}
    pos = 0
    for i in ids:
        if i in known:
            known[i].sort_order = pos
            pos += 1
    for t in d.tasks:
        if t.id not in ids:
            t.sort_order = pos
            pos += 1
    await session.commit()
    stations = {s["id"]: s for s in await _station_rows(session, event)}
    counts = await _done_counts(session, quest)
    return [task_admin(t, stations, counts) for t in sorted(d.tasks, key=lambda t: t.sort_order)]


# ─── Milestones and rewards ────────────────────────────────────────────


async def _milestone(session, quest, milestone_id) -> QuestMilestones:
    m = await session.scalar(select(QuestMilestones).where(QuestMilestones.id == milestone_id, QuestMilestones.quest_id == quest.id))
    if m is None:
        raise CustomHTTPException(404, message="That milestone could not be found.")
    return m


def _milestone_out(m: QuestMilestones) -> dict:
    return {"id": m.id, "title": m.title, "threshold": m.threshold, "sort_order": m.sort_order, "rewards": []}


async def create_milestone(session, event, body) -> dict:
    quest = await ensure(session, event)
    count = await session.scalar(select(func.count()).select_from(QuestMilestones).where(QuestMilestones.quest_id == quest.id)) or 0
    m = QuestMilestones(id=uuid.uuid4(), quest_id=quest.id, title=" ".join(body.title.split())[:80], threshold=body.threshold, sort_order=count)
    session.add(m)
    if quest.enabled:
        await signals.recompute(session, event.id)
    await session.commit()
    return await _milestone_view(session, event, m.id)


async def _milestone_view(session, event, milestone_id) -> dict:
    v = await view(session, event)
    return next((m for m in v["milestones"] if m["id"] == milestone_id), None) or {}


async def update_milestone(session, event, milestone_id, body) -> dict:
    quest = await ensure(session, event)
    m = await _milestone(session, quest, milestone_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("title"):
        m.title = " ".join(data["title"].split())[:80]
    if data.get("sort_order") is not None:
        m.sort_order = data["sort_order"]
    if data.get("threshold") is not None and data["threshold"] != m.threshold:
        m.threshold = data["threshold"]
        if quest.enabled:
            await signals.recompute(session, event.id)
    m.updated_at = _now()
    await session.commit()
    return await _milestone_view(session, event, m.id)


async def _release_reward(session, quest, event, reward_ids: list) -> None:
    if not reward_ids:
        return
    claims = list(
        (await session.scalars(select(QuestClaims).where(QuestClaims.reward_id.in_(reward_ids), QuestClaims.status.notin_((COLLECTED, RELEASED))))).all()
    )
    d = await engine.definition(session, quest)
    await engine.release_claims(session, d, event, claims)


async def delete_milestone(session, event, milestone_id) -> None:
    quest = await ensure(session, event)
    m = await _milestone(session, quest, milestone_id)
    rewards = list((await session.scalars(select(QuestRewards.id).where(QuestRewards.milestone_id == m.id))).all())
    await _release_reward(session, quest, event, rewards)
    await session.execute(delete(QuestMilestones).where(QuestMilestones.id == m.id))
    await session.commit()


async def _reward(session, quest, reward_id) -> QuestRewards:
    r = await session.scalar(select(QuestRewards).where(QuestRewards.id == reward_id, QuestRewards.quest_id == quest.id))
    if r is None:
        raise CustomHTTPException(404, message="That reward could not be found.")
    return r


async def _reward_view(session, event, reward_id) -> dict:
    v = await view(session, event)
    for m in v["milestones"]:
        for r in m["rewards"]:
            if r["id"] == reward_id:
                return {**r, "milestone_id": m["id"]}
    return {}


async def add_reward(session, event, milestone_id, body) -> dict:
    from app.api.merch.models import MerchItems

    quest = await ensure(session, event)
    m = await _milestone(session, quest, milestone_id)
    item = await session.get(MerchItems, body.merch_item_id)
    if item is None or item.event_id != event.id:
        raise CustomHTTPException(404, message="That swag item could not be found.")
    taken = await session.scalar(select(QuestRewards.id).where(QuestRewards.quest_id == quest.id, QuestRewards.merch_item_id == item.id))
    if taken is not None:
        raise CustomHTTPException(409, message="That item is already a reward of this quest.", error_code="QUEST_ITEM_ALREADY_A_REWARD")
    count = await session.scalar(select(func.count()).select_from(QuestRewards).where(QuestRewards.milestone_id == m.id)) or 0
    r = QuestRewards(
        id=uuid.uuid4(), quest_id=quest.id, event_id=event.id, milestone_id=m.id, merch_item_id=item.id, sold_out_mode=body.sold_out_mode, sort_order=count
    )
    session.add(r)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise CustomHTTPException(409, message="That item is already a reward of this quest.", error_code="QUEST_ITEM_ALREADY_A_REWARD")
    if quest.enabled:
        # Guests who already reached the milestone are given their claims.
        await signals.recompute(session, event.id)
    await session.commit()
    return await _reward_view(session, event, r.id)


async def update_reward(session, event, reward_id, body) -> dict:
    quest = await ensure(session, event)
    r = await _reward(session, quest, reward_id)
    if body.sold_out_mode is not None:
        r.sold_out_mode = body.sold_out_mode
        r.updated_at = _now()
    await session.commit()
    return await _reward_view(session, event, r.id)


async def delete_reward(session, event, reward_id) -> None:
    quest = await ensure(session, event)
    r = await _reward(session, quest, reward_id)
    await _release_reward(session, quest, event, [r.id])
    await session.execute(delete(QuestRewards).where(QuestRewards.id == r.id))
    await session.commit()


async def reoffer(session, event, reward_id, user, by_name: str) -> dict:
    quest = await ensure(session, event)
    r = await _reward(session, quest, reward_id)
    st = await stock.lock(session, r.merch_item_id)
    held = await stock.reoffer(session, st, event, r.id) if st is not None else []
    for c in held:
        await signals.held(session, c.id)
    left = await session.scalar(select(func.count()).select_from(QuestClaims).where(QuestClaims.reward_id == r.id, QuestClaims.status == "sold_out")) or 0
    session.add(
        QuestAudit(quest_id=quest.id, reward_id=r.id, action="reoffer", by_user_id=getattr(user, "id", None), by_name=by_name, detail={"held": len(held)})
    )
    await session.commit()
    return {"held": len(held), "still_sold_out": int(left)}


# ─── Stations ──────────────────────────────────────────────────────────


async def create_station(session, event, body) -> dict:
    from app.api.checkins.schemas import DoorOut, LinkOut

    quest = await ensure(session, event)
    door = await doors_mod.create_door(session, event, name=body.name, kind=DoorKind.door, purpose=body.purpose)
    task = None
    if body.purpose == STATION and body.task is not None:
        from app.api.quests.schemas import TaskIn

        task = await create_task(
            session,
            event,
            TaskIn(kind=K.STATION, title=body.task.title, points=body.task.points, required=body.task.required, config={"door_id": str(door.id)}),
        )
    link = None
    if body.link is not None:
        link = LinkOut(**await doors_mod.issue_link(session, event, door.id, can_walkup=False, days=body.link.days)).model_dump()
    door_out = DoorOut(**next(d for d in await doors_mod.list_doors(session, event) if d["id"] == door.id)).model_dump()
    return {"door": door_out, "link": link, "task": task}


# ─── The live dashboard ────────────────────────────────────────────────


async def live(session, event) -> dict:
    from app.api.checkins.models import CheckIns
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.merch.models import MerchItems
    from app.api.quests.guest import short_name

    v = await view(session, event)
    quest = await engine.quest_of(session, event.id)
    participants = (
        await session.scalar(
            select(func.count()).select_from(QuestEntries).where(QuestEntries.quest_id == quest.id, or_(QuestEntries.tasks_done > 0, QuestEntries.points > 0))
        )
        or 0
    )
    checked_in = await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.event_id == event.id)) or 0
    unlocked = dict(
        (
            await session.execute(
                select(QuestUnlocks.milestone_id, func.count())
                .where(QuestUnlocks.quest_id == quest.id, QuestUnlocks.revoked_at.is_(None))
                .group_by(QuestUnlocks.milestone_id)
            )
        ).all()
    )
    recent_unlocks = (
        await session.execute(
            select(QuestUnlocks.unlocked_at, R.full_name, QuestMilestones.title)
            .join(R, R.id == QuestUnlocks.registration_id)
            .join(QuestMilestones, QuestMilestones.id == QuestUnlocks.milestone_id)
            .where(QuestUnlocks.quest_id == quest.id, QuestUnlocks.revoked_at.is_(None))
            .order_by(QuestUnlocks.unlocked_at.desc())
            .limit(20)
        )
    ).all()
    recent_handovers = (
        await session.execute(
            select(QuestClaims.collected_at, R.full_name, MerchItems.name)
            .join(R, R.id == QuestClaims.registration_id)
            .join(MerchItems, MerchItems.id == QuestClaims.merch_item_id)
            .where(QuestClaims.quest_id == quest.id, QuestClaims.status == COLLECTED, QuestClaims.collected_at.isnot(None))
            .order_by(QuestClaims.collected_at.desc())
            .limit(20)
        )
    ).all()
    recent = sorted(
        [{"at": at, "name": short_name(n), "what": f"unlocked {t}"} for at, n, t in recent_unlocks]
        + [{"at": at, "name": short_name(n), "what": f"collected {i}"} for at, n, i in recent_handovers],
        key=lambda r: r["at"],
        reverse=True,
    )[:20]
    rewards = []
    for m in v["milestones"]:
        for r in m["rewards"]:
            c = r["claims"]
            rewards.append(
                {
                    "id": r["id"],
                    "milestone_id": m["id"],
                    "name": r["name"],
                    "reserved": c.get(RESERVED, 0),
                    "needs_size": c.get("needs_size", 0),
                    "waitlisted": c.get("waitlisted", 0),
                    "sold_out": c.get("sold_out", 0),
                    "collected": c.get(COLLECTED, 0),
                    "stock": r["stock"],
                }
            )
    return {
        "enabled": v["enabled"],
        "participants": int(participants),
        "checked_in": int(checked_in),
        "tasks": [{"id": t["id"], "title": t["title"], "kind": t["kind"], "done": t["done_count"], "in_progress": t["in_progress"]} for t in v["tasks"]],
        "milestones": [{"id": m["id"], "title": m["title"], "threshold": m["threshold"], "unlocked": int(unlocked.get(m["id"], 0))} for m in v["milestones"]],
        "rewards": rewards,
        "stations": [
            {"door_id": s["door_id"], "name": s["name"], "purpose": s["purpose"], "visits": s["visits"], "last_visit_at": s["last_visit_at"]}
            for s in v["stations"]
        ],
        "recent": recent,
        "computed_at": _now(),
    }


# ─── Guests ────────────────────────────────────────────────────────────


async def guests(session, event, *, q=None, milestone_id=None, task_id=None, claim_status=None, sort="points", page=1, page_size=50) -> dict:
    from app.api.checkins.models import CheckIns
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.merch.models import MerchItems, MerchVariants

    quest = await ensure(session, event)
    E = QuestEntries
    base = select(R, E).outerjoin(E, (E.registration_id == R.id) & (E.quest_id == quest.id)).where(R.event_id == event.id, R.is_deleted == False)  # noqa: E712
    has_any = or_(
        E.id.isnot(None),
        select(QuestClaims.id).where(QuestClaims.registration_id == R.id, QuestClaims.quest_id == quest.id).exists(),
    )
    base = base.where(has_any)
    if q:
        pat = f"%{q.strip()}%"
        base = base.where(or_(R.full_name.ilike(pat), R.email.ilike(pat), R.ticket_id.ilike(pat)))
    if milestone_id is not None:
        base = base.where(
            select(QuestUnlocks.id)
            .where(QuestUnlocks.registration_id == R.id, QuestUnlocks.milestone_id == milestone_id, QuestUnlocks.revoked_at.is_(None))
            .exists()
        )
    if task_id is not None:
        base = base.where(
            select(QuestProgress.id)
            .where(
                QuestProgress.registration_id == R.id, QuestProgress.task_id == task_id, QuestProgress.done_at.isnot(None), QuestProgress.revoked_at.is_(None)
            )
            .exists()
        )
    if claim_status:
        base = base.where(
            select(QuestClaims.id).where(QuestClaims.registration_id == R.id, QuestClaims.quest_id == quest.id, QuestClaims.status == claim_status).exists()
        )
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    order = {"name": (R.full_name.asc(),), "recent": (E.updated_at.desc().nulls_last(),)}.get(
        sort, (E.score.desc().nulls_last(), E.score_at.asc().nulls_last())
    )
    page, page_size = max(1, page), max(1, min(page_size, 200))
    rows = (await session.execute(base.order_by(*order, R.id).limit(page_size).offset((page - 1) * page_size))).all()
    ids = [r.id for r, _ in rows]
    checked = set((await session.scalars(select(CheckIns.registration_id).where(CheckIns.registration_id.in_(ids or [uuid.uuid4()])))).all())
    unlocks: dict = {}
    for reg_id, mid in (
        await session.execute(
            select(QuestUnlocks.registration_id, QuestUnlocks.milestone_id).where(
                QuestUnlocks.registration_id.in_(ids or [uuid.uuid4()]), QuestUnlocks.revoked_at.is_(None)
            )
        )
    ).all():
        unlocks.setdefault(reg_id, []).append(mid)
    claims: dict = {}
    for c, name, label in (
        await session.execute(
            select(QuestClaims, MerchItems.name, MerchVariants.label)
            .join(MerchItems, MerchItems.id == QuestClaims.merch_item_id)
            .outerjoin(MerchVariants, MerchVariants.id == QuestClaims.variant_id)
            .where(QuestClaims.registration_id.in_(ids or [uuid.uuid4()]), QuestClaims.quest_id == quest.id)
        )
    ).all():
        claims.setdefault(c.registration_id, []).append({"claim_id": c.id, "reward_id": c.reward_id, "name": name, "status": c.status, "variant_label": label})
    return {
        "items": [
            {
                "registration_id": r.id,
                "name": r.full_name or "",
                "email": r.email,
                "ticket_id": r.ticket_id,
                "checked_in": r.id in checked,
                "points": e.points if e else 0,
                "tasks_done": e.tasks_done if e else 0,
                "score": e.score if e else 0,
                "milestones": unlocks.get(r.id, []),
                "claims": claims.get(r.id, []),
            }
            for r, e in rows
        ],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


async def _registration(session, event, registration_id):
    from app.api.checkins.service import registration_of

    return await registration_of(session, event, registration_id)


async def guest(session, event, registration_id) -> dict:
    from app.api.quests import guest as guest_mod

    reg = await _registration(session, event, registration_id)
    out = await guest_mod.card(session, event, None, view_all=True, reg=reg)
    quest = await engine.quest_of(session, event.id)
    out["guest"] = {"registration_id": reg.id, "name": reg.full_name or "", "email": reg.email, "ticket_id": reg.ticket_id}
    out["audit"] = await _audit_rows(session, quest, registration_id=reg.id, limit=100)
    return out


async def _set(session, event, registration_id, task_id, user, by_name: str, note: Optional[str], action: str) -> dict:
    quest = await ensure(session, event)
    reg = await _registration(session, event, registration_id)
    t = await session.scalar(select(QuestTasks).where(QuestTasks.id == task_id, QuestTasks.quest_id == quest.id))
    if t is None:
        raise CustomHTTPException(404, message="That task could not be found.")
    by = getattr(user, "id", None)
    if action == "grant":
        await engine.grant(session, quest, t, reg.id, by)
    else:
        await engine.revoke(session, quest, t, reg.id, by)
    session.add(QuestAudit(quest_id=quest.id, registration_id=reg.id, task_id=t.id, action=action, by_user_id=by, by_name=by_name[:120], note=(note or None)))
    await session.commit()
    if quest.enabled:
        await engine.refresh_guests(session, event.id, [reg.id])
    return await guest(session, event, reg.id)


async def grant(session, event, registration_id, task_id, user, by_name: str, note: Optional[str] = None) -> dict:
    return await _set(session, event, registration_id, task_id, user, by_name, note, "grant")


async def revoke(session, event, registration_id, task_id, user, by_name: str, note: Optional[str] = None) -> dict:
    return await _set(session, event, registration_id, task_id, user, by_name, note, "revoke")


async def _audit_rows(session, quest, *, registration_id=None, limit=50, offset=0) -> list:
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.merch.models import MerchItems

    if quest is None:
        return []
    A = QuestAudit
    q = (
        select(A, R.full_name, QuestTasks.title, MerchItems.name)
        .outerjoin(R, R.id == A.registration_id)
        .outerjoin(QuestTasks, QuestTasks.id == A.task_id)
        .outerjoin(QuestRewards, QuestRewards.id == A.reward_id)
        .outerjoin(MerchItems, MerchItems.id == QuestRewards.merch_item_id)
        .where(A.quest_id == quest.id)
    )
    if registration_id is not None:
        q = q.where(A.registration_id == registration_id)
    rows = (await session.execute(q.order_by(A.created_at.desc(), A.id).limit(limit).offset(offset))).all()
    return [
        {
            "at": a.created_at,
            "by": {"id": a.by_user_id, "name": a.by_name},
            "action": a.action,
            "registration_id": a.registration_id,
            "name": name,
            "task": {"id": a.task_id, "title": title} if a.task_id else None,
            "reward": {"id": a.reward_id, "name": item} if a.reward_id else None,
            "note": a.note,
            "detail": a.detail,
        }
        for a, name, title, item in rows
    ]


async def audit(session, event, page: int = 1, page_size: int = 50) -> dict:
    quest = await ensure(session, event)
    total = await session.scalar(select(func.count()).select_from(QuestAudit).where(QuestAudit.quest_id == quest.id)) or 0
    page, page_size = max(1, page), max(1, min(page_size, 200))
    return {
        "items": await _audit_rows(session, quest, limit=page_size, offset=(page - 1) * page_size),
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


async def recompute(session, event) -> dict:
    quest = await ensure(session, event)
    if quest.enabled:
        await signals.recompute(session, event.id)
        await session.commit()
    return {"queued": bool(quest.enabled)}


# ─── The export ────────────────────────────────────────────────────────


async def export_rows(session, event) -> tuple[list[str], list[list]]:
    from app.api.checkins.models import CheckIns
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.merch.models import MerchItems, MerchVariants

    quest = await ensure(session, event)
    tasks = list(
        (
            await session.scalars(
                select(QuestTasks)
                .where(QuestTasks.quest_id == quest.id)
                .order_by(QuestTasks.archived_at.nulls_first(), QuestTasks.sort_order, QuestTasks.created_at)
            )
        ).all()
    )
    d = await engine.definition(session, quest)
    rewards = [(r, m) for m in d.milestones for r in d.rewards.get(m.id, [])]
    items = {
        i.id: i.name
        for i in (await session.scalars(select(MerchItems).where(MerchItems.id.in_([r.merch_item_id for r, _ in rewards] or [uuid.uuid4()])))).all()
    }
    headers = ["Name", "Email", "Ticket", "Checked in", "Points", "Tasks done"]
    headers += [t.title + (" (removed)" if t.archived_at else "") for t in tasks]
    headers += ["Milestones reached"]
    for r, m in rewards:
        n = items.get(r.merch_item_id, "Reward")
        headers += [f"{n}: status", f"{n}: size", f"{n}: collected at", f"{n}: desk"]
    regs = list(
        (
            await session.execute(
                select(R, QuestEntries)
                .outerjoin(QuestEntries, (QuestEntries.registration_id == R.id) & (QuestEntries.quest_id == quest.id))
                .where(R.event_id == event.id, R.is_deleted == False)
                .order_by(R.full_name, R.id)  # noqa: E712
            )
        ).all()
    )
    progress: dict = {}
    for p in (await session.scalars(select(QuestProgress).where(QuestProgress.quest_id == quest.id))).all():
        progress[(p.registration_id, p.task_id)] = p
    unlocks: dict = {}
    titles = {m.id: m.title for m in d.milestones}
    for u in (await session.scalars(select(QuestUnlocks).where(QuestUnlocks.quest_id == quest.id, QuestUnlocks.revoked_at.is_(None)))).all():
        unlocks.setdefault(u.registration_id, []).append(titles.get(u.milestone_id, ""))
    claims = {
        (c.registration_id, c.reward_id): (c, label)
        for c, label in (
            await session.execute(
                select(QuestClaims, MerchVariants.label)
                .outerjoin(MerchVariants, MerchVariants.id == QuestClaims.variant_id)
                .where(QuestClaims.quest_id == quest.id)
            )
        ).all()
    }
    from app.api.merch.models import MerchCollections

    desks = dict(
        (
            await session.execute(
                select(MerchCollections.id, MerchCollections.door_label).where(
                    MerchCollections.id.in_([c.collection_id for c, _ in claims.values() if c.collection_id] or [uuid.uuid4()])
                )
            )
        ).all()
    )
    checked = set((await session.scalars(select(CheckIns.registration_id).where(CheckIns.event_id == event.id))).all())
    rows = []
    for r, e in regs:
        if e is None and not any((r.id, rw.id) in claims for rw, _ in rewards):
            continue
        row = [r.full_name or "", r.email or "", r.ticket_id, "yes" if r.id in checked else "no", e.points if e else 0, e.tasks_done if e else 0]
        for t in tasks:
            p = progress.get((r.id, t.id))
            if p is None:
                row.append("")
            elif p.revoked_at is not None:
                row.append("revoked")
            elif p.done_at is not None:
                row.append("✓")
            else:
                row.append(f"{p.count}/{K.target(t.kind, t.config)}")
        row.append(", ".join(unlocks.get(r.id, [])))
        for rw, _ in rewards:
            c = claims.get((r.id, rw.id))
            if c is None:
                row += ["", "", "", ""]
            else:
                claim, label = c
                row += [claim.status, label or "", claim.collected_at, desks.get(claim.collection_id, "") or ""]
        rows.append(row)
    return headers, rows


async def export_csv(session, event) -> str:
    import io

    from app.core.utils import csv_safe

    headers, rows = await export_rows(session, event)
    buf = io.StringIO()
    w = csv_safe.writer(buf)
    w.writerow(headers)
    for row in rows:
        w.writerow([v.isoformat() if isinstance(v, datetime) else v for v in row])
    return buf.getvalue()


async def export_xlsx(session, event) -> str:
    from app.core.utils import spreadsheet

    headers, rows = await export_rows(session, event)
    return await spreadsheet.write_xlsx(rows, title="Quest", headers=headers)


__all__ = [
    "view",
    "patch",
    "create_task",
    "update_task",
    "delete_task",
    "order_tasks",
    "create_milestone",
    "update_milestone",
    "delete_milestone",
    "add_reward",
    "update_reward",
    "delete_reward",
    "reoffer",
    "create_station",
    "live",
    "guests",
    "guest",
    "grant",
    "revoke",
    "audit",
    "recompute",
    "export_csv",
    "export_xlsx",
    "PURPOSES",
]
