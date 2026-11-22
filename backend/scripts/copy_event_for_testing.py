"""Make a test copy of an existing event, in a new test club, so every feature
can be tried without touching the real event.

    # What it would do: counts per table, nothing written (the default).
    docker compose exec -T backend python scripts/copy_event_for_testing.py \\
        --source devfest-2026-abc123 --club-email me@example.com

    # Do it.
    docker compose exec -T backend python scripts/copy_event_for_testing.py \\
        --source devfest-2026-abc123 --club-email me@example.com --confirm

    # Show or hide the test club on the public site and in search (see below).
    ... --set-visible on  --confirm
    ... --set-visible off --confirm

    # Take the test club, its event and everything made on them away again.
    ... --remove            # a dry run: counts per table
    ... --remove --confirm

Piped instead of mounted: `python - < copy_event_for_testing.py`, with the
arguments in the environment variable COPY_EVENT_ARGS (read when the command
line has none), e.g. COPY_EVENT_ARGS="--source 12 --club-email me@example.com".

What it makes, all named with MARKER:

- a club (verified, so its dashboard login works) with a login for --club-email
  and a password nobody knows: the owner sets theirs with "Forgot password?" on
  the club dashboard's login page;
- a subscription with no charge (an admin's complimentary grant) to the highest
  plan that has every feature flag;
- an event, a copy of the source's setup: the ticket types, the form, the review
  settings, every email and design, the door and ID card settings, the doors, the
  live room's look and rounds (as drafts), the quest, the games, the posters, the
  merchandise (stock reset), the partners, the agenda;
- three invented speakers, used in the copied agenda in place of the source's;
- two invented posts and a note.

Never copied: registrations, requests, waiting lists, payments, check-ins,
deliveries, certificates issued, print jobs, feedback, anything a guest earned
or made, the kiosk devices, followers, the team, co-hosts, or any person's data.
Pictures are not copied: a row names the same stored file the source does, and
nothing here ever deletes a stored file.

Why a script rather than the API: creating an event through the API notifies the
club's followers and the people whose interests match, with real push messages.
Everything here is written straight into the tables, and no notification code
runs.

Hiding. The platform has no "private event" flag: whether the public may open an
event page, register, or open the live room all hang on the club being *listed*
(verified, not deleted, its organisation not blocked), and so do discovery,
search and the sitemaps. The test club is therefore made inside a blocked
organisation (the only lever that keeps a verified club unlisted): it is on none
of the public lists, but the public event page, the registration form and the
live room's guest side answer "not found" too. The club dashboard, the door, the
kiosk, the designs, the ticket links and the app's own screens work as usual.
To try the public pages, run --set-visible on (the event then appears on the
public site, search and the app until --set-visible off).

Removal does not rely on a list of ids: it follows the foreign keys from the
test club's events, the club, its account and its speakers, so what the owner
made while testing (registrations, check-ins, messages) goes too. It refuses
when money records exist, and it only runs on a club whose name carries MARKER.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shlex
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeDecorator

sys.path.insert(0, ".")

from app.api.clubs.models import ClubInterestsLink, Clubs, Notes  # noqa: E402
from app.api.closeout.models import EventStatus  # noqa: E402
from app.api.live.models import LivePolls, LiveQuizzes, PollStatus, QuizStatus  # noqa: E402
from app.api.orgs.models import Organizations, OrgTypes  # noqa: E402
from app.api.speakers import service as speakers_service  # noqa: E402
from app.api.subscriptions.models import (  # noqa: E402
    FEATURE_KEYS,
    BillingCycle,
    Plans,
)
from app.api.subscriptions.service import grant_complimentary  # noqa: E402
from app.api.threads.models import ClubThreads  # noqa: E402
from app.api.users.models import SigninProviders, UserTypes, Users  # noqa: E402
from app.core.auth.authentication import get_password_hash  # noqa: E402
from app.core.public_urls import event_url  # noqa: E402
from app.core.utils.keys import generate_slug  # noqa: E402
from app.db import registry  # noqa: E402,F401 (registers every model, so the metadata is complete)
from app.db.base import AbstractSQLModel  # noqa: E402

MARKER = "[TEST]"
KOLKATA = ZoneInfo("Asia/Kolkata")
TABLES = AbstractSQLModel.metadata.tables

#: Tables whose rows a copy may point at without mapping them: reference data
#: shared by the whole platform.
SHARED_PARENTS = frozenset({"interests", "event_categories", "interest_categories", "plans"})

#: Columns the database keeps by trigger; never copied.
TRIGGER_KEPT = frozenset({"search_text", "geocell"})

#: Where the invented test venue is (Thiruvananthapuram).
VENUE_POINT = (8.5241, 76.9366)

SPEAKERS = (
    dict(
        name="Meera Quillfeather", designation="Principal Engineer, Northwind Labs (made up)",
        bio="A made-up speaker for the test event. Builds developer tools and likes tiny, fast feedback loops.",
        talk="Shipping small, shipping often",
    ),
    dict(
        name="Tobias Wrenfield", designation="Developer Advocate, Lanternworks (made up)",
        bio="A made-up speaker for the test event. Explains hard things with small demos.",
        talk="From demo to production without the drama",
    ),
    dict(
        name="Ishita Moonbridge", designation="Design Engineer, Paperkite Studio (made up)",
        bio="A made-up speaker for the test event. Works where design and code meet.",
        talk="Interfaces that feel fast",
    ),
)

THREADS = (
    "Welcome to the test club. Nothing here is real: it exists so every screen of the dashboard can be tried without touching a real event.",
    "Reminder: the test event is a copy. Register test guests, check them in, print a card, run the live room. Remove it all when you are done.",
)
NOTE = ("Test notice", "This club and its event are a test copy. Everything in it can be removed with one command.")

#: Nullable children of the club's account that are the account's own rows.
USER_OWNED = frozenset({
    "user_sessions", "notification_settings", "user_device_tokens", "user_profiles", "user_interests",
    "email_verification_codes", "password_reset_codes", "mcp_grants", "mcp_auth_codes", "mcp_tokens",
    "sign_in_attempts",
})


class Abort(Exception):
    """Something is not as the script needs it; nothing is written."""


# ───────────────────────────── Options ─────────────────────────────


@dataclass
class Options:
    source: str = ""
    club_name: str = f"{MARKER} MyOtherApp Test Club"
    club_email: str = ""
    event_name: Optional[str] = None
    starts: Optional[datetime] = None
    hours: float = 9.0
    venue_name: str = "Test Hall (not a real venue)"
    venue_address: str = "Test Road, Thiruvananthapuram, Kerala"
    plan: Optional[str] = None
    visible: bool = False


@dataclass
class Report:
    counts: Counter = field(default_factory=Counter)
    club_id: Optional[int] = None
    club_slug: str = ""
    event_id: Optional[int] = None
    event_slug: str = ""
    plan_key: str = ""
    missing_features: list = field(default_factory=list)
    paid_ticket_types: int = 0
    visible: bool = False

    def total(self) -> int:
        return sum(self.counts.values())


def with_marker(name: str, limit: int) -> str:
    name = (name or "").strip()
    if not name.startswith(MARKER):
        name = f"{MARKER} {name}"
    return name[:limit]


def default_start() -> datetime:
    tomorrow = datetime.now(KOLKATA) + timedelta(days=1)
    return tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)


def parse_start(raw: Optional[str]) -> datetime:
    if not raw:
        return default_start()
    when = datetime.fromisoformat(raw)
    return when.replace(tzinfo=KOLKATA) if when.tzinfo is None else when


# ───────────────────────────── Copying rows ─────────────────────────────


def _is_storage(column) -> bool:
    kind = column.type
    return isinstance(kind, TypeDecorator) and type(kind).__module__.startswith("app.core.storage")


def _is_json(column) -> bool:
    name = type(column.type).__name__.upper()
    if name in ("JSON", "JSONB"):
        return True
    item = getattr(column.type, "item_type", None)
    return item is not None and type(item).__name__.upper() in ("JSON", "JSONB")


def _readable(table, skip=()):
    """The table's columns as plain values: a stored-file column as its key,
    not the object that signs links for it."""
    out = []
    for c in table.columns:
        if c.name in TRIGGER_KEPT or c.name in skip:
            continue
        # By name, as text: the column's own type is a picture type that hands
        # back an object with links in it, and cannot be part of a plain select.
        out.append(sa.literal_column(f'"{table.name}"."{c.name}"', sa.String).label(c.name) if _is_storage(c) else c)
    return out


class Copier:
    """Copies rows between tables, remapping every id they hold."""

    def __init__(self, session: AsyncSession, now: datetime):
        self.session = session
        self.now = now
        #: map name -> {old id: new id}
        self.maps: dict[str, dict] = defaultdict(dict)
        self.counts: Counter = Counter()

    # -- reading --

    async def rows(self, name: str, *where, order=None, skip=()) -> list[dict]:
        table = TABLES[name]
        query = select(*_readable(table, skip)).where(*where)
        if "is_deleted" in table.c:
            query = query.where(table.c.is_deleted == False)  # noqa: E712
        if order is not None:
            query = query.order_by(*order)
        return [dict(r._mapping) for r in (await self.session.execute(query)).all()]

    # -- id remapping --

    def uuid_lookup(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for mapping in self.maps.values():
            for old, new in mapping.items():
                if not isinstance(old, int):
                    out[str(old)] = str(new)
        return out

    def remap_json(self, value):
        lookup = self.uuid_lookup()
        types = self.maps.get("ticket_type", {})

        def walk(v, key=None):
            if isinstance(v, str):
                return lookup.get(v, v)
            if isinstance(v, dict):
                return {k: walk(x, k) for k, x in v.items()}
            if isinstance(v, list):
                if key == "ticket_type_ids":
                    return [types.get(x, x) for x in v]
                return [walk(x, key) for x in v]
            if key == "ticket_type_id" and isinstance(v, int) and not isinstance(v, bool):
                return types.get(v, v)
            return v

        return walk(value)

    # -- writing --

    async def copy(
        self, name: str, where=(), *, fk: Optional[dict] = None, set_: Optional[dict] = None, skip=(),
        key: Optional[str] = None, order=None, rows: Optional[list] = None,
    ) -> int:
        """Copy the rows of `name` matching `where` (or `rows` as given).

        fk:   column -> map name: the column's old id becomes its new one.
        set_: column -> value, or a function of the old row: what the copy holds instead.
        skip: columns left to their defaults.
        key:  a map name to record old id -> new id under.
        """
        table = TABLES[name]
        fk, set_ = fk or {}, set_ or {}
        source_rows = rows if rows is not None else await self.rows(name, *where, order=order)
        pk = list(table.primary_key.columns)
        single_pk = pk[0] if len(pk) == 1 else None
        made = 0
        for old in source_rows:
            values: dict[str, Any] = {}
            for c in table.columns:
                if c.name in TRIGGER_KEPT or c.name in skip or c.name not in old:
                    continue
                if c.name in ("created_at", "updated_at"):
                    continue
                if c is single_pk and c.name not in fk and c.name not in set_:
                    continue
                if c.name in set_:
                    given = set_[c.name]
                    values[c.name] = given(old) if callable(given) else given
                    continue
                value = old[c.name]
                if c.name in fk and value is not None:
                    mapping = self.maps[fk[c.name]]
                    if value not in mapping:
                        raise Abort(f"{name}.{c.name}: {value} is not among the copied {fk[c.name]} rows")
                    value = mapping[value]
                elif value is not None and c.foreign_keys:
                    parents = {f.column.table.name for f in c.foreign_keys}
                    if not parents <= SHARED_PARENTS:
                        raise Abort(f"{name}.{c.name} points at {', '.join(sorted(parents))} and is not mapped")
                if _is_json(c) and value is not None:
                    value = self.remap_json(value)
                values[c.name] = value
            result = await self.session.execute(table.insert().values(**values).returning(*pk))
            new = result.one()
            if key and single_pk is not None:
                self.maps[key][old[single_pk.name]] = new[0]
            self.counts[name] += 1
            made += 1
        return made


# ───────────────────────────── Finding things ─────────────────────────────


def same_name(column):
    return sa.func.lower(sa.func.replace(column, " ", ""))


async def find_source(session, ref: str) -> dict:
    """The source event's row, as plain values."""
    table = TABLES["events"]
    ref = (ref or "").strip()
    if not ref:
        raise Abort("--source is needed: an event's id or slug")
    condition = table.c.id == int(ref) if ref.isdigit() and int(ref) < 2**31 else table.c.slug == ref
    row = (await session.execute(
        select(*_readable(table)).where(condition, table.c.is_deleted == False)  # noqa: E712
    )).first()
    if row is None and not ref.isdigit():
        from app.api.slugs.service import event_slug_matches

        row = (await session.execute(
            select(*_readable(table)).where(event_slug_matches(ref), table.c.is_deleted == False)  # noqa: E712
        )).first()
    if row is None:
        raise Abort("the source event was not found")
    return dict(row._mapping)


async def pick_plan(session, key: Optional[str]) -> tuple[Plans, list[str]]:
    """The plan the test club gets, and the feature flags it lacks.

    A named plan as asked. Otherwise the plan with the most flags on (every one,
    when there is one), and of those the dearest: the top of the ladder."""
    from sqlalchemy.orm import selectinload

    plans = list((await session.scalars(
        select(Plans).options(selectinload(Plans.features), selectinload(Plans.limits))
        .where(Plans.is_deleted == False)  # noqa: E712
    )).all())
    if key:
        chosen = next((p for p in plans if p.key == key), None)
        if chosen is None:
            raise Abort(f"there is no plan with the key {key!r}")
    else:
        def score(p):
            on = sum(bool(getattr(p.features, k, False)) for k in FEATURE_KEYS) if p.features else 0
            visibility = getattr(p.visibility, "value", p.visibility)
            return (visibility != "legacy", on, p.price_monthly_paise or 0, p.sort_order or 0)

        if not plans:
            raise Abort("the platform has no plans")
        chosen = max(plans, key=score)
    missing = [k for k in FEATURE_KEYS if not (chosen.features and getattr(chosen.features, k, False))]
    return chosen, missing


async def club_login_taken(session, email: str) -> bool:
    from app.api.users.service import same_email

    return await session.scalar(
        select(Users.id).where(same_email(Users.email, email), Users.user_type == UserTypes.club, Users.is_deleted == False).limit(1)  # noqa: E712
    ) is not None


# ───────────────────────────── The copy ─────────────────────────────


async def copy_event(session: AsyncSession, opts: Options, *, now: Optional[datetime] = None) -> Report:
    """Make the club and the event. Writes inside the caller's transaction and
    commits nothing."""
    now = now or datetime.now(timezone.utc)
    report = Report(visible=opts.visible)
    source = await find_source(session, opts.source)
    if source["club_id"] is None:
        raise Abort("the source event has no club")
    if not (opts.club_email or "").strip() or "@" not in opts.club_email:
        raise Abort("--club-email is needed: the login email for the test club")
    email = opts.club_email.strip()

    club_name = with_marker(opts.club_name, 100)
    event_name = with_marker(opts.event_name or f"{MARKER} {source['name']}", 100)
    starts = (opts.starts or default_start()).astimezone(timezone.utc)
    if starts <= now:
        raise Abort("--starts must be in the future")
    if not (0 < opts.hours <= 72):
        raise Abort("--hours must be more than 0 and at most 72")
    delta = starts - source["event_datetime"]

    taken = await session.scalar(
        select(Clubs.id).where(same_name(Clubs.name) == same_name(sa.literal(club_name)), Clubs.is_deleted == False).limit(1)  # noqa: E712
    )
    if taken is not None:
        raise Abort("a club with that name exists already: remove it first, or pass another --club-name")
    if await club_login_taken(session, email):
        raise Abort("that email already has a club login: pass another --club-email")

    plan, missing = await pick_plan(session, opts.plan)
    report.plan_key, report.missing_features = plan.key, missing

    copier = Copier(session, now)
    src_club_id = source["club_id"]

    # ── the account, the club, its plan ──
    password_hash = get_password_hash(secrets.token_urlsafe(48))
    user = Users(
        full_name=club_name, email=email, password=password_hash, user_type=UserTypes.club,
        provider=SigninProviders.email, email_verified_at=now,
    )
    from app.api.users.service import generate_username

    await generate_username(user, session)
    session.add(user)
    await session.flush()

    src_club = (await session.execute(
        select(*_readable(TABLES["clubs"])).where(TABLES["clubs"].c.id == src_club_id)
    )).one()._mapping
    club = Clubs(
        name=club_name, slug=generate_slug(club_name), user_id=user.id, is_verified=True,
        about="A private test club. Everything in it is a copy made for testing, and none of it is real.",
        tagline="A test copy, not a real club",
        brand_primary=src_club["brand_primary"], brand_secondary=src_club["brand_secondary"],
        contact_email=email, location_name="Thiruvananthapuram",
    )
    session.add(club)
    await session.flush()
    # The look: the same stored pictures as the source club, by key.
    clubs_table = TABLES["clubs"]
    await session.execute(clubs_table.update().where(clubs_table.c.id == club.id).values(logo=src_club["logo"], cover=src_club["cover"]))
    report.club_id, report.club_slug = club.id, club.slug
    copier.maps["club"][src_club_id] = club.id
    copier.counts["users"] += 1
    copier.counts["clubs"] += 1

    # Hidden: inside a blocked organisation (see the module's note).
    if not opts.visible:
        await set_visible(session, club, False)
        copier.counts["organizations"] += 1

    await grant_complimentary(
        session, club_id=club.id, plan=plan, cycle=BillingCycle.monthly,
        reason=f"{MARKER} test copy: no charge", ends_at=None, granted_by=None, now=now,
    )
    copier.counts["club_subscriptions"] += 1

    await copier.copy(
        "clubs_interests_link", (TABLES["clubs_interests_link"].c.club_id == src_club_id,), fk={"club_id": "club"},
    )

    # ── the club's own settings the event uses ──
    club_scope = lambda t: (TABLES[t].c.club_id == src_club_id, TABLES[t].c.event_id.is_(None))  # noqa: E731
    await copier.copy("club_partners", (TABLES["club_partners"].c.club_id == src_club_id,), fk={"club_id": "club"}, key="club_partner")

    # Designs the event can fall back on: what it points at, and each kind's club default.
    design_tables = {"ticket_templates": "ticket_template_id", "certificate_templates": "certificate_template_id", "badge_templates": None, "agenda_templates": None}
    for tname, event_column in design_tables.items():
        t = TABLES[tname]
        wanted = [t.c.is_default == True]  # noqa: E712
        if event_column and source[event_column] is not None:
            wanted.append(t.c.id == source[event_column])
        await copier.copy(tname, (t.c.club_id == src_club_id, t.c.event_id.is_(None), sa.or_(*wanted)), fk={"club_id": "club"}, key=f"design:{tname}")

    # The club's email library (the event's own copies point back at it).
    await copier.copy(
        "email_templates", club_scope("email_templates"), fk={"club_id": "club"},
        set_={"created_by": None, "source_id": None, "due_at": None}, key="email_library",
    )

    # The club's store items this event offers, or that belong to it.
    offers = await copier.rows("event_store_offers", TABLES["event_store_offers"].c.event_id == source["id"])
    items_t = TABLES["event_store_items"]
    offered = [o["item_id"] for o in offers]
    item_rows = await copier.rows(
        "event_store_items", items_t.c.club_id == src_club_id,
        sa.or_(items_t.c.id.in_(offered) if offered else sa.false(), items_t.c.event_id == source["id"]),
    )

    # ── the event ──
    new_event = await copy_event_row(copier, source, club, event_name, starts, opts, email)
    report.event_id, report.event_slug = new_event["id"], new_event["slug"]
    copier.maps["event"][source["id"]] = new_event["id"]
    copier.counts["events"] += 1
    event_fk = {"event_id": "event"}

    await copier.copy("event_interests_link", (TABLES["event_interests_link"].c.event_id == source["id"],), fk=event_fk)

    await copier.copy(
        "ticket_types", (TABLES["ticket_types"].c.event_id == source["id"],), fk=event_fk, key="ticket_type",
        set_={
            "taken_count": 0,
            "sales_start": lambda r: (now - timedelta(hours=1)) if r["sales_start"] else None,
            "sales_end": lambda r: starts if r["sales_end"] else None,
        },
    )
    report.paid_ticket_types = int((await session.scalar(
        select(sa.func.count()).select_from(TABLES["ticket_types"]).where(
            TABLES["ticket_types"].c.event_id == report.event_id, TABLES["ticket_types"].c.price > 0,
            TABLES["ticket_types"].c.is_deleted == False,  # noqa: E712
        )
    )) or 0)

    await copier.copy("waitlist_settings", (TABLES["waitlist_settings"].c.event_id == source["id"],), fk=event_fk)
    await copier.copy(
        "discount_codes", (TABLES["discount_codes"].c.event_id == source["id"],), fk={**event_fk, "club_id": "club"}, key="discount_code",
        set_={
            "created_by_id": None,
            "starts_at": lambda r: (now - timedelta(hours=1)) if r["starts_at"] else None,
            "ends_at": lambda r: starts if r["ends_at"] else None,
            "ticket_type_ids": lambda r: [copier.maps["ticket_type"][i] for i in (r["ticket_type_ids"] or []) if i in copier.maps["ticket_type"]] or None,
        },
    )

    # Doors: the same entrances, stations and desks, without the links staff were given.
    await copier.copy(
        "doors", (TABLES["doors"].c.event_id == source["id"],), fk=event_fk, key="door", order=(TABLES["doors"].c.sort_order,),
        set_={"link_hash": None, "link_prefix": None, "link_expires_at": None},
    )

    # Designs of the event itself (every role), and the emails.
    for tname in design_tables:
        t = TABLES[tname]
        await copier.copy(
            tname, (t.c.event_id == source["id"],), fk=event_fk, key=f"design:{tname}:event",
            set_={"club_id": lambda r: copier.maps["club"].get(r["club_id"])},
        )
    await copy_email_templates(copier, source)

    # Sponsors and partners.
    await copier.copy(
        "event_partners", (TABLES["event_partners"].c.event_id == source["id"],), fk=event_fk,
        set_={"club_partner_id": lambda r: copier.maps["club_partner"].get(r["club_partner_id"])},
    )
    await copier.copy("event_files", (TABLES["event_files"].c.event_id == source["id"],), fk=event_fk)

    # The registration form's last revision.
    revisions = await copier.rows(
        "event_form_revisions", TABLES["event_form_revisions"].c.event_id == source["id"],
        order=(TABLES["event_form_revisions"].c.version.desc(),),
    )
    await copier.copy("event_form_revisions", rows=revisions[:1], fk=event_fk, set_={"changed_by_user_id": None, "changed_by_member_id": None})

    # The survey: its questions, unsent.
    await copier.copy(
        "feedback_surveys", (TABLES["feedback_surveys"].c.event_id == source["id"],), fk=event_fk,
        set_={"sent_at": None, "sent_count": 0, "due_at": None},
    )

    await copy_merch(copier, source, club.id, new_event["id"], item_rows, offers)
    await copy_speakers_and_agenda(session, copier, source, club, new_event["id"], delta)
    await copy_live(copier, source)
    await copy_quests(copier, source, delta, now)
    await copy_games(copier, source)
    await copy_posters(copier, source)
    await make_posts(session, copier, club, user)

    report.counts = copier.counts
    return report


async def copy_event_row(copier: Copier, source: dict, club: Clubs, name: str, starts: datetime, opts: Options, email: str) -> dict:
    """The event itself: every column of the source's, bar the ones about a
    place, a time, a count or a person."""
    maps = copier.maps
    design_ticket = maps.get("design:ticket_templates", {})
    design_cert = maps.get("design:certificate_templates", {})
    now = copier.now

    def template(mapping, column):
        old = source[column]
        return mapping.get(old) if old is not None else None

    values = {c: source[c] for c in source if c not in TRIGGER_KEPT}
    for column in ("id", "created_at", "updated_at"):
        values.pop(column, None)
    values.update(
        name=name, slug=generate_slug(name), club_id=club.id,
        event_datetime=starts, duration=opts.hours,
        location_name=f"{opts.venue_name}, {opts.venue_address}".strip(", "), location_link=None,
        lat=VENUE_POINT[0], lng=VENUE_POINT[1], geo_source="manual", link_checked_at=None,
        reg_startdate=now, reg_enddate=starts if source["reg_enddate"] is not None else None,
        # A count is a count of people: the copy has none yet.
        page_views=0, going_count=0, paid_count=0, popularity=0.0, admission_seq=0,
        # A person: the source's organiser. The test club's login stands in.
        contact_name="Test organiser", contact_phone=None, contact_email=email, report_recipients=[],
        ticket_template_id=template(design_ticket, "ticket_template_id"),
        certificate_template_id=template(design_cert, "certificate_template_id"),
        status=EventStatus.published, status_clock=True, is_deleted=False, deleted_at=None,
    )
    if values.get("additional_details") is not None:
        values["additional_details"] = copier.remap_json(values["additional_details"])
    table = TABLES["events"]
    result = await copier.session.execute(table.insert().values(**values).returning(table.c.id, table.c.slug))
    row = result.one()
    return {"id": row[0], "slug": row[1]}


async def copy_email_templates(copier: Copier, source: dict) -> None:
    t = TABLES["email_templates"]
    library = copier.maps["email_library"]
    await copier.copy(
        "email_templates", (t.c.event_id == source["id"],), fk={"event_id": "event"}, key="email",
        set_={
            "club_id": copier.maps["club"][source["club_id"]], "created_by": None, "due_at": None,
            "source_id": lambda r: library.get(r["source_id"]),
        },
    )


async def copy_merch(copier: Copier, source: dict, club_id: int, event_id: int, item_rows: list, offers: list) -> None:
    mt, vt = TABLES["merch_items"], TABLES["merch_variants"]
    await copier.copy(
        "merch_items", (mt.c.event_id == source["id"],), fk={"event_id": "event"}, key="merch",
        set_={"collected_count": 0, "reserved_count": 0, "manual_handed_out": 0},
    )
    if copier.maps["merch"]:
        old_ids = list(copier.maps["merch"])
        await copier.copy(
            "merch_variants", (vt.c.item_id.in_(old_ids),), fk={"item_id": "merch"}, set_={"collected_count": 0, "reserved_count": 0},
        )

    # The store: the club's items this event sells, with nothing sold.
    st = TABLES["event_store_items"]
    await copier.copy(
        "event_store_items", rows=item_rows, key="store_item", fk={"club_id": "club"},
        set_={
            "event_id": lambda r: event_id if r["event_id"] == source["id"] else None,
            "sold_count": 0, "reserved_count": 0,
        },
    )
    if copier.maps["store_item"]:
        old_ids = list(copier.maps["store_item"])
        vt2 = TABLES["event_store_variants"]
        await copier.copy("event_store_variants", (vt2.c.item_id.in_(old_ids),), fk={"item_id": "store_item"}, set_={"sold_count": 0, "reserved_count": 0})
    wanted = [o for o in offers if o["item_id"] in copier.maps["store_item"]]
    await copier.copy("event_store_offers", rows=wanted, fk={"event_id": "event", "item_id": "store_item"})


async def copy_speakers_and_agenda(session, copier: Copier, source: dict, club: Clubs, event_id: int, delta: timedelta) -> None:
    """Three invented speakers, in the place of the source's, on the agenda."""
    # Straight into the table, not through the ORM: a flushed speaker would
    # queue a call to the public site's cache (app/core/site_revalidate).
    made: list[SimpleNamespace] = []
    speakers_table = TABLES["event_speakers"]
    for position, spec in enumerate(SPEAKERS):
        profile = await speakers_service.create_profile(
            session, name=spec["name"], headline=spec["designation"], bio=spec["bio"], club_id=club.id,
            city="Thiruvananthapuram",
        )
        new_id = await session.scalar(speakers_table.insert().values(
            event_id=event_id, name=spec["name"], designation=spec["designation"], bio=spec["bio"],
            talk_title=spec["talk"], display_order=position, speaker_profile_id=profile.id,
        ).returning(speakers_table.c.id))
        made.append(SimpleNamespace(id=new_id, name=spec["name"]))
        copier.counts["speaker_profiles"] += 1
        copier.counts["speaker_history"] += 1
        copier.counts["event_speakers"] += 1

    at, links = TABLES["agenda_items"], TABLES["agenda_item_speakers"]
    items = await copier.rows("agenda_items", at.c.event_id == source["id"], order=(at.c.sort_order, at.c.starts_at))
    old_links = await copier.rows("agenda_item_speakers", links.c.agenda_item_id.in_([i["id"] for i in items])) if items else []
    per_item = Counter(link["agenda_item_id"] for link in old_links)
    turn = 0
    chosen: dict = {}
    for item in items:
        count = per_item.get(item["id"]) or (1 if (item["speaker"] or "").strip() else 0)
        picks = [made[(turn + k) % len(made)] for k in range(min(count, len(made)))]
        turn += max(count, 0)
        chosen[item["id"]] = picks
    await copier.copy(
        "agenda_items", rows=items, fk={"event_id": "event"}, key="agenda",
        set_={
            "starts_at": lambda r: r["starts_at"] + delta,
            "ends_at": lambda r: (r["ends_at"] + delta) if r["ends_at"] else None,
            "speaker": lambda r: ", ".join(p.name for p in chosen[r["id"]]) or None,
            "speaker_photo_key": None,
        },
    )
    for item in items:
        for position, speaker in enumerate(chosen[item["id"]]):
            await session.execute(links.insert().values(
                agenda_item_id=copier.maps["agenda"][item["id"]], event_speaker_id=speaker.id, position=position,
            ))
            copier.counts["agenda_item_speakers"] += 1


async def copy_live(copier: Copier, source: dict) -> None:
    """The live room: its look, its rounds and polls as drafts. What guests
    asked, answered or posted does not come."""
    eid = source["id"]
    await copier.copy("live_themes", (TABLES["live_themes"].c.event_id == eid,), fk={"event_id": "event"})
    qt, pt = TABLES["live_quizzes"], TABLES["live_polls"]
    await copier.copy(
        "live_quizzes", (qt.c.event_id == eid,), fk={"event_id": "event"}, key="quiz", order=(qt.c.sort_order,),
        set_={
            "status": QuizStatus.draft, "join_code": None, "current_index": -1, "question_opened_at": None,
            "ended_at": None, "paused_at": None, "revealed_at": None, "asked": None,
        },
    )
    if copier.maps["quiz"]:
        qq = TABLES["live_quiz_questions"]
        await copier.copy("live_quiz_questions", (qq.c.quiz_id.in_(list(copier.maps["quiz"])),), fk={"quiz_id": "quiz"}, order=(qq.c.sort_order,))
    await copier.copy(
        "live_polls", (pt.c.event_id == eid,), fk={"event_id": "event"}, order=(pt.c.sort_order,),
        set_={"status": PollStatus.draft, "launched_at": None, "closed_at": None, "closes_at": None},
    )


async def copy_quests(copier: Copier, source: dict, delta: timedelta, now: datetime) -> None:
    """The quest: tasks, milestones and rewards. Nothing earned."""
    eid = source["id"]
    shift = lambda column: (lambda r: (r[column] + delta) if r[column] else None)  # noqa: E731
    await copier.copy(
        "quests", (TABLES["quests"].c.event_id == eid,), fk={"event_id": "event"}, key="quest",
        set_={"updated_by": None, "starts_at": shift("starts_at"), "ends_at": shift("ends_at")},
    )
    if not copier.maps["quest"]:
        return
    old = list(copier.maps["quest"])
    await copier.copy(
        "quest_tasks", (TABLES["quest_tasks"].c.quest_id.in_(old),), fk={"quest_id": "quest", "event_id": "event"}, key="quest_task",
        order=(TABLES["quest_tasks"].c.sort_order,),
        set_={
            "door_id": lambda r: copier.maps["door"][r["door_id"]] if r["door_id"] else None,
            "starts_at": shift("starts_at"), "ends_at": shift("ends_at"),
        },
    )
    await copier.copy("quest_milestones", (TABLES["quest_milestones"].c.quest_id.in_(old),), fk={"quest_id": "quest"}, key="quest_milestone")
    rewards = await copier.rows("quest_rewards", TABLES["quest_rewards"].c.quest_id.in_(old))
    await copier.copy(
        "quest_rewards", rows=[r for r in rewards if r["merch_item_id"] in copier.maps["merch"]],
        fk={"quest_id": "quest", "event_id": "event", "milestone_id": "quest_milestone", "merch_item_id": "merch"},
    )


async def copy_games(copier: Copier, source: dict) -> None:
    """The games' settings, card config, heroes, and the hunt with its points. Nothing played."""
    eid = source["id"]
    fk = {"event_id": "event"}
    await copier.copy("game_settings", (TABLES["game_settings"].c.event_id == eid,), fk=fk, set_={"player_seq": 0})
    await copier.copy("game_card_config", (TABLES["game_card_config"].c.event_id == eid,), fk=fk)
    await copier.copy("game_heroes", (TABLES["game_heroes"].c.event_id == eid,), fk=fk, key="hero", order=(TABLES["game_heroes"].c.number,))
    await copier.copy(
        "game_hunts", (TABLES["game_hunts"].c.event_id == eid,), fk=fk,
        set_={
            "status": "draft",
            "starts_at": None, "ends_at": None, "paused_at": None, "leader_registration_id": None,
        },
    )
    await copier.copy("game_hunt_points", (TABLES["game_hunt_points"].c.event_id == eid,), fk=fk, key="hunt_point", order=(TABLES["game_hunt_points"].c.number,))


async def copy_posters(copier: Copier, source: dict) -> None:
    """The "I'm attending" posters: settings, designs, and the size renders by key. No guest's poster."""
    eid = source["id"]
    fk = {"event_id": "event"}
    await copier.copy("poster_settings", (TABLES["poster_settings"].c.event_id == eid,), fk=fk, set_={"updated_by": None})
    await copier.copy("poster_designs", (TABLES["poster_designs"].c.event_id == eid,), fk=fk, set_={"updated_by": None})
    await copier.copy("poster_variants", (TABLES["poster_variants"].c.event_id == eid,), fk=fk)


async def make_posts(session, copier: Copier, club: Clubs, user: Users) -> None:
    for body in THREADS:
        session.add(ClubThreads(club_id=club.id, body=body, kind="post", notify_followers=False, created_by=user.id))
        copier.counts["club_threads"] += 1
    session.add(Notes(club_id=club.id, user_id=user.id, title=NOTE[0], note=NOTE[1]))
    copier.counts["notes"] += 1
    await session.flush()


# ───────────────────────────── Visibility ─────────────────────────────


def hidden_org_name(club_id: int) -> str:
    return f"{MARKER} hidden (club {club_id})"


async def set_visible(session, club: Clubs, visible: bool) -> None:
    """Show the club on the public site, or hide it again.

    Hidden is a blocked organisation (clubs.listed is false for a club under
    one); shown is no organisation. The row is kept apart by an exact name."""
    org = await session.scalar(select(Organizations).where(Organizations.name == hidden_org_name(club.id)).execution_options(include_deleted=True))
    if visible:
        club.org_id = None
    else:
        if org is None:
            org = Organizations(name=hidden_org_name(club.id), type=OrgTypes.other, is_verified=False, is_blocked=True)
            session.add(org)
            await session.flush()
        org.is_blocked = True
        club.org_id = org.id
    await session.flush()


async def marked_clubs(session, club_id: Optional[int] = None) -> list[Clubs]:
    query = select(Clubs).where(Clubs.name.like(f"{MARKER}%"), Clubs.is_deleted == False)  # noqa: E712
    if club_id is not None:
        query = query.where(Clubs.id == club_id)
    return list((await session.scalars(query.order_by(Clubs.id))).all())


async def the_test_club(session, club_id: Optional[int]) -> Clubs:
    clubs = await marked_clubs(session, club_id)
    if not clubs:
        raise Abort("no test club found" + (f" with id {club_id}" if club_id else ""))
    if len(clubs) > 1:
        raise Abort("more than one test club exists; pass --club-id with one of: " + ", ".join(str(c.id) for c in clubs))
    club = clubs[0]
    owner = await session.get(Users, club.user_id)
    if owner is None or not (owner.full_name or "").startswith(MARKER) or getattr(owner.user_type, "value", owner.user_type) != "club":
        raise Abort("the club's account is not a test account; refusing")
    return club


# ───────────────────────────── Removal ─────────────────────────────


def _children() -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for table in TABLES.values():
        for column in table.columns:
            for fk in column.foreign_keys:
                out[fk.column.table.name].append((table.name, column.name, fk.column.name))
    return out


CHILDREN = _children()


async def purge(session, name: str, where, counts: Counter, stack: tuple = ()) -> None:
    """Delete the rows of `name` matching `where`, and every row that points at
    them, following the foreign keys down. A pointer that would loop back is
    emptied instead."""
    table = TABLES[name]
    for child, column, ref in CHILDREN.get(name, ()):
        child_table = TABLES[child]
        child_column = child_table.c[column]
        inside = child_column.in_(select(table.c[ref]).where(where))
        if child == name or child in stack:
            if not child_column.nullable:
                raise Abort(f"{child}.{column} loops back and cannot be emptied")
            await session.execute(child_table.update().where(inside).values({column: None}))
            continue
        if name == "users":
            if child_column.nullable:
                await session.execute(child_table.update().where(inside).values({column: None}))
                continue
            if child not in USER_OWNED:
                found = await session.scalar(select(sa.func.count()).select_from(child_table).where(inside))
                if found:
                    raise Abort(f"the club's account is used by {child}; refusing")
        await purge(session, child, inside, counts, stack + (name,))
    deleted = (await session.execute(table.delete().where(where))).rowcount or 0
    if deleted:
        counts[name] += deleted


async def money_records(session, event_ids: list[int], club_id: int) -> list[str]:
    found = []
    for tname, column, ids in (
        ("ledger_entries", "event_id", event_ids), ("ledger_entries", "club_id", [club_id]),
        ("payment_orders", "event_id", event_ids), ("payment_logs", "event_id", event_ids),
    ):
        table = TABLES.get(tname)
        if table is None or column not in table.c or not ids:
            continue
        n = await session.scalar(select(sa.func.count()).select_from(table).where(table.c[column].in_(ids)))
        if n:
            found.append(f"{tname} ({n})")
    return found


async def remove_test_club(session: AsyncSession, club_id: Optional[int] = None) -> Counter:
    """Take the test club away with everything made in it. Commits nothing."""
    club = await the_test_club(session, club_id)
    counts: Counter = Counter()
    events_t = TABLES["events"]
    event_ids = list((await session.scalars(select(events_t.c.id).where(events_t.c.club_id == club.id))).all())
    owed = await money_records(session, event_ids, club.id)
    if owed:
        raise Abort("the test club has money records, which are never deleted here: " + ", ".join(owed))

    user_id, cid = club.user_id, club.id
    org_name = hidden_org_name(cid)
    # What no foreign key reaches.
    aliases = TABLES["slug_aliases"]
    counts["slug_aliases"] += (await session.execute(aliases.delete().where(
        sa.or_(sa.and_(aliases.c.kind == "event", aliases.c.target_id.in_(event_ids or [-1])), sa.and_(aliases.c.kind == "club", aliases.c.target_id == cid))
    ))).rowcount or 0

    for eid in event_ids:
        await purge(session, "events", events_t.c.id == eid, counts)
    profiles = TABLES["speaker_profiles"]
    await purge(session, "speaker_profiles", profiles.c.created_by_club_id == cid, counts)
    await purge(session, "clubs", TABLES["clubs"].c.id == cid, counts)
    await purge(session, "users", TABLES["users"].c.id == user_id, counts)

    orgs = TABLES["organizations"]
    org_id = await session.scalar(select(orgs.c.id).where(orgs.c.name == org_name))
    if org_id is not None:
        used = await session.scalar(select(sa.func.count()).select_from(TABLES["clubs"]).where(TABLES["clubs"].c.org_id == org_id))
        if not used:
            await purge(session, "organizations", orgs.c.id == org_id, counts)
    return counts


# ───────────────────────────── Running ─────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", help="the event to copy: its id or slug")
    p.add_argument("--club-name", default=f"{MARKER} MyOtherApp Test Club")
    p.add_argument("--club-email", help="the login email for the test club")
    p.add_argument("--event-name", help='default: "[TEST] " and the source\'s name')
    p.add_argument("--starts", help="ISO date and time (Asia/Kolkata unless it says otherwise); default tomorrow 10:00")
    p.add_argument("--hours", type=float, default=9.0, help="how long it runs (default 9)")
    p.add_argument("--venue-name", default=Options.venue_name)
    p.add_argument("--venue-address", default=Options.venue_address)
    p.add_argument("--plan", help="the plan key the test club gets (default: the highest plan with every feature)")
    p.add_argument("--visible", action="store_true", help="make the club public from the start (default: hidden, see the notes)")
    p.add_argument("--set-visible", choices=("on", "off"), help="show or hide an existing test club")
    p.add_argument("--club-id", type=int, help="which test club, with --remove or --set-visible, when there are several")
    p.add_argument("--remove", action="store_true", help="remove the test club, its event and everything made in them")
    p.add_argument("--confirm", action="store_true", help="write for real; without it nothing is written and counts are printed")
    return p


def print_counts(counts: Counter, heading: str) -> None:
    print(heading)
    for name in sorted(counts):
        print(f"  {name:32s} {counts[name]}")
    print(f"  {'total':32s} {sum(counts.values())}")


def print_report(report: Report, *, written: bool) -> None:
    from app.config import settings

    web = settings.PUBLIC_WEB_URL.rstrip("/")
    print_counts(report.counts, "Rows copied or made, by table:" if written else "Rows that would be copied or made, by table (nothing written):")
    print()
    plan_line = f"Plan: {report.plan_key} (no charge)"
    if report.missing_features:
        plan_line += f"; it lacks these features: {', '.join(report.missing_features)}"
    print(plan_line)
    if report.paid_ticket_types:
        print(f"Note: {report.paid_ticket_types} ticket type(s) are paid. A test payment would be a real one; keep to free tickets or the gateway's test mode.")
    if not written:
        print("\nDry run: nothing was written. Add --confirm to do it.")
        return
    print(f"\nTest club:  id {report.club_id}, handle {report.club_slug}")
    print(f"Test event: id {report.event_id}, slug {report.event_slug}")
    print(f"Dashboard:  {web}/events/{report.event_id}")
    print(f"Public:     {event_url(report.event_slug)}" + ("" if report.visible else "  (hidden: it answers 'not found' until --set-visible on)"))
    print(f"\nLogin: {web}/login with the --club-email address.")
    print("The password is not set. On that page choose \"Forgot password?\", enter the address, and type the code mailed there with a new password.")
    print("To remove everything again: --remove --confirm")


async def run(args: argparse.Namespace) -> int:
    from app.db.core import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        try:
            if args.remove:
                counts = await remove_test_club(session, args.club_id)
                print_counts(counts, "Rows deleted, by table:" if args.confirm else "Rows that would be deleted, by table (nothing written):")
                if args.confirm:
                    await session.commit()
                    print("\nRemoved. Stored pictures were not touched.")
                else:
                    await session.rollback()
                    print("\nDry run: nothing was deleted. Add --confirm to do it.")
                return 0
            if args.set_visible:
                club = await the_test_club(session, args.club_id)
                await set_visible(session, club, args.set_visible == "on")
                if args.confirm:
                    await session.commit()
                    print(f"The test club is now {'visible on the public site' if args.set_visible == 'on' else 'hidden'}.")
                else:
                    await session.rollback()
                    print("Dry run: nothing changed. Add --confirm.")
                return 0
            opts = Options(
                source=args.source or "", club_name=args.club_name, club_email=args.club_email or "", event_name=args.event_name,
                starts=parse_start(args.starts), hours=args.hours, venue_name=args.venue_name, venue_address=args.venue_address,
                plan=args.plan, visible=args.visible,
            )
            report = await copy_event(session, opts)
            if args.confirm:
                await session.commit()
            else:
                await session.rollback()
            print_report(report, written=args.confirm)
            return 0
        except Abort as stop:
            await session.rollback()
            print(f"Stopped, nothing written: {stop}", file=sys.stderr)
            return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv and os.environ.get("COPY_EVENT_ARGS"):
        argv = shlex.split(os.environ["COPY_EVENT_ARGS"])
    args = parser.parse_args(argv)
    if not args.remove and not args.set_visible and not args.source:
        parser.error("--source is needed")
    if not args.remove and not args.set_visible and not args.club_email:
        parser.error("--club-email is needed")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
