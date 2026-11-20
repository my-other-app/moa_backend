"""The guest's side: the quest card, a code, a size, the leaderboard (CONTRACT §8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from app.api.quests import engine, stock
from app.api.quests import evaluators as ev
from app.api.quests import kinds as K
from app.api.quests.models import (
    COLLECTED,
    FIRST_COME,
    POINTS,
    RELEASED,
    RESERVED,
    STATION,
    SWAG_DESK,
    WAITLISTED,
    QuestClaims,
    QuestEntries,
    QuestProgress,
    QuestRewards,
    QuestUnlocks,
)
from app.response import CustomHTTPException

#: The card looks again at what no event reports (posters' links, the profile, the follow) at most this often.
REFRESH_EVERY = timedelta(minutes=1)
CODE_WINDOW = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def short_name(full: Optional[str]) -> str:
    """ "Asha Kumar" → "Asha K."."""
    parts = (full or "").split()
    if not parts:
        return "A guest"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0].upper()}."


async def registration_for(session, event_id: int, user) -> Optional[object]:
    from app.api.events.models import EventRegistrationsLink as R

    if user is None:
        return None
    return await session.scalar(select(R).where(R.event_id == event_id, R.user_id == user.id, R.is_deleted == False))  # noqa: E712


def _open(quest, now: datetime) -> bool:
    return bool(quest.enabled) and (quest.starts_at is None or now >= quest.starts_at) and (quest.ends_at is None or now <= quest.ends_at)


async def _links(session, event, reg) -> dict:
    """The addresses the card's buttons open, when the server knows them (§3.2)."""
    from app.api.guest_posters.service import poster_page_url
    from app.api.live.models import FeedbackSurveys
    from app.api.live.service import feedback_url
    from app.core.public_urls import event_url, site_url

    base = event_url(event.slug) if getattr(event, "slug", None) else None
    feedback = None
    if reg is not None:
        survey = await session.scalar(select(FeedbackSurveys).where(FeedbackSurveys.event_id == event.id))
        try:
            feedback = feedback_url(event, reg, survey)
        except Exception:  # noqa: BLE001 - no link rather than no card
            feedback = None
    return {
        "event": base,
        "live_room": f"{base}/live" if base else None,
        "feedback": feedback,
        "poster": poster_page_url(reg.ticket_id) if reg is not None else None,
        "club": site_url(f"/clubs/{event.club.slug}") if getattr(event, "club", None) is not None and getattr(event.club, "slug", None) else None,
        "network_scan": "myotherapp://network/scan",
    }


async def _stations(session, event_id: int) -> dict:
    from app.api.checkins.models import Doors

    rows = (
        await session.execute(
            select(Doors.id, Doors.name, Doors.purpose, Doors.active)
            .where(Doors.event_id == event_id, Doors.purpose.isnot(None))
            .order_by(Doors.sort_order, Doors.created_at)
        )
    ).all()
    return {i: {"door_id": i, "name": n, "purpose": p, "active": a} for i, n, p, a in rows}


async def _items(session, item_ids) -> dict:
    from app.api.merch.models import MerchItems, MerchVariants

    if not item_ids:
        return {}
    items = {m.id: m for m in (await session.scalars(select(MerchItems).where(MerchItems.id.in_(item_ids)))).all()}
    variants: dict = {}
    for v in (
        await session.scalars(select(MerchVariants).where(MerchVariants.item_id.in_(item_ids)).order_by(MerchVariants.created_at, MerchVariants.id))
    ).all():
        variants.setdefault(v.item_id, []).append(v)
    return {i: (m, variants.get(i, [])) for i, m in items.items()}


def task_out(t, stations: dict, me: Optional[dict]) -> dict:
    kind = K.KINDS.get(t.kind)
    st = stations.get(t.door_id) if t.door_id else None
    return {
        "id": t.id,
        "kind": t.kind,
        "title": t.title,
        "description": t.description,
        "points": t.points,
        "required": t.required,
        "starts_at": t.starts_at,
        "ends_at": t.ends_at,
        "target": K.target(t.kind, t.config),
        "action": kind.action if kind else None,
        "station": {"door_id": st["door_id"], "name": st["name"]} if st else None,
        "me": me,
    }


def _me_task(p) -> dict:
    if p is None:
        return {"count": 0, "done": False, "done_at": None, "source": None, "revoked": False}
    done = p.done_at is not None and p.revoked_at is None
    return {"count": p.count, "done": done, "done_at": p.done_at if done else None, "source": p.source if done else None, "revoked": p.revoked_at is not None}


async def _rank(session, quest, entry) -> Optional[int]:
    if entry is None or not entry.score:
        return None
    higher = (
        await session.scalar(select(func.count()).select_from(QuestEntries).where(QuestEntries.quest_id == quest.id, QuestEntries.score > entry.score)) or 0
    )
    return int(higher) + 1


async def _maybe_refresh(session, event, quest, d, reg, entry) -> None:
    """Posters' links, the profile and the follow: no event says when they change, so the card looks (≤ once a minute)."""
    if not any(t.kind in K.POLLED for t in d.tasks) or not _open(quest, _now()):
        return
    if entry is not None and entry.refreshed_at is not None and _now() - entry.refreshed_at < REFRESH_EVERY:
        return
    await engine.evaluate(session, event.id, kinds=K.POLLED, reg_ids=[reg.id])
    import uuid

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = _now()
    stmt = pg_insert(QuestEntries).values(
        id=uuid.uuid4(), quest_id=quest.id, registration_id=reg.id, user_id=reg.user_id, refreshed_at=now, created_at=now, updated_at=now
    )
    await session.execute(stmt.on_conflict_do_update(constraint="uq_quest_entries_quest_registration", set_={"refreshed_at": now}))
    await session.commit()


async def card(session, event, user, *, view_all: bool = False, reg=None) -> dict:
    """GET /events/{id}/quest. `reg` given: the organiser's view of that guest (§9.3)."""
    quest = await engine.quest_of(session, event.id)
    if quest is None or (not quest.enabled and not view_all):
        return {"enabled": False}
    d = await engine.definition(session, quest)
    if reg is None and user is not None:
        reg = await registration_for(session, event.id, user)
    entry = None
    if reg is not None:
        entry = await session.scalar(select(QuestEntries).where(QuestEntries.quest_id == quest.id, QuestEntries.registration_id == reg.id))
        if not view_all:
            await _maybe_refresh(session, event, quest, d, reg, entry)
            entry = await session.scalar(
                select(QuestEntries).where(QuestEntries.quest_id == quest.id, QuestEntries.registration_id == reg.id).execution_options(populate_existing=True)
            )
    progress, unlocks, claims = {}, {}, {}
    if reg is not None:
        progress = {
            p.task_id: p
            for p in (await session.scalars(select(QuestProgress).where(QuestProgress.registration_id == reg.id, QuestProgress.quest_id == quest.id))).all()
        }
        unlocks = {
            u.milestone_id: u
            for u in (await session.scalars(select(QuestUnlocks).where(QuestUnlocks.registration_id == reg.id, QuestUnlocks.quest_id == quest.id))).all()
        }
        claims = {
            c.reward_id: c
            for c in (await session.scalars(select(QuestClaims).where(QuestClaims.registration_id == reg.id, QuestClaims.quest_id == quest.id))).all()
        }
    stations = await _stations(session, event.id)
    items = await _items(session, {r.merch_item_id for rs in d.rewards.values() for r in rs})
    from app.core.storage import objects

    milestones = []
    for m in d.milestones:
        u = unlocks.get(m.id)
        unlocked = u is not None and u.revoked_at is None
        rewards = []
        for r in d.rewards.get(m.id, []):
            item, variants = items.get(r.merch_item_id, (None, []))
            if item is None:
                continue
            st = stock.Stock(item, variants)
            rewards.append(
                {
                    "reward_id": r.id,
                    "name": item.name,
                    "photo_url": objects.photo_url(item.photo_key),
                    "sizes": st.sized,
                    "sold_out_mode": r.sold_out_mode,
                    "sold_out": r.sold_out_mode == FIRST_COME and st.pool() <= 0,
                    "claim": await _claim_out(session, claims.get(r.id), st)
                    if unlocked or (claims.get(r.id) is not None and claims[r.id].status == COLLECTED)
                    else None,
                }
            )
        milestones.append(
            {
                "id": m.id,
                "title": m.title,
                "threshold": m.threshold,
                "unlocked": unlocked,
                "unlocked_at": u.unlocked_at if unlocked else None,
                "rewards": rewards,
            }
        )
    me = None
    if reg is not None:
        score = entry.score if entry else 0
        left = entry.required_left if entry else d.required_total
        nxt = next((m for m in d.milestones if not (unlocks.get(m.id) is not None and unlocks[m.id].revoked_at is None)), None)
        fails = entry.code_fails if entry and entry.code_window_at and _now() - entry.code_window_at < CODE_WINDOW else 0
        me = {
            "registration_id": reg.id,
            "points": entry.points if entry else 0,
            "tasks_done": entry.tasks_done if entry else 0,
            "required_left": left,
            "score": score,
            "next": {"milestone_id": nxt.id, "title": nxt.title, "threshold": nxt.threshold, "left": max(0, nxt.threshold - score)} if nxt else None,
            "rank": await _rank(session, quest, entry) if quest.leaderboard else None,
            "code_attempts_left": max(0, quest.code_attempts_per_hour - fails),
        }
    desks = [{"door_id": s["door_id"], "name": s["name"]} for s in stations.values() if s["purpose"] == SWAG_DESK and s["active"]]
    out = {
        "enabled": bool(quest.enabled),
        "id": quest.id,
        "title": quest.title,
        "intro": quest.intro,
        "mode": quest.mode,
        "starts_at": quest.starts_at,
        "ends_at": quest.ends_at,
        "open": _open(quest, _now()),
        "leaderboard": bool(quest.leaderboard),
        "tasks": [task_out(t, stations, _me_task(progress.get(t.id)) if reg is not None else None) for t in d.tasks],
        "milestones": milestones,
        "me": me,
        "collect_at": desks,
        "links": await _links(session, event, reg),
    }
    if view_all:
        # The organiser's view: the code of a code task, and tasks no longer on the quest that this guest had.
        codes = {t.id: (t.config or {}).get("code") for t in d.tasks if t.kind == K.CODE}
        for t in out["tasks"]:
            if t["id"] in codes:
                t["code"] = codes[t["id"]]
    return out


async def _claim_out(session, claim, st: stock.Stock) -> Optional[dict]:
    if claim is None or claim.status == RELEASED:
        return None
    v = st.variant(claim.variant_id) if claim.variant_id is not None else None
    position = None
    if claim.status == WAITLISTED:
        position = (
            int(
                await session.scalar(
                    select(func.count())
                    .select_from(QuestClaims)
                    .where(
                        QuestClaims.merch_item_id == claim.merch_item_id,
                        QuestClaims.status == WAITLISTED,
                        (QuestClaims.unlocked_at < claim.unlocked_at) | ((QuestClaims.unlocked_at == claim.unlocked_at) & (QuestClaims.id < claim.id)),
                    )
                )
                or 0
            )
            + 1
        )
    return {
        "claim_id": claim.id,
        "status": claim.status,
        "needs_size": claim.status == RESERVED and st.sized and v is None,
        "variant_label": v.label if v is not None else None,
        "choices": st.choices() if claim.status == RESERVED and st.sized else [],
        "waitlist_position": position,
        "collected_at": claim.collected_at,
    }


# ─── A code ───────────────────────────────────────────────────────────


async def enter_code(session, event, user, code: str) -> dict:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    import uuid

    from app.api.quests.models import QuestTasks

    quest = await engine.quest_of(session, event.id)
    now = _now()
    if quest is None or not _open(quest, now):
        raise CustomHTTPException(409, message="The quest isn't open right now.", error_code="QUEST_CLOSED")
    reg = await registration_for(session, event.id, user)
    if reg is None:
        raise CustomHTTPException(403, message="Register for the event to take part in its quest.", error_code="QUEST_NOT_REGISTERED")
    E = QuestEntries
    await session.execute(
        pg_insert(E)
        .values(id=uuid.uuid4(), quest_id=quest.id, registration_id=reg.id, user_id=reg.user_id, created_at=now, updated_at=now)
        .on_conflict_do_nothing(constraint="uq_quest_entries_quest_registration")
    )
    entry = await session.scalar(
        select(E).where(E.quest_id == quest.id, E.registration_id == reg.id).with_for_update().execution_options(populate_existing=True)
    )
    if entry.code_window_at is None or now - entry.code_window_at >= CODE_WINDOW:
        entry.code_fails, entry.code_window_at = 0, None
    limit = quest.code_attempts_per_hour
    if entry.code_fails >= limit:
        retry = int((entry.code_window_at + CODE_WINDOW - now).total_seconds()) + 1
        await session.commit()
        raise CustomHTTPException(429, message="Too many wrong codes. Try again later.", error_code="QUEST_CODE_LOCKED", errors={"retry_after": retry})
    normal = K.normal_code(code)
    task = (
        await session.scalar(
            select(QuestTasks).where(QuestTasks.quest_id == quest.id, QuestTasks.kind == K.CODE, QuestTasks.code == normal, QuestTasks.archived_at.is_(None))
        )
        if normal
        else None
    )
    if task is None:
        entry.code_fails += 1
        entry.code_window_at = entry.code_window_at or now
        left = max(0, limit - entry.code_fails)
        await session.commit()
        raise CustomHTTPException(404, message="That isn't one of the quest's codes.", error_code="QUEST_CODE_WRONG", errors={"left": left})
    ctx = ev.Ctx(event=event, quest=quest, now=now)
    if not ev.open_now(ctx, task):
        start, end = ev.window(ctx, task)
        await session.commit()
        raise CustomHTTPException(
            409,
            message="That code isn't open right now.",
            error_code="QUEST_CODE_NOT_OPEN",
            errors={"opens_at": start.isoformat() if start else None, "closes_at": end.isoformat() if end else None},
        )
    existing = await session.scalar(select(QuestProgress).where(QuestProgress.task_id == task.id, QuestProgress.registration_id == reg.id))
    if existing is not None and existing.revoked_at is not None:
        await session.commit()
        raise CustomHTTPException(409, message="The organiser took this one back.", error_code="QUEST_TASK_REVOKED")
    ticked = await engine.tick(session, quest, task, reg.id, source="code")
    if ticked:
        d = await engine.definition(session, quest)
        await engine.refresh(session, d, event, [ev.Guest(reg.id, reg.user_id)], commit=False)
    await session.commit()
    out = await card(session, event, user)
    return {"result": "DONE" if ticked else "ALREADY_DONE", "task": next((t for t in out["tasks"] if t["id"] == task.id), None), "card": out}


# ─── A size ───────────────────────────────────────────────────────────


async def choose_size(session, event, user, claim_id, label: str) -> dict:
    reg = await registration_for(session, event.id, user)
    if reg is None:
        raise CustomHTTPException(403, message="Register for the event to take part in its quest.", error_code="QUEST_NOT_REGISTERED")
    first = await session.scalar(
        select(QuestClaims)
        .join(QuestRewards, QuestRewards.id == QuestClaims.reward_id)
        .where(QuestClaims.id == claim_id, QuestClaims.registration_id == reg.id, QuestRewards.event_id == event.id)
    )
    if first is None:
        raise CustomHTTPException(404, message="That reward could not be found.")
    st = await stock.lock(session, first.merch_item_id)
    claim = await session.scalar(select(QuestClaims).where(QuestClaims.id == claim_id).with_for_update().execution_options(populate_existing=True))
    if claim.status != RESERVED or not st.sized:
        await session.commit()
        raise CustomHTTPException(409, message="There is no size to choose for this reward now.", error_code="QUEST_CLAIM_NOT_RESERVED")
    try:
        stock.choose_size(st, claim, label, reg, event)
    except LookupError:
        await session.commit()
        raise CustomHTTPException(422, message="That is not one of the sizes.", errors={"choices": st.choices()})
    except stock.SizeGone as gone:
        await session.commit()
        raise CustomHTTPException(409, message="That size is gone.", error_code="QUEST_SIZE_GONE", errors={"choices": gone.choices})
    await session.commit()
    return await _claim_out(session, claim, st)


# ─── The leaderboard ──────────────────────────────────────────────────


async def leaderboard(session, quest, *, limit: int = 20, full_names: Optional[bool] = None, reg=None) -> dict:
    from app.api.events.models import EventRegistrationsLink as R

    full = quest.leaderboard_names == "full" if full_names is None else full_names
    rows = (
        await session.execute(
            select(QuestEntries.registration_id, QuestEntries.score, R.full_name)
            .join(R, R.id == QuestEntries.registration_id)
            .where(QuestEntries.quest_id == quest.id, QuestEntries.score > 0, R.is_deleted == False)  # noqa: E712
            .order_by(QuestEntries.score.desc(), QuestEntries.score_at.asc().nulls_last(), QuestEntries.registration_id)
            .limit(max(1, min(limit, 100)))
        )
    ).all()
    out, rank, last, seen = [], 0, None, 0
    for reg_id, score, name in rows:
        seen += 1
        if score != last:
            rank, last = seen, score
        out.append({"rank": rank, "name": (name or "A guest") if full else short_name(name), "score": int(score), "me": reg is not None and reg_id == reg.id})
    me = None
    if reg is not None:
        entry = await session.scalar(select(QuestEntries).where(QuestEntries.quest_id == quest.id, QuestEntries.registration_id == reg.id))
        if entry is not None and entry.score:
            me = {"rank": await _rank(session, quest, entry), "score": entry.score}
    return {"mode": quest.mode, "metric": "points" if quest.mode == POINTS else "tasks", "rows": out, "me": me}


__all__ = ["card", "enter_code", "choose_size", "leaderboard", "short_name", "registration_for", "STATION"]
