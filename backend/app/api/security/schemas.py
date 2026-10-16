from typing import Optional

from pydantic import BaseModel, Field


class SessionOut(BaseModel):
    id: str
    device: str
    kind: str  # phone | tablet | computer | unknown
    place: Optional[str]
    ip_address: Optional[str]
    created_at: Optional[str]
    last_seen_at: Optional[str]
    current: bool
    # A club team member's session on the club's account: who it is.
    who: Optional[str] = None


class Counts(BaseModel):
    active_sessions: int
    sessions_30d: int
    sessions_delta: Optional[float]
    places_30d: int
    places_delta: Optional[float]
    failed_30d: int
    failed_delta: Optional[float]
    last_blocked_place: Optional[str]


class PlaceRow(BaseModel):
    place: str
    total: int
    failed: int


class HistoryRow(BaseModel):
    id: str
    at: str
    place: Optional[str]
    device: str
    ip_address: Optional[str]
    method: str
    result: str


class TwoFactor(BaseModel):
    enabled: bool
    recovery_codes_remaining: int


class SecurityOverview(BaseModel):
    account: str
    counts: Counts
    sessions: list[SessionOut]
    by_place: list[PlaceRow]
    history: list[HistoryRow]
    two_factor: TwoFactor
    notify_new_device: bool
    lockout: dict
    generated_at: str


class PreferencesPatch(BaseModel):
    notify_new_device: bool = Field(...)
