"""Put each seeded demo event's pin on its own venue.

    python -m scripts.demo.fix_pins --dry-run    # what it would move; changes nothing
    python -m scripts.demo.fix_pins              # moves them, in one transaction

The seed used to pin every in-person event within about 450 m of its club,
whatever its venue said: "Systems Weekend at Vagamon" (Idukki) sat in
Panampilly Nagar, so the app put it 2.9 km from Kochi when it is some 73 km
away. The data files now give every venue its own point (data/clubs/*.json)
and a new seed uses it; this moves the pins of a demo seeded before.

What it touches, and nothing else:

- a demo club: one whose slug is in the data files and whose contact address
  or owner's sign-in is at demo.myotherapp.in. Both, so a real club that took
  the slug of a removed demo club is never one;
- of its events, those the data files name by slug, held in person, not
  deleted;
- whose pin is still the one the seed made: pinned (geo_source 'manual')
  within OLD_JITTER of the club's point in the data file, with the seed's
  map link for exactly that pin, and the seed's venue words. A pin, map link
  or venue someone has changed since is left alone, and said so.

It can be run again: a pin already on its venue is left as it is. A moved
pin gets the map link of its new point, as the seed now writes it. The
search index and the grid cells follow by themselves (triggers); the site's
pages within a minute.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from scripts.demo.schema import DEMO_DOMAIN, Club, DataError, Event, load  # noqa: E402
from scripts.demo.seed import maps_link, venue_line  # noqa: E402

#: How far, in degrees of latitude and of longitude, the seed scattered a pin around its club.
OLD_JITTER = 0.004
#: Points this close, in degrees, are the same point (a float read back from the database).
SAME = 1e-6


@dataclass
class Move:
    club: str
    event: str
    old: tuple[float, float]
    new: tuple[float, float]
    km: float


@dataclass
class Report:
    clubs: int = 0
    moved: list[Move] = field(default_factory=list)
    #: (club, event): already on the venue.
    right: list[tuple[str, str]] = field(default_factory=list)
    #: (club, event, why): changed by someone since the seed; left alone.
    kept: list[tuple[str, str, str]] = field(default_factory=list)
    #: What the data files name that the database does not have (removed, never seeded, or not demo).
    missing: list[str] = field(default_factory=list)


def _at_demo(address: Optional[str]) -> bool:
    a = (address or "").strip().lower()
    return a.endswith("@" + DEMO_DOMAIN) or a.endswith("." + DEMO_DOMAIN)


def _same(a: tuple[float, float], b: tuple[float, float], within: float) -> bool:
    return abs(a[0] - b[0]) <= within and abs(a[1] - b[1]) <= within


def changed_since_seed(ev, club: Club, e: Event) -> Optional[str]:
    """Why this event's pin is not the one the seed made, or None when it is."""
    from app.api.discover import geo

    if ev.is_online:
        return "it is online now"
    if ev.lat is None or ev.lng is None:
        return "it has no pin"
    old = (ev.lat, ev.lng)
    if ev.geo_source != geo.MANUAL:
        return f"its point came from its {ev.geo_source or 'nothing'}, not the seed's pin"
    if not _same(old, (club.location.lat, club.location.lng), OLD_JITTER + SAME):
        return "its pin was moved (it is not beside the club, where the seed put it)"
    if (ev.location_link or "") != maps_link(*old):
        return "its map link was changed"
    if (ev.location_name or "") != venue_line(e.venue.name, e.venue.address):
        return "its venue was changed"
    return None


async def fix(session, clubs: list[Club], *, dry_run: bool) -> Report:
    """Move the seed's pins of these clubs' events to their venues (see the
    module's docstring), committing once at the end; a dry run only reads."""
    from app.api.clubs.models import Clubs
    from app.api.discover import geo
    from app.api.events.models import Events
    from app.api.users.models import Users

    report = Report()
    wanted = {c.slug: c for c in clubs}
    rows = (await session.execute(
        select(Clubs, Users.email).outerjoin(Users, Users.id == Clubs.user_id).where(Clubs.slug.in_(list(wanted)))
    )).all()
    demo = {club.slug: club for club, owner in rows if _at_demo(club.contact_email) or _at_demo(owner)}
    for slug, c in wanted.items():
        club = demo.get(slug)
        if club is None:
            report.missing.append(f"club {slug}")
            continue
        report.clubs += 1
        in_person = [e for e in c.events if e.format == "in_person" and e.venue is not None]
        found = {ev.slug: ev for ev in (await session.scalars(
            select(Events).where(Events.club_id == club.id, Events.slug.in_([e.slug for e in in_person]), Events.is_deleted == False)  # noqa: E712
        )).all()}
        for e in in_person:
            ev = found.get(e.slug)
            if ev is None:
                report.missing.append(f"event {slug}/{e.slug}")
                continue
            new = (e.venue.lat, e.venue.lng)
            if ev.lat is not None and ev.lng is not None and _same((ev.lat, ev.lng), new, SAME):
                report.right.append((slug, e.slug))
                continue
            why = changed_since_seed(ev, c, e)
            if why:
                report.kept.append((slug, e.slug, why))
                continue
            report.moved.append(Move(slug, e.slug, (ev.lat, ev.lng), new, geo.km(ev.lat, ev.lng, *new)))
            if not dry_run:
                geo.apply_place(ev, link=maps_link(*new), manual=new)
    if not dry_run and report.moved:
        await session.commit()
    return report


def render(report: Report, *, dry_run: bool) -> str:
    verb = "would move" if dry_run else "moved"
    lines = [f"{report.clubs} demo club(s) found. {len(report.moved)} pin(s) {verb}, "
             f"{len(report.right)} already on their venue, {len(report.kept)} changed by hand and left alone."]
    for m in report.moved:
        lines.append(f"  {verb}: {m.club}/{m.event}  {m.old[0]:.5f},{m.old[1]:.5f} -> {m.new[0]:.5f},{m.new[1]:.5f}  ({m.km:.1f} km)")
    for club, event, why in report.kept:
        lines.append(f"  left alone: {club}/{event}: {why}")
    if report.missing:
        lines.append("  not in this database (or not demo): " + ", ".join(report.missing))
    if dry_run:
        lines.append("Dry run: nothing changed. To move them: python -m scripts.demo.fix_pins")
    elif report.moved:
        lines.append("Done, in one transaction.")
    return "\n".join(lines)


async def run(*, dry_run: bool, data_dir: Optional[Path] = None, out=print) -> int:
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    try:
        clubs, _ = load(data_dir)
    except DataError as e:
        out(str(e))
        return 2
    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        report = await fix(session, clubs, dry_run=dry_run)
    out(render(report, dry_run=dry_run))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo.fix_pins", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="say what would move, and change nothing")
    parser.add_argument("--data-dir", type=Path, default=None, help="where clubs/*.json are (default: scripts/demo/data)")
    args = parser.parse_args(argv)
    return asyncio.run(run(dry_run=args.dry_run, data_dir=args.data_dir))


if __name__ == "__main__":
    sys.exit(main())
