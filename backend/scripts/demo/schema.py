"""What a demo club's file and the people file hold, checked before
anything is written.

    data/clubs/<slug>.json   one club: its page, brand, team, store, events, posts
    data/people.json         the main demo user, the attendees' names, colleges, comment lines

Loading a file that breaks a rule stops the seed with every problem named,
before the database is touched. Two rules guard real money:

- an upcoming event with a fee must be sold out: nobody from the public can
  then pay for a ticket to an event that is not happening;
- a sold-out event takes exactly its capacity in registrations (set here,
  whatever `registrations` says), so it stays full.

Interest and topic slugs are checked against the database by the seed, as
the tree lives there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

HEX = r"^#[0-9A-Fa-f]{6}$"
SLUG = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
CLOCK = r"^([01]\d|2[0-3]):[0-5]\d$"

#: The one domain every invented address is at; the mailer never writes to it (settings.MAIL_SUPPRESS_DOMAINS).
DEMO_DOMAIN = "demo.myotherapp.in"

DATA = Path(__file__).resolve().parent / "data"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Org(Strict):
    name: str = Field(min_length=2, max_length=100)
    city: str = Field(min_length=2, max_length=100)


class Location(Strict):
    name: str = Field(min_length=2, max_length=200)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class Contact(Strict):
    email: str = Field(max_length=100)
    phone: str = Field(max_length=20)

    @field_validator("email")
    @classmethod
    def _demo_address(cls, v: str) -> str:
        if not v.lower().endswith("." + DEMO_DOMAIN) and not v.lower().endswith("@" + DEMO_DOMAIN):
            raise ValueError(f"a demo club's address must be at {DEMO_DOMAIN} (or a subdomain of it), never a real inbox")
        return v


class Socials(Strict):
    instagram: Optional[str] = Field(default=None, max_length=100)
    linkedin: Optional[str] = Field(default=None, max_length=100)
    x: Optional[str] = Field(default=None, max_length=100)


class Brand(Strict):
    primary: str = Field(pattern=HEX)
    accent: str = Field(pattern=HEX)
    ink: str = Field(pattern=HEX)
    paper: str = Field(pattern=HEX)
    display_font: Literal["bebas", "dm-serif", "inter"]
    motif: Literal["waves", "grid", "circles", "stripes", "blobs", "triangles", "dots"]


class TeamMember(Strict):
    name: str = Field(min_length=2, max_length=100)
    role: str = Field(min_length=2, max_length=80)
    bio: Optional[str] = Field(default=None, max_length=300)


class Partner(Strict):
    name: str = Field(min_length=2, max_length=100)
    role: Optional[str] = Field(default=None, max_length=80)
    since: Optional[int] = Field(default=None, ge=1990, le=2100)


class MerchItem(Strict):
    key: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    name: str = Field(min_length=2, max_length=120)
    description: Optional[str] = Field(default=None, max_length=300)
    price: float = Field(ge=0, le=100000)
    kind: Literal["tee", "hoodie", "cap", "tote", "sticker", "mug", "lanyard", "notebook"]
    color: str = Field(pattern=HEX)
    sizes: Optional[dict[str, int]] = None
    stock: Optional[int] = Field(default=None, ge=0, le=1_000_000)

    @field_validator("sizes")
    @classmethod
    def _sizes(cls, v):
        if v is None:
            return v
        if not v or len(v) > 20:
            raise ValueError("sizes: one to twenty labels, or null")
        for label, n in v.items():
            if not label.strip() or len(label) > 40 or n < 0:
                raise ValueError(f"sizes: {label!r} needs a label up to 40 characters and a stock of 0 or more")
        return v

    @model_validator(mode="after")
    def _stock_or_sizes(self):
        if self.sizes is not None and self.stock is not None:
            raise ValueError(f"merch {self.key}: give sizes (stock per size) or stock, not both")
        return self


class When(Strict):
    days_from_now: int = Field(ge=-400, le=400)
    time: str = Field(pattern=CLOCK)
    duration_hours: float = Field(gt=0, le=72)

    @field_validator("days_from_now")
    @classmethod
    def _not_today(cls, v: int) -> int:
        if v == 0:
            raise ValueError("days_from_now 0 is past or upcoming depending on the hour the seed runs; use -1 or 1")
        return v


class Venue(Strict):
    name: str = Field(min_length=2, max_length=120)
    address: str = Field(min_length=2, max_length=200)
    # Where the venue is: its event's pin and map link. The venue's own,
    # never its club's (a weekend at Vagamon was once pinned in Kochi).
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class Speaker(Strict):
    name: str = Field(min_length=2, max_length=100)
    designation: Optional[str] = Field(default=None, max_length=200)
    bio: Optional[str] = Field(default=None, max_length=600)


class AgendaEntry(Strict):
    at: str = Field(pattern=CLOCK)
    title: str = Field(min_length=1, max_length=160)
    by: Optional[str] = Field(default=None, max_length=120)
    #: Which day of a multi-day event, from 1. Unset, an entry is on the
    #: day of the one before it, or the next day when its time is earlier.
    day: Optional[int] = Field(default=None, ge=1, le=4)


class Question(Strict):
    key: str = Field(pattern=r"^[a-z0-9_]{1,60}$")
    label: str = Field(min_length=2, max_length=200)
    type: Literal["text", "select", "radio", "checkbox", "number", "url", "date"]
    options: list[str] = Field(default_factory=list)
    required: bool = True

    @model_validator(mode="after")
    def _options(self):
        if self.type in ("select", "radio", "checkbox") and len(self.options) < 2:
            raise ValueError(f"question {self.key}: a {self.type} needs at least two options")
        return self


class Swag(Strict):
    name: str = Field(min_length=2, max_length=120)
    sponsor: Optional[str] = Field(default=None, max_length=120)


class Feedback(Strict):
    responses: int = Field(ge=0, le=5000)
    avg_rating: float = Field(ge=1, le=5)
    comments: list[str] = Field(default_factory=list)

    @field_validator("comments")
    @classmethod
    def _short(cls, v: list[str]) -> list[str]:
        for c in v:
            if len(c) > 1000:
                raise ValueError("a feedback comment is at most 1000 characters")
        return v


class LivePoll(Strict):
    question: str = Field(min_length=2, max_length=300)
    options: list[str] = Field(min_length=2, max_length=8)


class Live(Strict):
    poll: Optional[LivePoll] = None
    qna: list[str] = Field(default_factory=list)

    @field_validator("qna")
    @classmethod
    def _short(cls, v: list[str]) -> list[str]:
        for q in v:
            if len(q) > 500:
                raise ValueError("a live question is at most 500 characters")
        return v


class Poster(Strict):
    headline: str = Field(min_length=2, max_length=90)
    sub: str = Field(default="", max_length=160)


class Event(Strict):
    slug: str = Field(pattern=SLUG, max_length=110)
    name: str = Field(min_length=2, max_length=100)
    when: When
    format: Literal["in_person", "online"]
    venue: Optional[Venue] = None
    category: Literal["Workshop", "Meetup", "Hackathon", "Talk", "Competition", "Festival", "Exhibition", "Tournament"]
    interests: list[str] = Field(default_factory=list)
    fee: float = Field(default=0, ge=0, le=100000)
    sold_out: bool = False
    capacity: int = Field(ge=1, le=20000)
    registrations: int = Field(ge=0, le=20000)
    # Past events only; null (or left out) on an upcoming one.
    attended_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    certificates: Optional[bool] = None
    about: str = Field(min_length=10, max_length=8000)
    guidelines: Optional[str] = Field(default=None, max_length=8000)
    speakers: list[Speaker] = Field(default_factory=list)
    agenda: list[AgendaEntry] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    merch: list[str] = Field(default_factory=list)
    swag: list[Swag] = Field(default_factory=list)
    feedback: Optional[Feedback] = None
    live: Optional[Live] = None
    poster: Poster

    @property
    def past(self) -> bool:
        return self.when.days_from_now < 0

    @property
    def seats(self) -> int:
        """Registrations to make: the capacity when sold out, else as given (never past the capacity)."""
        return self.capacity if self.sold_out else min(self.registrations, self.capacity)

    @model_validator(mode="after")
    def _rules(self):
        problems = []
        if not self.past and self.fee > 0 and not self.sold_out:
            problems.append(
                f"event {self.slug}: an upcoming event with a fee (₹{self.fee:g}) must be sold_out, "
                "so no member of the public can pay real money for it"
            )
        if self.registrations > self.capacity:
            problems.append(f"event {self.slug}: {self.registrations} registrations over its capacity of {self.capacity}")
        if self.format == "in_person" and self.venue is None:
            problems.append(f"event {self.slug}: an in-person event needs a venue")
        keys = [q.key for q in self.questions]
        if len(keys) != len(set(keys)):
            problems.append(f"event {self.slug}: question keys repeat")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class ThreadComment(Strict):
    text: str = Field(min_length=1, max_length=500)


class Thread(Strict):
    days_ago: int = Field(ge=0, le=400)
    kind: Literal["post", "poll", "announcement"]
    text: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(default_factory=list)
    likes: int = Field(default=0, ge=0, le=100000)
    comments: list[ThreadComment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _poll(self):
        if self.kind == "poll":
            if not 2 <= len(self.options) <= 4:
                raise ValueError("a poll has two to four options")
            if any(not o.strip() or len(o) > 60 for o in self.options):
                raise ValueError("a poll option is 1 to 60 characters")
            if len({o.strip().lower() for o in self.options}) != len(self.options):
                raise ValueError("a poll's options must differ")
        elif self.options:
            raise ValueError(f"a {self.kind} has no options")
        return self


class Club(Strict):
    slug: str = Field(pattern=SLUG, max_length=60)
    name: str = Field(min_length=2, max_length=100)
    tagline: str = Field(min_length=2, max_length=80)
    about: str = Field(min_length=10, max_length=8000)
    org: Optional[Org] = None
    location: Location
    contact: Contact
    socials: Socials = Field(default_factory=Socials)
    topics: list[str] = Field(default_factory=list)
    interests: list[str] = Field(min_length=1)
    brand: Brand
    team: list[TeamMember] = Field(min_length=1, max_length=12)
    partners: list[Partner] = Field(default_factory=list, max_length=8)
    followers: int = Field(default=0, ge=0, le=100000)
    merch: list[MerchItem] = Field(default_factory=list)
    events: list[Event] = Field(min_length=1)
    threads: list[Thread] = Field(default_factory=list)

    @property
    def owner_email(self) -> str:
        return f"{self.slug}@{DEMO_DOMAIN}"

    @model_validator(mode="after")
    def _references(self):
        problems = []
        keys = [m.key for m in self.merch]
        if len(keys) != len(set(keys)):
            problems.append("merch keys repeat")
        for e in self.events:
            missing = [k for k in e.merch if k not in keys]
            if missing:
                problems.append(f"event {e.slug} offers merch that is not in the club's store: {', '.join(missing)}")
        slugs = [e.slug for e in self.events]
        if len(slugs) != len(set(slugs)):
            problems.append("event slugs repeat")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class MainUser(Strict):
    name: str = Field(min_length=2, max_length=100)
    username: str = Field(pattern=r"^[A-Za-z0-9_.]{3,40}$")
    college: Optional[str] = Field(default=None, max_length=100)
    interests: list[str] = Field(default_factory=list)
    bio: Optional[str] = Field(default=None, max_length=600)


class People(Strict):
    main_user: MainUser
    attendee_names: list[str] = Field(min_length=10)
    colleges: list[str] = Field(default_factory=list)
    comment_snippets: list[str] = Field(default_factory=list)

    @field_validator("attendee_names")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        bad = [n for n in v if len(n.split()) < 2 or len(n) > 100]
        if bad:
            raise ValueError(f"every attendee needs a first and a last name (up to 100 characters): {bad[:5]}")
        return v


class DataError(Exception):
    """The content files break the rules; the message names every problem."""


def _problems(path: Path, exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"])
        out.append(f"{path.name}: {where}: {err['msg']}" if where else f"{path.name}: {err['msg']}")
    return out


def load(data_dir: Path | None = None, *, include_example: bool = False) -> tuple[list[Club], People]:
    """Every club file (files starting with _ only when `include_example`)
    and the people file, checked; DataError lists everything wrong."""
    root = Path(data_dir) if data_dir else DATA
    problems: list[str] = []
    clubs: list[Club] = []
    files = sorted((root / "clubs").glob("*.json"))
    for path in files:
        if path.name.startswith("_") and not include_example:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"{path.name}: not readable JSON ({e})")
            continue
        try:
            club = Club.model_validate(raw)
        except ValidationError as e:
            problems.extend(_problems(path, e))
            continue
        if path.stem.lstrip("_") != club.slug and not path.name.startswith("_"):
            problems.append(f"{path.name}: the file is named for {path.stem!r} but the club's slug is {club.slug!r}")
        clubs.append(club)
    people = None
    people_path = root / "people.json"
    try:
        people = People.model_validate(json.loads(people_path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        problems.append(f"{people_path}: missing")
    except json.JSONDecodeError as e:
        problems.append(f"people.json: not readable JSON ({e})")
    except ValidationError as e:
        problems.extend(_problems(people_path, e))

    if not clubs and not problems:
        problems.append(f"no club files in {root / 'clubs'}")
    seen_clubs: dict[str, str] = {}
    seen_events: dict[str, str] = {}
    for c in clubs:
        if c.slug in seen_clubs:
            problems.append(f"club slug {c.slug} is used twice")
        seen_clubs[c.slug] = c.slug
        for e in c.events:
            if e.slug in seen_events:
                problems.append(f"event slug {e.slug} is used by {seen_events[e.slug]} and {c.slug}")
            seen_events[e.slug] = c.slug
    names = {}
    for c in clubs:
        key = re.sub(r"\s+", "", c.name).lower()
        if key in names:
            problems.append(f"clubs {names[key]} and {c.slug} have the same name")
        names[key] = c.slug
    if problems:
        raise DataError("The demo content has problems:\n  " + "\n  ".join(problems))
    return clubs, people
