from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

ReprintName = Literal["anyone", "organiser", "never"]


class CardSettings(BaseModel):
    reprint_policy: ReprintName = "anyone"
    auto_print_badges: bool = False
    preprinted_carries: list[str] = Field(default_factory=list)


class CardSettingsUpdate(BaseModel):
    reprint_policy: Optional[ReprintName] = None
    auto_print_badges: Optional[bool] = None
    preprinted_carries: Optional[list[str]] = Field(default=None, max_length=10)


class RoleCount(BaseModel):
    role: str
    registered: int
    printed: int


class CardSummary(BaseModel):
    registered: int
    printed: int
    by_role: list[RoleCount]
    design_version: int
    jobs: int
    settings: CardSettings
    preprinted_layers: list[dict]
    reprint_policies: list[dict]


class PrintJobOut(BaseModel):
    id: UUID
    source: str
    door: Optional[str]
    cards: int
    guest: Optional[str]
    design_version: int
    overrode: bool
    error: Optional[str]
    created_at: datetime
