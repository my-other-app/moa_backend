"""Hero Cards: reveal and collect (SPEC §4). The server decides; a phone never does.

Reveal (one transaction): the event's four tier-counter rows are locked
(`SELECT ... FOR UPDATE`), so 300 reveals at once are taken one after another
and no tier can overshoot. With R reveals so far, c_t in tier t and share s_t,

    deficit_t = s_t * (R + 1) - c_t

the candidates are the tiers whose deficit is above 0 and within 0.5 of the
largest; one is picked at random weighted by its deficit, and inside it the
hero fewest players hold (ties at random). A tier picked has c_t < s_t (R+1),
so after the reveal it is over its target by less than one card.

The Legendary floor F: with L Legendary reveals and P players checked in and
not yet revealed (this one included), when F - L >= P the reveal is forced
Legendary.
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

from app.api.checkins.models import CheckIns
from app.api.events.models import EventRegistrationsLink
from app.api.games import boards, common, tokens
from app.api.games.common import err
from app.api.games.models import (
    CARDS,
    PRESET_SHARES,
    TIERS,
    GameCardConfig,
    GameCardScores,
    GameDeckItems,
    GameHeroes,
    GameMeetings,
    GamePlayers,
    GameReveals,
    GameTierCounters,
)

#: Collect limits (SPEC §4): one scan in this long, and this many in an hour.
COLLECT_GAP = 5.0
COLLECT_PER_HOUR = 60
TIER_LABEL = {"common": "Common", "uncommon": "Uncommon", "rare": "Rare", "legendary": "Legendary"}

DEFAULT_HEROES = [
    ("common", "Ember Scout", "Quick on the trail, first to the spark.", "ember"),
    ("common", "Tide Runner", "Rides every wave to the finish.", "tide"),
    ("common", "Moss Guard", "Quiet, steady, never moved.", "moss"),
    ("common", "Dune Rider", "Faster than the desert wind.", "dune"),
    ("uncommon", "Storm Weaver", "Spins lightning into plans.", "storm"),
    ("uncommon", "Iron Sage", "Every move planned ten ahead.", "iron"),
    ("uncommon", "Frost Lynx", "Cool head, sharp claws.", "frost"),
    ("rare", "Solar Warden", "Keeper of the daylight.", "solar"),
    ("rare", "Void Dancer", "Steps between the shadows.", "void"),
    ("legendary", "Aurora Titan", "One of a kind. Few will ever meet.", "aurora"),
]


# ─── Config and heroes ─────────────────────────────────────────────────


def shares_of(config: GameCardConfig) -> dict:
    return {
        "common": float(config.share_common),
        "uncommon": float(config.share_uncommon),
        "rare": float(config.share_rare),
        "legendary": float(config.share_legendary),
    }


def config_out(config: GameCardConfig) -> dict:
    return {
        "preset": config.preset,
        "shares": shares_of(config),
        "legendary_floor": config.legendary_floor,
        "pin_required": config.pin_required,
        "collect_on": config.collect_on,
        "two_way": config.two_way,
        "show_holders": config.show_holders,
    }


async def config_of(session, event_id: int) -> GameCardConfig:
    config = await session.scalar(select(GameCardConfig).where(GameCardConfig.event_id == event_id))
    if config is None:
        raise err(404, "GAME_NOT_ON", "This game is not set up for this event.")
    return config


async def heroes_of(session, event_id: int) -> list[GameHeroes]:
    return list((await session.scalars(select(GameHeroes).where(GameHeroes.event_id == event_id).order_by(GameHeroes.number))).all())


async def ensure_cards(session, event_id: int) -> GameCardConfig:
    """The event's card settings, tier counters and the ten default heroes, made once."""
    from app.api.games.models import GameSettings

    # The settings row is the lock two organisers (or two requests) take turns on.
    await common.ensure_settings(session, event_id)
    await session.execute(select(GameSettings.id).where(GameSettings.event_id == event_id).with_for_update())
    config = await session.scalar(select(GameCardConfig).where(GameCardConfig.event_id == event_id))
    if config is None:
        config = GameCardConfig(id=uuid.uuid4(), event_id=event_id, preset="medium")
        session.add(config)
    for tier in TIERS:
        await session.execute(pg_insert(GameTierCounters).values(id=uuid.uuid4(), event_id=event_id, tier=tier).on_conflict_do_nothing(constraint="uq_game_tier_counters"))
    if not await heroes_of(session, event_id):
        for i, (tier, name, tagline, emblem) in enumerate(DEFAULT_HEROES, start=1):
            session.add(GameHeroes(id=uuid.uuid4(), event_id=event_id, number=i, name=name, tagline=tagline, tier=tier, emblem_key=emblem))
    await session.flush()
    return config


def apply_preset(config: GameCardConfig, preset: str) -> None:
    shares = PRESET_SHARES[preset]
    config.preset = preset
    config.share_common, config.share_uncommon = Decimal(shares["common"]), Decimal(shares["uncommon"])
    config.share_rare, config.share_legendary = Decimal(shares["rare"]), Decimal(shares["legendary"])


async def hero_counts(session, event_id: int) -> dict:
    """Each hero's number of holders (own or collected), one pass."""
    rows = await session.execute(select(GameDeckItems.hero_id, func.count()).where(GameDeckItems.event_id == event_id).group_by(GameDeckItems.hero_id))
    return {h: int(n) for h, n in rows.all()}


def hero_out(h: GameHeroes, *, holders: Optional[int] = None, reveals: Optional[int] = None) -> dict:
    art = h.art if isinstance(h.art, dict) else None
    out = {
        "id": h.id,
        "number": h.number,
        "name": h.name,
        "tagline": h.tagline,
        "tier": h.tier,
        "art": art,
        "art_url": (art or {}).get("medium") or (art or {}).get("large") or (art or {}).get("original"),
        "emblem_key": h.emblem_key,
    }
    if holders is not None:
        out["holders"] = holders
    if reveals is not None:
        out["reveals"] = reveals
    return out


def locked_out(h: GameHeroes) -> dict:
    """A hero the player does not hold: its place and tier, not its face."""
    return {"id": h.id, "number": h.number, "tier": h.tier, "locked": True}


# ─── Choosing ──────────────────────────────────────────────────────────


def pick_tier(shares: dict, counts: dict, revealed: int, available: set, rng: random.Random) -> str:
    """The tier of the next reveal: weighted-deficit assignment."""
    deficits = {t: shares[t] / 100.0 * (revealed + 1) - counts.get(t, 0) for t in TIERS if t in available and shares[t] > 0}
    if not deficits:
        return rng.choice(sorted(available))
    positive = {t: d for t, d in deficits.items() if d > 0}
    if not positive:
        return max(deficits, key=deficits.get)
    top = max(positive.values())
    candidates = {t: d for t, d in positive.items() if d >= top - 0.5}
    names = sorted(candidates)
    return rng.choices(names, weights=[candidates[t] for t in names], k=1)[0]


def pick_hero(heroes: list, holders: dict, rng: random.Random):
    least = min(holders.get(h.id, 0) for h in heroes)
    return rng.choice([h for h in heroes if holders.get(h.id, 0) == least])


# ─── Reveal ────────────────────────────────────────────────────────────


async def _reveal_body(session, ctx, config, reveal: GameReveals, *, already: bool) -> dict:
    hero = await session.get(GameHeroes, reveal.hero_id)
    holders = (await hero_counts(session, ctx.event.id)).get(hero.id, 0)
    score = await session.scalar(select(GameCardScores.distinct_heroes).where(GameCardScores.registration_id == ctx.reg.id))
    total = len(await heroes_of(session, ctx.event.id))
    return {
        "already": already,
        "hero": hero_out(hero),
        "tier": reveal.tier,
        "tier_label": TIER_LABEL[reveal.tier],
        "holders": holders if config.show_holders else None,
        "deck_count": int(score or 1),
        "total": total,
    }


async def reveal(session, ctx, rng: Optional[random.Random] = None) -> dict:
    rng = rng or random
    event, reg = ctx.event, ctx.reg
    await common.require_active(session, event, ctx.settings, CARDS)
    config = await config_of(session, event.id)
    existing = await session.scalar(select(GameReveals).where(GameReveals.registration_id == reg.id))
    if existing is not None:
        return await _reveal_body(session, ctx, config, existing, already=True)
    await common.require_player_ok(session, reg)
    # Every reveal takes these four rows in the same order, one reveal at a time.
    counters = {
        c.tier: c
        for c in (
            await session.scalars(select(GameTierCounters).where(GameTierCounters.event_id == event.id).order_by(GameTierCounters.tier).with_for_update())
        ).all()
    }
    if len(counters) < len(TIERS):
        raise err(409, "CARDS_NOT_READY", "The card game is not set up yet.")
    existing = await session.scalar(select(GameReveals).where(GameReveals.registration_id == reg.id).execution_options(populate_existing=True))
    if existing is not None:
        await session.rollback()
        return await _reveal_body(session, ctx, config, existing, already=True)
    config = await session.scalar(select(GameCardConfig).where(GameCardConfig.event_id == event.id).execution_options(populate_existing=True))
    heroes = await heroes_of(session, event.id)
    by_tier: dict = {t: [h for h in heroes if h.tier == t] for t in TIERS}
    available = {t for t in TIERS if by_tier[t]}
    if not available:
        raise err(409, "CARDS_NOT_READY", "There are no heroes to reveal yet.")
    counts = {t: counters[t].reveals for t in TIERS}
    revealed = sum(counts.values())
    shares = shares_of(config)
    tier, forced = None, False
    floor_left = config.legendary_floor - counts["legendary"]
    if floor_left > 0 and "legendary" in available:
        checked = int(
            await session.scalar(
                select(func.count()).select_from(CheckIns).join(EventRegistrationsLink, EventRegistrationsLink.id == CheckIns.registration_id).where(
                    CheckIns.event_id == event.id, EventRegistrationsLink.is_deleted == False  # noqa: E712
                )
            )
            or 0
        )
        waiting = max(1, checked - revealed)
        if floor_left >= waiting:
            tier, forced = "legendary", True
    if tier is None:
        tier = pick_tier(shares, counts, revealed, available, rng)
    hero = pick_hero(by_tier[tier], await hero_counts(session, event.id), rng)
    now = common.now()
    session.add(GameReveals(id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, hero_id=hero.id, tier=tier, forced=forced))
    session.add(GameDeckItems(id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, hero_id=hero.id, source="reveal"))
    counters[tier].reveals += 1
    await session.execute(
        pg_insert(GameCardScores)
        .values(id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, distinct_heroes=1, reached_at=now)
        .on_conflict_do_nothing(constraint="uq_game_card_scores_registration")
    )
    if tier in ("rare", "legendary"):
        who = await common.player_label(session, reg.id)
        message = f"{who} pulled a {TIER_LABEL[tier]} hero: {hero.name}!"
        await common.add_feed(session, event.id, CARDS, f"{tier}_revealed", message, {"hero_id": str(hero.id), "tier": tier})
        if tier == "legendary":
            total_checked = int(
                await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.event_id == event.id)) or 0
            )
            target = max(config.legendary_floor, round(shares["legendary"] / 100.0 * total_checked))
            hidden = max(0, target - counters["legendary"].reveals)
            if hidden:
                await common.add_feed(session, event.id, CARDS, "legendary_hidden", f"{hidden} Legendary holder{'s' if hidden != 1 else ''} still hidden", {"hidden": hidden})
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        # The same player's other tap won: its reveal is theirs.
        again = await session.scalar(select(GameReveals).where(GameReveals.registration_id == reg.id))
        if again is None:
            raise
        return await _reveal_body(session, ctx, config, again, already=True)
    common.told(event.id, "board", "feed")
    return await _reveal_body(session, ctx, config, await session.scalar(select(GameReveals).where(GameReveals.registration_id == reg.id)), already=False)


# ─── The deck ──────────────────────────────────────────────────────────


async def deck(session, ctx) -> dict:
    event, reg = ctx.event, ctx.reg
    await common.require_active(session, event, ctx.settings, CARDS)
    config = await config_of(session, event.id)
    heroes = await heroes_of(session, event.id)
    held = {d.hero_id: d for d in (await session.scalars(select(GameDeckItems).where(GameDeckItems.registration_id == reg.id))).all()}
    counts = await hero_counts(session, event.id) if config.show_holders else {}
    cards, missing = [], {t: 0 for t in TIERS}
    for h in heroes:
        if h.id in held:
            cards.append({**hero_out(h, holders=counts.get(h.id) if config.show_holders else None), "owned": True, "source": held[h.id].source, "got_at": held[h.id].created_at})
        else:
            cards.append({**locked_out(h), "owned": False})
            missing[h.tier] += 1
    score = await session.scalar(select(GameCardScores).where(GameCardScores.registration_id == reg.id))
    own = await session.scalar(select(GameReveals).where(GameReveals.registration_id == reg.id))
    return {
        "revealed": own is not None,
        "cards": cards,
        "deck_count": len(held),
        "total": len(heroes),
        "missing": missing,
        "complete": bool(score is not None and score.completed_at is not None),
        "completed_at": score.completed_at if score else None,
        "rank": await boards.rank_of(session, event.id, CARDS, reg.id),
    }


# ─── Collect ───────────────────────────────────────────────────────────


async def _give(session, event_id: int, to_reg, hero_id, from_reg, total: int, now) -> bool:
    """`hero_id` into `to_reg`'s deck; False when they already held it."""
    done = await session.execute(
        pg_insert(GameDeckItems)
        .values(id=uuid.uuid4(), event_id=event_id, registration_id=to_reg, hero_id=hero_id, source="collect", from_registration_id=from_reg)
        .on_conflict_do_nothing(constraint="uq_game_deck_items")
        .returning(GameDeckItems.id)
    )
    if done.first() is None:
        return False
    row = (
        await session.execute(
            update(GameCardScores)
            .where(GameCardScores.registration_id == to_reg)
            .values(distinct_heroes=GameCardScores.distinct_heroes + 1, reached_at=now)
            .returning(GameCardScores.distinct_heroes)
        )
    ).first()
    if row is not None and row[0] >= total:
        completed = await session.execute(
            update(GameCardScores).where(GameCardScores.registration_id == to_reg, GameCardScores.completed_at.is_(None)).values(completed_at=now).returning(GameCardScores.id)
        )
        if completed.first() is not None:
            who = await common.player_label(session, to_reg)
            await common.add_feed(session, event_id, CARDS, "deck_complete", f"{who} completed the whole deck!", {"registration": None})
    return True


async def _throttle(session, ctx, config) -> None:
    """One collect in COLLECT_GAP seconds and COLLECT_PER_HOUR an hour, by the scanner's own scans."""
    now = common.now()
    last = await session.scalar(
        select(func.max(GameMeetings.created_at)).where(GameMeetings.collector_id == ctx.reg.id, GameMeetings.scanned == True)  # noqa: E712
    )
    if last is not None and (now - last).total_seconds() < COLLECT_GAP:
        raise err(429, "RATE_LIMITED", "Slow down a moment.", retry_after=max(1, int(COLLECT_GAP - (now - last).total_seconds() + 0.999)))
    from datetime import timedelta

    hour = (
        await session.execute(
            select(func.count(), func.min(GameMeetings.created_at)).where(
                GameMeetings.collector_id == ctx.reg.id, GameMeetings.scanned == True, GameMeetings.created_at > now - timedelta(hours=1)  # noqa: E712
            )
        )
    ).first()
    if hour[0] >= COLLECT_PER_HOUR:
        wait = max(1, int(3600 - (now - hour[1]).total_seconds()))
        raise err(429, "RATE_LIMITED", "You have collected a lot this hour. Try again soon.", retry_after=wait)


async def collect(session, ctx, token: str) -> dict:
    event, reg = ctx.event, ctx.reg
    await common.require_active(session, event, ctx.settings, CARDS)
    config = await config_of(session, event.id)
    badge = tokens.read_badge(token)
    if badge is None or badge.event_id != event.id:
        raise err(422, "BAD_CODE", "That is not a player's code for this event.")
    if badge.registration_id == reg.id:
        raise err(409, "SELF_SCAN", "That is your own code. Scan someone else's.")
    if not config.collect_on:
        raise err(403, "COLLECT_OFF", "Collecting by scanning is off for this event.")
    owner_player = await session.scalar(select(GamePlayers).where(GamePlayers.registration_id == badge.registration_id, GamePlayers.event_id == event.id))
    owner = await common.registration_of(session, event.id, badge.registration_id)
    version = owner_player.pin_version if owner_player is not None else 0
    if owner is None or owner.ticket_revoked_at is not None or badge.pin_version != version:
        raise err(422, "BAD_CODE", "That code is not valid any more.")
    mine = await session.scalar(select(GameReveals).where(GameReveals.registration_id == reg.id))
    if mine is None:
        raise err(409, "NOT_REVEALED", "Unlock your own hero first.")
    theirs = await session.scalar(select(GameReveals).where(GameReveals.registration_id == owner.id))
    if theirs is None:
        raise err(409, "OWNER_NOT_REVEALED", "They haven't unlocked their hero yet.")
    await _throttle(session, ctx, config)

    now = common.now()
    total = len(await heroes_of(session, event.id))
    two_way = bool(config.two_way)
    # The pair rows go in a fixed order, so two people scanning each other at once queue instead of deadlocking.
    pairs = [(reg.id, owner.id, True)]
    if two_way:
        pairs.append((owner.id, reg.id, False))
    pairs.sort(key=lambda p: (str(p[0]), str(p[1])))
    for collector, owner_id, scanned in pairs:
        done = await session.execute(
            pg_insert(GameMeetings)
            .values(id=uuid.uuid4(), event_id=event.id, collector_id=collector, owner_id=owner_id, scanned=scanned)
            .on_conflict_do_nothing(constraint="uq_game_meetings_pair")
            .returning(GameMeetings.id)
        )
        if done.first() is None and collector == reg.id:
            await session.rollback()
            raise err(409, "ALREADY_MET", "You have already met this player.")
    gives = [(reg.id, theirs.hero_id, owner.id)]
    if two_way:
        gives.append((owner.id, mine.hero_id, reg.id))
    gives.sort(key=lambda g: str(g[0]))
    results = {}
    for to_reg, hero_id, from_reg in gives:
        results[to_reg] = await _give(session, event.id, to_reg, hero_id, from_reg, total, now)
    await session.commit()
    common.told(event.id, "board", "feed")
    got_hero = await session.get(GameHeroes, theirs.hero_id)
    gave_hero = await session.get(GameHeroes, mine.hero_id)
    score = await session.scalar(select(GameCardScores).where(GameCardScores.registration_id == reg.id))
    new = results.get(reg.id, False)
    return {
        "got": {"hero": hero_out(got_hero), "new": new, "message": f"You collected {got_hero.name}!" if new else "Already in your deck"},
        "gave": {"hero": hero_out(gave_hero), "new": results.get(owner.id, False)} if two_way else None,
        "deck_count": score.distinct_heroes if score else 1,
        "total": total,
        "complete": bool(score is not None and score.completed_at is not None),
        "rank": await boards.rank_of(session, event.id, CARDS, reg.id),
    }


# ─── The organiser's numbers ───────────────────────────────────────────


async def live_numbers(session, event) -> dict:
    """SPEC §8.1: revealed against checked in, Legendary against target, full decks, badge-to-badge scans, each tier against expected."""
    config = await config_of(session, event.id)
    heroes = await heroes_of(session, event.id)
    checked = int(await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.event_id == event.id)) or 0)
    tiers = dict((t, int(n)) for t, n in (await session.execute(select(GameReveals.tier, func.count()).where(GameReveals.event_id == event.id).group_by(GameReveals.tier))).all())
    revealed = sum(tiers.values())
    shares = shares_of(config)
    full = int(await session.scalar(select(func.count()).select_from(GameCardScores).where(GameCardScores.event_id == event.id, GameCardScores.completed_at.isnot(None))) or 0)
    scans = int(await session.scalar(select(func.count()).select_from(GameMeetings).where(GameMeetings.event_id == event.id, GameMeetings.scanned == True)) or 0)  # noqa: E712
    legendary_target = max(config.legendary_floor, round(shares["legendary"] / 100.0 * checked))
    registered = int(await session.scalar(select(func.count()).select_from(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)) or 0)  # noqa: E712
    return {
        "revealed": revealed,
        "checked_in": checked,
        "registered": registered,
        "legendary_revealed": tiers.get("legendary", 0),
        "legendary_target": legendary_target,
        "legendary_floor": config.legendary_floor,
        "full_decks": full,
        "collects": scans,
        "heroes": len(heroes),
        "tiers": [
            {"tier": t, "revealed": tiers.get(t, 0), "expected": round(shares[t] / 100.0 * revealed, 2), "share": shares[t]}
            for t in TIERS
        ],
    }


async def retry_deadlocks(session, fn, attempts: int = 3):
    """Run `fn()` again when Postgres picks it as the deadlock's loser."""
    for n in range(attempts):
        try:
            return await fn()
        except DBAPIError as exc:
            await session.rollback()
            if "deadlock" not in str(exc).lower() or n == attempts - 1:
                raise
