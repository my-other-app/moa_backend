"""What the quest routes take. What they answer is documented field by field
in feature-register/quests/CONTRACT.md (§2, §3, §4, §7, §8, §9)."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

Mode = Literal["checkmarks", "points"]
SoldOutMode = Literal["first_come", "waitlist"]
Purpose = Literal["station", "swag_desk"]


class QuestPatch(BaseModel):
    enabled: Optional[bool] = None
    title: Optional[str] = Field(default=None, max_length=80)
    intro: Optional[str] = Field(default=None, max_length=400)
    mode: Optional[Mode] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    leaderboard: Optional[bool] = None
    leaderboard_names: Optional[Literal["initial", "full"]] = None
    code_attempts_per_hour: Optional[int] = Field(default=None, ge=1, le=50)


class TaskIn(BaseModel):
    kind: str = Field(min_length=1, max_length=24)
    title: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=300)
    points: int = Field(default=10, ge=0, le=1000)
    required: bool = False
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    config: dict = Field(default_factory=dict)


class TaskPatch(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=300)
    points: Optional[int] = Field(default=None, ge=0, le=1000)
    required: Optional[bool] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    config: Optional[dict] = None


class OrderIn(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=200)


class MilestoneIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    threshold: int = Field(ge=1, le=100_000)


class MilestonePatch(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=80)
    threshold: Optional[int] = Field(default=None, ge=1, le=100_000)
    sort_order: Optional[int] = Field(default=None, ge=0)


class RewardIn(BaseModel):
    merch_item_id: UUID
    sold_out_mode: SoldOutMode = "first_come"


class RewardPatch(BaseModel):
    sold_out_mode: Optional[SoldOutMode] = None


class StationTask(BaseModel):
    title: Optional[str] = Field(default=None, max_length=80)
    points: int = Field(default=10, ge=0, le=1000)
    required: bool = False


class StationLink(BaseModel):
    days: int = Field(default=30, ge=1, le=365)


class StationIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    purpose: Purpose
    task: Optional[StationTask] = None
    link: Optional[StationLink] = None


class NoteIn(BaseModel):
    note: Optional[str] = Field(default=None, max_length=300)


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=80)


class SizeIn(BaseModel):
    variant_label: str = Field(min_length=1, max_length=40)


class HandoverIn(BaseModel):
    variant_label: Optional[str] = Field(default=None, max_length=40)
    door: Optional[str] = Field(default=None, max_length=40)
