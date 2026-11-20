"""The merch tab and the merch page, for an item that is a quest reward
(CONTRACT §6, §7.3).

The merch module asks `reward_items` once per call; for an event without a
quest that is an empty set and nothing else here runs. For a reward item,
what a guest is owed comes from their claim, never from the item's own
eligibility rule, and a collect on the merch tab hands the claim over (the
same handover as the swag desk), so the stock held for claims is never
given to someone else.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select

from app.api.merch import rules
from app.api.quests.models import COLLECTED, RESERVED, SOLD_OUT, WAITLISTED, QuestClaims, QuestRewards
from app.response import CustomHTTPException


async def reward_items(session, event_id: int) -> set:
    return set((await session.scalars(select(QuestRewards.merch_item_id).where(QuestRewards.event_id == event_id))).all())


async def is_reward(session, item_id) -> bool:
    return bool(await session.scalar(select(func.count()).select_from(QuestRewards).where(QuestRewards.merch_item_id == item_id)))


def refuse_reward(action: str) -> CustomHTTPException:
    return CustomHTTPException(409, message=f"This item is a quest reward. {action}", error_code="MERCH_ITEM_IS_A_REWARD")


async def _claim(session, item_id, reg_id) -> Optional[QuestClaims]:
    return await session.scalar(select(QuestClaims).where(QuestClaims.merch_item_id == item_id, QuestClaims.registration_id == reg_id))


async def status_row(session, event, m, reg) -> dict:
    """One row of GET /gate/merch/{registration_id} for a reward item."""
    from app.api.quests import stock
    from app.core.storage import objects

    claim = await _claim(session, m.id, reg.id)
    st = stock.Stock(m, list(m.variants))
    status, label, remaining, choices = rules.NOT_ELIGIBLE, None, None, []
    if claim is not None:
        v = st.variant(claim.variant_id) if claim.variant_id is not None else None
        label = v.label if v is not None else None
        if claim.status == COLLECTED:
            status = rules.ALREADY_COLLECTED
        elif claim.status == RESERVED:
            if st.sized and v is None:
                status, choices = rules.NO_VARIANT_VALUE, st.choices()
            else:
                status, remaining = rules.COLLECTABLE, st.remaining(claim.variant_id) + 1
        elif claim.status in (WAITLISTED, SOLD_OUT):
            status, remaining = rules.OUT_OF_STOCK, 0
    return {
        "item_id": m.id, "name": m.name, "photo_url": objects.photo_url(m.photo_key), "status": status, "variant_label": label,
        "remaining": remaining, "on_file": rules.field_value(reg, event, m.variant_field) if m.variant_field else None,
        "choices": [{"label": c["label"], "remaining": c["free"]} for c in choices],
    }


async def collect(session, event, m, reg, actor, *, variant_label: Optional[str], door_label: Optional[str]) -> dict:
    """POST /gate/merch/{item_id}/collect for a reward item: the claim handed over."""
    from app.api.quests import gate

    claim = await _claim(session, m.id, reg.id)
    if claim is None:
        return {"status": rules.NOT_ELIGIBLE, "collected": False, "variant_label": None, "remaining": None}
    out = await gate.handover(session, event, claim.id, actor, variant_label=variant_label, door_label=door_label)
    row = out["claim"]
    status = {
        "HANDED_OVER": rules.COLLECTABLE, "ALREADY_COLLECTED": rules.ALREADY_COLLECTED, "NEEDS_SIZE": rules.NO_VARIANT_VALUE,
        "SIZE_GONE": rules.OUT_OF_STOCK, "NOT_RESERVED": rules.OUT_OF_STOCK,
    }[out["result"]]
    return {"status": status, "collected": out["result"] == "HANDED_OVER", "variant_label": row["variant_label"], "remaining": out["remaining"]}


async def stock_changed(session, event, item_id) -> None:
    """Units added: held for the item's waiting list, in order."""
    from app.api.quests import signals, stock

    st = await stock.lock(session, item_id)
    if st is None:
        return
    for claim in await stock.promote(session, st, event):
        await signals.held(session, claim.id)
    await session.commit()
