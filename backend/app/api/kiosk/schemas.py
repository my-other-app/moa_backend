from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class KioskDeviceOut(BaseModel):
    id: UUID
    event_id: int
    name: str
    door_id: Optional[UUID]
    door_label: str
    token_prefix: str
    expires_at: datetime
    last_seen_at: Optional[datetime]
    revoked_at: Optional[datetime]
    online: bool
    active: bool
    created_at: Optional[datetime] = None


class PairedKiosk(BaseModel):
    device: KioskDeviceOut
    token: str
    url: str


class KioskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    door_id: Optional[UUID] = None
    door_label: Optional[str] = Field(default=None, max_length=40)
    shift_hours: int = Field(default=12, ge=1, le=72)


class KioskRotate(BaseModel):
    shift_hours: int = Field(default=12, ge=1, le=72)


class KioskSettings(BaseModel):
    self_check_in_enabled: bool = False
    self_check_in_verify: Literal["name", "email", "phone"] = "email"
    self_check_in_print: bool = True
    has_pin: bool = False


class KioskSettingsUpdate(BaseModel):
    self_check_in_enabled: Optional[bool] = None
    self_check_in_verify: Optional[Literal["name", "email", "phone"]] = None
    self_check_in_print: Optional[bool] = None
    kiosk_pin: Optional[str] = Field(default=None, max_length=12)


class KioskContext(BaseModel):
    device: dict
    event: dict
    self_check_in: KioskSettings
    counters: dict
    agenda: list = []
    has_programme: bool = False


class RedeemBody(BaseModel):
    code: str = Field(min_length=4, max_length=128)
    client_ref: str = Field(min_length=8, max_length=64)


class RedeemResult(BaseModel):
    outcome: Literal["MATCHED", "NO_MATCH", "WRONG_EVENT", "REVOKED", "UNPAID", "EVENT_NOT_OPEN"]
    guest: Optional[dict]
    other_event: Optional[dict]
    redemption_token: Optional[str]
    print_url: Optional[str]
    auto_print: bool = False


class TokenBody(BaseModel):
    token: str = Field(min_length=1, max_length=2000)


class StaffBody(BaseModel):
    pin: str = Field(min_length=4, max_length=12)


class SelfQr(BaseModel):
    enabled: bool
    verify: str
    print: bool
    url: Optional[str]
    expires_at: Optional[datetime]
    recent: list[dict]


class SessionBody(BaseModel):
    session: str = Field(min_length=10, max_length=200)


class LookupBody(SessionBody):
    name: str = Field(min_length=2, max_length=120)
    contact: Optional[str] = Field(default=None, max_length=160)


class VerifyBody(SessionBody):
    code: str = Field(pattern=r"^\d{6}$")


class PrintedBody(BaseModel):
    session: str = Field(min_length=10, max_length=200)
