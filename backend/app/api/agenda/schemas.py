from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


def _blank_to_none(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


class AgendaItemIn(BaseModel):
    starts_at: datetime
    ends_at: Optional[datetime] = None
    title: str = Field(min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=1000)
    speaker: Optional[str] = Field(default=None, max_length=120)
    speaker_photo_key: Optional[str] = Field(default=None, max_length=300)
    location: Optional[str] = Field(default=None, max_length=120)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Give the block a title.")
        return v

    @field_validator("description", "speaker", "speaker_photo_key", "location", mode="before")
    @classmethod
    def _blank(cls, v):
        return _blank_to_none(v)

    @model_validator(mode="after")
    def _ends_after(self):
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("A block must end after it starts.")
        return self


class AgendaItemPatch(BaseModel):
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=1000)
    speaker: Optional[str] = Field(default=None, max_length=120)
    speaker_photo_key: Optional[str] = Field(default=None, max_length=300)
    location: Optional[str] = Field(default=None, max_length=120)

    @field_validator("description", "speaker", "speaker_photo_key", "location", mode="before")
    @classmethod
    def _blank(cls, v):
        return _blank_to_none(v)


class AgendaItemOut(BaseModel):
    id: UUID
    event_id: int
    starts_at: datetime
    ends_at: Optional[datetime]
    title: str
    description: Optional[str]
    speaker: Optional[str]
    speaker_photo_key: Optional[str]
    speaker_photo_url: Optional[str]
    location: Optional[str]
    sort_order: int
    # Two blocks are parallel when their times overlap.
    parallel: bool


class AgendaOut(BaseModel):
    items: list[AgendaItemOut]
    timezone: str
    # Whether the organiser has saved a programme design; a kiosk shows it only then.
    has_programme: bool
