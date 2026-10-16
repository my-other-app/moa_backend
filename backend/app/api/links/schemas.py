from datetime import datetime
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
    #: "custom", or "website" for the club website's own link.
    kind: str = "custom"


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


# ─── The Tracking links page ──────────────────────────────────────────────


class LinkTarget(BaseModel):
    kind: str
    id: Optional[int]
    name: str
    starts_at: Optional[datetime]


class LinkRow(BaseModel):
    id: UUID
    name: str
    code: str
    url: str
    destination: str
    utm_source: str
    utm_medium: str
    utm_campaign: Optional[str]
    utm_content: Optional[str]
    target: LinkTarget
    conversion_noun: str
    second_noun: str
    clicks: int
    uniques: int
    conversions: int
    rate: float
    second: int
    clicks_ever: int
    last_click_at: Optional[datetime]
    created_at: datetime
    kind: str = "custom"


class SeriesPoint(BaseModel):
    date: str
    clicks: int
    conversions: int


class SourceSummary(BaseModel):
    source: str
    links: int
    clicks: int
    conversions: int
    rate: float


class BestSource(BaseModel):
    source: str
    conversions: int
    rate: float


class BestLink(BaseModel):
    id: UUID
    name: str
    source: str
    conversions: int
    rate: float


class OverviewTotals(BaseModel):
    links: int
    clicks: int
    uniques: int
    conversions: int
    rate: float
    follows: int
    registrations: int
    second: int
    share_of_all: float
    best_source: Optional[BestSource]
    best_link: Optional[BestLink]
    last_click_at: Optional[datetime]


class TrackedOfAll(BaseModel):
    tracked: int
    total: int


class OverviewUntracked(BaseModel):
    follows: TrackedOfAll
    registrations: TrackedOfAll
    conversions: int
    total: int


class NeverOpened(BaseModel):
    id: UUID
    name: str
    target: LinkTarget


class OverviewAttention(BaseModel):
    never_opened: list[NeverOpened]


class AllTime(BaseModel):
    links: int
    clicks: int


class LinksOverview(BaseModel):
    scope: str
    period_days: int
    grain: str
    links: list[LinkRow]
    series: list[SeriesPoint]
    by_source: list[SourceSummary]
    totals: OverviewTotals
    deltas: Deltas
    untracked: OverviewUntracked
    attention: OverviewAttention
    all_time: AllTime
    generated_at: datetime


class DetailPoint(BaseModel):
    date: str
    clicks: int


class DeviceShare(BaseModel):
    device: str
    clicks: int


class ReferrerShare(BaseModel):
    referrer: str
    clicks: int


class HourShare(BaseModel):
    hour: int
    clicks: int


class LinkDetail(BaseModel):
    id: UUID
    name: str
    code: str
    url: str
    destination: str
    clicks: int
    uniques: int
    first_click_at: Optional[datetime]
    last_click_at: Optional[datetime]
    series: list[DetailPoint]
    devices: list[DeviceShare]
    referrers: list[ReferrerShare]
    hours: list[HourShare]
    created_at: datetime
    generated_at: datetime
