"""Request bodies of the games API. Answers are plain dicts (see the services)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BadgeIn(In):
    token: str = Field(min_length=1, max_length=300)


class PinIn(In):
    token: str = Field(min_length=1, max_length=300)
    pin: str = Field(min_length=1, max_length=12)


class MePatch(In):
    public: bool


class CollectIn(In):
    token: str = Field(min_length=1, max_length=300)


class ScanIn(In):
    token: str = Field(min_length=1, max_length=300)


class HintIn(In):
    point_id: Optional[uuid.UUID] = None


HEX = r"^#[0-9a-fA-F]{6}$"


class ScreenColors(In):
    """The venue screen's colours over the theme; a missing or null key keeps the theme's."""

    background: Optional[str] = Field(default=None, pattern=HEX)
    surface: Optional[str] = Field(default=None, pattern=HEX)
    text: Optional[str] = Field(default=None, pattern=HEX)
    accent: Optional[str] = Field(default=None, pattern=HEX)
    gold: Optional[str] = Field(default=None, pattern=HEX)
    silver: Optional[str] = Field(default=None, pattern=HEX)
    bronze: Optional[str] = Field(default=None, pattern=HEX)


class SettingsPatch(In):
    cards_on: Optional[bool] = None
    cards_name: Optional[str] = Field(default=None, min_length=1, max_length=40)
    hunt_on: Optional[bool] = None
    hunt_name: Optional[str] = Field(default=None, min_length=1, max_length=40)
    theme: Optional[Literal["neon_arcade", "sunset_pop", "ocean_glow", "mono_night"]] = None
    accent: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    logo_url: Optional[str] = Field(default=None, max_length=500)
    board_layout: Optional[Literal["podium", "list"]] = None
    #: 0 shows everyone on the venue screen (up to boards.SCREEN_MAX).
    board_rows: Optional[Literal[0, 5, 7, 10, 25, 50]] = None
    ticker_on: Optional[bool] = None
    join_box_on: Optional[bool] = None
    #: null clears every custom colour.
    colors: Optional[ScreenColors] = None

    @field_validator("cards_name", "hunt_name")
    @classmethod
    def _trim(cls, v):
        return v.strip() if isinstance(v, str) else v


class Shares(In):
    common: float = Field(ge=0, le=100)
    uncommon: float = Field(ge=0, le=100)
    rare: float = Field(ge=0, le=100)
    legendary: float = Field(ge=0, le=100)


class CardConfigPatch(In):
    preset: Optional[Literal["easy", "medium", "hard", "custom"]] = None
    shares: Optional[Shares] = None
    legendary_floor: Optional[int] = Field(default=None, ge=1, le=100)
    pin_required: Optional[bool] = None
    collect_on: Optional[bool] = None
    two_way: Optional[bool] = None
    show_holders: Optional[bool] = None


class HeroIn(In):
    name: str = Field(min_length=1, max_length=18)
    tagline: str = Field(default="", max_length=60)
    tier: Literal["common", "uncommon", "rare", "legendary"]
    emblem_key: Optional[str] = Field(default=None, max_length=24)


class HeroPatch(In):
    name: Optional[str] = Field(default=None, min_length=1, max_length=18)
    tagline: Optional[str] = Field(default=None, max_length=60)
    tier: Optional[Literal["common", "uncommon", "rare", "legendary"]] = None
    emblem_key: Optional[str] = Field(default=None, max_length=24)


class OrderIn(In):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class HuntPut(In):
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=720)
    starts_at: Optional[datetime] = None
    clear_start: bool = False
    mode: Optional[Literal["ordered", "free"]] = None
    bonus_n: Optional[int] = Field(default=None, ge=0, le=1000)
    bonus_pts: Optional[int] = Field(default=None, ge=0, le=500)
    hint_cost: Optional[int] = Field(default=None, ge=0, le=500)


class PointIn(In):
    label: str = Field(min_length=1, max_length=80)
    clue: str = Field(min_length=1, max_length=140)
    zone: Optional[str] = Field(default=None, max_length=40)
    base_points: int = Field(default=30, ge=30, le=100)
    hint: Optional[str] = Field(default=None, max_length=100)


class PointPatch(In):
    label: Optional[str] = Field(default=None, min_length=1, max_length=80)
    clue: Optional[str] = Field(default=None, min_length=1, max_length=140)
    zone: Optional[str] = Field(default=None, max_length=40)
    base_points: Optional[int] = Field(default=None, ge=30, le=100)
    hint: Optional[str] = Field(default=None, max_length=100)


class ExtendIn(In):
    minutes: int = Field(ge=1, le=240)


class FeedPatch(In):
    hidden: bool
