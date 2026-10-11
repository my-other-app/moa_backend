import re
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class StatusOption(BaseModel):
    key: str
    label: str
    note: str


class SettingsOut(BaseModel):
    status: str
    status_clock: bool
    retention_mode: str
    retention_hours: int
    report_recipients: list[str]
    keep_contacts: bool
    starts_at: datetime
    ends_at: datetime
    statuses: list[StatusOption]


class SettingsPatch(BaseModel):
    status: Optional[str] = Field(default=None, pattern="^(draft|published|live|closed|archived)$")
    status_clock: Optional[bool] = None
    retention_mode: Optional[str] = Field(default=None, pattern="^(keep|delete)$")
    retention_hours: Optional[int] = Field(default=None, ge=1, le=24 * 365)
    report_recipients: Optional[list[str]] = Field(default=None, max_length=10)
    keep_contacts: Optional[bool] = None

    @field_validator("report_recipients")
    @classmethod
    def _emails(cls, v):
        if v is None:
            return v
        out = []
        for raw in v:
            e = (raw or "").strip().lower()
            if not e:
                continue
            if not EMAIL.match(e):
                raise ValueError(f"{raw} is not an email address.")
            if e not in out:
                out.append(e)
        return out


class ReportOut(BaseModel):
    id: UUID
    filename: str
    figures: dict
    generated_at: datetime
    generated_by_name: Optional[str]
    delivered_to: list[str]


class RetentionStatusOut(BaseModel):
    event_id: int
    mode: str
    state: str
    run_at: datetime
    attempts: int
    last_error: Optional[str]
    refusal: Optional[str]
    refused_at: Optional[datetime]
    completed_at: Optional[datetime]
    deleted: Optional[dict]
    recipients: list[str]
    preview: dict
    keeps_contacts: bool
    contacts_kept: int
    ended: bool
    ends_at: datetime


class RunBody(BaseModel):
    confirm_slug: str = Field(min_length=1, max_length=120)


class RunResult(BaseModel):
    state: str
    refusal: Optional[str]
    error: Optional[str]
    deleted: Optional[dict]
