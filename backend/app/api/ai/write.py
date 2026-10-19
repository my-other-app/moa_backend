"""Writing for a club: an event description, an announcement, an email, a
thank-you to a sponsor, a caption.

The model is given what the club asked for (as data) and, when it names one
of its events, that event's public facts -- its name, date, venue and about,
and the club's name -- and nothing else about it: no attendee, no contact.
It is told to invent nothing; a fact the piece needs and does not have is
left as a placeholder in square brackets ("[venue]") for the club to fill.

`ml` is Malayalam, in Malayalam script; `en_ml` is the English, then the
same in Malayalam. Only an email has a subject.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

from app.api.events.models import Events
from app.api.subscriptions.service import IST
from app.core.ai.prompting import DATA_RULES, data_block
from app.core.ai.runner import parse_json

KINDS = {
    "event_description": (
        "an event description for the event's page: two to four short paragraphs of plain text, "
        "no headings and no markdown"
    ),
    "announcement": (
        "a short announcement to the club's followers, sent as a phone notification: plain text, "
        "at most 300 characters"
    ),
    "email": (
        "an email from the club: a subject line under 80 characters, and a plain-text body with a greeting, "
        "the message and a sign-off from the club"
    ),
    "sponsor_thanks": (
        "a thank-you note to a sponsor or partner of the club: warm and specific, 80 to 150 words, plain text"
    ),
    "social_caption": (
        "a social media caption: under 1,500 characters, plain text, with a few relevant hashtags at the end"
    ),
}

LANGUAGES = {
    "en": "English",
    "ml": "Malayalam, written in Malayalam script",
    "en_ml": (
        "English first, then the same text in Malayalam (Malayalam script), the two separated by a blank line; "
        "an email's subject in English"
    ),
}

TONES = {
    "friendly": "friendly: warm and simple",
    "formal": "formal: polite and professional",
    "hype": "hype: energetic and exciting, emojis welcome",
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "subject"],
    "properties": {
        "text": {"type": "string", "description": "The piece itself."},
        "subject": {"type": "string", "description": "An email's subject line; \"\" for anything else."},
    },
}


class RawWriting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    subject: Optional[str] = ""

    @field_validator("text")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not (value or "").strip():
            raise ValueError("no text")
        return value


def parse(text: str) -> RawWriting:
    return RawWriting.model_validate(parse_json(text))


def system_prompt(kind: str, language: str, tone: str) -> str:
    return (
        "You write for a college club in India that runs its events on MyOtherApp. " + DATA_RULES + "\n\n"
        f"Write {KINDS[kind]}.\n"
        f"Language: {LANGUAGES[language]}.\n"
        f"Tone: {TONES[tone]}.\n"
        "Use only the facts in the event's details and the club's request. Never invent a date, time, price, "
        "venue, name, link, number or quote. If the piece needs a fact you were not given, write a short "
        "placeholder in square brackets, such as [venue] or [date], for the club to fill in."
    )


def event_facts(event: Events, club_name: Optional[str]) -> str:
    """An event's public facts, as the model reads them."""
    when = event.event_datetime.astimezone(IST) if event.event_datetime else None
    lines = [f"Name: {event.name}"]
    if club_name:
        lines.append(f"Club: {club_name}")
    if when is not None:
        lines.append(f"Date: {when:%A} {when.day} {when:%B %Y}, {when:%I:%M %p} India time")
    if event.is_online:
        lines.append("Venue: online")
    elif event.location_name:
        lines.append(f"Venue: {event.location_name}")
    if event.about:
        lines.append(f"About: {event.about[:2000]}")
    return "\n".join(lines)


def user_prompt(*, prompt: str, event: Optional[Events], club_name: Optional[str]) -> str:
    parts = []
    if event is not None:
        parts.append("The event:")
        parts.append(data_block("event", event_facts(event, club_name)))
    elif club_name:
        parts.append("The club:")
        parts.append(data_block("club", club_name))
    if (prompt or "").strip():
        parts.append("What the club asked for:")
        parts.append(data_block("request", prompt))
    parts.append("Write it as JSON.")
    return "\n".join(parts)


def result(raw: RawWriting, kind: str) -> tuple[str, Optional[str]]:
    text = raw.text.strip()[:10000]
    subject = " ".join((raw.subject or "").split())[:150] or None
    return text, subject if kind == "email" else None
