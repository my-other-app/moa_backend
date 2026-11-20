"""Progress, totals, milestones and claims (CONTRACT §5).

- `apply` writes what the rules counted for one task: an upsert per guest,
  and a tick once the count reaches the target. A tick is never taken back
  here (sticky, §5.3), and a revoked task is left alone.
- `refresh` recomputes a guest's totals from their ticks, then their
  milestones: an unlock reserves its rewards (stock.py), one withdrawn
  releases them. Each guest's stock work is committed on its own, so no
  item stays locked longer than that guest takes.
- `evaluate` is both: for some kinds and some guests (a hook's job), or for
  everything and everyone (the recompute). Running it twice changes nothing.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.quests import evaluators as ev
from app.api.quests import kinds as K
from app.api.quests import stock
from app.api.quests.models import (
    CHECKMARKS,
    COLLECTED,
    RELEASED,
    QuestClaims,
    QuestEntries,
    QuestMilestones,
    QuestProgress,
    QuestRewards,
    Quests,
    QuestTasks,
    QuestUnlocks,
)

logger = logging.getLogger(__name__)

BATCH = 500
ROWS_PER_STATEMENT = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Definition:
    quest: Quests
    tasks: list = field(default_factory=list)
    milestones: list = field(default_factory=list)
    rewards: dict = field(default_factory=dict)

    @property
    def required_total(self) -> int:
        return sum(1 for t in self.tasks if t.required)

    def score_of(self, tasks_done: int, points: int) -> int:
        return tasks_done if self.quest.mode == CHECKMARKS else points


async def quest_of(session, event_id: int) -> Optional[Quests]:
    return await session.scalar(select(Quests).where(Quests.event_id == event_id))


async def definition(session, quest: Quests) -> Definition:
    tasks = list((await session.scalars(
        select(QuestTasks).where(QuestTasks.quest_id == quest.id, QuestTasks.archived_at.is_(None)).order_by(QuestTasks.sort_order, QuestTasks.created_at)
    )).all())
    milestones = list((await session.scalars(
        select(QuestMilestones).where(QuestMilestones.quest_id == quest.id).order_by(QuestMilestones.threshold, QuestMilestones.sort_order, QuestMilestones.created_at)
    )).all())
    rewards: dict = {}
    for r in (await session.scalars(select(QuestRewards).where(QuestRewards.quest_id == quest.id).order_by(QuestRewards.sort_order, QuestRewards.created_at))).all():
        rewards.setdefault(r.milestone_id, []).append(r)
    return Definition(quest, tasks, milestones, rewards)


async def guests(session, event_id: int, *, reg_ids: Optional[Iterable] = None, user_ids: Optional[Iterable] = None) -> list[ev.Guest]:
    from app.api.events.models import EventRegistrationsLink as R

    q = select(R.id, R.user_id).where(R.event_id == event_id, R.is_deleted == False)  # noqa: E712
    if reg_ids is not None:
        ids = [uuid.UUID(str(r)) for r in reg_ids]
        if not ids:
            return []
        q = q.where(R.id.in_(ids))
    if user_ids is not None:
        uids = [int(u) for u in user_ids]
        if not uids:
            return []
        q = q.where(R.user_id.in_(uids))
    return [ev.Guest(r, u) for r, u in (await session.execute(q.order_by(R.id))).all()]


# ─── Ticks ──────────────────────────────────────────────────────────────


async def apply(session, quest: Quests, task: QuestTasks, counts: dict, regs: list[ev.Guest], *, source: str = "auto") -> set:
    """Write the counts; answer the guests whose task became done."""
    target = K.target(task.kind, task.config)
    P = QuestProgress
    ids = [g.id for g in regs]
    existing = {
        r: (c, d, x) for r, c, d, x in (await session.execute(
            select(P.registration_id, P.count, P.done_at, P.revoked_at).where(P.task_id == task.id, P.registration_id.in_(ids))
        )).all()
    }
    now = _now()
    rows, changed = [], set()
    for g in regs:
        n = min(int(counts.get(g.id, 0) or 0), target)
        ex = existing.get(g.id)
        if ex is not None and (ex[1] is not None or ex[2] is not None):
            continue  # done (sticky) or revoked (the organiser's word)
        if ex is None and n == 0:
            continue
        done = n >= target
        if ex is not None and not done and ex[0] == n:
            continue
        rows.append(dict(id=uuid.uuid4(), quest_id=quest.id, task_id=task.id, registration_id=g.id, count=n,
                         done_at=now if done else None, source=source if done else None, updated_at=now))
        if done:
            changed.add(g.id)
    for i in range(0, len(rows), ROWS_PER_STATEMENT):
        stmt = pg_insert(P).values(rows[i:i + ROWS_PER_STATEMENT])
        await session.execute(stmt.on_conflict_do_update(
            constraint="uq_quest_progress_task_registration",
            set_={"count": stmt.excluded.count, "done_at": stmt.excluded.done_at, "source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at},
            where=and_(P.done_at.is_(None), P.revoked_at.is_(None)),
        ))
    return changed


async def tick(session, quest: Quests, task: QuestTasks, reg_id, *, source: str) -> bool:
    """A station scan or a code: done now, unless it was already or the
    organiser revoked it. True when this made it done."""
    P = QuestProgress
    stmt = pg_insert(P).values(id=uuid.uuid4(), quest_id=quest.id, task_id=task.id, registration_id=reg_id,
                               count=K.target(task.kind, task.config), done_at=_now(), source=source, updated_at=_now())
    got = await session.scalar(stmt.on_conflict_do_update(
        constraint="uq_quest_progress_task_registration",
        set_={"count": stmt.excluded.count, "done_at": stmt.excluded.done_at, "source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at},
        where=and_(P.done_at.is_(None), P.revoked_at.is_(None)),
    ).returning(P.id))
    return got is not None


async def grant(session, quest: Quests, task: QuestTasks, reg_id, by: Optional[int]) -> None:
    P = QuestProgress
    now = _now()
    stmt = pg_insert(P).values(id=uuid.uuid4(), quest_id=quest.id, task_id=task.id, registration_id=reg_id,
                               count=K.target(task.kind, task.config), done_at=now, source="grant", granted_by=by, updated_at=now)
    await session.execute(stmt.on_conflict_do_update(
        constraint="uq_quest_progress_task_registration",
        set_={
            "count": stmt.excluded.count, "revoked_at": None, "revoked_by": None, "updated_at": now,
            # A tick already there keeps its time and how it came; a revoked one is given again now.
            "done_at": case((and_(P.done_at.isnot(None), P.revoked_at.is_(None)), P.done_at), else_=stmt.excluded.done_at),
            "source": case((and_(P.done_at.isnot(None), P.revoked_at.is_(None)), P.source), else_="grant"),
            "granted_by": case((and_(P.done_at.isnot(None), P.revoked_at.is_(None)), P.granted_by), else_=by),
        },
    ))


async def revoke(session, quest: Quests, task: QuestTasks, reg_id, by: Optional[int]) -> None:
    P = QuestProgress
    now = _now()
    stmt = pg_insert(P).values(id=uuid.uuid4(), quest_id=quest.id, task_id=task.id, registration_id=reg_id, count=0,
                               revoked_at=now, revoked_by=by, updated_at=now)
    await session.execute(stmt.on_conflict_do_update(
        constraint="uq_quest_progress_task_registration",
        set_={"revoked_at": now, "revoked_by": by, "updated_at": now},
    ))


# ─── Totals, milestones, claims ─────────────────────────────────────────


async def _totals(session, d: Definition, ids: list) -> dict:
    P, T = QuestProgress, QuestTasks
    rows = await session.execute(
        select(P.registration_id, func.count(), func.coalesce(func.sum(T.points), 0), func.count().filter(T.required == True))  # noqa: E712
        .join(T, T.id == P.task_id)
        .where(P.quest_id == d.quest.id, P.registration_id.in_(ids), P.done_at.isnot(None), P.revoked_at.is_(None), T.archived_at.is_(None))
        .group_by(P.registration_id)
    )
    return {r: (int(n), int(p), int(q)) for r, n, p, q in rows.all()}


async def _write_entries(session, d: Definition, regs: list[ev.Guest], totals: dict) -> dict:
    """Upsert every guest's totals; answer {reg: (score, required_left)}."""
    E = QuestEntries
    now = _now()
    out, rows = {}, []
    have = set((await session.scalars(select(E.registration_id).where(E.quest_id == d.quest.id, E.registration_id.in_([g.id for g in regs])))).all())
    for g in regs:
        n, p, q = totals.get(g.id, (0, 0, 0))
        left = max(0, d.required_total - q)
        score = d.score_of(n, p)
        out[g.id] = (score, left)
        if not n and g.id not in have:
            continue  # nothing done and no row yet: none needed (the recompute's other guests)
        rows.append(dict(id=uuid.uuid4(), quest_id=d.quest.id, registration_id=g.id, user_id=g.user_id, tasks_done=n, points=p,
                         required_left=left, score=score, score_at=now if score else None, created_at=now, updated_at=now))
    for i in range(0, len(rows), ROWS_PER_STATEMENT):
        stmt = pg_insert(E).values(rows[i:i + ROWS_PER_STATEMENT])
        await session.execute(stmt.on_conflict_do_update(
            constraint="uq_quest_entries_quest_registration",
            set_={
                "tasks_done": stmt.excluded.tasks_done, "points": stmt.excluded.points, "required_left": stmt.excluded.required_left,
                "score": stmt.excluded.score, "user_id": stmt.excluded.user_id, "updated_at": now,
                # When the score went up: the leaderboard's tie-break.
                "score_at": case((stmt.excluded.score > E.score, now), (stmt.excluded.score == 0, None), else_=E.score_at),
            },
        ))
    return out


async def refresh(session, d: Definition, event, regs: list[ev.Guest], *, commit: bool = True) -> list:
    """Totals and milestones for these guests. Answers the unlocks made now."""
    from app.api.events.models import EventRegistrationsLink

    if not regs:
        return []
    ids = [g.id for g in regs]
    scores = await _write_entries(session, d, regs, await _totals(session, d, ids))
    U, C = QuestUnlocks, QuestClaims
    unlocks = {(u.milestone_id, u.registration_id): u for u in (await session.scalars(select(U).where(U.quest_id == d.quest.id, U.registration_id.in_(ids)))).all()}
    claims = {(c.reward_id, c.registration_id): c for c in (await session.scalars(select(C).where(C.quest_id == d.quest.id, C.registration_id.in_(ids)))).all()}
    made: list = []
    now = _now()
    for g in regs:
        score, left = scores[g.id]
        work: list = []  # (reward, milestone, "claim" | "release")
        for m in d.milestones:
            reached = left == 0 and score >= m.threshold
            u = unlocks.get((m.id, g.id))
            if reached:
                if u is None:
                    uid = await session.scalar(
                        pg_insert(U).values(id=uuid.uuid4(), quest_id=d.quest.id, milestone_id=m.id, registration_id=g.id, unlocked_at=now)
                        .on_conflict_do_nothing(constraint="uq_quest_unlocks_milestone_registration").returning(U.id)
                    )
                    if uid is not None:
                        made.append(uid)
                elif u.revoked_at is not None:
                    u.revoked_at = None
                for r in d.rewards.get(m.id, []):
                    c = claims.get((r.id, g.id))
                    if c is None or (c.status == RELEASED):
                        work.append((r, m, "claim"))
            elif u is not None and u.revoked_at is None:
                u.revoked_at = now
                for r in d.rewards.get(m.id, []):
                    c = claims.get((r.id, g.id))
                    if c is not None and c.status not in (COLLECTED, RELEASED):
                        work.append((r, m, "release"))
        if not work:
            continue
        reg = await session.get(EventRegistrationsLink, g.id)
        await _stock_work(session, d, event, reg, work)
        if commit:
            await session.commit()
    if made:
        from app.api.quests import signals

        for uid in made:
            await signals.unlocked(session, uid)
    return made


async def _stock_work(session, d: Definition, event, reg, work: list) -> None:
    """Claim or release each reward, its item locked, items in id order."""
    from app.api.quests import signals

    for r, m, what in sorted(work, key=lambda w: str(w[0].merch_item_id)):
        st = await stock.lock(session, r.merch_item_id)
        if st is None:
            continue
        if what == "claim":
            await session.execute(
                pg_insert(QuestClaims).values(id=uuid.uuid4(), quest_id=d.quest.id, reward_id=r.id, milestone_id=m.id, registration_id=reg.id,
                                              merch_item_id=r.merch_item_id, status=RELEASED, holds=False, unlocked_at=_now())
                .on_conflict_do_nothing(constraint="uq_quest_claims_reward_registration")
            )
        claim = await session.scalar(
            select(QuestClaims).where(QuestClaims.reward_id == r.id, QuestClaims.registration_id == reg.id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if claim is None:
            continue
        if what == "claim":
            if claim.status == RELEASED:
                claim.released_at = None
                claim.unlocked_at = claim.unlocked_at or _now()
            await stock.reserve(session, st, claim, r, reg, event)
        else:
            stock.release(st, claim)
            for held in await stock.promote(session, st, event):
                await signals.held(session, held.id)
        await session.flush()


async def release_claims(session, d: Definition, event, claims: list[QuestClaims]) -> None:
    """Let go of these claims' units (a reward or a milestone removed), then serve each item's waiting list."""
    from app.api.quests import signals

    for c in sorted(claims, key=lambda c: str(c.merch_item_id)):
        st = await stock.lock(session, c.merch_item_id)
        claim = await session.scalar(select(QuestClaims).where(QuestClaims.id == c.id).with_for_update().execution_options(populate_existing=True))
        if st is None or claim is None:
            continue
        stock.release(st, claim)
        await session.flush()
        for held in await stock.promote(session, st, event):
            await signals.held(session, held.id)
        await session.flush()


# ─── Evaluate ───────────────────────────────────────────────────────────


async def evaluate(session, event_id: int, *, kinds: Optional[Iterable[str]] = None, reg_ids=None, user_ids=None, everyone: bool = False) -> int:
    """Count, tick, total and unlock. Answers how many guests' ticks changed.
    Does nothing while the quest is off."""
    from app.api.events.models import Events

    quest = await session.scalar(select(Quests).where(Quests.event_id == event_id, Quests.enabled == True))  # noqa: E712
    if quest is None:
        return 0
    event = await session.get(Events, event_id)
    if event is None or event.is_deleted:
        return 0
    d = await definition(session, quest)
    wanted = set(kinds) if kinds else None
    tasks = [t for t in d.tasks if t.kind in K.AUTOMATIC and (wanted is None or t.kind in wanted)]
    if everyone:
        regs = await guests(session, event_id)
    else:
        regs = await guests(session, event_id, reg_ids=reg_ids if reg_ids is not None else None, user_ids=user_ids)
    ctx = ev.Ctx(event=event, quest=quest, now=_now())
    total = 0
    for i in range(0, len(regs), BATCH):
        batch = regs[i:i + BATCH]
        changed: set = set()
        for t in tasks:
            changed |= await apply(session, quest, t, await ev.count(session, ctx, t, batch), batch)
        total += len(changed)
        await refresh(session, d, event, batch if everyone else [g for g in batch if g.id in changed])
        await session.commit()
    return total


async def refresh_guests(session, event_id: int, reg_ids: Iterable) -> None:
    """Totals and milestones only (after a grant, a revoke, a rule or threshold change)."""
    from app.api.events.models import Events

    quest = await quest_of(session, event_id)
    if quest is None:
        return
    event = await session.get(Events, event_id)
    d = await definition(session, quest)
    regs = await guests(session, event_id, reg_ids=list(reg_ids))
    for i in range(0, len(regs), BATCH):
        await refresh(session, d, event, regs[i:i + BATCH])
    await session.commit()
