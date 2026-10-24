"""Plan gates: where a club's plan says no.

`service.resolve_entitlements` answers what a club's plan allows; this module
is where the product acts on the answer. The rest of the code calls one of:

- `require_feature(session, club_id, key)` before a write that starts or
  creates a paid feature (a paid ticket, a poll, a tracking link). Reads of
  what already exists are never gated: a club that drops a plan keeps seeing
  its data.
- `require_within(session, club_id, limit_key, adding=1, used=None)` before a
  write that counts against a limit (an event this month, a team seat).
- `check_registration(session, event)` from the one place a registration is
  made, for the two registration caps.

The refusals are a contract shared with the dashboard and the website:

- a locked feature: 403 PLAN_FEATURE_LOCKED, `errors` naming the flag, its
  label, the cheapest public plan with it, and where to upgrade;
- a limit reached: 403 PLAN_LIMIT_REACHED, `errors` with the limit, its value,
  what is used, the cheapest public plan with room, and where to upgrade;
- an attendee turned away by the club's plan: 409 REGISTRATIONS_CLOSED, with
  nothing about plans in it. The club's owner is emailed instead, at most once
  a day per limit.

Nothing here is enforced when:

- the acting user is an admin, or an admin acting as the club (impersonation);
- `settings.PLAN_ENFORCEMENT` is false (the kill switch);
- the platform has no free plan (`service._unlimited`: everything allowed);
- for an event-scoped gate, the event is grandfathered: created before
  `settings.PLAN_ENFORCEMENT_FROM` and happening before that plus
  `settings.PLAN_GRANDFATHER_DAYS`. Events already under way keep working as
  they were -- paid tickets, both registration caps, the live room,
  networking, its links, report, store and swag, ID cards, competitions --
  so nothing breaks mid-event at the switch-on. Club-wide gates (website and
  its templates and its own domain, roles and seats, finance, the club's own links and card
  design, events and announcements per month) hold for every club. The
  scope rule lives in `_exempt`; call sites only pass `event=`.

Limits deliberately not enforced:

- `ticket_types_per_event`, `coupons_per_event`: an event has one ticket type
  and there are no coupons; there is nothing to count.

One request resolves a club's entitlements once (`RequestScope`), however many
gates it passes.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.api.subscriptions.models import FEATURE_CATALOG
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

FEATURE_LOCKED = "PLAN_FEATURE_LOCKED"
LIMIT_REACHED = "PLAN_LIMIT_REACHED"
REGISTRATIONS_CLOSED = "REGISTRATIONS_CLOSED"
REGISTRATIONS_CLOSED_MESSAGE = "This event isn't taking more registrations right now."

#: Every plan has these; nothing gates them.
ALWAYS_ALLOWED = frozenset({"checkin", "certificates", "regform", "reviews", "threads"})

#: Gates about one event, which a grandfathered event is exempt from. Asked
#: with `event=`, they let a grandfathered one through (`_exempt`). `links`
#: is event-scoped only for an event's links: the club's own links are asked
#: without an event, and so are always enforced.
EVENT_SCOPED_FEATURES = frozenset({
    "tickets", "quiz", "polls", "qa", "networking", "links", "report",
    "merch", "swag", "badges", "teams", "judging", "rounds",
})
EVENT_SCOPED_LIMITS = frozenset({"registrations_per_event", "merch_items_per_event"})
#: Gates about the whole club, enforced whatever its events: website and
#: its own domain, roles, finance, AI assistants over MCP, the club's own
#: WhatsApp number (app/api/club_whatsapp), and the per-club limits (events and
#: announcements per month, committee seats, website templates,
#: registrations per month -- though a registration to a grandfathered event
#: is never refused, it still counts in `usage`).
CLUB_WIDE_FEATURES = frozenset({"website", "custom_domain", "roles", "finance", "mcp", "whatsapp_own"})

#: The limits nothing enforces, and why (see the module docstring).
NOT_ENFORCED = {
    "ticket_types_per_event": "An event has one ticket type.",
    "coupons_per_event": "There are no coupons.",
}

#: The website templates in catalogue order; a plan's `website_templates`
#: allows the first N of them. Mirrors sites.models.TEMPLATES, which is also
#: the order the dashboard lists them in.
def template_order() -> tuple[str, ...]:
    from app.api.sites.models import TEMPLATES

    return tuple(TEMPLATES)


LABELS = dict(FEATURE_CATALOG)

#: How a locked feature is named in a sentence: (subject, verb).
_SUBJECTS = {
    "tickets": ("Paid tickets", "are"),
    "website": ("A club website", "is"),
    "custom_domain": ("Your own domain for the club website", "is"),
    "quiz": ("The live quiz", "is"),
    "polls": ("Live polls", "are"),
    "qa": ("Live Q&A", "is"),
    "networking": ("Attendee networking", "is"),
    "links": ("Tracking links", "are"),
    "report": ("Post-event reports", "are"),
    "merch": ("The merchandise store", "is"),
    "swag": ("Swag handouts", "are"),
    "badges": ("ID cards and badge printing", "are"),
    "roles": ("Team roles other than Editor", "are"),
    "teams": ("Competitions", "are"),
    "judging": ("Judging", "is"),
    "rounds": ("Competition rounds", "are"),
    "finance": ("Finance and expenses", "are"),
    "ai_assist": ("AI assist", "is"),
    "ai_byok": ("AI assist with your own key", "is"),
    "mcp": ("Connecting AI assistants", "is"),
    "whatsapp_own": ("Your own WhatsApp Business number", "is"),
}

#: A limit's unit, singular and plural, and whether it is counted per month.
_UNITS = {
    "events_per_month": ("event", "events", True),
    "registrations_per_month": ("registration", "registrations", True),
    "registrations_per_event": ("registration", "registrations", False),
    "committee_seats": ("team seat", "team seats", False),
    "announcements_per_month": ("announcement", "announcements", True),
    "merch_items_per_event": ("item", "items", False),
    "website_templates": ("website template", "website templates", False),
    "ai_actions_per_month": ("AI action", "AI actions", True),
    "emails_per_month": ("email", "emails", True),
    "whatsapp_per_month": ("WhatsApp message", "WhatsApp messages", True),
}

#: An owner is told registrations are paused at most this often, per club and limit.
NOTIFY_EVERY = timedelta(hours=24)
PAUSED_ACTION = "plan.paused"


def upgrade_url() -> str:
    return f"{settings.PUBLIC_WEB_URL.rstrip('/')}/billing"


def enforcing() -> bool:
    return bool(settings.PLAN_ENFORCEMENT)


def cutoff() -> datetime:
    value = settings.PLAN_ENFORCEMENT_FROM
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def grace_until() -> datetime:
    """An event made before the cutoff stays grandfathered only while it
    happens before this: the cutoff plus PLAN_GRANDFATHER_DAYS."""
    return cutoff() + timedelta(days=int(settings.PLAN_GRANDFATHER_DAYS))


def grandfathered(event, *, event_datetime: Optional[datetime] = None) -> bool:
    """An event already under way when enforcement began, which the switch-on
    must not break: created before `PLAN_ENFORCEMENT_FROM`, and happening
    before `grace_until()`. Every event-scoped gate leaves it alone (see
    EVENT_SCOPED); the club-wide ones do not.

    Both conditions, so an old event cannot be redated and reused for ever:
    moved past the window, it is gated like any other from then on. A paid
    one keeps selling (the tickets gate only fires on free to paid), but is
    capped. `event_datetime` judges an edit by the date it is being moved to.
    An event not saved yet, or without a date, is never grandfathered."""
    created = _aware(getattr(event, "created_at", None))
    when = _aware(event_datetime if event_datetime is not None else getattr(event, "event_datetime", None))
    if created is None or when is None:
        return False
    return created < cutoff() and when < grace_until()


async def plan_grandfathered(session, event) -> bool:
    """`grandfathered`, as the dashboard reads it (events/info): false too
    when nothing is enforced -- the switch is off, or there is no Free plan
    for a club without a subscription to fall to."""
    if not enforcing() or getattr(event, "club_id", None) is None or not grandfathered(event):
        return False
    from sqlalchemy import select

    from app.api.subscriptions.models import Plans
    from app.api.subscriptions.service import DEFAULT_PLAN_KEY

    return bool(
        await session.scalar(
            select(Plans.id).where(Plans.key == DEFAULT_PLAN_KEY, Plans.is_deleted == False).limit(1)  # noqa: E712
        )
    )


# ---------------------------------------------------------------------------
# The request: who is acting, and what was already resolved
# ---------------------------------------------------------------------------

_scope: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("plan_gate_scope", default=None)


class RequestScope:
    """ASGI middleware giving each request a fresh scope: the entitlements it
    resolved, the public plans it read, and whether an admin (or an admin as
    someone else) is behind it. Set and reset around the request, so nothing
    carries from one request to the next."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = _scope.set({})
        try:
            await self.app(scope, receive, send)
        finally:
            _scope.reset(token)


def note_actor(user, impersonated_by: Optional[int] = None) -> None:
    """Called by the auth dependency for whoever the request signed in as."""
    ctx = _scope.get()
    if ctx is None:
        return
    if impersonated_by:
        ctx["impersonated_by"] = impersonated_by
    if user is not None and _is_admin(user):
        ctx["admin"] = True


def impersonated_by() -> Optional[int]:
    """The admin behind this request when it is an admin acting as the club
    (impersonation), else None. For what only the club's own people may see
    -- a club's WhatsApp conversations -- whatever an admin may otherwise do."""
    ctx = _scope.get()
    return (ctx or {}).get("impersonated_by")


def _is_admin(user) -> bool:
    from app.api.events.access import is_admin_user

    return bool(getattr(user, "user_type", None) is not None and is_admin_user(user))


def bypassed(actor=None) -> bool:
    """Admins, and admins acting as a club, are never gated."""
    if actor is not None and _is_admin(actor):
        return True
    ctx = _scope.get()
    return bool(ctx and (ctx.get("admin") or ctx.get("impersonated_by")))


async def entitlements(session, club_id: int):
    """`service.resolve_entitlements`, once per club per request."""
    from app.api.subscriptions import service

    ctx = _scope.get()
    cache = ctx.setdefault("entitlements", {}) if ctx is not None else None
    if cache is not None and club_id in cache:
        return cache[club_id]
    resolved = await service.resolve_entitlements(session, club_id)
    if cache is not None:
        cache[club_id] = resolved
    return resolved


# ---------------------------------------------------------------------------
# The plan to suggest (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanCard:
    """What the suggestion needs of a plan."""

    key: str
    name: str
    price_monthly_paise: int = 0
    contact_sales: bool = False
    sort_order: int = 0
    features: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)


def card_of(plan) -> PlanCard:
    from app.api.subscriptions.models import FEATURE_KEYS, LIMIT_KEYS

    return PlanCard(
        key=plan.key,
        name=plan.name,
        price_monthly_paise=int(plan.price_monthly_paise or 0),
        contact_sales=bool(plan.contact_sales),
        sort_order=int(plan.sort_order or 0),
        features={k: bool(plan.features and getattr(plan.features, k)) for k in FEATURE_KEYS},
        limits=(
            {k: getattr(plan.limits, k) for k in LIMIT_KEYS}
            if plan.limits
            else {k: None for k in LIMIT_KEYS}
        ),
    )


def _cheapest(cards) -> Optional[PlanCard]:
    # Priced-on-request plans show a price of zero; they come after every
    # plan with a price, since "Talk to us" is not cheaper than ₹299.
    return min(cards, key=lambda c: (c.contact_sales, c.price_monthly_paise, c.sort_order, c.key), default=None)


def plan_for_feature(cards, feature: str) -> Optional[PlanCard]:
    """The cheapest public plan that includes `feature`, or None."""
    return _cheapest([c for c in cards if c.features.get(feature)])


def plan_for_limit(
    cards, limit_key: str, *, current: Optional[int], needed: int, feature: Optional[str] = None
) -> Optional[PlanCard]:
    """The cheapest public plan whose `limit_key` is unlimited, or higher than
    the club's `current` cap and at least `needed` (what the refused action
    would bring the count to), or None. With `feature`, only plans that also
    include it: more store items are no use on a plan without the store."""

    def roomier(card: PlanCard) -> bool:
        if feature is not None and not card.features.get(feature):
            return False
        cap = card.limits.get(limit_key)
        if cap is None:
            return True
        return (current is None or cap > current) and cap >= needed

    return _cheapest([c for c in cards if roomier(c)])


async def _public_cards(session) -> list[PlanCard]:
    from app.api.subscriptions import service

    ctx = _scope.get()
    if ctx is not None and "plans" in ctx:
        return ctx["plans"]
    cards = [card_of(p) for p in await service.public_plans(session)]
    if ctx is not None:
        ctx["plans"] = cards
    return cards


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def _plural(n: int, unit: tuple) -> str:
    return unit[0] if n == 1 else unit[1]


def feature_message(feature: str, plan: Optional[PlanCard]) -> str:
    subject, verb = _SUBJECTS.get(feature, (LABELS.get(feature, feature), "is"))
    if plan is None:
        return f"{subject} {verb} not included in your plan."
    return f"{subject} {verb} part of {plan.name} and up."


def limit_message(limit_key: str, *, cap: int, used: int, plan_name: str, template: Optional[str] = None) -> str:
    unit = _UNITS.get(limit_key, (limit_key, limit_key, False))
    if limit_key == "website_templates":
        name = (template or "That").title()
        return f"{plan_name} includes the first {cap} {_plural(cap, unit)}; {name} is not one of them."
    if limit_key == "events_per_month":
        return f"You've created {used} of {cap} {_plural(cap, unit)} this month on {plan_name}."
    if limit_key == "announcements_per_month":
        return f"You've sent {used} of {cap} {_plural(cap, unit)} this month on {plan_name}."
    if limit_key == "registrations_per_month":
        return f"Your events have had {used} of {cap} {_plural(cap, unit)} this month on {plan_name}."
    if limit_key == "registrations_per_event":
        return f"This event has {used} of {cap} {_plural(cap, unit)} allowed on {plan_name}."
    if limit_key == "committee_seats":
        return f"You've used {used} of {cap} {_plural(cap, unit)} on {plan_name}."
    if limit_key == "merch_items_per_event":
        return f"This event has {used} of {cap} merchandise {_plural(cap, unit)} allowed on {plan_name}."
    per = " this month" if unit[2] else ""
    return f"You've used {used} of {cap} {_plural(cap, unit)}{per} on {plan_name}."


async def feature_locked(session, feature: str) -> CustomHTTPException:
    plan = plan_for_feature(await _public_cards(session), feature)
    return CustomHTTPException(
        status_code=403,
        message=feature_message(feature, plan),
        error_code=FEATURE_LOCKED,
        errors={
            "feature": feature,
            "label": LABELS.get(feature, feature),
            "required_plan": plan.key if plan else None,
            "required_plan_name": plan.name if plan else None,
            "upgrade_url": upgrade_url(),
        },
    )


async def limit_reached(
    session, ent, limit_key: str, *, cap: int, used: int, adding: int = 1, template: Optional[str] = None, feature: Optional[str] = None
) -> CustomHTTPException:
    plan = plan_for_limit(await _public_cards(session), limit_key, current=cap, needed=used + adding, feature=feature)
    plan_name = ent.plan.name if ent.plan is not None else "your plan"
    return CustomHTTPException(
        status_code=403,
        message=limit_message(limit_key, cap=cap, used=used, plan_name=plan_name, template=template),
        error_code=LIMIT_REACHED,
        errors={
            "limit": limit_key,
            "limit_value": cap,
            "used": used,
            "required_plan": plan.key if plan else None,
            "required_plan_name": plan.name if plan else None,
            "upgrade_url": upgrade_url(),
        },
    )


def registrations_closed() -> CustomHTTPException:
    """What an attendee sees. Nothing about the club's plan is in it."""
    return CustomHTTPException(status_code=409, message=REGISTRATIONS_CLOSED_MESSAGE, error_code=REGISTRATIONS_CLOSED)


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def _exempt(key: str, event) -> bool:
    """The grandfather rule, in one place: an event-scoped gate asked about a
    grandfathered event lets it through. A club-wide key never is exempt,
    whatever event it is asked about."""
    return event is not None and (key in EVENT_SCOPED_FEATURES or key in EVENT_SCOPED_LIMITS) and grandfathered(event)


async def allows(session, club_id: Optional[int], feature: str, *, actor=None, event=None) -> bool:
    """Whether `feature` may be used, without raising: for the attendee-facing
    places, which refuse in their own words rather than the plan's. Pass the
    `event` the use is on, so a grandfathered one is let through."""
    if feature in ALWAYS_ALLOWED or not enforcing() or club_id is None or bypassed(actor) or _exempt(feature, event):
        return True
    return (await entitlements(session, club_id)).allows(feature)


async def require_feature(session, club_id: Optional[int], key: str, *, actor=None, event=None) -> None:
    """Raise 403 PLAN_FEATURE_LOCKED unless the club's plan includes `key`.
    Pass the `event` for an event-scoped feature: a grandfathered one is exempt."""
    if await allows(session, club_id, key, actor=actor, event=event):
        return
    raise await feature_locked(session, key)


#: How long the live room's feed trusts what it last read of a club's plan.
#: The feed is polled by every phone in the room; an upgrade shows within this.
FEED_TTL = timedelta(seconds=30)
_feed: dict = {}


async def allows_for_feed(session, club_id: Optional[int], feature: str, *, event) -> bool:
    """`allows` for the public live feed, remembered for FEED_TTL per club."""
    if feature in ALWAYS_ALLOWED or not enforcing() or club_id is None or _exempt(feature, event):
        return True
    now = datetime.now(timezone.utc)
    hit = _feed.get((club_id, feature))
    if hit is not None and hit[0] > now:
        return hit[1]
    allowed = (await entitlements(session, club_id)).allows(feature)
    _feed[(club_id, feature)] = (now + FEED_TTL, allowed)
    return allowed


async def cap(session, club_id: Optional[int], limit_key: str, *, actor=None) -> Optional[int]:
    """The club's cap on `limit_key` as it applies to this caller: None when
    nothing is enforced for it (admin, switch off, unlimited)."""
    if limit_key in NOT_ENFORCED or not enforcing() or club_id is None or bypassed(actor):
        return None
    return (await entitlements(session, club_id)).limit(limit_key)


async def require_within(
    session,
    club_id: Optional[int],
    limit_key: str,
    *,
    adding: int = 1,
    used: Optional[int] = None,
    actor=None,
    template: Optional[str] = None,
    feature: Optional[str] = None,
    event=None,
) -> None:
    """Raise 403 PLAN_LIMIT_REACHED unless `used + adding` fits the club's
    cap. `used` is counted here for the per-club limits; a per-event or a
    template limit is counted by the caller and passed in. `feature` is the
    one the action also needs, so the plan suggested has it too. Pass the
    `event` for a per-event limit: a grandfathered one is exempt."""
    if limit_key in NOT_ENFORCED or not enforcing() or club_id is None or bypassed(actor) or _exempt(limit_key, event):
        return
    ent = await entitlements(session, club_id)
    cap = ent.limit(limit_key)
    if cap is None:
        return
    if used is None:
        used = await count(session, club_id, limit_key)
    if used + adding <= cap:
        return
    raise await limit_reached(session, ent, limit_key, cap=cap, used=used, adding=adding, template=template, feature=feature)


# ---------------------------------------------------------------------------
# Counting, the way `usage` (and so the dashboard) counts
# ---------------------------------------------------------------------------


async def count(session, club_id: int, limit_key: str, now: Optional[datetime] = None) -> int:
    """What the club has used of a per-club limit: the numbers
    `service.usage` reports, counted the same way (IST calendar months)."""
    from sqlalchemy import func, select

    from app.api.club_access.models import ClubMembers
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.subscriptions import service

    since = service.month_start(now)
    if limit_key == "events_per_month":
        return int(
            await session.scalar(
                select(func.count(Events.id)).where(
                    Events.club_id == club_id, Events.is_deleted == False, Events.created_at >= since  # noqa: E712
                )
            )
            or 0
        )
    if limit_key == "registrations_per_month":
        # Runs on every registration to a capped club. Each of the club's
        # events is one probe of ix_registrations_event_created for this
        # month's rows, read from the index alone. Both soft deletes are
        # spelled out and the session's own filter is switched off for the
        # statement: it adds `is_deleted IS false`, which Postgres cannot
        # match to a partial index `WHERE NOT is_deleted` -- with it every
        # count read the whole events table, and every registration's row.
        return int(
            await session.scalar(
                select(func.count()).select_from(EventRegistrationsLink).where(
                    EventRegistrationsLink.event_id.in_(
                        select(Events.id).where(Events.club_id == club_id, Events.is_deleted == False)  # noqa: E712
                    ),
                    EventRegistrationsLink.is_deleted == False,  # noqa: E712
                    EventRegistrationsLink.created_at >= since,
                ).execution_options(include_deleted=True)
            )
            or 0
        )
    if limit_key == "committee_seats":
        return int(
            await session.scalar(
                select(func.count(ClubMembers.id)).where(ClubMembers.club_id == club_id, ClubMembers.status != "removed")
            )
            or 0
        )
    if limit_key == "announcements_per_month":
        return int((await service.usage(session, club_id, now))["announcements_per_month"])
    raise ValueError(f"{limit_key} is not counted per club; pass `used`")


async def event_registrations(session, event) -> int:
    """An event's registrations as its own capacity counts them: on a paid
    event the paid ones (read from ix_registrations_event_created alone), on
    a free event all of them -- its going_count, which the database keeps
    equal to that count in every writer's transaction (events.models)."""
    from sqlalchemy import func, select

    from app.api.events.models import EventRegistrationsLink, Events

    if not event.has_fee:
        return int(await session.scalar(select(Events.going_count).where(Events.id == event.id)) or 0)
    query = select(func.count()).select_from(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event.id,
        EventRegistrationsLink.is_deleted == False,  # noqa: E712
        EventRegistrationsLink.is_paid == True,  # noqa: E712
    ).execution_options(include_deleted=True)  # spelled out above, in the form the index can serve (see count)
    return int(await session.scalar(query) or 0)


async def merch_items(session, event_id: int) -> int:
    """Items an event offers: the club's store items it offers (for sale) and
    its handouts (swag), together. `merch_items_per_event` caps the two as one
    number."""
    from sqlalchemy import func, select

    from app.api.store.models import EventStoreItems, EventStoreOffers

    store = await session.scalar(
        select(func.count()).select_from(EventStoreOffers)
        .join(EventStoreItems, EventStoreItems.id == EventStoreOffers.item_id)
        .where(EventStoreOffers.event_id == event_id, EventStoreItems.is_deleted == False)  # noqa: E712
    )
    return int(store or 0) + await swag_items(session, event_id)


async def swag_items(session, event_id: Optional[int]) -> int:
    """An event's handout items (none for an event not saved yet)."""
    if event_id is None:
        return 0
    from sqlalchemy import func, select

    from app.api.merch.models import MerchItems

    return int(await session.scalar(select(func.count(MerchItems.id)).where(MerchItems.event_id == event_id)) or 0)


# ---------------------------------------------------------------------------
# The gates as the features call them
# ---------------------------------------------------------------------------


async def check_event_create(session, club_id: Optional[int], *, has_fee: bool, actor=None) -> None:
    """A new event (created, or duplicated from another): paid tickets need
    `tickets`, and it counts against `events_per_month`."""
    if has_fee:
        await require_feature(session, club_id, "tickets", actor=actor)
    await require_within(session, club_id, "events_per_month", actor=actor)


async def check_event_update(
    session, event, *, has_fee=None, merch_enabled=None, swag_enabled=None, event_datetime=None, actor=None
) -> None:
    """An event's settings changing. Only a switch turned on is gated: a paid
    event stays paid, and an event switching the store off is never refused.
    A grandfathered event is exempt -- judged by the date it is being moved
    to (`event_datetime`), so redating it past the window does not carry the
    exemption along."""
    from types import SimpleNamespace

    # The event as it will be: its own creation, the date it is being given.
    scope = SimpleNamespace(created_at=event.created_at, event_datetime=event_datetime or event.event_datetime)
    if has_fee and not event.has_fee:
        await require_feature(session, event.club_id, "tickets", actor=actor, event=scope)
    if merch_enabled and not event.merch_enabled:
        await require_feature(session, event.club_id, "merch", actor=actor, event=scope)
    if swag_enabled and not event.swag_enabled:
        await require_feature(session, event.club_id, "swag", actor=actor, event=scope)


async def check_merch_item(session, event, feature: str, *, actor=None) -> None:
    """A new item for an event's store (`merch`) or its handouts (`swag`)."""
    await require_feature(session, event.club_id, feature, actor=actor, event=event)
    if not enforcing() or bypassed(actor) or _exempt("merch_items_per_event", event):
        return
    await require_within(
        session, event.club_id, "merch_items_per_event", used=await merch_items(session, event.id), actor=actor, feature=feature
    )


async def check_store_offers(session, event, *, keeping: int, adding: int, actor=None) -> None:
    """An event offering `adding` more of its club's store items, beside the
    `keeping` it already offers: the store (`merch`), and room for them with
    the event's handouts under `merch_items_per_event`, counted as
    check_merch_item counts. Taking offers away, or reordering them, is never
    refused. `event` may be one not saved yet (no id: no handouts), or one as
    it will be after an edit (its new date decides the grandfathering)."""
    if adding <= 0:
        return
    await require_feature(session, event.club_id, "merch", actor=actor, event=event)
    if not enforcing() or bypassed(actor) or _exempt("merch_items_per_event", event):
        return
    await require_within(
        session, event.club_id, "merch_items_per_event",
        used=await swag_items(session, getattr(event, "id", None)) + keeping, adding=adding, actor=actor, feature="merch",
    )


async def duplicate_allowances(session, club_id: Optional[int], *, actor=None) -> dict:
    """What a copy of an event may carry over. The copy is a new event, never
    grandfathered, so it gets only what the plan allows: the store switched
    on and its items offered with `merch`, the handouts (and their switch)
    with `swag`, at most `merch_items_per_event` of the two together, the ID
    card designs with `badges`, the report's recipients with `report`."""
    return {
        "merch": await allows(session, club_id, "merch", actor=actor),
        "swag": await allows(session, club_id, "swag", actor=actor),
        "badges": await allows(session, club_id, "badges", actor=actor),
        "report": await allows(session, club_id, "report", actor=actor),
        "merch_items_per_event": await cap(session, club_id, "merch_items_per_event", actor=actor),
    }


async def check_template(session, club_id: int, template: Optional[str], *, actor=None) -> None:
    """A website template beyond the first N in catalogue order (see
    `template_order`) needs a plan with more."""
    order = template_order()
    if not template or template not in order:
        return
    await require_within(
        session, club_id, "website_templates", used=order.index(template), actor=actor, template=template, feature="website"
    )


@dataclass
class RegistrationRoom:
    """What a registration's plan check found, for the steps that follow it
    once the registration holds the event's row (registration.service).

    `event_cap`: the plan's per-event cap, when one applies. On a free event
    the registration's own atomic UPDATE of the event row enforces it
    exactly, against `going_count` (on a paid event it counts paid places,
    which a registration does not change).
    `month_cap` / `month_used`: the club's monthly cap and what it had used
    when checked; `going_seen`: the event's going_count then. See
    `recheck_month`."""

    ent: object
    event_cap: Optional[int] = None
    month_cap: Optional[int] = None
    month_used: int = 0
    going_seen: int = 0


#: How close to the monthly cap a registration has to be (allowing for the
#: ones made to the same event since it was checked) to be counted again
#: under the event's lock.
MONTH_RECOUNT_MARGIN = 20


async def refuse_registration(session, event, ent, limit_key: str, *, cap: int, used: int, organiser: bool) -> CustomHTTPException:
    """The refusal for a registration past `limit_key`, the owner told."""
    refusal = await limit_reached(session, ent, limit_key, cap=cap, used=used)
    notify_paused(
        club_id=event.club_id,
        limit_key=limit_key,
        event_name=event.name,
        message=refusal.message,
        plan_name=ent.plan.name if ent.plan is not None else None,
        required_plan_name=refusal.errors.get("required_plan_name"),
    )
    return refusal if organiser else registrations_closed()


async def check_registration(session, event, *, organiser: bool = False, actor=None) -> Optional[RegistrationRoom]:
    """A new registration, from any path. The plan's per-event cap stacks on
    the organiser's own `max_participants` (whichever is lower holds; theirs
    is checked first and keeps its own answer), and the club-wide monthly cap
    on top. A grandfathered event is capped by neither: a registration to it
    is never refused, though it still counts in the month's `usage` (and so
    against the club's other events).

    An attendee is told only that registrations are closed (409); the
    organiser adding someone by hand, importing or at the door gets the
    plan's reason (403). Either way the club's owner is emailed.

    Made before the registration takes the event's row, so the row is held
    only for the insert. Returns what the steps after need to keep the caps
    exact under concurrency (RegistrationRoom), or None when no cap applies."""
    if not enforcing() or event.club_id is None or bypassed(actor) or grandfathered(event):
        return None
    ent = await entitlements(session, event.club_id)
    room = RegistrationRoom(ent=ent, going_seen=int(event.going_count or 0))
    for limit_key in ("registrations_per_event", "registrations_per_month"):
        limit = ent.limit(limit_key)
        if limit is None:
            continue
        if limit_key == "registrations_per_event":
            used = await event_registrations(session, event)
            room.event_cap = limit
        else:
            used = await count(session, event.club_id, limit_key)
            room.month_cap, room.month_used = limit, used
        if used + 1 <= limit:
            continue
        raise await refuse_registration(session, event, ent, limit_key, cap=limit, used=used, organiser=organiser)
    return room


async def recheck_month(session, event, room: Optional[RegistrationRoom], *, going_now: int, organiser: bool) -> None:
    """The monthly cap again, now the registration holds the event's row,
    when the room left could have gone since `check_registration`: only near
    the cap, so the count stays off the lock the rest of the time.

    Registrations to this event since then are the ones the check could not
    see (`going_now - going_seen`); each holds the row in turn, so the count
    made here sees every one of them, as the check made under the lock did
    before. Registrations to the club's other events meanwhile were never
    serialised with this one and still are not."""
    if room is None or room.month_cap is None:
        return
    since_check = max(0, going_now - room.going_seen)
    if room.month_cap - room.month_used > since_check + MONTH_RECOUNT_MARGIN:
        return
    used = await count(session, event.club_id, "registrations_per_month")
    if used + 1 <= room.month_cap:
        return
    # The caller's transaction ends with the refusal (the request's session
    # rolls back as it closes), and with it the hold on the event's row.
    raise await refuse_registration(session, event, room.ent, "registrations_per_month", cap=room.month_cap, used=used, organiser=organiser)


# ---------------------------------------------------------------------------
# Telling the club's owner registrations are paused
# ---------------------------------------------------------------------------

#: Sends started and not finished, held so the loop does not drop them.
_pending: set = set()
#: (club, limit) -> when this process last knew the owner was told.
_told: dict = {}


def reset() -> None:
    """Forget what this process remembers (for tests)."""
    _told.clear()
    _feed.clear()


def schedule(coro) -> None:
    """Run `coro` after the request, on the loop. Swapped by tests."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for every send started (for tests)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


@asynccontextmanager
async def session_scope():
    """A session of its own: the refused request's transaction is rolled back,
    and the record that the owner was told has to outlive it. Swapped by tests."""
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


def notify_paused(**payload) -> None:
    key = (payload["club_id"], payload["limit_key"])
    told = _told.get(key)
    if told is not None and datetime.now(timezone.utc) - told < NOTIFY_EVERY:
        return
    schedule(_tell_owner(**payload))


async def _tell_owner(*, club_id: int, limit_key: str, event_name: str, message: str, plan_name: Optional[str], required_plan_name: Optional[str]) -> None:
    """Email the club's own account that registrations are paused, unless it
    was told about this limit in the last day.

    The audit log is the record of having told it: durable, shared by every
    worker, and something an admin can read. A transaction-scoped advisory
    lock stops two refusals at once from both sending."""
    from sqlalchemy import select, text

    from app.api.audit.models import AdminAuditLogs
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users
    from app.core.email import email as email_mod

    now = datetime.now(timezone.utc)
    action = f"{PAUSED_ACTION}:{limit_key}"
    try:
        async with session_scope() as s:
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"{action}:{club_id}"})
            recent = await s.scalar(
                select(AdminAuditLogs.id)
                .where(
                    AdminAuditLogs.action == action,
                    AdminAuditLogs.entity_type == "club",
                    AdminAuditLogs.entity_id == str(club_id),
                    AdminAuditLogs.created_at >= now - NOTIFY_EVERY,
                )
                .limit(1)
            )
            if recent is not None:
                # Nothing written; closing the session ends the transaction
                # and with it the lock.
                _told[(club_id, limit_key)] = now
                return
            club = await s.get(Clubs, club_id)
            owner = await s.get(Users, club.user_id) if club is not None and club.user_id else None
            to = (getattr(owner, "email", None) or getattr(club, "contact_email", None) or "").strip()
            s.add(
                AdminAuditLogs(
                    action=action,
                    entity_type="club",
                    entity_id=str(club_id),
                    summary=f"Registrations paused for {club.name if club else club_id}: {limit_key} reached"[:255],
                    details={"limit": limit_key, "event": event_name, "plan": plan_name, "emailed": to or None},
                )
            )
            await s.commit()
            _told[(club_id, limit_key)] = now
        if not to:
            return
        subject, text_body, html_body = paused_email(
            club_name=club.name if club else "Your club",
            event_name=event_name,
            message=message,
            required_plan_name=required_plan_name,
        )
        await asyncio.to_thread(email_mod.send_email, recipients=[to], subject=subject, text=text_body, html=html_body)
    except Exception:
        logger.exception("Could not tell club %s its registrations are paused", club_id)


def paused_email(*, club_name: str, event_name: str, message: str, required_plan_name: Optional[str]) -> tuple[str, str, str]:
    import html

    url = upgrade_url()
    upgrade = f"Upgrade to {required_plan_name} to open them again" if required_plan_name else "See your plan's options"
    subject = f"Registrations are paused for {event_name}"
    text_body = (
        f"Hi {club_name},\n\n"
        f"New registrations for {event_name} are paused: {message}\n"
        "Guests trying to register are told registrations are closed for now. "
        "Everyone already registered keeps their ticket.\n\n"
        f"{upgrade}: {url}\n\n"
        "We send this at most once a day.\n"
    )
    esc = html.escape
    html_body = (
        f"<p>Hi {esc(club_name)},</p>"
        f"<p>New registrations for <strong>{esc(event_name)}</strong> are paused: {esc(message)}</p>"
        "<p>Guests trying to register are told registrations are closed for now. "
        "Everyone already registered keeps their ticket.</p>"
        f"<p><a href=\"{esc(url)}\">{esc(upgrade)}</a></p>"
        "<p>We send this at most once a day.</p>"
    )
    return subject, text_body, html_body
