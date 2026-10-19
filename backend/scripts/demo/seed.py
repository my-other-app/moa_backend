"""Seed the demo: invented clubs, fully public, with everything a real club
has -- its page, team, partners, store, a custom ticket and certificate,
events past and upcoming, people registered, checked in, certified and
giving feedback, posts with likes and comments, followers -- and one main
demo user whose app is full of it.

    python -m scripts.demo.seed --main-email demo@demo.myotherapp.in \\
        --main-password '...' --club-password '...'

It reads every club in data/clubs/*.json and data/people.json (schema.py
says what they hold), checks them, and refuses before writing anything if a
rule is broken, or if any club already exists (`--force` seeds beside real
clubs, never over a demo club already there: remove that first with
scripts/demo/remove.py).

Everything goes through the app's own service functions wherever one
exists -- create_club, create_user, create_event, register_event, the
dashboard's design save, the store, admit, issue, submit_survey,
rate_event, follow_club, grant_complimentary -- so counters, admission
numbers, the ledger and the caches are what the app would have made. Rows
the app only ever writes from a person's browser (a post's likes, a live
poll's answers) are inserted as those paths insert them, and their
counters are left to the database's triggers.

All of it runs in quiet mode (quiet.py): no mail, push, alert or post
leaves, and no job is written for the API's runner to pick up. Every
invented address is at demo.myotherapp.in, which the mailer and the push
sender skip for good (settings.MAIL_SUPPRESS_DOMAINS), so the sweeps that
run after the seed -- reminders, surveys, billing -- never reach them
either. The safety checks at the end make sure of it.

Money: no Razorpay order is ever made. An upcoming event with a fee is
sold out (schema.py refuses one that is not), its places paid at the desk;
a past event's paid tickets and store orders were paid at the desk, as the
walk-up desk records them, with the ledger posting the desk records. The
store is shown only on events nobody can register for any more.

The credentials are printed at the end and written nowhere.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import random
import re
import sys
import time as clock
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from scripts.demo import images  # noqa: E402
from scripts.demo.schema import DEMO_DOMAIN, Club, DataError, Event, People, load  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

#: The schema's categories, as the event types the product ships are named.
CATEGORY_NAMES = {"Festival": "Fest", "Tournament": "Competition"}

#: The topic slugs of the production tree (migrations/versions/topics.py).
GENERIC_ANSWERS = [
    "Looking forward to it", "Heard about it from a friend", "Want to meet people who build things",
    "First time at one of these", "Came to the last one too", "Here to learn", "Curious, mostly",
    "To get better at this", "My seniors recommended it", "Saw the post on Instagram",
]


class Refused(Exception):
    """The seed will not run; the message says why."""


@dataclass
class Person:
    user_id: int
    full_name: str
    email: str
    username: str


@dataclass
class Report:
    clubs: list[dict] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    main_user: Optional[dict] = None


def ascii_slug(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", plain.lower()) or "guest"


def ist(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST).astimezone(timezone.utc)


def between(rnd: random.Random, start: datetime, end: datetime, *, late: float = 0.5) -> datetime:
    """A moment in [start, end], leaning later as `late` rises above a half."""
    if end <= start:
        return start
    t = rnd.triangular(0, 1, late)
    return start + (end - start) * t


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def weekday_for(e: Event, club: Club) -> tuple[Optional[int], bool]:
    """The weekday an event's own words put it on, or the one its kind
    belongs on (0 Monday .. 6 Sunday), and whether that is only a leaning
    (an event already on a weekend day stays where it is); None when any day
    will do.

    - a weekday in its name ("Friday Crit", "First PR Saturday");
    - over a day long: from Friday when it starts in the evening, else Saturday;
    - "weekend" in its words: Saturday;
    - a night, when the club's words name its night ("our Thursday night...");
    - one weekday named in its words ("every Sunday we...");
    - a morning start (before 11:00): a weekend day, the one its club's words
      name (Saturday when they name neither or both)."""
    name = e.name.lower()
    for i, d in enumerate(WEEKDAYS):
        if re.search(rf"\b{d}\b", name):
            return i, False
    words = " ".join([e.name, e.about, e.poster.headline, e.poster.sub]).lower()
    club_words = (club.about + " " + club.tagline).lower()
    start_hour = int(e.when.time[:2])
    if e.when.duration_hours >= 20:
        return (4 if start_hour >= 16 else 5), False
    if "weekend" in words:
        return 5, False
    nights = {m.group(1) for m in re.finditer(r"\b(\w+day)s?[ -]nights?\b", club_words)}
    if "night" in name and len(nights) == 1 and nights <= set(WEEKDAYS):
        return WEEKDAYS.index(next(iter(nights))), False
    named = [i for i, d in enumerate(WEEKDAYS) if re.search(rf"\b{d}s?\b", words)]
    if len(named) == 1:
        return named[0], False
    if start_hour < 11:
        has_sat, has_sun = bool(re.search(r"\bsaturdays?\b", club_words)), bool(re.search(r"\bsundays?\b", club_words))
        return (6 if has_sun and not has_sat else 5), True
    return None, False


def snap(days: int, target: tuple[Optional[int], bool] | Optional[int], today: date, *, past: bool) -> int:
    """`days` from today moved to the nearest `target` weekday that keeps the
    event on its side of today (a past one at least two days back). A
    leaning (see weekday_for) leaves an event already on a weekend alone."""
    wd, leaning = target if isinstance(target, tuple) else (target, False)
    if wd is None:
        return days
    if leaning and (today + timedelta(days=days)).weekday() >= 5:
        return days
    for shift in sorted(range(-6, 7), key=lambda k: (abs(k), k)):
        d = days + shift
        if (today + timedelta(days=d)).weekday() != wd:
            continue
        if (past and d <= -2) or (not past and d >= 1):
            return d
    return days


def venue_line(name: str, address: str) -> str:
    """The venue and its address as one line, without saying a place twice:
    a part of the name that the address already begins with is left out
    ("Canteen Hall, Ponnila College" + "Ponnila College of Engineering, Kottayam")."""
    parts = [p.strip() for p in name.split(",") if p.strip()]
    lowered = [a.strip().lower() for a in address.split(",")]
    kept = [p for p in parts if not any(a.startswith(p.lower()) for a in lowered)]
    return ", ".join(kept + [address.strip()]) if kept else address.strip()


def maps_link(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}"


def upload_file(data: bytes, filename: str):
    from starlette.datastructures import UploadFile

    return UploadFile(file=io.BytesIO(data), filename=filename)


def ratings_around(rnd: random.Random, n: int, mean: float) -> list[int]:
    """`n` whole ratings from 1 to 5 whose average is as near `mean` as whole numbers allow."""
    if n <= 0:
        return []
    out = [min(5, max(1, round(rnd.gauss(mean, 0.7)))) for _ in range(n)]
    target = round(mean * n)
    for _ in range(n * 6):
        total = sum(out)
        if total == target:
            break
        i = rnd.randrange(n)
        if total < target and out[i] < 5:
            out[i] += 1
        elif total > target and out[i] > 1:
            out[i] -= 1
    return out


class Seeder:
    """One run of the seed, on one session. `run()` does it all."""

    def __init__(self, session, clubs: list[Club], people: People, *, main_email: str, main_password: str,
                 club_password: str, force: bool = False, now: Optional[datetime] = None, out=print, seed: str = "moa-demo"):
        self.s = session
        self.clubs = clubs
        self.people = people
        self.main_email = main_email.strip()
        self.main_password = main_password
        self.club_password = club_password
        self.force = force
        self.now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        self.today = self.now.astimezone(IST).date()
        self.out = out
        self.rnd = random.Random(seed)
        self.report = Report()
        self.pool: list[Person] = []
        self.main: Optional[Person] = None
        self.interests: dict[str, int] = {}
        self.topics: dict[str, int] = {}
        self.categories: dict[str, int] = {}
        self.colleges: dict[str, int] = {}
        self.avatars: list[int] = []
        self.created: dict[str, list] = {"clubs": [], "events": [], "users": []}
        self.days: dict[str, int] = {}
        self.hidden_stores: list[str] = []
        self.skipped: Counter = Counter()
        for c in clubs:
            for e in c.events:
                self.days[e.slug] = snap(e.when.days_from_now, weekday_for(e, c), self.today, past=e.past)

    # ── Helpers ──────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        self.out(msg)

    def when(self, e: Event) -> tuple[datetime, datetime]:
        """The event's start and end, on its day as snapped to its weekday
        (weekday_for); a past event that would not have ended yet goes a week back."""
        days = self.days.get(e.slug, e.when.days_from_now)
        start = ist(self.today + timedelta(days=days), e.when.time)
        end = start + timedelta(hours=e.when.duration_hours)
        while e.past and end > self.now - timedelta(hours=2):
            start, end = start - timedelta(days=7), end - timedelta(days=7)
        return start, end

    async def commit(self) -> None:
        await self.s.commit()

    # ── Checks before anything is written ────────────────────────────────

    async def preflight(self) -> None:
        from sqlalchemy import func, select

        from app.api.clubs.models import Clubs
        from app.api.events.models import EventCategories, Events
        from app.api.interests.models import InterestCategory, Interests
        from app.api.subscriptions.models import Plans
        from app.api.users.models import UserAvatars, Users, UserTypes
        from app.api.users.service import same_email

        problems = []
        live_clubs = int(await self.s.scalar(select(func.count()).select_from(Clubs).where(Clubs.is_deleted == False)) or 0)  # noqa: E712
        if live_clubs and not self.force:
            problems.append(f"{live_clubs} club(s) already exist; reset first (scripts/demo/reset.py), or pass --force to seed beside them")
        slugs = [c.slug for c in self.clubs]
        taken = (await self.s.scalars(select(Clubs.slug).where(Clubs.slug.in_(slugs)).execution_options(include_deleted=True))).all()
        if taken:
            problems.append(f"club slugs already in use: {', '.join(sorted(taken))} (remove the demo first: scripts/demo/remove.py)")
        event_slugs = [e.slug for c in self.clubs for e in c.events]
        taken = (await self.s.scalars(select(Events.slug).where(Events.slug.in_(event_slugs)).execution_options(include_deleted=True))).all()
        if taken:
            problems.append(f"event slugs already in use: {', '.join(sorted(taken))}")
        if "@" not in self.main_email:
            problems.append("--main-email is not an address")
        elif await self.s.scalar(select(Users.id).where(same_email(Users.email, self.main_email), Users.is_deleted == False).limit(1)):  # noqa: E712
            problems.append(f"an account already uses {self.main_email}")
        if len(self.main_password) < 8 or len(self.club_password) < 8:
            problems.append("passwords must be at least 8 characters")
        owners = [c.owner_email for c in self.clubs]
        clash = (await self.s.scalars(select(Users.email).where(func.lower(Users.email).in_([o.lower() for o in owners]), Users.is_deleted == False))).all()  # noqa: E712
        if clash:
            problems.append(f"club logins already exist: {', '.join(clash)}")

        self.interests = {slug: i for slug, i in (await self.s.execute(
            select(Interests.slug, Interests.id).where(Interests.is_deleted == False))).all()}  # noqa: E712
        self.topics = {slug: i for slug, i in (await self.s.execute(
            select(InterestCategory.slug, InterestCategory.id).where(InterestCategory.is_deleted == False))).all()}  # noqa: E712
        wanted = {s for c in self.clubs for s in c.interests} | {s for c in self.clubs for e in c.events for s in e.interests} | set(self.people.main_user.interests)
        unknown = sorted(wanted - set(self.interests))
        if unknown:
            problems.append(f"interest slugs not in this database's tree: {', '.join(unknown)}")
        unknown_topics = sorted({t for c in self.clubs for t in c.topics} - set(self.topics))
        if unknown_topics:
            problems.append(f"topic slugs not in this database's tree: {', '.join(unknown_topics)}")
        self.categories = {name: i for name, i in (await self.s.execute(
            select(EventCategories.name, EventCategories.id).where(EventCategories.created_by_id.is_(None), EventCategories.is_deleted == False))).all()}  # noqa: E712
        plan = await self._top_plan()
        if plan is None:
            problems.append("no plan to put the demo clubs on (the plans table is empty)")
        self.avatars = list((await self.s.scalars(select(UserAvatars.id).where(UserAvatars.is_deleted == False))).all())  # noqa: E712
        admins = int(await self.s.scalar(select(func.count()).select_from(Users).where(Users.user_type == UserTypes.admin)) or 0)
        self.report.counts["admins_present"] = admins
        if problems:
            raise Refused("The seed will not run:\n  " + "\n  ".join(problems))

    async def _top_plan(self):
        from sqlalchemy import select

        from app.api.subscriptions.models import Plans

        plans = list((await self.s.scalars(select(Plans).where(Plans.is_deleted == False))).all())  # noqa: E712
        if not plans:
            return None
        by_key = {p.key: p for p in plans}
        return by_key.get("enterprise") or by_key.get("campus") or max(plans, key=lambda p: p.sort_order)

    # ── People ───────────────────────────────────────────────────────────

    def _names(self, need: int) -> list[str]:
        """The attendees' names, and more made from their first and last
        names when the events need more people than the list has."""
        names = list(dict.fromkeys(self.people.attendee_names))
        firsts = [n.split()[0] for n in names]
        lasts = [n.split()[-1] for n in names]
        seen = set(names)
        rnd = random.Random("names")
        while len(names) < need:
            candidate = f"{rnd.choice(firsts)} {rnd.choice(lasts)}"
            if candidate not in seen:
                seen.add(candidate)
                names.append(candidate)
        return names[:max(need, len(self.people.attendee_names))]

    async def _org(self, name: str, *, city: str | None = None, kind: str = "college") -> int:
        """An organisation by name: the one already there, or a new one marked as the demo's."""
        from sqlalchemy import func, select

        from app.api.orgs.models import Organizations, OrgTypes

        key = name.strip().lower()
        if key in self.colleges:
            return self.colleges[key]
        found = await self.s.scalar(select(Organizations.id).where(func.lower(Organizations.name) == key, Organizations.is_deleted == False).limit(1))  # noqa: E712
        if found is None:
            lowered = name.lower()
            if "university" in lowered:
                kind = "university"
            elif any(w in lowered for w in ("college", "institute", "polytechnic")):
                kind = "college"
            elif "school" in lowered:
                kind = "school"
            elif kind == "college":
                kind = "community"
            org = Organizations(
                name=name[:100], type=OrgTypes(kind), address=city, is_verified=True,
                # The mark remove.py knows the demo's own organisations by.
                email=f"org-{ascii_slug(name)[:40]}@{DEMO_DOMAIN}",
            )
            self.s.add(org)
            await self.s.flush()
            found = org.id
            self.report.counts["organisations"] += 1
        self.colleges[key] = found
        return found

    async def make_people(self) -> None:
        """Everyone the clubs need: the attendees named in people.json made
        through create_user (Google sign-ins at the demo domain: no password,
        so nobody can sign in as them), and as many more as the busiest
        event and the most followed club need, made in bulk from the same
        first and last names. Each has a college and a few interests."""
        from sqlalchemy import insert, select, update

        from app.api.users.models import SigninProviders, UserInterests, UserProfiles, Users, UserTypes
        from app.api.users.service import create_user

        need = max([e.seats for c in self.clubs for e in c.events] + [c.followers for c in self.clubs] + [len(self.people.attendee_names)])
        names = self._names(need)
        all_interests = sorted({s for c in self.clubs for s in c.interests})
        colleges = self.people.colleges or ["Government Engineering College"]
        college_ids = [await self._org(c) for c in colleges]
        earliest = self.now - timedelta(days=420)
        named = len(self.people.attendee_names)
        taken = set((await self.s.scalars(select(Users.username))).all())

        def extras_of(user_id: int, name: str, joined: datetime) -> tuple[dict, list[dict]]:
            profile = dict(user_id=user_id, full_name=name, org_id=self.rnd.choice(college_ids),
                           avatar_id=self.rnd.choice(self.avatars) if self.avatars and self.rnd.random() < 0.55 else None,
                           created_at=joined, updated_at=joined, is_deleted=False)
            picks = self.rnd.sample(all_interests, k=min(len(all_interests), self.rnd.randint(2, 4)))
            return profile, [dict(user_id=user_id, interest_id=self.interests[s], created_at=joined, updated_at=joined, is_deleted=False) for s in picks]

        for n, name in enumerate(names[:named], start=1):
            first, last = ascii_slug(name.split()[0]), ascii_slug(name.split()[-1])
            user = await create_user(self.s, full_name=name, email=f"{first}.{last}.{n}@{DEMO_DOMAIN}", provider="google")
            joined = between(self.rnd, earliest, self.now - timedelta(days=150), late=0.6)
            profile, interests = extras_of(user.id, name, joined)
            await self.s.execute(insert(UserProfiles), [profile])
            await self.s.execute(insert(UserInterests), interests)
            await self.s.execute(update(Users).where(Users.id == user.id).values(created_at=joined, updated_at=joined))
            self.pool.append(Person(user.id, name, user.email, user.username))
            taken.add(user.username)
            if n % 50 == 0:
                await self.commit()
        await self.commit()

        batch: list[tuple[int, str]] = []
        for n, name in enumerate(names[named:], start=named + 1):
            batch.append((n, name))
            if len(batch) == 500 or n == len(names):
                rows = []
                for i, nm in batch:
                    first, last = ascii_slug(nm.split()[0]), ascii_slug(nm.split()[-1])
                    username = f"{first}.{last}{i}"[:100]
                    while username in taken:
                        username = f"{first}.{last}{i}_{self.rnd.randint(10, 99)}"
                    taken.add(username)
                    joined = between(self.rnd, earliest, self.now - timedelta(days=20), late=0.7)
                    rows.append(dict(full_name=nm, username=username, email=f"{first}.{last}.{i}@{DEMO_DOMAIN}",
                                     provider=SigninProviders.google, user_type=UserTypes.app_user, email_verified_at=joined,
                                     token_version=0, is_active=True, totp_enabled=False, notify_new_device=False,
                                     created_at=joined, updated_at=joined, is_deleted=False))
                made = (await self.s.execute(insert(Users).returning(Users.id, Users.full_name, Users.email, Users.username, Users.created_at), rows)).all()
                profiles, interests = [], []
                for uid, nm, email, username, joined in made:
                    profile, chosen = extras_of(uid, nm, joined)
                    profiles.append(profile)
                    interests.extend(chosen)
                    self.pool.append(Person(uid, nm, email, username))
                await self.s.execute(insert(UserProfiles), profiles)
                await self.s.execute(insert(UserInterests), interests)
                await self.commit()
                batch = []
        self.report.counts["attendees"] = len(self.pool)
        self.report.counts["attendees_named_in_people_json"] = named

    async def make_main_user(self) -> None:
        from sqlalchemy import select, update

        from app.api.users.models import UserInterests, UserProfiles, Users
        from app.api.users.service import create_user

        m = self.people.main_user
        user = await create_user(self.s, full_name=m.name, email=self.main_email, password=self.main_password, provider="email")
        joined = self.now - timedelta(days=240)
        values = dict(email_verified_at=joined, created_at=joined, updated_at=joined)
        if not await self.s.scalar(select(Users.id).where(Users.username == m.username, Users.id != user.id)):
            values["username"] = m.username
        await self.s.execute(update(Users).where(Users.id == user.id).values(**values))
        org_id = await self._org(m.college) if m.college else None
        self.s.add(UserProfiles(user_id=user.id, full_name=m.name, org_id=org_id, created_at=joined, updated_at=joined))
        for slug in m.interests:
            self.s.add(UserInterests(user_id=user.id, interest_id=self.interests[slug], created_at=joined, updated_at=joined))
        await self.commit()
        self.main = Person(user.id, m.name, self.main_email, values.get("username", user.username))
        if m.bio:
            self.skipped["main_user.bio (no bio on users or profiles)"] += 1
        self.report.main_user = {"email": self.main_email, "username": self.main.username, "user_id": user.id}

    # ── A club ───────────────────────────────────────────────────────────

    async def make_club(self, c: Club) -> dict:
        from sqlalchemy import select, update

        from app.api.clubs.models import Clubs, ClubPartners, ClubTeamMembers
        from app.api.clubs import service as clubs
        from app.api.discover import geo
        from app.api.users.models import Users
        from app.core.auth.authentication import get_password_hash_async
        from app.core.storage.fields import upload

        brand = images.Brand.of(c.brand)
        first_event = min(self.when(e)[0] for e in c.events)
        founded = min(self.now - timedelta(days=self.rnd.randint(200, 330)), first_event - timedelta(days=60))
        org_id = await self._org(c.org.name, city=c.org.city, kind="community") if c.org else None
        club = await clubs.create_club(
            self.s, name=c.name, email=c.owner_email, about=c.about, org_id=org_id, location_name=c.location.name,
            interest_ids=[self.interests[s] for s in c.interests], is_admin_created=True,
            location_link=maps_link(c.location.lat, c.location.lng), contact_phone=c.contact.phone, contact_email=c.contact.email,
            socials={"instagram": c.socials.instagram, "linkedin": c.socials.linkedin},
        )
        owner = await self.s.get(Users, club.user_id)
        owner.password = await get_password_hash_async(self.club_password)
        owner.email_verified_at = founded
        club.slug = c.slug
        club.tagline = c.tagline
        club.brand_primary = c.brand.primary.lower()
        club.brand_secondary = c.brand.accent.lower()
        geo.apply_place(club, link=club.location_link, manual=(c.location.lat, c.location.lng))
        club.logo = await upload(Clubs.logo, {"bytes": images.logo(brand, c.name, c.slug), "filename": "logo.png"})
        club.cover = await upload(Clubs.cover, {"bytes": images.cover(brand, c.slug), "filename": "cover.jpg"})
        await self.commit()
        await self.s.execute(update(Users).where(Users.id == owner.id).values(created_at=founded, updated_at=founded))
        await self.s.execute(update(Clubs).where(Clubs.id == club.id).values(created_at=founded))
        if c.socials.x:
            self.skipped["socials.x (ClubSocials has instagram, linkedin, youtube, website; no x)"] += 1
        if c.topics:
            from app.api.interests.models import Interests

            covered = {t for (t,) in (await self.s.execute(select(Interests.category_id).where(
                Interests.id.in_([self.interests[s] for s in c.interests])))).all()}
            missing = [t for t in c.topics if self.topics[t] not in covered]
            if missing:
                self.report.notes.append(f"{c.slug}: topics {missing} are not reached by its interests; a club's topics come from its interests")

        for i, m in enumerate(c.team):
            # (Image columns are set on the row, never in an UPDATE statement:
            # their type carries its sizes as a dict, which SQLAlchemy's
            # statement cache cannot key on.)
            row = await clubs.create_member(self.s, club.id, {"name": m.name, "role": m.role})
            member = await self.s.get(ClubTeamMembers, row["id"])
            member.photo = await upload(ClubTeamMembers.photo, {"bytes": images.avatar(brand, m.name, variant=i), "filename": "photo.jpg"})
            member.created_at = founded
        for p in c.partners:
            row = await clubs.create_partner(self.s, club.id, p.name, p.role, p.since)
            partner = await self.s.get(ClubPartners, row["id"])
            partner.logo = await upload(ClubPartners.logo, {"bytes": images.partner_mark(brand, p.name), "filename": "logo.png"})
            partner.created_at = founded
        await self.commit()
        if any(m.bio for m in c.team):
            self.skipped["team[].bio (club_team_members has no bio)"] += 1

        await self._plan(club)
        await self._designs(club, owner, c)
        self.created["clubs"].append(club.id)
        self.report.counts["clubs"] += 1
        return {"club": club, "owner": owner, "founded": founded, "brand": brand}

    async def _plan(self, club) -> None:
        from app.api.subscriptions import service as subs
        from app.api.subscriptions.models import BillingCycle

        plan = await self._top_plan()
        await subs.grant_complimentary(
            self.s, club_id=club.id, plan=plan, cycle=BillingCycle.yearly,
            reason="Demo club (scripts/demo): every feature, no charge", ends_at=None, granted_by=None,
        )
        await self.commit()
        self.report.counts[f"plan:{plan.key}"] += 1

    async def _designs(self, club, owner, c: Club) -> None:
        from app.api.event_designs.router import _store_layout_image
        from scripts.demo import designs

        signers = [(m.name, f"{m.role}, {c.name}" if len(m.role) + len(c.name) < 44 else m.role) for m in c.team[:2]]
        await designs.save_designs(self.s, club=club, owner=owner, brand=c.brand, signers=signers, store=_store_layout_image)
        self.report.counts["designs"] += 2

    async def make_store(self, club, c: Club) -> dict[str, object]:
        """The club's catalogue: a product shot each, sizes or stock as given."""
        from app.api.store import service as store
        from app.api.store.schemas import StoreItemIn, VariantIn

        brand = images.Brand.of(c.brand)
        items = {}
        for m in c.merch:
            shot = images.product(brand, kind=m.kind, colour=m.color, name=c.name, slug=c.slug)
            photo = store.club_photo(club.id, shot)
            body = StoreItemIn(
                name=m.name, description=m.description, photo_key=photo["key"], price=m.price,
                stock=None if m.sizes else m.stock,
                variants=[VariantIn(label=k, stock=v) for k, v in (m.sizes or {}).items()],
            )
            items[m.key] = await store.create_item(self.s, club.id, body)
            self.report.counts["store_items"] += 1
        return items

    # ── An event ─────────────────────────────────────────────────────────

    async def _category(self, name: str, owner) -> int:
        from app.api.events.models import EventCategories

        wanted = CATEGORY_NAMES.get(name, name)
        if wanted in self.categories:
            return self.categories[wanted]
        row = EventCategories(name=wanted, created_by_id=owner.id)
        self.s.add(row)
        await self.s.flush()
        self.categories[wanted] = row.id
        self.report.notes.append(f"event type {wanted!r} is not one the product ships; made as a club type")
        return row.id

    async def make_event(self, ctx: dict, c: Club, e: Event, items: dict) -> dict:
        from sqlalchemy import update

        from app.api.agenda import service as agenda
        from app.api.agenda.schemas import AgendaItemIn
        from app.api.events import service as events
        from app.api.events.models import Events
        from app.api.events.schemas import EventAdditionalDetail, EventSpeakerCreate, FieldTypes
        from app.api.merch import service as merch
        from app.api.merch.schemas import MerchItemIn

        club, owner, brand = ctx["club"], ctx["owner"], ctx["brand"]
        start, end = self.when(e)
        made = min(self.now - timedelta(days=self.rnd.randint(2, 9)), start - timedelta(days=self.rnd.randint(18, 40)))
        made = max(made, ctx["founded"] + timedelta(days=3))
        # The store only where nobody can register any more (past, or sold
        # out): a pick at registration is paid online, and no member of the
        # public may pay for something from a club that is not real.
        store_open = bool(e.merch) and (e.past or e.sold_out)
        if e.merch and not store_open:
            self.hidden_stores.append(e.slug)
        venue = venue_line(e.venue.name, e.venue.address) if e.venue else None
        short_place = (e.venue.name.split(",")[0] + ", " + e.venue.address.split(",")[-1].strip()) if e.venue else "Online"
        lat = c.location.lat + self.rnd.uniform(-0.004, 0.004)
        lng = c.location.lng + self.rnd.uniform(-0.004, 0.004)
        price = f"₹{e.fee:g}" if e.fee else "Free"
        poster = images.poster(
            brand, club_name=c.name, slug=c.slug, headline=e.poster.headline, sub=e.poster.sub,
            when=start.astimezone(IST), place=short_place, category=CATEGORY_NAMES.get(e.category, e.category), price=price,
        )
        speakers = [EventSpeakerCreate(name=s.name, designation=s.designation, bio=s.bio, photo_index=i) for i, s in enumerate(e.speakers)]
        photos = [upload_file(images.avatar(brand, s.name, variant=i + 1), f"speaker-{i}.jpg") for i, s in enumerate(e.speakers)]
        questions = [
            EventAdditionalDetail(key=q.key, label=q.label, field_type=FieldTypes(q.type), required=q.required, options=q.options or None)
            for q in e.questions
        ]
        event = await events.create_event(
            self.s, user_id=owner.id, name=e.name, event_datetime=start, duration=e.when.duration_hours,
            category_id=await self._category(e.category, owner), poster=upload_file(poster, "poster.jpg"),
            has_fee=e.fee > 0, reg_fee=e.fee or None, location_name=venue,
            location_link=maps_link(lat, lng) if e.format == "in_person" else None, is_online=e.format == "online",
            merch_enabled=store_open, swag_enabled=bool(e.swag),
            store_item_ids=[items[k].id for k in e.merch] if store_open else None,
            reg_startdate=made, reg_enddate=None, about=e.about, contact_name=c.team[0].name,
            contact_phone=c.contact.phone, contact_email=c.contact.email, additional_details=questions,
            interest_ids=[self.interests[s] for s in e.interests], max_participants=e.capacity,
            event_guidelines=e.guidelines, speakers=speakers, speaker_photos=photos, status="published",
            pin=(lat, lng) if e.format == "in_person" else None,
        )
        ev = await self.s.get(Events, event.id)
        ev.slug = e.slug
        await self.commit()
        await self.s.execute(update(Events).where(Events.id == ev.id).values(created_at=made, updated_at=made))
        await self.commit()

        # The run of show: each entry on the day it names, else on the day of
        # the one before it, rolling to the next day when its time is earlier.
        day0 = start.astimezone(IST).date()
        starts, day, prev = [], 0, None
        for a in e.agenda:
            if a.day is not None:
                day = a.day - 1
            elif prev is not None and a.at < prev:
                day += 1
            starts.append(ist(day0 + timedelta(days=day), a.at))
            prev = a.at
        for i, a in enumerate(e.agenda):
            at = starts[i]
            ends = starts[i + 1] if i + 1 < len(starts) and starts[i + 1] > at else min(end, at + timedelta(hours=1)) if end > at else None
            await agenda.create_item(self.s, ev, AgendaItemIn(starts_at=at, ends_at=ends if ends and ends > at else None, title=a.title, speaker=a.by))
        swag = []
        for sw in e.swag:
            kind = next((k for k in ("sticker", "tee", "lanyard", "notebook", "mug", "cap", "hoodie") if k in sw.name.lower()), "tote")
            if "shirt" in sw.name.lower():
                kind = "tee"
            shot = images.product(brand, kind=kind, colour=c.brand.primary if kind != "sticker" else c.brand.accent, name=c.name, slug=c.slug)
            photo = merch.store_photo(ev.id, shot)
            item = await merch.create_item(self.s, ev, MerchItemIn(name=sw.name, photo_key=photo["key"], stock=e.seats + 20, sponsor=sw.sponsor))
            swag.append(item)
            self.report.counts["swag_items"] += 1
        self.created["events"].append(ev.id)
        self.report.counts["events_past" if e.past else "events_upcoming"] += 1
        return {"event": ev, "start": start, "end": end, "made": made, "swag": swag, "store_open": store_open}

    # ── Registrations ────────────────────────────────────────────────────

    def answers(self, e: Event, person: Person, rnd: random.Random) -> dict:
        out: dict = {}
        for q in e.questions:
            label = q.label.lower()
            if q.type in ("select", "radio"):
                out[q.key] = rnd.choice(q.options)
            elif q.type == "checkbox":
                out[q.key] = rnd.sample(q.options, k=rnd.randint(1, min(2, len(q.options))))
            elif q.type == "number":
                out[q.key] = rnd.randint(1, 4) if "year" in label else rnd.randint(0, 6) if "experience" in label else rnd.randint(1, 10)
            elif q.type == "url":
                host = "linkedin.com/in" if "linkedin" in label else "github.com"
                out[q.key] = f"https://{host}/{person.username.lower()}"
            elif q.type == "date":
                out[q.key] = date(rnd.randint(1998, 2006), rnd.randint(1, 12), rnd.randint(1, 28)).isoformat()
            else:
                if "college" in label or "institution" in label:
                    out[q.key] = rnd.choice(self.people.colleges or ["Government Engineering College"])
                elif "team" in label:
                    out[q.key] = f"Team {rnd.choice(['Nova', 'Orbit', 'Banyan', 'Monsoon', 'Kayal', 'Spark'])}"
                elif "phone" in label or "whatsapp" in label:
                    out[q.key] = f"+91 9{rnd.randint(100000000, 999999999)}"
                else:
                    out[q.key] = rnd.choice(GENERIC_ANSWERS)
        return out

    async def register(self, ev_ctx: dict, e: Event, people: list[Person], *, buyers: set[int]) -> list:
        """Everyone in `people` registered through register_event, each at a
        moment between the event's announcement and its start; a paid
        ticket paid at the desk; a store pick paid at the desk on the day."""
        from sqlalchemy import select, update

        from app.api.events.models import EventRegistrationsLink as Reg
        from app.api.events.registration.service import register_event
        from app.api.ledger import book as ledger_book
        from app.api.ledger import service as ledger
        from app.api.store import service as store
        from app.api.store.models import EventStoreItems, EventStoreOrders
        from app.api.store.schemas import StorePick
        from app.response import CustomHTTPException

        ev, start, made = ev_ctx["event"], ev_ctx["start"], ev_ctx["made"]
        rnd = random.Random(f"reg:{e.slug}")
        last = min(start - timedelta(hours=1), self.now - timedelta(minutes=5))
        offered = []
        if ev_ctx["store_open"] and e.past:
            offered = list((await self.s.scalars(select(EventStoreItems).where(EventStoreItems.id.in_(
                [i.id for i in ev_ctx["items"]])))).all())
        regs = []
        for person in people:
            picks = None
            if offered and (person.user_id in buyers or rnd.random() < 0.22):
                item = rnd.choice(offered)
                variant = None
                if item.variants:
                    weights = [max(0, v.stock - v.sold_count - v.reserved_count) for v in item.variants]
                    if sum(weights):
                        variant = rnd.choices(item.variants, weights=weights)[0].id
                    else:
                        item = None
                if item is not None:
                    picks = [StorePick(item_id=item.id, variant_id=variant, qty=1)]
            try:
                out = await register_event(
                    self.s, full_name=person.full_name, email=person.email, event_id=ev.id, user_id=person.user_id,
                    additional_details=self.answers(e, person, rnd), notify=False, by_organiser=True, store_picks=picks,
                )
            except CustomHTTPException as exc:
                if picks and "store" in str(getattr(exc, "message", "")).lower() + str(getattr(exc, "error_code", "")).lower():
                    out = await register_event(
                        self.s, full_name=person.full_name, email=person.email, event_id=ev.id, user_id=person.user_id,
                        additional_details=self.answers(e, person, rnd), notify=False, by_organiser=True,
                    )
                else:
                    raise
            reg = await self.s.get(Reg, out["event_registration_id"])
            at = between(rnd, made + timedelta(hours=2), last, late=0.62)
            if e.fee > 0:
                # Paid at the desk: the walk-up desk's own record of cash.
                reg.is_paid = True
                reg.paid_amount = reg.actual_amount
                await ledger.sync_registration(self.s, reg, kind=ledger_book.DESK_SALE, channel=ledger.DESK, at=at if not e.past else start)
                await self.commit()
                self.report.counts["paid_at_desk"] += 1
            pending = await store.pending_for(self.s, reg.id)
            if pending is not None:
                # Picked at registration, paid at the desk on the day.
                await store.mark_paid(self.s, pending)
                await ledger.sync_registration(self.s, reg, kind=ledger_book.DESK_SALE, channel=ledger.DESK, at=start + timedelta(minutes=rnd.randint(5, 50)))
                await self.commit()
                await self.s.execute(update(EventStoreOrders).where(EventStoreOrders.id == pending.id).values(
                    created_at=at, updated_at=at, paid_at=start + timedelta(minutes=rnd.randint(5, 50))))
                self.report.counts["store_orders_paid_at_desk"] += 1
            await self.s.execute(update(Reg).where(Reg.id == reg.id).values(created_at=at, updated_at=at))
            await self.commit()
            regs.append(reg)
        self.report.counts["registrations"] += len(regs)
        return regs

    # ── The day of a past event ──────────────────────────────────────────

    async def hold(self, ev_ctx: dict, e: Event, regs: list, owner) -> list:
        """Check-ins, store hand-overs, swag, certificates, feedback and the
        live room, for an event that has happened."""
        from sqlalchemy import select, update

        from app.api.certificates import service as certificates
        from app.api.checkins import service as checkins
        from app.api.checkins.gate_auth import GateActor
        from app.api.checkins.models import CheckIns, CheckInVia
        from app.api.events.models import EventRegistrationsLink as Reg
        from app.api.merch import service as merch
        from app.api.merch.models import MerchCollections
        from app.api.store import service as store
        from app.api.store.models import EventStoreOrders

        ev, start, end = ev_ctx["event"], ev_ctx["start"], ev_ctx["end"]
        rnd = random.Random(f"day:{e.slug}")
        actor = GateActor(kind="user", name=owner.full_name, user=owner, full=True)
        main_reg = [r for r in regs if self.main and r.user_id == self.main.user_id]
        others = [r for r in regs if r not in main_reg]
        count = max(len(main_reg), round(len(regs) * (e.attended_ratio or 0)))
        attended = main_reg + rnd.sample(others, k=max(0, min(len(others), count - len(main_reg))))
        times = {}
        for reg in attended:
            at = start - timedelta(minutes=25) + timedelta(minutes=rnd.triangular(0, 70, 15))
            await checkins.admit(self.s, ev, reg, actor=actor, via=rnd.choice([CheckInVia.gate, CheckInVia.gate, CheckInVia.app, CheckInVia.desk]), checked_in_at=at)
            await self.s.execute(update(CheckIns).where(CheckIns.registration_id == reg.id).values(created_at=at, updated_at=at))
            times[reg.id] = at
        await self.commit()
        self.report.counts["check_ins"] += len(attended)

        # The store's orders handed over at the desk, to most who came.
        orders = list((await self.s.scalars(select(EventStoreOrders).where(
            EventStoreOrders.event_id == ev.id, EventStoreOrders.status == "paid"))).all())
        for o in orders:
            if o.registration_id in times and (self.main and o.user_id == self.main.user_id or rnd.random() < 0.82):
                await store.hand_over(self.s, o, owner.id)
                await self.s.execute(update(EventStoreOrders).where(EventStoreOrders.id == o.id).values(
                    handed_over_at=times[o.registration_id] + timedelta(minutes=rnd.randint(2, 40))))
                self.report.counts["store_orders_handed_over"] += 1
        await self.commit()

        for item in ev_ctx["swag"]:
            for reg in attended:
                if rnd.random() < 0.9 or reg in main_reg:
                    result = await merch.collect(self.s, ev, item["id"], reg.id, actor)
                    if result.get("collected"):
                        await self.s.execute(update(MerchCollections).where(
                            MerchCollections.registration_id == reg.id, MerchCollections.item_id == item["id"]).values(
                            collected_at=times[reg.id] + timedelta(minutes=rnd.randint(1, 15))))
                        self.report.counts["swag_collected"] += 1
            await self.commit()

        if e.certificates and attended:
            result = await certificates.issue(self.s, ev, "checked_in", [])
            await self.s.execute(update(Reg).where(Reg.event_id == ev.id, Reg.certificate_issued_at.isnot(None)).values(
                certificate_issued_at=end + timedelta(hours=rnd.randint(18, 40))))
            await self.commit()
            self.report.counts["certificates"] += int(result.get("issued", 0) if isinstance(result, dict) else 0) or len(attended)

        if e.feedback:
            await self.feedback(ev_ctx, e, attended, times)
        if e.live:
            await self.live(ev_ctx, e, attended)
        return attended

    async def feedback(self, ev_ctx: dict, e: Event, attended: list, times: dict) -> None:
        """The survey, already sent and never sent again (auto_send off), and
        its answers through submit_survey; each answer's rating through
        rate_event too, which gives the club its rating."""
        from sqlalchemy import update

        from app.api.events.models import EventRatingsLink
        from app.api.events.service import rate_event
        from app.api.live import service as live
        from app.api.live.models import FeedbackResponses, FeedbackSends, FeedbackSurveys

        ev, end = ev_ctx["event"], ev_ctx["end"]
        rnd = random.Random(f"feedback:{e.slug}")
        sent_at = end + timedelta(minutes=60)
        survey = FeedbackSurveys(
            event_id=ev.id, auto_send=False, delay_minutes=60, sent_at=sent_at, sent_count=len(attended),
            questions=[
                {"key": "overall", "kind": "rating", "label": "How was the event overall?"},
                {"key": "recommend", "kind": "nps", "label": "How likely are you to recommend us to a friend?"},
                {"key": "change", "kind": "text", "label": "What should we change next time?"},
            ],
            created_at=sent_at, updated_at=sent_at,
        )
        self.s.add(survey)
        await self.s.flush()
        for reg in attended:
            self.s.add(FeedbackSends(survey_id=survey.id, registration_id=reg.id, sent_at=sent_at, mailed_at=sent_at))
        await self.commit()
        main_reg = [r for r in attended if self.main and r.user_id == self.main.user_id]
        others = [r for r in attended if r not in main_reg]
        n = min(len(attended), max(e.feedback.responses, len(main_reg)))
        answering = main_reg + rnd.sample(others, k=max(0, n - len(main_reg)))
        scores = ratings_around(rnd, len(answering), e.feedback.avg_rating)
        comments = list(e.feedback.comments)
        rnd.shuffle(comments)
        for i, (reg, score) in enumerate(zip(answering, scores)):
            comment = comments[i] if i < len(comments) else None
            nps = min(10, max(0, round(score * 2 + rnd.uniform(-1.5, 0.5))))
            answers = {"overall": score, "recommend": nps}
            if comment:
                answers["change"] = comment
            await live.submit_survey(self.s, ev, reg.id, live._survey_token(reg.id), answers)
            at = sent_at + timedelta(minutes=rnd.triangular(5, 60 * 30, 90))
            await self.s.execute(update(FeedbackResponses).where(FeedbackResponses.registration_id == reg.id).values(submitted_at=at))
            await rate_event(self.s, ev.id, reg.user_id, float(score), review=comment)
            await self.s.execute(update(EventRatingsLink).where(
                EventRatingsLink.event_id == ev.id, EventRatingsLink.user_id == reg.user_id).values(created_at=at, updated_at=at))
            await self.commit()
        self.report.counts["feedback_responses"] += len(answering)

    async def live(self, ev_ctx: dict, e: Event, attended: list) -> None:
        """The live room as it was left: a closed poll with its answers, and
        the questions asked, answered, with their votes."""
        from app.api.live.models import (
            LivePollResponses, LivePolls, LiveQuestions, LiveQuestionVotes, PollKind, PollStatus, QuestionStatus,
        )

        ev, start = ev_ctx["event"], ev_ctx["start"]
        rnd = random.Random(f"live:{e.slug}")
        keys = {reg.id: uuid.uuid4().hex for reg in attended}
        if e.live.poll:
            opened = start + timedelta(minutes=35)
            poll = LivePolls(event_id=ev.id, kind=PollKind.choice, prompt=e.live.poll.question, options=list(e.live.poll.options),
                             status=PollStatus.closed, launched_at=opened, closed_at=opened + timedelta(minutes=6),
                             created_at=opened - timedelta(days=1), updated_at=opened + timedelta(minutes=6))
            self.s.add(poll)
            await self.s.flush()
            weights = [rnd.uniform(0.5, 3) for _ in e.live.poll.options]
            for reg in attended:
                if rnd.random() < 0.68:
                    self.s.add(LivePollResponses(poll_id=poll.id, voter_key=keys[reg.id], value=rnd.choices(e.live.poll.options, weights=weights)[0],
                                                 created_at=opened + timedelta(seconds=rnd.randint(5, 300))))
            self.report.counts["live_polls"] += 1
        for i, text in enumerate(e.live.qna):
            author = rnd.choice(attended) if attended else None
            asked = start + timedelta(minutes=rnd.randint(20, 120))
            voters = rnd.sample(attended, k=min(len(attended), rnd.randint(3, 25))) if attended else []
            q = LiveQuestions(
                event_id=ev.id, text=text, author_name=(f"{author.full_name.split()[0]} {author.full_name.split()[-1][0]}." if author else None),
                author_key=keys[author.id] if author else uuid.uuid4().hex, registration_id=author.id if author else None,
                status=QuestionStatus.answered, pinned=i == 0, votes=len(voters), answered_at=asked + timedelta(minutes=rnd.randint(5, 40)),
                created_at=asked, updated_at=asked,
            )
            self.s.add(q)
            await self.s.flush()
            for v in voters:
                self.s.add(LiveQuestionVotes(question_id=q.id, voter_key=keys[v.id], created_at=asked + timedelta(minutes=rnd.randint(1, 30))))
            self.report.counts["live_questions"] += 1
        await self.commit()

    # ── Followers and posts ──────────────────────────────────────────────

    async def follow(self, ctx: dict, c: Club, registrants: set[int]) -> list[int]:
        """`followers` real follow rows (the count on the club is the
        database's own, from the rows): the club's registrants first, then
        others from the pool. The main user's goes through follow_club, the
        rest in bulk; the trigger counts every one."""
        from sqlalchemy import insert, update

        from app.api.clubs.models import ClubUsersLink
        from app.api.clubs.service import follow_club

        club, founded = ctx["club"], ctx["founded"]
        rnd = random.Random(f"follow:{c.slug}")
        ids = [p.user_id for p in self.pool]
        first = [i for i in ids if i in registrants]
        rnd.shuffle(first)
        rest = [i for i in ids if i not in registrants]
        rnd.shuffle(rest)
        want = max(0, c.followers - (1 if self.main else 0))
        chosen = (first + rest)[:min(want, len(ids))]
        if want > len(ids):
            self.report.notes.append(f"{c.slug}: {c.followers} followers asked for, {len(ids) + 1} people to follow with")
        rows = []
        for uid in chosen:
            at = between(rnd, founded + timedelta(days=1), self.now - timedelta(hours=2), late=0.6)
            rows.append(dict(club_id=club.id, user_id=uid, is_following=True, is_pinned=False, is_hidden=False, is_muted=False,
                             followed_at=at, created_at=at, updated_at=at, is_deleted=False))
        for i in range(0, len(rows), 1000):
            await self.s.execute(insert(ClubUsersLink), rows[i:i + 1000])
        await self.commit()
        if self.main:
            await follow_club(self.s, club.id, self.main.user_id)
            at = founded + (self.now - founded) * 0.4
            await self.s.execute(update(ClubUsersLink).where(ClubUsersLink.club_id == club.id, ClubUsersLink.user_id == self.main.user_id).values(
                followed_at=at, created_at=at, updated_at=at, is_pinned=c is self.clubs[0]))
            chosen = [self.main.user_id] + chosen
        await self.commit()
        self.report.counts["follows"] += len(chosen)
        return chosen

    async def posts(self, ctx: dict, c: Club, followers: list[int]) -> None:
        """The club's posts, backdated, with the poll votes, likes, comments
        and views people left on them."""
        from app.api.threads.models import ClubThreads, ThreadComments, ThreadLikes, ThreadPollOptions, ThreadViews, ThreadVotes

        club, owner = ctx["club"], ctx["owner"]
        rnd = random.Random(f"posts:{c.slug}")
        snippets = self.people.comment_snippets or ["Count me in!", "This was great", "When's the next one?"]
        announcements = sorted([t for t in c.threads if t.kind == "announcement"], key=lambda t: t.days_ago)
        pinned = announcements[0] if announcements else None
        audience = list(followers) or [p.user_id for p in self.pool]
        for t in c.threads:
            at = self.now - timedelta(days=t.days_ago, hours=rnd.randint(0, 9), minutes=rnd.randint(0, 59))
            if at > self.now - timedelta(minutes=30):
                at = self.now - timedelta(minutes=rnd.randint(30, 300))
            thread = ClubThreads(club_id=club.id, body=t.text, kind="poll" if t.kind == "poll" else "post", pinned=t is pinned,
                                 notify_followers=False, created_by=owner.id, created_at=at, updated_at=at)
            self.s.add(thread)
            await self.s.flush()
            people = [u for u in audience if not (self.main and u == self.main.user_id)]
            touched: set[int] = set()
            age = max(timedelta(minutes=20), self.now - at)

            def later(rnd=rnd, at=at, age=age):
                return at + age * rnd.triangular(0.01, 1, 0.08)

            if t.kind == "poll":
                options = []
                for i, label in enumerate(t.options):
                    o = ThreadPollOptions(thread_id=thread.id, label=label, sort_order=i)
                    self.s.add(o)
                    options.append(o)
                await self.s.flush()
                weights = [rnd.uniform(0.6, 3) for _ in options]
                voters = rnd.sample(people, k=min(len(people), max(3, int(t.likes * 1.4) or rnd.randint(8, 40))))
                if self.main:
                    voters.append(self.main.user_id)
                for u in voters:
                    when = later()
                    self.s.add(ThreadVotes(thread_id=thread.id, option_id=rnd.choices(options, weights=weights)[0].id, user_id=u, created_at=when, updated_at=when))
                    touched.add(u)
            likers = rnd.sample(people, k=min(len(people), t.likes))
            if self.main and rnd.random() < 0.6:
                likers = [self.main.user_id] + likers[:-1] if likers else [self.main.user_id]
            for u in likers:
                when = later()
                self.s.add(ThreadLikes(thread_id=thread.id, user_id=u, created_at=when, updated_at=when))
                touched.add(u)
            previous, last_when = None, at
            for cm in t.comments:
                # A reply straight after a question is the club answering (a
                # comment by the club's own account shows as the club).
                answering = previous is not None and previous.rstrip().endswith("?") and not cm.text.rstrip().endswith("?")
                u = owner.id if answering else (rnd.choice(people) if people else owner.id)
                when = max(later(), last_when + timedelta(minutes=rnd.randint(3, 90)))
                when = min(when, self.now - timedelta(minutes=1))
                self.s.add(ThreadComments(thread_id=thread.id, user_id=u, body=cm.text, created_at=when, updated_at=when))
                if u != owner.id:
                    touched.add(u)
                previous, last_when = cm.text, when
            if self.main and t.kind == "announcement" and rnd.random() < 0.5:
                when = later()
                self.s.add(ThreadComments(thread_id=thread.id, user_id=self.main.user_id, body=rnd.choice(snippets)[:500], created_at=when, updated_at=when))
                touched.add(self.main.user_id)
            lookers = set(touched) | set(rnd.sample(people, k=min(len(people), int(len(touched) * rnd.uniform(1.5, 3.5)) + 5)))
            for u in lookers:
                self.s.add(ThreadViews(thread_id=thread.id, visitor=f"u:{u}"))
            thread.views_count = len(lookers)
            await self.s.flush()
            self.report.counts["posts"] += 1
            self.report.counts["post_likes"] += len(likers)
            self.report.counts["post_comments"] += len(t.comments)
        await self.commit()

    async def publish_site(self, ctx: dict, c: Club) -> None:
        from app.api.sites import service as sites
        from app.api.sites.models import FONTS, TEMPLATES

        club = ctx["club"]
        brand = images.Brand.of(c.brand)
        s = await sites.get_or_create(self.s, club)
        font = {"bebas": "space_grotesk", "dm-serif": "fraunces", "inter": "inter"}.get(c.brand.display_font, "inter")
        await sites.patch(self.s, s, {
            "template": TEMPLATES[sum(map(ord, c.slug)) % len(TEMPLATES)],
            "theme": "light" if images.luminance(brand.w) > 0.6 else "dark",
            "font": font if font in FONTS else None,
            "headline": c.tagline, "subline": None,
        })
        await sites.publish(self.s, club, s)
        self.report.counts["sites"] += 1

    # ── The whole run ────────────────────────────────────────────────────

    def choose(self, k: int, rnd: random.Random) -> list[Person]:
        """`k` registrants: most from the attendees people.json names, the
        rest from the wider pool, so the same faces recur across a club's events."""
        named = self.pool[:len(self.people.attendee_names)]
        wider = self.pool[len(named):]
        from_named = min(len(named), max(k - len(wider), round(k * 0.6)))
        out = rnd.sample(named, k=from_named) + rnd.sample(wider, k=min(len(wider), k - from_named))
        rnd.shuffle(out)
        return out

    def main_events(self, c: Club) -> set[str]:
        """The main user's events at this club: its next one, its latest
        past one, and a past one that gave certificates."""
        upcoming = sorted((e for e in c.events if not e.past), key=lambda e: e.when.days_from_now)
        past = sorted((e for e in c.events if e.past), key=lambda e: -e.when.days_from_now)
        picks = set()
        if upcoming:
            picks.add(upcoming[0].slug)
        if past:
            picks.add(past[0].slug)
            certified = [e for e in past if e.certificates]
            if certified:
                picks.add(certified[0].slug)
        return picks

    async def run(self) -> Report:
        from sqlalchemy import func, select

        from app.core import jobs

        started = clock.monotonic()
        await self.preflight()
        jobs_before = int(await self.s.scalar(select(func.count()).select_from(jobs.Jobs)) or 0)
        self.log(f"Seeding {len(self.clubs)} club(s) as of {self.now.astimezone(IST):%d %b %Y %H:%M} IST")
        await self.make_people()
        await self.make_main_user()
        self.log(f"  {len(self.pool)} attendees and the main user made")
        buy_once = True
        for c in self.clubs:
            t0 = clock.monotonic()
            ctx = await self.make_club(c)
            items = await self.make_store(ctx["club"], c)
            mine = self.main_events(c)
            registrants: set[int] = set()
            for e in sorted(c.events, key=lambda e: e.when.days_from_now):
                ev_ctx = await self.make_event(ctx, c, e, items)
                ev_ctx["items"] = [items[k] for k in e.merch] if ev_ctx["store_open"] else []
                rnd = random.Random(f"who:{e.slug}")
                with_main = self.main is not None and e.slug in mine and e.seats > 0
                k = e.seats - (1 if with_main else 0)
                people = ([self.main] if with_main else []) + self.choose(k, rnd)
                buyers = set()
                if with_main and e.past and ev_ctx["store_open"] and buy_once:
                    buyers.add(self.main.user_id)
                    buy_once = False
                regs = await self.register(ev_ctx, e, people, buyers=buyers)
                registrants |= {p.user_id for p in people}
                if e.past and regs:
                    await self.hold(ev_ctx, e, regs, ctx["owner"])
                await self._close_registration(ev_ctx)
            followers = await self.follow(ctx, c, registrants)
            await self.posts(ctx, c, followers)
            await self.publish_site(ctx, c)
            self.report.clubs.append({"slug": c.slug, "name": c.name, "login": c.owner_email, "id": ctx["club"].id})
            self.log(f"  {c.name}: {len(c.events)} events, {len(followers)} followers ({clock.monotonic() - t0:.0f}s)")

        from app.api.closeout.service import advance_clock

        if self.hidden_stores:
            self.report.notes.append(
                f"the store is hidden on {len(self.hidden_stores)} upcoming event(s) open for registration "
                f"(a pick there is paid online, and nobody may pay a club that is not real): {', '.join(self.hidden_stores)}")
        for what, n in sorted(self.skipped.items()):
            self.report.notes.append(f"not stored, no home in the backend: {what} ({n})")
        moved = await advance_clock(self.s, self.now)
        self.report.counts["events_moved_by_status_clock"] = moved
        await self.commit()
        await self.safety_checks(jobs_before)
        self.report.counts["seconds"] = round(clock.monotonic() - started)
        return self.report

    async def _close_registration(self, ev_ctx: dict) -> None:
        """Registration closes at the start, as the dashboard sets it."""
        from sqlalchemy import update

        from app.api.events.models import Events

        await self.s.execute(update(Events).where(Events.id == ev_ctx["event"].id).values(reg_enddate=ev_ctx["start"]))
        await self.commit()

    async def safety_checks(self, jobs_before: int) -> None:
        """What must hold once the seed is done, or later sweeps would write
        to someone or take money: no job left for a runner, no journey mail
        set up, no survey that sends itself, no delivery waiting, no order
        held, every paid upcoming event full."""
        from sqlalchemy import func, select

        from app.api.deliveries.models import Deliveries
        from app.api.emails.models import EmailTemplates
        from app.api.events.models import Events
        from app.api.live.models import FeedbackSurveys
        from app.api.store.models import EventStoreOrders
        from app.core import jobs

        ids = self.created["events"]
        problems = []
        jobs_now = int(await self.s.scalar(select(func.count()).select_from(jobs.Jobs)) or 0)
        if jobs_now != jobs_before:
            problems.append(f"{jobs_now - jobs_before} job row(s) were written")
        n = await self.s.scalar(select(func.count()).select_from(EmailTemplates).where(EmailTemplates.event_id.in_(ids)))
        if n:
            problems.append(f"{n} journey mail template(s) were made for demo events")
        n = await self.s.scalar(select(func.count()).select_from(FeedbackSurveys).where(FeedbackSurveys.event_id.in_(ids), FeedbackSurveys.auto_send == True))  # noqa: E712
        if n:
            problems.append(f"{n} survey(s) would send themselves")
        n = await self.s.scalar(select(func.count()).select_from(Deliveries).where(Deliveries.event_id.in_(ids)))
        if n:
            problems.append(f"{n} delivery row(s) were made")
        n = await self.s.scalar(select(func.count()).select_from(EventStoreOrders).where(EventStoreOrders.event_id.in_(ids), EventStoreOrders.status == "pending"))
        if n:
            problems.append(f"{n} store order(s) are still pending (a hold, or an online payment to come)")
        open_paid = (await self.s.execute(select(Events.slug, Events.paid_count, Events.max_participants).where(
            Events.id.in_(ids), Events.has_fee == True, Events.event_datetime > self.now,  # noqa: E712
            Events.paid_count < Events.max_participants))).all()
        if open_paid:
            problems.append(f"paid upcoming events with places left: {', '.join(r[0] for r in open_paid)}")
        if problems:
            raise Refused("The safety checks after the seed failed (the data is written; remove it with scripts/demo/remove.py):\n  " + "\n  ".join(problems))
        self.report.counts["safety_checks_passed"] = 1


def render(report: Report, quiet_summary: dict, *, main_password: str, club_password: str) -> str:
    lines = ["", "Seeded:"]
    order = ["clubs", "events_past", "events_upcoming", "attendees", "registrations", "paid_at_desk", "check_ins", "certificates",
             "store_items", "store_orders_paid_at_desk", "store_orders_handed_over", "swag_items", "swag_collected",
             "feedback_responses", "live_polls", "live_questions", "follows", "posts", "post_likes", "post_comments",
             "designs", "sites", "organisations"]
    for k in order + sorted(k for k in report.counts if k not in order):
        if k in report.counts:
            lines.append(f"  {k.replace('_', ' '):<28} {report.counts[k]}")
    lines.append(f"  quiet mode: {quiet_summary}")
    for note in report.notes:
        lines.append(f"  note: {note}")
    lines += ["", "CREDENTIALS (shown once; not written anywhere)", "  Clubs dashboard -- every demo club signs in with the same password:"]
    for c in report.clubs:
        lines.append(f"    {c['name']:<36} {c['login']}")
    lines.append(f"    password: {club_password}")
    if report.main_user:
        lines += ["  App and site -- the main demo user:", f"    {report.main_user['email']}  (username {report.main_user['username']})", f"    password: {main_password}"]
    return "\n".join(lines)


async def seed(*, main_email: str, main_password: str, club_password: str, data_dir: Optional[Path], force: bool,
               include_example: bool = False, bucket: Optional[Path] = None, out=print) -> int:
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria
    from scripts.demo.quiet import quiet

    try:
        clubs, people = load(data_dir, include_example=include_example)
    except DataError as e:
        out(str(e))
        return 2
    with contextlib.ExitStack() as stack:
        if bucket is not None:
            stack.enter_context(_bucket(bucket))
        q = stack.enter_context(quiet())
        async with AsyncSessionLocal() as session:
            add_loader_criteria(session)
            from sqlalchemy import text

            await session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": 0x6D6F615F64656D6F})
            try:
                seeder = Seeder(session, clubs, people, main_email=main_email, main_password=main_password,
                                club_password=club_password, force=force, out=out)
                try:
                    report = await seeder.run()
                except Refused as e:
                    await session.rollback()
                    out(str(e))
                    return 2
            finally:
                await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": 0x6D6F615F64656D6F})
                await session.commit()
        from app.core import site_revalidate

        await site_revalidate.flush()
        if q.sent:
            out(f"WARNING: quiet mode stopped {len(q.sent)} message(s) the seed tried to send: {q.sent[:5]}")
    out(render(report, q.summary(), main_password=main_password, club_password=club_password))
    return 0


def _bucket(root: Path):
    from scripts.demo.local_bucket import local_bucket

    return local_bucket(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo.seed", description=__doc__.split("\n\n")[0])
    parser.add_argument("--main-email", required=True, help=f"the main demo user's address (one at {DEMO_DOMAIN} is never mailed)")
    parser.add_argument("--main-password", required=True)
    parser.add_argument("--club-password", required=True, help="every demo club's dashboard password")
    parser.add_argument("--data-dir", type=Path, default=None, help="where clubs/*.json and people.json are (default: scripts/demo/data)")
    parser.add_argument("--force", action="store_true", help="seed even though other clubs exist")
    parser.add_argument("--include-example", action="store_true", help="seed data/clubs/_example.json too (for a rehearsal)")
    parser.add_argument("--local-bucket", type=Path, default=None, help="a folder instead of S3 -- for a rehearsal on a laptop, never on the server")
    args = parser.parse_args(argv)
    return asyncio.run(seed(
        main_email=args.main_email, main_password=args.main_password, club_password=args.club_password,
        data_dir=args.data_dir, force=args.force, include_example=args.include_example, bucket=args.local_bucket,
    ))


if __name__ == "__main__":
    sys.exit(main())
