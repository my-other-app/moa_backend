"""An event's survey comments, summed up: what people loved, what they did
not, what to fix next time, the overall mood, and a few quotes.

Only what attendees wrote and the ratings they gave go to the model -- no
name, no email, no registration -- with addresses and numbers scrubbed out
of the comments too, and the event's name for context. The quotes that come
back are kept only when they are word for word in a comment.

A summary needs at least MIN_RESPONSES responses with a comment; below
that there is no call and no summary. It is kept (ai_feedback_summaries)
with the number of responses it was made from and given again, with no
call, until that number changes; `refresh` makes a new one whatever, and
counts as an action like any other.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select

from app.api.ai.schemas import FeedbackSummary
from app.api.events.models import Events
from app.api.live.models import FeedbackResponses, FeedbackSurveys
from app.core.ai.prompting import DATA_RULES, data_block, scrub_contacts
from app.core.ai.runner import parse_json

MIN_RESPONSES = 5
#: The newest responses sent, at most: a summary does not need a thousand.
MAX_SENT = 200
MAX_POINTS = 5
MAX_QUOTES = 3

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["loved", "complaints", "fix_next", "sentiment", "quotes"],
    "properties": {
        "loved": {"type": "array", "items": {"type": "string"}},
        "complaints": {"type": "array", "items": {"type": "string"}},
        "fix_next": {"type": "array", "items": {"type": "string"}},
        "sentiment": {"type": "string", "enum": ["positive", "mixed", "negative"]},
        "quotes": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = (
    "You summarise attendees' feedback on a college club's event for the club. " + DATA_RULES + "\n\n"
    "Each response is a JSON object with the ratings the attendee gave and the comments they wrote.\n"
    f"- loved: at most {MAX_POINTS} short points attendees liked.\n"
    f"- complaints: at most {MAX_POINTS} short points attendees did not like.\n"
    f"- fix_next: at most {MAX_POINTS} concrete, practical things to do better next time, drawn from the feedback.\n"
    "- sentiment: positive, mixed or negative, overall.\n"
    f"- quotes: at most {MAX_QUOTES} short quotes copied word for word from the comments. Never write a quote "
    "that is not in a comment, and never one that names a person.\n"
    "Write the points in plain English. Base everything on the responses; add nothing they do not say."
)


class RawSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    loved: list[str] = []
    complaints: list[str] = []
    fix_next: list[str] = []
    sentiment: str = "mixed"
    quotes: list[str] = []

    @field_validator("loved", "complaints", "fix_next", "quotes", mode="before")
    @classmethod
    def _list(cls, value):
        return [] if value is None else value


def parse(text: str) -> RawSummary:
    return RawSummary.model_validate(parse_json(text))


async def responses(session, event: Events) -> list[dict]:
    """The survey's responses that have a comment, newest first: each its
    ratings (by question label) and its comments, and nothing else."""
    survey = await session.scalar(select(FeedbackSurveys).where(FeedbackSurveys.event_id == event.id))
    if survey is None:
        return []
    questions = [q for q in (survey.questions or []) if isinstance(q, dict) and q.get("key")]
    texts = [q for q in questions if q.get("kind") == "text"]
    scores = [q for q in questions if q.get("kind") in ("rating", "nps")]
    if not texts:
        return []
    rows = (await session.scalars(
        select(FeedbackResponses.answers)
        .where(FeedbackResponses.survey_id == survey.id)
        .order_by(FeedbackResponses.submitted_at.desc())
    )).all()
    out = []
    for answers in rows:
        answers = answers or {}
        comments = [" ".join(str(answers[q["key"]]).split())[:1000] for q in texts if str(answers.get(q["key"]) or "").strip()]
        if not comments:
            continue
        ratings = {}
        for q in scores:
            value = answers.get(q["key"])
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                label = str(q.get("label") or q["key"])[:80]
                ratings[label] = f"{value}/{10 if q.get('kind') == 'nps' else 5}"
        out.append({"ratings": ratings, "comments": comments})
    return out


def user_prompt(event: Events, items: list[dict]) -> str:
    lines = "\n".join(json.dumps(item, ensure_ascii=False) for item in items[:MAX_SENT])
    return "\n".join([
        "The event:",
        data_block("event", event.name),
        f"The responses ({min(len(items), MAX_SENT)} of {len(items)}, newest first), one per line:",
        data_block("responses", lines),
        "Summarise them as JSON.",
    ])


def _norm(value: str) -> str:
    return re.sub(r"\W+", " ", (value or "").lower()).strip()


def _points(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        value = " ".join(str(value or "").split())[:200]
        if value and value not in out:
            out.append(value)
    return out[:MAX_POINTS]


def build(raw: RawSummary, items: list[dict]) -> FeedbackSummary:
    """The summary as kept and shown; a quote survives only if a comment has it word for word."""
    said = " | ".join(_norm(scrub_contacts(c)) for item in items[:MAX_SENT] for c in item["comments"])
    quotes: list[str] = []
    for quote in raw.quotes or []:
        quote = " ".join(str(quote or "").strip().strip("\"“”'").split())[:300]
        if quote and len(_norm(quote)) >= 3 and _norm(quote) in said and quote not in quotes:
            quotes.append(quote)
    sentiment = raw.sentiment if raw.sentiment in ("positive", "mixed", "negative") else "mixed"
    return FeedbackSummary(
        loved=_points(raw.loved), complaints=_points(raw.complaints), fix_next=_points(raw.fix_next),
        sentiment=sentiment, quotes=quotes[:MAX_QUOTES],
    )


def cached_summary(value: Optional[dict]) -> Optional[FeedbackSummary]:
    try:
        return FeedbackSummary.model_validate(value) if value else None
    except ValueError:
        return None
