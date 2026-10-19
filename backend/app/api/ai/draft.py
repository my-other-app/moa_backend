"""A draft event from a club's notes, a poster, or both.

The model is given the material (as data), today's date in India, the live
topics and interests with their slugs, and the event categories, and asked
for JSON in `SCHEMA`. It is told to use only what the material states; what
comes back is then checked here, never trusted:

- a date and a start time make `event_datetime` (India time, +05:30); a date
  without a time, or no date, leaves it null with a warning saying so;
- missing price, venue (or link, online) and capacity are null with a
  warning each: "No price found in the poster.";
- topics and interests are kept only when they are live slugs; a category
  only when it is one of the platform's; agenda times only as HH:MM;
  question types only the ones the registration form has, options only
  where a choice needs them;
- every string is trimmed to what the event form takes.

Nothing is saved: the organiser reviews the draft in the form.

The model's own field names differ a little from the draft's (a date and a
time rather than one datetime; "" rather than null for text it did not
find): fewer nullable fields is a schema both providers take.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select

from app.api.ai.schemas import QUESTION_TYPES, AgendaItem, EventDraft, Question, Speaker
from app.api.events.models import EventCategories
from app.api.interests import topics as topic_tree
from app.api.subscriptions.service import IST
from app.core.ai.prompting import DATA_RULES, data_block
from app.core.ai.runner import parse_json

MAX_TEXT = 6000
MAX_FILE_BYTES = 10 * 1024 * 1024
#: What a poster or a PDF may be, by its first bytes (never by the name or
#: the browser's say-so).
FILE_TYPES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "application/pdf": (b"%PDF-",),
}

_HHMM = re.compile(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*$")
_URL = re.compile(r"^https?://[^\s<>\"']+$", re.I)
#: What the registration form asks everyone anyway.
_ASKED_ANYWAY = re.compile(
    r"^\s*(your\s+)?(full\s+)?(name|e-?mail(\s+address|\s+id)?|phone(\s+number)?|mobile(\s+number)?|contact\s+number)\s*[?:*]*\s*$",
    re.I,
)
_CHOICE_TYPES = ("select", "radio", "checkbox")

LIMITS = dict(name=100, venue=200, address=300, link=500, about=5000, guidelines=3000, title=160, by=120,
              speaker=100, designation=200, bio=600, label=200, option=100)


def sniff(data: bytes) -> Optional[str]:
    """The file's type from its first bytes: PNG, JPEG, WebP or PDF; None for anything else."""
    for mime, magics in FILE_TYPES.items():
        if any(data.startswith(m) for m in magics):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _text(value, limit: int) -> Optional[str]:
    out = " ".join(str(value or "").split()) if limit <= 300 else str(value or "").strip()
    return out[:limit].strip() or None


# --- what the model is asked for --------------------------------------------------------------

_STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name", "date", "start_time", "end_time", "duration_hours", "format", "venue_name", "venue_address",
        "online_link", "fee", "capacity", "about", "guidelines", "category", "topics", "interests", "agenda",
        "speakers", "questions", "notes",
    ],
    "properties": {
        "name": {"type": "string", "description": "The event's name, or \"\"."},
        "date": {"type": "string", "description": "The start date as YYYY-MM-DD in India time, or \"\" when not stated."},
        "start_time": {"type": "string", "description": "The start time as 24-hour HH:MM, or \"\" when not stated."},
        "end_time": {"type": "string", "description": "The end time as 24-hour HH:MM, or \"\" when not stated."},
        "duration_hours": {"type": ["number", "null"], "description": "Hours, only when stated or given by the times."},
        "format": {"type": "string", "enum": ["in_person", "online", ""]},
        "venue_name": _STR,
        "venue_address": _STR,
        "online_link": {"type": "string", "description": "A meeting or stream link, or \"\"."},
        "fee": {"type": ["number", "null"], "description": "Registration fee in rupees; 0 only when stated as free; null when not stated."},
        "capacity": {"type": ["integer", "null"], "description": "Seats or registrations allowed, or null."},
        "about": _STR,
        "guidelines": _STR,
        "category": {"type": "string", "description": "One name from the categories list, or \"\"."},
        "topics": {"type": "array", "items": _STR},
        "interests": {"type": "array", "items": _STR},
        "agenda": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["at", "title", "by"],
                "properties": {"at": {"type": "string", "description": "HH:MM"}, "title": _STR, "by": _STR},
            },
        },
        "speakers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "designation", "bio"],
                "properties": {"name": _STR, "designation": _STR, "bio": _STR},
            },
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "type", "options", "required"],
                "properties": {
                    "label": _STR,
                    "type": {"type": "string", "enum": list(QUESTION_TYPES)},
                    "options": {"type": "array", "items": _STR},
                    "required": {"type": "boolean"},
                },
            },
        },
        "notes": {"type": "array", "items": _STR},
    },
}


class _Loose(BaseModel):
    """A model's answer: unknown keys ignored, a null where text was asked for read as ""."""

    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _none_is_empty(cls, value, info):
        if value is None:
            annotation = cls.model_fields[info.field_name].annotation
            if annotation is str:
                return ""
            if getattr(annotation, "__origin__", None) is list:
                return []
        return value


class RawAgenda(_Loose):
    at: str = ""
    title: str = ""
    by: str = ""


class RawSpeaker(_Loose):
    name: str = ""
    designation: str = ""
    bio: str = ""


class RawQuestion(_Loose):
    label: str = ""
    type: str = "text"
    options: list[str] = []
    required: bool = False


class RawDraft(_Loose):
    name: str = ""
    date: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_hours: Optional[float] = None
    format: str = ""
    venue_name: str = ""
    venue_address: str = ""
    online_link: str = ""
    fee: Optional[float] = None
    capacity: Optional[int] = None
    about: str = ""
    guidelines: str = ""
    category: str = ""
    topics: list[str] = []
    interests: list[str] = []
    agenda: list[RawAgenda] = []
    speakers: list[RawSpeaker] = []
    questions: list[RawQuestion] = []
    notes: list[str] = []


def parse(text: str) -> RawDraft:
    return RawDraft.model_validate(parse_json(text))


# --- the prompt --------------------------------------------------------------------------------


async def catalogue(session) -> tuple[list[dict], list[str]]:
    """The live topic tree (slugs and names) and the event categories' names."""
    tree = await topic_tree.tree(session)
    names = (await session.scalars(
        select(EventCategories.name).where(EventCategories.is_deleted == False).order_by(EventCategories.name)  # noqa: E712
    )).all()
    categories: list[str] = []
    for name in names:
        if name and name not in categories:
            categories.append(name)
    return tree, categories


def system_prompt(today: date) -> str:
    return (
        "You turn a college club's notes, poster or document into a draft event for MyOtherApp, "
        "an events platform for clubs in India. " + DATA_RULES + "\n\n"
        "Rules:\n"
        "- Use only facts the material states. Never guess or invent a date, time, price, venue, capacity, "
        "link, speaker, agenda item or question. Leave a field empty (\"\", or null where the schema allows) "
        "when the material does not state it.\n"
        f"- Today is {today:%A}, {today.isoformat()}, in India (IST). Resolve relative dates such as "
        "\"this Saturday\" or \"tomorrow\" against today. Dates are YYYY-MM-DD; times are 24-hour HH:MM, India time.\n"
        "- fee is the registration fee in rupees: 0 only when the material says the event is free; null when it says nothing.\n"
        "- topics and interests: only slugs from the lists given, the few that fit best; none when nothing fits.\n"
        "- category: one name from the categories list, written exactly as listed, or \"\".\n"
        "- about: a clear description of the event, built only from the material, at most four short paragraphs.\n"
        "- questions: only questions the material asks registrants, beyond their name, email and phone.\n"
        "- notes: at most three short notes about anything unclear or contradictory in the material."
    )


def user_prompt(*, tree: list[dict], categories: list[str], text: Optional[str], has_file: bool) -> str:
    lines = ["Topics and their interests (slug: name):"]
    for topic in tree:
        lines.append(f"- {topic['slug']}: {topic['name']}")
        for interest in topic.get("interests") or []:
            lines.append(f"  - {interest['slug']}: {interest['name']}")
    lines.append("")
    lines.append("Event categories: " + (", ".join(categories) if categories else "(none)"))
    lines.append("")
    if has_file:
        lines.append("The club's poster or document is attached.")
    if text:
        lines.append("The club's notes:")
        lines.append(data_block("notes", text))
    lines.append("")
    lines.append("Write the draft event as JSON.")
    return "\n".join(lines)


# --- what comes back ---------------------------------------------------------------------------


def _hhmm(value: str) -> Optional[str]:
    match = _HHMM.match(value or "")
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def _date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _where(text: bool, file: bool) -> str:
    if text and file:
        return "your notes or the file"
    return "the file" if file else "your notes"


def build(raw: RawDraft, *, tree: list[dict], categories: list[str], today: date, text: bool, file: bool) -> tuple[EventDraft, list[str]]:
    """The draft the organiser sees, and what it could not find."""
    where = _where(text, file)
    warnings: list[str] = []

    name = _text(raw.name, LIMITS["name"]) or ""
    if not name:
        warnings.append(f"No event name found in {where}.")

    day = _date(raw.date)
    start = _hhmm(raw.start_time)
    end = _hhmm(raw.end_time)
    when = None
    if day is None:
        warnings.append(f"No date found in {where}.")
    elif start is None:
        warnings.append(f"No start time found in {where}; the date given is {day:%a} {day.day} {day:%b %Y}.")
    else:
        when = datetime.combine(day, time.fromisoformat(start), tzinfo=IST).isoformat()
    if day is not None and day < today:
        warnings.append(f"The date found ({day.day} {day:%b %Y}) is in the past.")

    duration = raw.duration_hours if raw.duration_hours is not None and 0 < raw.duration_hours <= 24 * 14 else None
    if duration is None and start and end:
        minutes = (datetime.combine(date.min, time.fromisoformat(end)) - datetime.combine(date.min, time.fromisoformat(start))) / timedelta(minutes=1)
        if minutes > 0:
            duration = minutes / 60
    duration = round(duration, 2) if duration is not None else None

    venue = _text(raw.venue_name, LIMITS["venue"])
    address = _text(raw.venue_address, LIMITS["address"])
    link = _text(raw.online_link, LIMITS["link"])
    if link and not _URL.match(link):
        link = None
    fmt = raw.format if raw.format in ("in_person", "online") else None
    if fmt is None and link and not (venue or address):
        fmt = "online"
    elif fmt is None and (venue or address) and not link:
        fmt = "in_person"
    if fmt == "online":
        if not link:
            warnings.append(f"No online link found in {where}.")
    elif not (venue or address):
        warnings.append(f"No venue found in {where}.")

    fee = round(raw.fee, 2) if raw.fee is not None and raw.fee >= 0 else None
    if fee is None:
        warnings.append(f"No price found in {where}; set the fee, or 0 if it's free.")
    capacity = raw.capacity if raw.capacity is not None and raw.capacity > 0 else None
    if capacity is None:
        warnings.append(f"No capacity found in {where}.")

    by_name = {c.lower(): c for c in categories}
    category = by_name.get((raw.category or "").strip().lower())

    topic_slugs = {t["slug"] for t in tree}
    interest_slugs = {i["slug"] for t in tree for i in (t.get("interests") or [])}
    topics = _pick(raw.topics, topic_slugs, 3)
    interests = _pick(raw.interests, interest_slugs, 6)

    agenda = []
    for item in raw.agenda[:30]:
        at, title = _hhmm(item.at), _text(item.title, LIMITS["title"])
        if at and title:
            agenda.append(AgendaItem(at=at, title=title, by=_text(item.by, LIMITS["by"])))

    speakers, seen = [], set()
    for person in raw.speakers[:20]:
        who = _text(person.name, LIMITS["speaker"])
        if who and who.lower() not in seen:
            seen.add(who.lower())
            speakers.append(Speaker(name=who, designation=_text(person.designation, LIMITS["designation"]), bio=_text(person.bio, LIMITS["bio"])))

    questions = []
    for q in raw.questions[:10]:
        label = _text(q.label, LIMITS["label"])
        if not label or _ASKED_ANYWAY.match(label):
            continue
        kind = q.type if q.type in QUESTION_TYPES else "text"
        options = []
        for option in q.options[:20]:
            option = _text(option, LIMITS["option"])
            if option and option not in options:
                options.append(option)
        if kind in _CHOICE_TYPES and not options:
            kind = "text"
        questions.append(Question(label=label, type=kind, options=options if kind in _CHOICE_TYPES else [], required=bool(q.required)))

    for note in raw.notes[:3]:
        note = _text(note, 200)
        if note:
            warnings.append(note)

    draft = EventDraft(
        name=name, event_datetime=when, duration_hours=duration, format=fmt, venue_name=venue, venue_address=address,
        online_link=link, fee=fee, capacity=capacity, about=_text(raw.about, LIMITS["about"]),
        guidelines=_text(raw.guidelines, LIMITS["guidelines"]), category=category, topics=topics, interests=interests,
        agenda=agenda, speakers=speakers, questions=questions,
    )
    return draft, warnings


def _pick(values: list[str], allowed: set[str], most: int) -> list[str]:
    out: list[str] = []
    for value in values or []:
        slug = (value or "").strip().lower()
        if slug in allowed and slug not in out:
            out.append(slug)
    return out[:most]
