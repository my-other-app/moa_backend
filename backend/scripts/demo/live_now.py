"""Make one of the showcase club's events live now, with people checked in,
so People Like You can be tried in the app.

    python -m scripts.demo.live_now --dry-run    # what it would do; writes nothing
    python -m scripts.demo.live_now              # do it

What it does, and only inside the demo:

- **The event.** The showcase club's "Open Night" (slug `<club>-open-night-live`),
  made the first time and moved on every run so it started ten minutes ago and
  runs four hours. Free, at the club's usual venue, published. Registration is
  closed and the event is full once the people below are on it, so nobody from
  the public can register for it.
- **The people.** The main demo user and up to `--people` demo members who
  share at least one of their interests (the most shared first), plus two who
  share none, so the list has an "also here" part. Each is registered and
  checked in at the desk, as the club's owner would by hand.
- **The switch.** The member sharing the most is set to hidden ("Show me in
  People Like You" off): they are checked in but never appear in the list,
  which shows the switch working. The other members are set to show.

It touches only the showcase club and accounts at demo.myotherapp.in: it
refuses a club whose owner is not at that domain, a main user who is not,
and it picks members only from there. It sends nothing: it runs in quiet
mode (no mail, no push, no job written), and the push sender skips the demo
domain anyway. No payment is made: the event is free.

Run it again at any time: the same event is moved to now, the people already
on it stay, and anyone missing is added. Afterwards, sign in to the app as
the main demo user: the event's page has the "People like you" card, Home
says where they are, and the list opens from either.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from scripts.demo.schema import DEMO_DOMAIN  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

#: How long ago the event started, and how long it runs.
STARTED_AGO = timedelta(minutes=10)
LENGTH_HOURS = 4.0
#: Members who share none of the main user's interests, for "also here".
UNMATCHED = 2


class Refused(Exception):
    """The script will not run; the message says why."""


@dataclass
class Plan:
    """What a run does (or, dry, would do)."""

    club: str = ""
    event: str = ""
    event_slug: str = ""
    creating: bool = False
    starts_at: Optional[datetime] = None
    closes_at: Optional[datetime] = None
    networking: bool = False
    main: str = ""
    sharing: list[tuple[str, int]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    hidden: Optional[str] = None
    registering: int = 0
    checking_in: int = 0
    notes: list[str] = field(default_factory=list)


def at_demo_domain(email: Optional[str]) -> bool:
    address = (email or "").strip().lower()
    return address.endswith("@" + DEMO_DOMAIN) or address.endswith("." + DEMO_DOMAIN)


def slug_for(club_slug: str) -> str:
    return f"{club_slug}-open-night-live"


async def _club(s, club_slug: str):
    from sqlalchemy import select

    from app.api.clubs.models import Clubs
    from app.api.users.models import Users

    club = await s.scalar(select(Clubs).where(Clubs.slug == club_slug, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise Refused(f"No club {club_slug!r}. Seed the demo first (scripts/demo/README.md).")
    owner = await s.get(Users, club.user_id)
    if owner is None or not at_demo_domain(owner.email):
        raise Refused(f"{club_slug!r} is not a demo club: its owner is not at {DEMO_DOMAIN}.")
    return club, owner


async def _main_user(s, email: str):
    from sqlalchemy import select

    from app.api.users.models import Users
    from app.api.users.service import same_email

    if not at_demo_domain(email):
        raise Refused(f"--main-email must be at {DEMO_DOMAIN}; this script touches only demo accounts.")
    user = await s.scalar(
        select(Users).where(same_email(Users.email, email), Users.is_deleted == False).order_by(Users.id).limit(1)  # noqa: E712
    )
    if user is None:
        raise Refused(f"No account {email}. Seed the demo first, or pass --main-email.")
    return user


async def _interests_of(s, user_id: int) -> set[int]:
    from sqlalchemy import select

    from app.api.users.models import UserInterests

    return set((await s.scalars(select(UserInterests.interest_id).where(
        UserInterests.user_id == user_id, UserInterests.is_deleted == False))).all())  # noqa: E712


def _members():
    """The demo's invented attendees: app accounts at the demo domain that can
    be shown, never an admin or a club's own login."""
    from sqlalchemy import func

    from app.api.users.models import Users, UserTypes

    domain = func.lower(func.btrim(Users.email))
    return (
        Users.user_type == UserTypes.app_user,
        Users.is_deleted == False,  # noqa: E712
        Users.is_active == True,  # noqa: E712
        domain.like(f"%@{DEMO_DOMAIN}") | domain.like(f"%.{DEMO_DOMAIN}"),
    )


async def _people(s, main_id: int, mine: set[int], *, want: int, already: list[int]) -> tuple[list[tuple[int, int]], list[int]]:
    """Those sharing interests with the main user, most shared first (the ones
    already on the event kept), and a few sharing none."""
    from sqlalchemy import exists, func, select

    from app.api.users.models import UserInterests, Users

    shared = func.count(UserInterests.interest_id)
    ranked = [] if not mine else list((await s.execute(
        select(Users.id, shared)
        .join(UserInterests, UserInterests.user_id == Users.id)
        .where(*_members(), Users.id != main_id, UserInterests.is_deleted == False, UserInterests.interest_id.in_(mine))  # noqa: E712
        .group_by(Users.id)
        .order_by(shared.desc(), Users.id)
        .limit(want + len(already) + 50)
    )).all())
    by_id = {uid: n for uid, n in ranked}
    keep = [(uid, by_id[uid]) for uid in already if uid in by_id]
    for uid, n in ranked:
        if len(keep) >= want:
            break
        if uid not in {k for k, _ in keep}:
            keep.append((uid, n))
    keep.sort(key=lambda p: (-p[1], p[0]))

    has_any = exists().where(UserInterests.user_id == Users.id, UserInterests.is_deleted == False)  # noqa: E712
    shares = exists().where(UserInterests.user_id == Users.id, UserInterests.is_deleted == False, UserInterests.interest_id.in_(mine or {-1}))  # noqa: E712
    taken_unmatched = [uid for uid in already if uid not in by_id]
    rest = list((await s.scalars(
        select(Users.id).where(*_members(), Users.id != main_id, has_any, ~shares).order_by(Users.id).limit(UNMATCHED)
    )).all())
    unmatched = (taken_unmatched + [u for u in rest if u not in taken_unmatched])[:max(UNMATCHED, len(taken_unmatched))]
    return keep, unmatched


async def _event(s, club, *, include_deleted: bool = True):
    from sqlalchemy import select

    from app.api.events.models import Events

    return await s.scalar(
        select(Events).where(Events.slug == slug_for(club.slug)).execution_options(include_deleted=include_deleted)
    )


async def _template(s, club):
    """The club's latest event, for its venue, kind and interests."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.api.events.models import Events

    return await s.scalar(
        select(Events).options(selectinload(Events.interests))
        .where(Events.club_id == club.id, Events.is_deleted == False, Events.slug != slug_for(club.slug))  # noqa: E712
        .order_by(Events.is_online, Events.event_datetime.desc()).limit(1)
    )


async def _category(s, template) -> int:
    from sqlalchemy import select

    from app.api.events.models import EventCategories

    if template is not None and template.category_id:
        return template.category_id
    found = await s.scalar(select(EventCategories.id).where(EventCategories.is_deleted == False).order_by(EventCategories.id).limit(1))  # noqa: E712
    if found is None:
        raise Refused("No event types in the database. Seed the demo first.")
    return found


async def _registrations(s, event_id: int) -> dict:
    from sqlalchemy import select

    from app.api.events.models import EventRegistrationsLink as Reg

    rows = (await s.scalars(select(Reg).where(Reg.event_id == event_id, Reg.is_deleted == False))).all()  # noqa: E712
    return {r.user_id: r for r in rows}


async def run(s, *, club_slug: str = "respawn-room", main_email: str = f"demo@{DEMO_DOMAIN}", people: int = 8,
              dry_run: bool = False, now: Optional[datetime] = None, out=print) -> Plan:
    """The whole run on session `s`. Dry, it reads and reports; otherwise it
    writes, committing as the app's own services do. Callers run it in quiet
    mode (`main` does)."""
    from sqlalchemy import select, update

    from app.api.checkins import service as checkins
    from app.api.checkins.gate_auth import GateActor
    from app.api.checkins.models import CheckInVia
    from app.api.events import service as events
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.events.networking import is_within_networking_window
    from app.api.events.registration.service import register_event
    from app.api.subscriptions import gates
    from app.api.users.models import Users

    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    start = now - STARTED_AGO
    club, owner = await _club(s, club_slug)
    main = await _main_user(s, main_email)
    mine = await _interests_of(s, main.id)
    plan = Plan(club=club.name, main=main.email, starts_at=start)
    if not mine:
        plan.notes.append(f"{main.email} has no interests: nobody can share one. Pick some in the app first.")

    ev = await _event(s, club)
    if ev is not None and ev.has_fee:
        raise Refused(f"{ev.slug} has a fee; this script makes only free events. Remove its fee or the event first.")
    regs = await _registrations(s, ev.id) if ev is not None else {}
    already = [uid for uid in regs if uid != main.id]
    sharing, unmatched = await _people(s, main.id, mine, want=people, already=already)
    if len(sharing) < people:
        plan.notes.append(f"only {len(sharing)} demo member(s) share an interest with {main.email} (wanted {people})")
    names = {u.id: u.full_name for u in (await s.scalars(
        select(Users).where(Users.id.in_([main.id, *[u for u, _ in sharing], *unmatched])))).all()}
    hidden_id = sharing[0][0] if sharing else None

    plan.event = ev.name if ev is not None else f"{club.name} Open Night"
    plan.event_slug = slug_for(club.slug)
    plan.creating = ev is None
    plan.sharing = [(names.get(u, str(u)), n) for u, n in sharing]
    plan.unmatched = [names.get(u, str(u)) for u in unmatched]
    plan.hidden = names.get(hidden_id) if hidden_id else None
    everyone = [main.id, *[u for u, _ in sharing], *unmatched]
    plan.registering = sum(1 for u in everyone if u not in regs)
    plan.checking_in = sum(1 for u in everyone if u not in regs or not regs[u].is_attended)

    if dry_run:
        plan.networking = await gates.allows(s, club.id, "networking", event=ev)
        plan.closes_at = start + timedelta(hours=LENGTH_HOURS + 1)
        return plan

    # The event: made once, moved to now on every run. Open for exactly the
    # places this run fills while it registers them, then closed and full.
    template = await _template(s, club)
    if ev is None:
        made = await events.create_event(
            s, user_id=owner.id, name=f"{club.name} Open Night", event_datetime=start, duration=LENGTH_HOURS,
            category_id=await _category(s, template), has_fee=False,
            location_name=(template.location_name if template is not None else None),
            is_online=False, reg_startdate=start - timedelta(days=7), reg_enddate=None,
            about=(f"An evening to meet the people who come to {club.name}: tables set up around the room, "
                   "and time to talk. Check in at the door, then open People Like You in the app to find "
                   "who here shares your interests."),
            contact_email=owner.email, additional_details=[],
            interest_ids=[i.id for i in (template.interests if template is not None else [])] or sorted(mine)[:3],
            status="published",
        )
        ev = await s.get(Events, made.id)
        ev.slug = slug_for(club.slug)
        if template is not None:
            ev.lat, ev.lng = template.lat, template.lng
    elif ev.is_deleted:
        ev.is_deleted = False
    ev.event_datetime = start
    ev.duration = LENGTH_HOURS
    ev.reg_startdate = min(ev.reg_startdate or start, start - timedelta(days=1))
    ev.reg_enddate = None
    ev.max_participants = len(regs) + plan.registering or None
    await s.commit()

    for uid in everyone:
        if uid in regs:
            continue
        user = await s.get(Users, uid)
        made = await register_event(s, full_name=user.full_name, email=user.email, event_id=ev.id, user_id=uid,
                                    notify=False, by_organiser=True)
        regs[uid] = await s.get(EventRegistrationsLink, made["event_registration_id"])

    # Checked in over the ten minutes since the doors opened, at the desk,
    # by the club's owner; nobody hears about it (quiet mode keeps the job
    # that would tell them from being written).
    rnd = random.Random(f"live-now:{club.slug}")
    actor = GateActor(kind="user", name=owner.full_name, user=owner, full=True)
    for uid in everyone:
        reg = regs[uid]
        if reg.is_attended:
            continue
        at = start + timedelta(seconds=rnd.randint(0, int(STARTED_AGO.total_seconds())))
        await checkins.admit(s, ev, reg, actor=actor, via=CheckInVia.desk, door_label="Desk", checked_in_at=at)

    # Who shows: the member sharing most is hidden, every other one shows.
    members = [u for u in everyone if u != main.id]
    if members:
        await s.execute(update(Users).where(Users.id.in_(members)).values(networking_visible=True))
    if hidden_id is not None:
        await s.execute(update(Users).where(Users.id == hidden_id).values(networking_visible=False))

    # Closed and full: nobody else can register.
    ev = await s.get(Events, ev.id)
    ev.reg_enddate = start
    ev.max_participants = len(regs)
    await s.commit()

    plan.networking = await gates.allows(s, club.id, "networking", event=ev)
    plan.closes_at = is_within_networking_window(ev)[1]
    if not plan.networking:
        plan.notes.append(
            f"{club.name}'s plan does not include networking: the app will say People Like You isn't available. "
            "Give the club a plan with Attendee networking (Admin -> Subscriptions)."
        )
    return plan


def render(plan: Plan, *, dry_run: bool, quiet_summary: Optional[dict] = None) -> str:
    def local(dt: Optional[datetime]) -> str:
        return dt.astimezone(IST).strftime("%-d %b %H:%M IST") if dt else "?"

    lines = ["", "DRY RUN: nothing was written." if dry_run else "Done.", ""]
    lines.append(f"  club:        {plan.club}")
    lines.append(f"  event:       {plan.event} (/events/{plan.event_slug}){' -- new' if plan.creating else ''}")
    lines.append(f"  starts:      {local(plan.starts_at)}, runs {LENGTH_HOURS:g} hours; People Like You open until {local(plan.closes_at)}")
    lines.append(f"  networking:  {'on (the club plan has it)' if plan.networking else 'OFF: the club plan does not have it'}")
    lines.append(f"  main user:   {plan.main}")
    lines.append(f"  sharing:     {len(plan.sharing)} member(s): " + ", ".join(f"{n} ({k})" for n, k in plan.sharing))
    if plan.unmatched:
        lines.append(f"  also here:   {', '.join(plan.unmatched)} (no shared interest)")
    if plan.hidden:
        lines.append(f"  hidden:      {plan.hidden} -- checked in, but never listed (Show me in People Like You off)")
    verb = "would register" if dry_run else "registered"
    lines.append(f"  {verb} {plan.registering}, {'would check in' if dry_run else 'checked in'} {plan.checking_in}")
    if quiet_summary is not None:
        lines.append(f"  quiet mode:  {quiet_summary}")
    for note in plan.notes:
        lines.append(f"  note: {note}")
    if not dry_run:
        lines += ["", f"Sign in to the app as {plan.main}: the event's page has the People like you card,",
                  "and Home says where you are. Run this again to move the event to now."]
    return "\n".join(lines)


async def live_now(*, club_slug: str, main_email: str, people: int, dry_run: bool, out=print) -> int:
    from sqlalchemy import text

    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria
    from scripts.demo.quiet import quiet

    with quiet() as q:
        async with AsyncSessionLocal() as session:
            add_loader_criteria(session)
            # The seed's lock: never at the same time as a seed or another run.
            await session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": 0x6D6F615F64656D6F})
            try:
                try:
                    plan = await run(session, club_slug=club_slug, main_email=main_email, people=people, dry_run=dry_run, out=out)
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
            out(f"WARNING: quiet mode stopped {len(q.sent)} message(s) (nothing left the process): {q.sent[:5]}")
    out(render(plan, dry_run=dry_run, quiet_summary=q.summary()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo.live_now", description=__doc__.split("\n\n")[0])
    parser.add_argument("--club", default="respawn-room", help="the demo club whose event goes live (default: the showcase)")
    parser.add_argument("--main-email", default=f"demo@{DEMO_DOMAIN}", help=f"the main demo user (must be at {DEMO_DOMAIN})")
    parser.add_argument("--people", type=int, default=8, help="demo members sharing an interest to check in (default 8)")
    parser.add_argument("--dry-run", action="store_true", help="say what it would do, and write nothing")
    args = parser.parse_args(argv)
    if not 1 <= args.people <= 30:
        parser.error("--people is from 1 to 30")
    return asyncio.run(live_now(club_slug=args.club, main_email=args.main_email, people=args.people, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
