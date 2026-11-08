"""The wire shapes of missing answers (feature-register/missing-answers-contract.md)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.core.response.base_model import CustomBaseModel


# ─── §1.3 impact ─────────────────────────────────────────────────────────


class ImpactIn(CustomBaseModel):
    additional_details: list[dict[str, Any]] = Field(default_factory=list, max_length=60)


class PeopleCounts(CustomBaseModel):
    registrations: int = 0
    requests: int = 0


class ImpactAdded(CustomBaseModel):
    key: str
    label: str
    required: bool
    askable: int


class ImpactRemoved(CustomBaseModel):
    key: str
    label: str
    answered: int


class ImpactNowRequired(CustomBaseModel):
    key: str
    label: str
    missing: int


class ImpactOptionRemoved(CustomBaseModel):
    key: str
    label: str
    option: Any
    chosen_by: int


class ImpactRelabelled(CustomBaseModel):
    key: str
    # "from" is a Python keyword: kept on the wire by its alias.
    from_: str = Field(..., alias="from", serialization_alias="from")
    to: str


class ImpactTypeChanged(CustomBaseModel):
    key: str
    label: str
    from_: str = Field(..., alias="from", serialization_alias="from")
    to: str
    invalid_after: int


class ImpactOut(CustomBaseModel):
    people: PeopleCounts
    added: list[ImpactAdded] = Field(default_factory=list)
    removed: list[ImpactRemoved] = Field(default_factory=list)
    now_required: list[ImpactNowRequired] = Field(default_factory=list)
    options_removed: list[ImpactOptionRemoved] = Field(default_factory=list)
    relabelled: list[ImpactRelabelled] = Field(default_factory=list)
    type_changed: list[ImpactTypeChanged] = Field(default_factory=list)


# ─── §1.4 summary ────────────────────────────────────────────────────────


class SummaryQuestion(CustomBaseModel):
    key: str
    label: str
    required: bool
    answered: int = 0
    blank: int = 0
    invalid: int = 0
    hidden: int = 0
    missing: int = 0


class StrayKey(CustomBaseModel):
    key: str
    count: int


class SummaryOut(CustomBaseModel):
    people: PeopleCounts
    people_missing: int = 0
    questions: list[SummaryQuestion] = Field(default_factory=list)
    stray: list[StrayKey] = Field(default_factory=list)
    open_asks: int = 0


# ─── §1.5 move ───────────────────────────────────────────────────────────


class MoveIn(CustomBaseModel):
    from_: str = Field(..., alias="from", min_length=1, max_length=100)
    to: str = Field(..., min_length=1, max_length=100)


class MoveOut(CustomBaseModel):
    moved: int
    skipped: int


# ─── §2.1 asks ───────────────────────────────────────────────────────────

Channel = Literal["push", "email", "whatsapp"]


class AskPreviewIn(CustomBaseModel):
    questions: list[str] = Field(..., min_length=1, max_length=20)
    include_requests: bool = True
    refs: Optional[list[str]] = Field(None, max_length=500)


class Reach(CustomBaseModel):
    push: int = 0
    email: int = 0
    whatsapp: int = 0


class WhatsAppState(CustomBaseModel):
    available: bool
    #: NO_FEATURE, NOT_CONNECTED or TEMPLATE_NOT_APPROVED.
    reason: Optional[str] = None


class AskPreviewOut(CustomBaseModel):
    people: int
    reach: Reach
    whatsapp: WhatsAppState


class AskIn(CustomBaseModel):
    questions: list[str] = Field(..., min_length=1, max_length=20)
    channels: list[Channel] = Field(..., min_length=1, max_length=3)
    message: str = Field(..., min_length=1, max_length=300)
    deadline: Optional[AwareDatetime] = None
    remind_after_hours: Optional[int] = Field(None, ge=12, le=168)
    include_requests: bool = True
    refs: Optional[list[str]] = Field(None, max_length=500)
    client_ref: Optional[str] = Field(None, max_length=64)


class AskQuestion(CustomBaseModel):
    key: str
    label: str


class AskCounts(CustomBaseModel):
    people: int = 0
    sent: int = 0
    failed: int = 0
    opened: int = 0
    answered: int = 0
    still_askable: int = 0


class AskOut(CustomBaseModel):
    id: UUID
    created_at: datetime
    created_by: str = ""
    questions: list[AskQuestion]
    channels: list[str]
    message: str
    deadline: Optional[datetime] = None
    remind_after_hours: Optional[int] = None
    status: str
    #: Why it closed: closed_by_club, deadline, done or event_ended (null while open).
    closed_reason: Optional[str] = None
    counts: AskCounts
    reminders_sent: int = 0
    next_reminder_at: Optional[datetime] = None


class AsksOut(CustomBaseModel):
    asks: list[AskOut]


# ─── §2.3 answering ──────────────────────────────────────────────────────


class AnswersIn(CustomBaseModel):
    answers: dict[str, str | int | float | bool | list[str] | None] = Field(default_factory=dict)


class AnswerEvent(CustomBaseModel):
    id: int
    name: str
    starts_at: Optional[datetime] = None
    org_name: str = ""
    poster: Optional[str] = None
    slug: Optional[str] = None


class AnswerPerson(CustomBaseModel):
    first_name: str = ""
    kind: Literal["registration", "request"]


class AnswersView(CustomBaseModel):
    status: Literal["open", "done", "closed", "nothing"]
    event: AnswerEvent
    person: AnswerPerson
    deadline: Optional[datetime] = None
    message: Optional[str] = None
    questions: list[dict[str, Any]] = Field(default_factory=list)
    asked: list[str] = Field(default_factory=list)
    answers: dict[str, Any] = Field(default_factory=dict)
    ticket_url: Optional[str] = None
    #: A request's own page (/requests/{token}): "View request" (null for a registration).
    request_url: Optional[str] = None


# ─── §2.4, §2.5 ──────────────────────────────────────────────────────────


class AnswersNeeded(CustomBaseModel):
    count: int
    url: str
    deadline: Optional[datetime] = None


class MissingBrief(CustomBaseModel):
    key: str
    label: str


class DoorAnswersOut(CustomBaseModel):
    missing_answers: list[MissingBrief] = Field(default_factory=list)
