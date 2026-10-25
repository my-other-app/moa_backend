"""What a club's own WhatsApp number cost this month, as Meta's pricing
analytics say: volume and approximate cost by pricing category, in the
WhatsApp Business Account's own currency.

Meta is asked by the sweep (sweep.py) at most once a day for each active
number on a plan that has it -- `GET /{WABA}?fields=currency,pricing_analytics
.start(..).end(..).granularity(DAILY).metric_types(COST,VOLUME)
.dimensions(PRICING_CATEGORY)` -- and the answer is kept
(club_whatsapp_spend). The settings page reads what was kept, never Meta:
a page view costs no call. On the first read of a new month the month
before is read once more, so its last days are counted too.

Meta's COST is approximate (its analytics page says so) and looks back a
year at most; the page says it is approximate and when it was read.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.club_whatsapp import crypto, graph
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.delivery_routes import month_start
from app.api.club_whatsapp.models import ACTIVE, ClubWhatsAppAccounts
from app.api.club_whatsapp.reach_models import ClubWhatsAppSpend

logger = logging.getLogger(__name__)

#: How often Meta is asked about one number's spend.
REFRESH = timedelta(hours=24)
#: Numbers asked about per sweep; the rest wait for the next.
BATCH = 50
#: What the page says beside Meta's figure.
NOTE = ("Meta's own figure, which it calls approximate, in your WhatsApp Business Account's currency, "
        "before tax. Your invoice from Meta is what you pay.")
CATEGORY_ORDER = ("MARKETING", "MARKETING_LITE", "UTILITY", "AUTHENTICATION", "AUTHENTICATION_INTERNATIONAL", "SERVICE",
                  "REFERRAL_CONVERSION")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fields(start: datetime, end: datetime) -> str:
    return (f"currency,pricing_analytics.start({int(start.timestamp())}).end({int(end.timestamp())})"
            ".granularity(DAILY).metric_types(COST,VOLUME).dimensions(PRICING_CATEGORY)")


def _number(value) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def summarise(answer: dict) -> dict:
    """Meta's answer as {"currency", "categories": [{"category", "volume", "cost"}], "volume", "cost"}."""
    totals: dict[str, list] = {}
    analytics = answer.get("pricing_analytics") if isinstance(answer.get("pricing_analytics"), dict) else {}
    for block in analytics.get("data") or []:
        points = block.get("data_points") if isinstance(block, dict) else None
        for point in points or []:
            if not isinstance(point, dict):
                continue
            category = str(point.get("pricing_category") or "OTHER").upper()[:32]
            row = totals.setdefault(category, [0, Decimal(0)])
            row[0] += int(_number(point.get("volume")))
            row[1] += _number(point.get("cost"))
    ordered = sorted(totals, key=lambda c: (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else len(CATEGORY_ORDER), c))
    categories = [{"category": c, "volume": totals[c][0], "cost": float(round(totals[c][1], 4))} for c in ordered]
    currency = (str(answer.get("currency") or "").upper()[:8] or None)
    return {
        "currency": currency, "categories": categories,
        "volume": sum(c["volume"] for c in categories), "cost": round(sum((totals[c][1] for c in totals), Decimal(0)), 4),
    }


async def fetch(keys: graph.Keys, start: datetime, end: datetime) -> dict:
    """One month (or its part so far) from Meta, summed. GraphError when Meta says no."""
    answer = await graph.call(keys, "GET", keys.waba_id, params={"fields": fields(start, end)})
    return summarise(answer)


def _previous_month(start: datetime) -> datetime:
    return month_start(start - timedelta(days=1))


def _month_key(start: datetime) -> date:
    from zoneinfo import ZoneInfo

    from app.api.events.documents import EVENT_TIMEZONE

    return start.astimezone(ZoneInfo(EVENT_TIMEZONE)).date().replace(day=1)


async def _keep(session, account: ClubWhatsAppAccounts, start: datetime, summary: dict, now: datetime) -> None:
    table = ClubWhatsAppSpend.__table__
    values = dict(account_id=account.id, club_id=account.club_id, month=_month_key(start), currency=summary["currency"],
                  categories=summary["categories"], volume=summary["volume"], cost=summary["cost"], fetched_at=now)
    stmt = pg_insert(table).values(**values)
    await session.execute(stmt.on_conflict_do_update(
        constraint="uq_club_whatsapp_spend_month",
        set_={k: stmt.excluded[k] for k in ("currency", "categories", "volume", "cost", "fetched_at")},
    ))


async def refresh(session, now: Optional[datetime] = None) -> int:
    """The sweep's pass: each active number not read for a day, on a plan
    that has it, read from Meta and kept. Nothing is held while Meta answers;
    the answer is kept only if the keys are the ones it was asked with.
    A refusal waits for tomorrow (the page keeps the last good figures).
    Returns how many numbers were read."""
    now = now or _now()
    if not crypto.available():
        return 0
    A = ClubWhatsAppAccounts
    ids = (await session.scalars(
        select(A.id).where(A.status == ACTIVE, or_(A.spend_checked_at.is_(None), A.spend_checked_at <= now - REFRESH))
        .order_by(A.spend_checked_at.asc().nulls_first(), A.id).limit(BATCH)
    )).all()
    done = 0
    for account_id in ids:
        try:
            account = await session.get(A, account_id)
            if account is None or account.status != ACTIVE or not await accounts.plan_allows(session, account.club_id):
                continue
            sealed, last = account.access_token_enc, account.spend_checked_at
            keys = accounts.keys_of(account)
            await session.commit()
            this = month_start(now)
            months = [(this, now)]
            if last is not None and last < this:
                # A new month: the last one's final days, read once more.
                months.insert(0, (_previous_month(this), this))
            read: list[tuple[datetime, dict]] = []
            failure = None
            for start, end in months:
                try:
                    read.append((start, await fetch(keys, start, end)))
                except graph.GraphError as exc:
                    failure = exc
                    break
            account = await session.scalar(select(A).where(A.id == account_id).with_for_update().execution_options(populate_existing=True))
            if account is None or account.access_token_enc != sealed:
                await session.commit()
                continue
            for start, summary in read:
                await _keep(session, account, start, summary, now)
                account.currency = summary["currency"] or account.currency
            account.spend_checked_at = now
            await session.commit()
            if failure is not None:
                logger.info("whatsapp spend: number %s not read (%s)", account_id, failure.code or type(failure).__name__)
            else:
                done += 1
        except crypto.Unavailable:
            await session.rollback()
        except Exception as exc:  # noqa: BLE001 - one number never stops the rest
            logger.warning("whatsapp spend: number %s could not be read (%s)", account_id, type(exc).__name__)
            await session.rollback()
    return done


async def this_month(session, club_id: int, now: Optional[datetime] = None) -> dict:
    """The settings page's "This month": Meta's volume and approximate cost
    by category, as last read (never asking Meta), and what went from the
    club's number and what fell back, by reason."""
    from app.api.club_whatsapp import delivery_routes

    now = now or _now()
    start = month_start(now)
    account = await accounts.live_account(session, club_id) or await accounts.latest_account(session, club_id)
    row = None
    if account is not None:
        row = await session.scalar(select(ClubWhatsAppSpend).where(
            ClubWhatsAppSpend.account_id == account.id, ClubWhatsAppSpend.month == _month_key(start),
        ))
    return {
        "month": _month_key(start).isoformat()[:7],
        "currency": (row.currency if row is not None else None) or (account.currency if account is not None else None),
        "fetched_at": row.fetched_at if row is not None else None,
        "checked_at": account.spend_checked_at if account is not None else None,
        "categories": list(row.categories or []) if row is not None else [],
        "volume": int(row.volume or 0) if row is not None else 0,
        "cost": float(row.cost or 0) if row is not None else 0.0,
        "approximate": True,
        "note": NOTE,
        **await delivery_routes.club_month(session, club_id, now),
    }
