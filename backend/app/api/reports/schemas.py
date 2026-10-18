from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.core.response.base_model import CustomBaseModel

TargetType = Literal["thread", "comment", "user", "club"]
Reason = Literal["spam", "harassment", "hate", "sexual", "violence", "misinformation", "other"]
Action = Literal["dismiss", "remove_content", "suspend_author"]


class ReportCreate(BaseModel):
    target_type: TargetType
    #: A thread's or a comment's id (the UUID string the threads API hands
    #: out), or a user's or a club's id (a number, or its digits as a string).
    target_id: int | str
    reason: Reason
    note: Optional[str] = Field(None, max_length=500)

    @field_validator("note")
    @classmethod
    def _trim(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        return v or None


class ReportOut(BaseModel):
    id: int
    status: Literal["open", "resolved"]


class BlockCreate(BaseModel):
    user_id: int


class BlockOut(BaseModel):
    user_id: int
    name: str
    #: An image's variations by name ({"original", "thumbnail", ...}), or null.
    avatar: Optional[dict] = None


class Person(CustomBaseModel):
    id: int
    name: str


class ReportClub(CustomBaseModel):
    id: int
    name: str
    slug: str


class AdminReport(CustomBaseModel):
    id: int
    target_type: TargetType
    #: Always a string: a thread's or a comment's UUID, or a user's or a club's id in digits.
    target_id: str
    reason: Reason
    note: Optional[str] = None
    status: Literal["open", "resolved"]
    reporter: Person
    #: The reported words (a thread's or comment's text, a user's or club's
    #: name), as they stand even after removal.
    target_preview: str
    target_author: Optional[Person] = None
    club: Optional[ReportClub] = None
    #: Open reports on the same thing from different people, now. A comment
    #: at three or more is hidden from everyone but its author.
    report_count: int
    #: The thread, comment, user or club is gone (removed or deleted).
    target_removed: bool = False
    created_at: datetime
    resolved_at: Optional[datetime] = None
    resolution: Optional[Action] = None
    resolution_note: Optional[str] = None


class AdminReportsPage(CustomBaseModel):
    items: list[AdminReport]
    total: int


class ResolveIn(BaseModel):
    action: Action
    note: Optional[str] = Field(None, max_length=500)


class ResolveOut(CustomBaseModel):
    #: How many open reports on the same thing this resolved, this one included.
    resolved: int
    report: AdminReport
