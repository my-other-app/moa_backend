"""Telling a guest: a milestone unlocked, a reward held for them (CONTRACT §10).

An inbox line, a push to their devices, and an email to the registration's
address. Each unlock (and each hold from the waiting list) is told once: the
row's `notified_at` / `held_notified_at` is set by the first run, so the
queue's second delivery of a job sends nothing.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core import jobs

logger = logging.getLogger(__name__)

UNLOCK_TYPE = "quest_unlocked"
HELD_TYPE = "quest_reward_held"
WALKUP_DOMAIN = "walkup.myotherapp.in"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _join(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


async def _desks(session, event_id: int) -> list[str]:
    from app.api.checkins.models import Doors
    from app.api.quests.models import SWAG_DESK

    return list((await session.scalars(
        select(Doors.name).where(Doors.event_id == event_id, Doors.purpose == SWAG_DESK, Doors.active == True).order_by(Doors.sort_order, Doors.created_at)  # noqa: E712
    )).all())


async def _org(session, event) -> str:
    from app.api.clubs.models import Clubs

    if not event.club_id:
        return ""
    return await session.scalar(select(Clubs.name).where(Clubs.id == event.club_id)) or ""


async def _tell(session, *, user_id: int, event, title: str, body: str, kind: str, data: dict) -> None:
    from app.api.notifications import service as notes
    from app.core.notifications import service as push

    line = await notes.create_notification(session=session, user_id=user_id, title=title, description=body, type=kind,
                                           event_id=event.id, from_club_id=event.club_id, data=data)
    push_data = {"type": kind, "event_id": str(event.id), "open": "quest"}
    if getattr(line, "id", None) is not None:
        push_data["notification_id"] = str(line.id)
    await session.commit()
    try:
        await push.send_notification_to_users(session=session, user_ids=[user_id], title=title, body=body, data=push_data)
    except Exception:  # noqa: BLE001 - the inbox line is there; a push that fails is not retried
        logger.exception("quest push failed for user %s", user_id)


def _email(to: str, subject: str, text: str, key: str, org: str) -> None:
    from app.core.email import email as email_mod

    body = "".join(f"<p>{html_mod.escape(p)}</p>" for p in text.split("\n\n") if p.strip())
    email_mod.send_email(recipients=[to], subject=subject, text=text, html=body, sender_name=org or None, idempotency_key=key)


async def unlock(unlock_id: str) -> None:
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.merch.models import MerchItems
    from app.api.quests.models import QuestClaims, QuestMilestones, QuestUnlocks, RESERVED, SOLD_OUT, WAITLISTED

    async with jobs.scope() as session:
        uid = uuid.UUID(str(unlock_id))
        # Claimed by the first run: a second one finds it told.
        claimed = await session.scalar(
            update(QuestUnlocks).where(QuestUnlocks.id == uid, QuestUnlocks.notified_at.is_(None), QuestUnlocks.revoked_at.is_(None))
            .values(notified_at=_now()).returning(QuestUnlocks.registration_id)
        )
        if claimed is None:
            return
        u = await session.get(QuestUnlocks, uid)
        reg = await session.get(EventRegistrationsLink, u.registration_id)
        milestone = await session.get(QuestMilestones, u.milestone_id)
        event = await session.get(Events, reg.event_id) if reg is not None else None
        if reg is None or event is None or milestone is None:
            await session.commit()
            return
        rows = (await session.execute(
            select(QuestClaims.status, MerchItems.name).join(MerchItems, MerchItems.id == QuestClaims.merch_item_id)
            .where(QuestClaims.milestone_id == milestone.id, QuestClaims.registration_id == reg.id)
        )).all()
        desks = await _desks(session, event.id)
        where = f"collect it at {_join(desks)}" if desks else "collect it at the event"
        held = [n for s, n in rows if s == RESERVED]
        waiting = [n for s, n in rows if s == WAITLISTED]
        gone = [n for s, n in rows if s == SOLD_OUT]
        parts = []
        if held:
            parts.append(f"{_join(held)}: {where}.")
        if waiting:
            parts.append(f"{_join(waiting)}: you're on the waiting list; we'll tell you when one is held for you.")
        if gone:
            parts.append(f"{_join(gone)}: all gone, sorry.")
        title = f"🎁 You unlocked {milestone.title}"
        body = " ".join(parts) or f"You reached {milestone.title} at {event.name}."
        await _tell(session, user_id=reg.user_id, event=event, title=title, body=body, kind=UNLOCK_TYPE,
                    data={"event_name": event.name, "milestone_id": str(milestone.id), "open": "quest"})
        if reg.email and not reg.email.endswith(WALKUP_DOMAIN):
            text = f"Hi {reg.full_name or 'there'},\n\nYou unlocked {milestone.title} at {event.name}.\n\n{body}\n\nOpen your ticket to see your quest."
            subject = f"You've unlocked {_join(held)} — {where}" if held else f"You unlocked {milestone.title} at {event.name}"
            org = await _org(session, event)
            try:
                await asyncio.to_thread(_email, reg.email, subject[:150], text, f"quest-unlock-{uid}", org)
            except Exception:  # noqa: BLE001
                logger.exception("quest unlock email failed for %s", uid)


async def held(claim_id: str) -> None:
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.merch.models import MerchItems
    from app.api.quests.models import RESERVED, QuestClaims

    async with jobs.scope() as session:
        cid = uuid.UUID(str(claim_id))
        claimed = await session.scalar(
            update(QuestClaims).where(QuestClaims.id == cid, QuestClaims.held_notified_at.is_(None), QuestClaims.status == RESERVED)
            .values(held_notified_at=_now()).returning(QuestClaims.registration_id)
        )
        if claimed is None:
            return
        claim = await session.get(QuestClaims, cid)
        reg = await session.get(EventRegistrationsLink, claim.registration_id)
        item = await session.get(MerchItems, claim.merch_item_id)
        event = await session.get(Events, reg.event_id) if reg is not None else None
        if reg is None or item is None or event is None:
            await session.commit()
            return
        desks = await _desks(session, event.id)
        where = _join(desks) or "the swag desk"
        title = f"🎁 Your {item.name} is waiting"
        body = f"One is held for you at {event.name}: collect it at {where}."
        await _tell(session, user_id=reg.user_id, event=event, title=title, body=body, kind=HELD_TYPE,
                    data={"event_name": event.name, "claim_id": str(cid), "open": "quest"})
        if reg.email and not reg.email.endswith(WALKUP_DOMAIN):
            org = await _org(session, event)
            text = f"Hi {reg.full_name or 'there'},\n\n{body}"
            try:
                await asyncio.to_thread(_email, reg.email, title[:150], text, f"quest-held-{cid}", org)
            except Exception:  # noqa: BLE001
                logger.exception("quest held email failed for %s", cid)
