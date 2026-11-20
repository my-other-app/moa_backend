"""Reward stock: holding a unit for a claim, letting it go, the waiting list,
sizes, and the handover (CONTRACT §6).

Every function here starts by locking the item's row (`SELECT ... FOR
UPDATE` on merch_items) and reading its sizes afresh, so two guests reaching
a milestone at once for the last T-shirt take turns: the second finds it
gone. Callers lock items in the order of their ids (engine.py), so two
transactions never wait on each other in a circle.

What is held lives in the item's own counters, beside what the merch page
already counts: `reserved_count` on the size held, or on the item for a
claim whose size isn't chosen (and for an item without sizes).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.merch import rules
from app.api.merch.models import MerchCollections, MerchItems, MerchVariants
from app.api.quests.models import COLLECTED, FIRST_COME, RELEASED, RESERVED, SOLD_OUT, WAITLISTED, QuestClaims

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Stock:
    """An item locked for this transaction, with its sizes as they are now."""

    def __init__(self, item: MerchItems, variants: list[MerchVariants]):
        self.item = item
        self.variants = variants

    @property
    def sized(self) -> bool:
        return bool(self.item.variant_field)

    def variant(self, variant_id) -> Optional[MerchVariants]:
        return next((v for v in self.variants if v.id == variant_id), None)

    def by_label(self, label: Optional[str]) -> Optional[MerchVariants]:
        if not label:
            return None
        return next((v for v in self.variants if rules.norm(v.label) == rules.norm(label)), None)

    @staticmethod
    def size_free(v: MerchVariants) -> int:
        return v.stock - v.collected_count - (v.reserved_count or 0)

    def pool(self) -> int:
        m = self.item
        if not self.sized:
            return (m.stock or 0) - m.collected_count - (m.manual_handed_out or 0) - (m.reserved_count or 0)
        return (
            sum(v.stock for v in self.variants)
            - m.collected_count
            - (m.manual_handed_out or 0)
            - sum(v.reserved_count or 0 for v in self.variants)
            - (m.reserved_count or 0)
        )

    def unsized_room(self) -> bool:
        """Room for one more hold whose size isn't chosen: the pool, and the sizes, have one."""
        if not self.sized:
            return self.pool() > 0
        return self.pool() > 0 and sum(max(0, self.size_free(v)) for v in self.variants) - (self.item.reserved_count or 0) > 0

    def choices(self) -> list[dict]:
        return [{"label": v.label, "free": max(0, self.size_free(v))} for v in self.variants if self.size_free(v) > 0]

    def remaining(self, variant_id=None) -> int:
        if self.sized and variant_id is not None:
            v = self.variant(variant_id)
            return max(0, self.size_free(v)) if v is not None else 0
        return max(0, self.pool())


async def lock(session, item_id) -> Optional[Stock]:
    item = await session.scalar(select(MerchItems).where(MerchItems.id == item_id).with_for_update().execution_options(populate_existing=True))
    if item is None:
        return None
    variants = list(
        (
            await session.scalars(
                select(MerchVariants)
                .where(MerchVariants.item_id == item_id)
                .order_by(MerchVariants.created_at, MerchVariants.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    return Stock(item, variants)


def _hold(st: Stock, claim: QuestClaims, size: Optional[str], *, unsized_ok: bool = True) -> bool:
    """Hold one unit for `claim` (which holds none): its size when it has
    one with a unit free, else (when `unsized_ok`) one whose size is chosen
    later. False when there is nothing to hold."""
    if not st.sized:
        if st.pool() <= 0:
            return False
        st.item.reserved_count = (st.item.reserved_count or 0) + 1
        claim.variant_id = None
    else:
        v = st.by_label(size)
        if v is not None and st.size_free(v) > 0 and st.pool() > 0:
            v.reserved_count = (v.reserved_count or 0) + 1
            claim.variant_id = v.id
        elif (unsized_ok or v is None) and st.unsized_room():
            st.item.reserved_count = (st.item.reserved_count or 0) + 1
            claim.variant_id = None
        else:
            return False
    claim.holds = True
    claim.status = RESERVED
    claim.reserved_at = _now()
    return True


def _unhold(st: Stock, claim: QuestClaims) -> None:
    if not claim.holds:
        return
    v = st.variant(claim.variant_id) if claim.variant_id is not None else None
    if v is not None:
        v.reserved_count = max(0, (v.reserved_count or 0) - 1)
    else:
        st.item.reserved_count = max(0, (st.item.reserved_count or 0) - 1)
    claim.holds = False


def size_on_file(st: Stock, reg, event) -> Optional[str]:
    if not st.sized:
        return None
    return rules.field_value(reg, event, st.item.variant_field)


async def _collected_already(session, reg_id, item_id) -> Optional[MerchCollections]:
    return await session.scalar(select(MerchCollections).where(MerchCollections.registration_id == reg_id, MerchCollections.item_id == item_id))


async def reserve(session, st: Stock, claim: QuestClaims, reward, reg, event) -> str:
    """At unlock (or re-unlock): the claim's status, with its unit held when there is one (§6.1)."""
    if claim.status == COLLECTED:
        return COLLECTED
    done = await _collected_already(session, reg.id, st.item.id)
    if done is not None:
        _unhold(st, claim)
        claim.status, claim.collected_at, claim.collection_id, claim.variant_id = COLLECTED, done.collected_at, done.id, done.variant_id
        return COLLECTED
    if claim.holds:
        claim.status = RESERVED
        return RESERVED
    if _hold(st, claim, size_on_file(st, reg, event)):
        return RESERVED
    if reward.sold_out_mode == FIRST_COME:
        claim.status = SOLD_OUT
    else:
        claim.status = WAITLISTED
        claim.waitlisted_at = claim.waitlisted_at or _now()
    return claim.status


def release(st: Stock, claim: QuestClaims) -> None:
    """The milestone withdrawn or the reward removed: the unit goes back (a collected claim stays)."""
    if claim.status == COLLECTED:
        return
    _unhold(st, claim)
    claim.status = RELEASED
    claim.released_at = _now()


async def promote(session, st: Stock, event) -> list[QuestClaims]:
    """Units free: held for the item's waiting list, in the order they
    unlocked. A guest with a size waits for that size; one without is held
    an unsized unit. Answers the claims now held (to tell them)."""
    from app.api.events.models import EventRegistrationsLink

    held: list[QuestClaims] = []
    if st.pool() <= 0:
        return held
    waiting = list(
        (
            await session.scalars(
                select(QuestClaims)
                .where(QuestClaims.merch_item_id == st.item.id, QuestClaims.status == WAITLISTED)
                .order_by(QuestClaims.unlocked_at, QuestClaims.id)
                .with_for_update()
            )
        ).all()
    )
    for claim in waiting:
        if st.pool() <= 0:
            break
        reg = await session.get(EventRegistrationsLink, claim.registration_id)
        if reg is None or reg.is_deleted:
            continue
        size = size_on_file(st, reg, event)
        if _hold(st, claim, size, unsized_ok=size is None):
            held.append(claim)
    return held


async def reoffer(session, st: Stock, event, reward_id) -> list[QuestClaims]:
    """The organiser's "re-offer": free units held for a reward's sold-out claims, in unlock order."""
    from app.api.events.models import EventRegistrationsLink

    held: list[QuestClaims] = []
    rows = list(
        (
            await session.scalars(
                select(QuestClaims)
                .where(QuestClaims.reward_id == reward_id, QuestClaims.status == SOLD_OUT)
                .order_by(QuestClaims.unlocked_at, QuestClaims.id)
                .with_for_update()
            )
        ).all()
    )
    for claim in rows:
        if st.pool() <= 0:
            break
        reg = await session.get(EventRegistrationsLink, claim.registration_id)
        if reg is None or reg.is_deleted:
            continue
        if _hold(st, claim, size_on_file(st, reg, event)):
            held.append(claim)
    return held


class SizeGone(Exception):
    def __init__(self, choices: list[dict]):
        self.choices = choices


def _write_size(reg, event, st: Stock, label: str) -> None:
    """The size chosen goes onto the guest's record when it has no usable one
    (the merch handover's rule: what is handed over is resolved from it)."""
    current = rules.field_value(reg, event, st.item.variant_field)
    if current and st.by_label(current) is not None and rules.norm(current) == rules.norm(label):
        return
    key = rules.field_key(event, st.item.variant_field)
    details = dict(reg.additional_details) if isinstance(reg.additional_details, dict) else {}
    details = {k: v for k, v in details.items() if rules.norm(k) != rules.norm(key)}
    details[key] = label
    reg.additional_details = details


def choose_size(st: Stock, claim: QuestClaims, label: str, reg, event) -> None:
    """Move the claim's hold to `label`. Raises LookupError for a size that
    isn't one of the item's, SizeGone when that size has no unit free."""
    v = st.by_label(label)
    if v is None:
        raise LookupError(label)
    if claim.variant_id == v.id:
        return
    if st.size_free(v) <= 0:
        raise SizeGone(st.choices())
    _unhold(st, claim)
    v.reserved_count = (v.reserved_count or 0) + 1
    claim.variant_id = v.id
    claim.holds = True
    _write_size(reg, event, st, v.label)


async def hand_over(
    session, st: Stock, claim: QuestClaims, reg, event, *, actor_user_id: Optional[int], door=None, door_label: Optional[str] = None
) -> Optional[MerchCollections]:
    """The reserved unit becomes a handover: the merch log's own row (item,
    size, door, who), the held unit counted as collected. None when the
    guest had already been handed one of this item another way (the claim
    is then marked collected with that handover)."""
    collection = MerchCollections(
        registration_id=reg.id,
        item_id=st.item.id,
        variant_id=claim.variant_id,
        door_label=door.name if door is not None else (door_label or None),
        door_id=door.id if door is not None else None,
        collected_by=actor_user_id,
    )
    try:
        async with session.begin_nested():
            session.add(collection)
            await session.flush()
    except IntegrityError:
        done = await _collected_already(session, reg.id, st.item.id)
        _unhold(st, claim)
        claim.status = COLLECTED
        if done is not None:
            claim.collected_at, claim.collection_id = done.collected_at, done.id
        return None
    v = st.variant(claim.variant_id) if claim.variant_id is not None else None
    _unhold(st, claim)
    if v is not None:
        v.collected_count += 1
    st.item.collected_count += 1
    claim.status = COLLECTED
    claim.collected_at = collection.collected_at or _now()
    claim.collection_id = collection.id
    return collection
