"""The shapes AI assist's endpoints take and give (the dashboard's contract)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.core.response.base_model import CustomBaseModel

QUESTION_TYPES = ("text", "select", "radio", "checkbox", "number", "url", "date")
WRITE_KINDS = ("event_description", "announcement", "email", "sponsor_thanks", "social_caption")


class StatusOut(CustomBaseModel):
    #: The server's switch (APP_AI_ENABLED). Off: every AI action answers 403 AI_DISABLED.
    enabled: bool
    #: The club may use AI assist: its plan has it, or it has its own key and its plan allows that.
    plan_allows: bool
    #: This month's actions on the platform's key (its own key's are not counted).
    used: int
    #: The plan's monthly cap on the platform's key; null is unlimited.
    limit: Optional[int] = None
    #: When `used` starts again from 0: the first of next month, India time.
    resets_at: datetime
    #: The club's actions run on its own key.
    byok: bool = False
    #: Whose model answers: "gemini" (the platform's), or the club's own provider.
    provider: str


class AgendaItem(BaseModel):
    at: str
    title: str
    by: Optional[str] = None


class Speaker(BaseModel):
    name: str
    designation: Optional[str] = None
    bio: Optional[str] = None


class Question(BaseModel):
    label: str
    type: Literal["text", "select", "radio", "checkbox", "number", "url", "date"]
    options: list[str] = Field(default_factory=list)
    required: bool = False


class EventDraft(BaseModel):
    name: str = ""
    #: ISO 8601 in India time (+05:30); null when the material gives no date and time.
    event_datetime: Optional[str] = None
    duration_hours: Optional[float] = None
    format: Optional[Literal["in_person", "online"]] = None
    venue_name: Optional[str] = None
    venue_address: Optional[str] = None
    online_link: Optional[str] = None
    #: In rupees; 0 is free; null when the material does not say.
    fee: Optional[float] = None
    capacity: Optional[int] = None
    about: Optional[str] = None
    guidelines: Optional[str] = None
    #: The name of one of the platform's event categories, or null.
    category: Optional[str] = None
    #: Slugs of live topics and interests only; anything else is dropped.
    topics: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    agenda: list[AgendaItem] = Field(default_factory=list)
    speakers: list[Speaker] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)


class DraftOut(BaseModel):
    draft: EventDraft
    #: What is missing or unclear ("No date found in the poster."), for the organiser to fill in.
    warnings: list[str] = Field(default_factory=list)


class WriteIn(BaseModel):
    kind: Literal["event_description", "announcement", "email", "sponsor_thanks", "social_caption"]
    #: en: English; ml: Malayalam; en_ml: English, then the same in Malayalam.
    language: Literal["en", "ml", "en_ml"] = "en"
    tone: Literal["friendly", "formal", "hype"] = "friendly"
    prompt: str = Field("", max_length=2000)
    #: One of the club's own events: its public facts are given to the model.
    event_id: Optional[int] = None


class WriteOut(BaseModel):
    text: str
    #: Only for an email.
    subject: Optional[str] = None


class FeedbackSummary(BaseModel):
    loved: list[str] = Field(default_factory=list)
    complaints: list[str] = Field(default_factory=list)
    fix_next: list[str] = Field(default_factory=list)
    sentiment: Literal["positive", "mixed", "negative"] = "mixed"
    #: Word for word from the comments.
    quotes: list[str] = Field(default_factory=list)


class FeedbackSummaryOut(CustomBaseModel):
    #: Null until the survey has MIN_RESPONSES responses with comments.
    summary: Optional[FeedbackSummary] = None
    #: Survey responses with comments: what a summary is made from.
    responses: int = 0
    generated_at: Optional[datetime] = None
    #: True when this is the kept summary, made from the same responses.
    cached: bool = False


class KeyIn(BaseModel):
    """A club's own key. Tested with one tiny call, encrypted, and never
    shown, logged or audited again."""

    provider: Literal["gemini", "anthropic"]
    api_key: str = Field(..., min_length=8, max_length=400)
    #: One of `models[provider]` from GET /clubs/ai/key; empty: the first.
    model: Optional[str] = Field(None, max_length=80)


class KeyOut(CustomBaseModel):
    connected: bool
    provider: Optional[str] = None
    model: Optional[str] = None
    #: All anyone sees of the key again.
    key_last4: Optional[str] = None
    verified_at: Optional[datetime] = None
    #: The models a club may pick, per provider, best first.
    models: dict[str, list[str]] = Field(default_factory=dict)
