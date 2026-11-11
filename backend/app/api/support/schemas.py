"""What the desk's routes take (feature-register/support-leads-contract.md).
Answers are plain dicts, shaped in the service."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

Priority = Literal["low", "normal", "high", "urgent"]
Status = Literal["new", "open", "waiting", "resolved", "closed"]


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RequestContext(_In):
    platform: Optional[Literal["app", "web", "dashboard"]] = None
    page_url: Optional[str] = Field(None, max_length=1000)
    error_code: Optional[str] = Field(None, max_length=100)
    track_id: Optional[str] = Field(None, max_length=100)
    event_id: Optional[int] = None
    registration_id: Optional[str] = Field(None, max_length=64)
    club_id: Optional[int] = None
    app_version: Optional[str] = Field(None, max_length=40)


class RequestIn(_In):
    topic: str = Field(..., max_length=30)
    subject: Optional[str] = Field(None, max_length=200)
    message: str = Field(..., min_length=1, max_length=5000)
    attachments: list[str] = Field(default_factory=list, max_length=3)
    email: Optional[EmailStr] = None
    name: Optional[str] = Field(None, max_length=120)
    context: Optional[RequestContext] = None


class ReplyIn(_In):
    message: str = Field(..., min_length=1, max_length=5000)
    attachments: list[str] = Field(default_factory=list, max_length=3)


class SurveyIn(_In):
    solved: bool
    comment: Optional[str] = Field(None, max_length=500)


# ─── Admin ───────────────────────────────────────────────────────────────


class MembersIn(_In):
    user_ids: list[int] = Field(default_factory=list, max_length=200)


class AwayIn(_In):
    away: bool
    away_until: Optional[datetime] = None


class TopicIn(_In):
    key: Optional[str] = Field(None, max_length=30, pattern=r"^[a-z][a-z0-9_]{1,29}$")
    label: Optional[str] = Field(None, min_length=1, max_length=80)
    assign: Optional[Literal["round_robin", "person"]] = None
    assignee_id: Optional[int] = None
    default_priority: Optional[Priority] = None
    order: Optional[int] = Field(None, ge=0, le=1000)
    active: Optional[bool] = None


class SavedReplyIn(_In):
    title: Optional[str] = Field(None, min_length=1, max_length=120)
    body: Optional[str] = Field(None, min_length=1, max_length=20000)
    topic: Optional[str] = Field(None, max_length=30)


class HelpAnswerIn(_In):
    topic: Optional[str] = Field(None, max_length=30)
    question: Optional[str] = Field(None, min_length=1, max_length=300)
    answer: Optional[str] = Field(None, min_length=1, max_length=20000)
    published: Optional[bool] = None
    order: Optional[int] = Field(None, ge=0, le=10000)


class LostReasonIn(_In):
    key: Optional[str] = Field(None, max_length=40, pattern=r"^[a-z][a-z0-9_]{1,39}$")
    label: Optional[str] = Field(None, min_length=1, max_length=80)
    order: Optional[int] = Field(None, ge=0, le=1000)
    active: Optional[bool] = None


class WorkingHours(_In):
    days: list[int] = Field(..., min_length=1, max_length=7)
    start: str
    end: str
    timezone: str = "Asia/Kolkata"


class DeskSettingsIn(_In):
    working_hours: Optional[WorkingHours] = None
    retention_days: Optional[int] = None
    first_reply_target_hours: Optional[float] = None
    resolution_target_hours: Optional[float] = None


class RequesterIn(_In):
    user_id: Optional[int] = None
    club_id: Optional[int] = None
    email: Optional[EmailStr] = None
    name: Optional[str] = Field(None, max_length=120)


class ManualCaseIn(_In):
    requester: RequesterIn
    topic: str = Field(..., max_length=30)
    subject: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1, max_length=20000)
    channel: Literal["manual"] = "manual"
    priority: Optional[Priority] = None


class CasePatch(_In):
    status: Optional[Status] = None
    priority: Optional[Priority] = None
    topic: Optional[str] = Field(None, max_length=30)
    assignee_id: Optional[int] = None
    tags: Optional[list[str]] = Field(None, max_length=20)
    links: Optional[dict] = None


class CaseMessageIn(_In):
    kind: Literal["reply_out", "note"]
    body: Optional[str] = Field(None, max_length=20000)
    attachments: list[str] = Field(default_factory=list, max_length=10)
    saved_reply_id: Optional[int] = None
    preview_only: bool = False


class MergeIn(_In):
    into_id: int


class ToLeadIn(_In):
    type: Literal["club", "sponsor", "venue", "vendor", "other"]
    name: Optional[str] = Field(None, max_length=200)
    force: bool = False
