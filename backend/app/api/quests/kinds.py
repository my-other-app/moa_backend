"""The task kinds: what each one means, its details and their limits (CONTRACT §3)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.response import CustomHTTPException

CHECKED_IN = "checked_in"
NETWORK = "network"
QUIZ_PLAYED = "quiz_played"
QUIZ_SCORE = "quiz_score"
QUIZ_TOP = "quiz_top"
POLLS = "polls"
QA_QUESTION = "qa_question"
FEEDBACK = "feedback"
POSTER_MADE = "poster_made"
POSTER_LINK = "poster_link"
FOLLOW_CLUB = "follow_club"
PROFILE_COMPLETE = "profile_complete"
STATION = "station"
CODE = "code"
MANUAL = "manual"

QUIZ_KINDS = (QUIZ_PLAYED, QUIZ_SCORE, QUIZ_TOP)
#: Kinds the server works out from its own tables (evaluators.py). The rest
#: are set directly: a station scan, a code, an organiser's grant.
AUTOMATIC = (CHECKED_IN, NETWORK, *QUIZ_KINDS, POLLS, QA_QUESTION, FEEDBACK, POSTER_MADE, POSTER_LINK, FOLLOW_CLUB, PROFILE_COMPLETE, STATION)
#: What no event tells the server about: the card and the sweep look again.
POLLED = (POSTER_LINK, FOLLOW_CLUB, PROFILE_COMPLETE)

DIRECTIONS = ("saved", "saved_by", "either")
METRICS = ("clicks", "registrations")


@dataclass(frozen=True)
class Field:
    key: str
    type: str  # "int" | "choice" | "datetime" | "text" | "door"
    default: Any = None
    min: Optional[int] = None
    max: Optional[int] = None
    options: tuple = ()
    required: bool = False
    label: str = ""


@dataclass(frozen=True)
class Kind:
    kind: str
    label: str
    description: str
    default_title: str
    action: Optional[str]
    fields: tuple = field(default_factory=tuple)
    #: The config key whose value is the target, or None for a yes/no task.
    target_key: Optional[str] = None


KINDS: dict[str, Kind] = {
    k.kind: k
    for k in (
        Kind(
            CHECKED_IN,
            "Checked in",
            "Ticked when their ticket is scanned at an entrance (optionally before a time).",
            "Check in",
            "ticket",
            (Field("before", "datetime", label="Before"),),
        ),
        Kind(
            NETWORK,
            "Meet people (Network QR)",
            "Distinct checked-in guests of this event saved by scanning their QR code in the app's Network section, at the event.",
            "Meet {count} people",
            "network_scan",
            (
                Field("count", "int", 3, 1, 50, label="People"),
                Field("direction", "choice", "either", options=DIRECTIONS, label="Who scans"),
                Field("per_minute", "int", 2, 1, 10, label="At most per minute"),
            ),
            target_key="count",
        ),
        Kind(QUIZ_PLAYED, "Played the quiz", "Joined the live quiz signed in and answered at least one question.", "Play the quiz", "live_room"),
        Kind(
            QUIZ_SCORE,
            "Quiz score",
            "Scored at least this many points in the live quiz.",
            "Score {min_score} in the quiz",
            "live_room",
            (Field("min_score", "int", 1000, 1, 1_000_000, required=True, label="Score at least"),),
        ),
        Kind(
            QUIZ_TOP,
            "Quiz top places",
            "Finished in the top places of the live quiz.",
            "Finish in the quiz's top {top}",
            "live_room",
            (Field("top", "int", 10, 1, 100, required=True, label="Top"),),
        ),
        Kind(
            POLLS,
            "Answered polls",
            "Answered live polls while signed in.",
            "Answer {count} polls",
            "live_room",
            (Field("count", "int", 1, 1, 50, label="Polls"),),
            target_key="count",
        ),
        Kind(
            QA_QUESTION,
            "Asked a question",
            "Asked the speakers a question in the live Q&A.",
            "Ask a question",
            "live_room",
            (Field("count", "int", 1, 1, 20, label="Questions"),),
            target_key="count",
        ),
        Kind(FEEDBACK, "Gave feedback", "Sent the event's feedback survey.", "Give feedback", "feedback"),
        Kind(POSTER_MADE, "Made a poster", 'Made their "I\'m attending" poster.', "Make your poster", "poster"),
        Kind(
            POSTER_LINK,
            "Poster link results",
            "Their poster's link brought this many visitors or registrations.",
            "Bring {count} people with your poster",
            "poster",
            (Field("metric", "choice", "clicks", options=METRICS, label="Count"), Field("count", "int", 5, 1, 1000, label="At least")),
            target_key="count",
        ),
        Kind(FOLLOW_CLUB, "Followed the club", "Follows the club hosting the event.", "Follow the club", "follow"),
        Kind(
            PROFILE_COMPLETE,
            "Completed their profile",
            "Their public profile has a headline, a photo, a link and a project.",
            "Complete your profile",
            "profile",
        ),
        Kind(
            STATION,
            "Visited a station",
            "Scanned by staff at a station (a booth, a workshop room).",
            "Visit {station}",
            "station",
            (Field("door_id", "door", required=True, label="Station"),),
        ),
        Kind(
            CODE,
            "Secret code",
            "Typed the code shown on stage, on a slide or around the venue.",
            "Find the secret code",
            "code",
            (Field("code", "text", required=True, label="Code"),),
        ),
        Kind(
            MANUAL,
            "Marked done by the organiser",
            "For what the platform can't see (a hackathon submission…): you tick it per guest.",
            "Submit your project",
            None,
        ),
    )
}


_SPACES = re.compile(r"\s+")


def normal_code(code: Optional[str]) -> str:
    """Case and every space ignored: " Cloud 24 " is "CLOUD24"."""
    return _SPACES.sub("", code or "").upper()


def target(kind: str, config: dict) -> int:
    k = KINDS.get(kind)
    if k is None or k.target_key is None:
        return 1
    try:
        return max(1, int((config or {}).get(k.target_key) or 1))
    except (TypeError, ValueError):
        return 1


def _bad(fields: dict) -> CustomHTTPException:
    return CustomHTTPException(422, message="Some of the task's details aren't right.", error_code="QUEST_TASK_CONFIG", errors={"fields": fields})


def clean_config(kind: str, raw: Optional[dict]) -> dict:
    """The config as kept: each field of the kind, checked and defaulted;
    anything else dropped. A code is kept as typed (shown to the organiser)."""
    k = KINDS.get(kind)
    if k is None:
        raise CustomHTTPException(422, message="That isn't a kind of task.", error_code="QUEST_TASK_CONFIG", errors={"fields": {"kind": "Pick a kind."}})
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {}
    problems: dict = {}
    for f in k.fields:
        value = raw.get(f.key, f.default)
        if value in (None, ""):
            if f.required:
                problems[f.key] = f"{f.label or f.key} is needed."
            continue
        if f.type == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                problems[f.key] = "A whole number."
                continue
            if (f.min is not None and value < f.min) or (f.max is not None and value > f.max):
                problems[f.key] = f"Between {f.min} and {f.max}."
                continue
        elif f.type == "choice":
            if value not in f.options:
                problems[f.key] = "One of: " + ", ".join(f.options) + "."
                continue
        elif f.type == "text":
            value = " ".join(str(value).split())
            if not 3 <= len(normal_code(value)) <= 40:
                problems[f.key] = "3 to 40 letters or digits."
                continue
        elif f.type in ("datetime", "door"):
            value = str(value)
        out[f.key] = value
    if problems:
        raise _bad(problems)
    return out


def default_title(kind: str, config: dict, station_name: Optional[str] = None) -> str:
    k = KINDS[kind]
    values = {**{f.key: f.default for f in k.fields}, **(config or {}), "station": station_name or "the station"}
    try:
        return k.default_title.format(**values)[:80]
    except (KeyError, IndexError, ValueError):
        return k.label


def templates() -> list[dict]:
    return [
        {
            "kind": k.kind,
            "label": k.label,
            "description": k.description,
            "default_title": default_title(k.kind, {}),
            "action": k.action,
            "automatic": k.kind in AUTOMATIC,
            "default_config": {f.key: f.default for f in k.fields if f.default is not None},
            "fields": [
                {
                    "key": f.key,
                    "type": f.type,
                    "label": f.label or f.key,
                    "required": f.required,
                    "min": f.min,
                    "max": f.max,
                    "options": list(f.options),
                    "default": f.default,
                }
                for f in k.fields
            ],
        }
        for k in KINDS.values()
    ]
