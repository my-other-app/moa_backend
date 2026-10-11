from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

TAG = r"^[A-Za-z0-9 _.\-]+$"


class LinkCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    utm_source: str = Field(..., min_length=1, max_length=60, pattern=TAG)
    utm_medium: str = Field(..., min_length=1, max_length=60, pattern=TAG)
    utm_campaign: Optional[str] = Field(default=None, max_length=80, pattern=TAG)
    utm_content: Optional[str] = Field(default=None, max_length=80, pattern=TAG)


class LinkPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    utm_source: Optional[str] = Field(default=None, min_length=1, max_length=60, pattern=TAG)
    utm_medium: Optional[str] = Field(default=None, min_length=1, max_length=60, pattern=TAG)
    utm_campaign: Optional[str] = Field(default=None, max_length=80, pattern=TAG)
    utm_content: Optional[str] = Field(default=None, max_length=80, pattern=TAG)


class LinkOut(BaseModel):
    id: UUID
    name: str
    code: str
    #: The short link to share; opening it records the click and goes on to the destination.
    url: str
    #: Where the short link lands, UTM tags and the code attached.
    destination: str
    utm_source: str
    utm_medium: str
    utm_campaign: Optional[str]
    utm_content: Optional[str]
    #: In the period asked for.
    clicks: int
    uniques: int
    conversions: int
    #: Conversions per hundred clicks.
    rate: float
    #: The step after the conversion: guests who came through the door, or followers who later registered.
    second: int
    created_at: str


class SourceRow(BaseModel):
    source: str
    clicks: int
    conversions: int
    rate: float


class Totals(BaseModel):
    clicks: int
    conversions: int
    rate: float
    #: Conversions that came through a link, as a share of every conversion in the period.
    share_of_all: float
    best_source: Optional[str]
    best_conversions: int


class Deltas(BaseModel):
    """Against the period before, as a percentage; null when there is nothing to compare to."""

    clicks: Optional[float]
    conversions: Optional[float]
    rate_pts: Optional[float]


class Untracked(BaseModel):
    #: Conversions in the period that carried no link.
    conversions: int
    total: int


class LinksSummary(BaseModel):
    scope: str
    period_days: int
    conversion_noun: str
    second_noun: str
    links: list[LinkOut]
    by_source: list[SourceRow]
    totals: Totals
    deltas: Deltas
    untracked: Untracked
    generated_at: str
