"""Which clubs may use the Integrations API: the platform admin's decision.

Nothing is on by default. A club is allocated when its row says so; with no
row, the plan's `api_access` decides (off on every plan for now, so it can
later come with a plan). The admin's row always wins, on or off.

The rate tier sets the limits a club's keys and its connected apps run
under: standard is 600 reads and 120 writes a minute per client; high is
five times that.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from app.api.partner.models import RATE_TIERS, PartnerAllocations

logger = logging.getLogger(__name__)

DEFAULT_MAX_KEYS = 5
#: (reads, writes) a minute per client.
LIMITS = {"standard": (600, 120), "high": (3000, 600)}


@dataclass(frozen=True)
class Allocation:
    allocated: bool
    #: "admin" (a row), "plan" (api_access), or None (neither).
    source: Optional[str]
    rate_tier: str = "standard"
    max_keys: int = DEFAULT_MAX_KEYS
    notes: Optional[str] = None
    row: Optional[PartnerAllocations] = None

    @property
    def rate_limit(self) -> dict:
        reads, writes = LIMITS.get(self.rate_tier, LIMITS["standard"])
        return {"read_per_min": reads, "write_per_min": writes}


def limits_for(tier: Optional[str]) -> dict:
    reads, writes = LIMITS.get(tier or "standard", LIMITS["standard"])
    return {"read_per_min": reads, "write_per_min": writes}


async def _plan_allows(session, club_id: int) -> bool:
    """The plan's own api_access flag, read straight from the entitlements:
    never the plan gates' `allows`, which lets everything through while plan
    enforcement is off."""
    try:
        from app.api.subscriptions import gates

        ent = await gates.entitlements(session, club_id)
        # A platform with no plans at all resolves to "everything on" (the
        # subscriptions' unlimited fallback): never API access on its strength.
        return bool(ent.configured and ent.plan is not None and ent.allows("api_access"))
    except Exception:  # noqa: BLE001 - no plan to read is no access
        logger.warning("could not read club %s's plan for api_access", club_id, exc_info=True)
        return False


async def of(session, club_id: int) -> Allocation:
    row = await session.get(PartnerAllocations, club_id)
    if row is not None:
        return Allocation(
            allocated=bool(row.enabled), source="admin", rate_tier=row.rate_tier or "standard",
            max_keys=int(row.max_keys if row.max_keys is not None else DEFAULT_MAX_KEYS), notes=row.notes, row=row,
        )
    if await _plan_allows(session, club_id):
        return Allocation(allocated=True, source="plan")
    return Allocation(allocated=False, source=None)


async def rows(session, club_ids: list[int]) -> dict[int, PartnerAllocations]:
    if not club_ids:
        return {}
    found = await session.scalars(select(PartnerAllocations).where(PartnerAllocations.club_id.in_(club_ids)))
    return {r.club_id: r for r in found}


async def put(
    session, club_id: int, *, enabled: bool, rate_tier: str = "standard", max_keys: int = DEFAULT_MAX_KEYS,
    notes: Optional[str] = None, by_user_id: Optional[int] = None, source: str = "admin",
) -> PartnerAllocations:
    """Set a club's allocation (flushed, not committed)."""
    if rate_tier not in RATE_TIERS:
        raise ValueError(f"rate_tier must be one of {', '.join(RATE_TIERS)}")
    row = await session.get(PartnerAllocations, club_id)
    if row is None:
        row = PartnerAllocations(club_id=club_id)
        session.add(row)
    row.enabled = bool(enabled)
    row.rate_tier = rate_tier
    row.max_keys = int(max_keys)
    row.notes = (notes or "").strip()[:1000] or None
    row.source = source
    row.updated_by_id = by_user_id
    await session.flush()
    return row
