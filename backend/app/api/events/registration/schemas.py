from datetime import datetime
from typing import Any
from uuid import UUID
from pydantic import EmailStr, Field

from app.core.response.base_model import CustomBaseModel
from app.api.auth.schemas import Token
from app.api.users.schemas import UserPublic
from app.api.events.volunteer.schemas import ListVolunteersResponse


class EventRegistrationResponse(CustomBaseModel):
    event_name: str = Field(...)
    event_id: int = Field(...)
    event_registration_id: UUID = Field(...)
    pay_amount: float = Field(...)
    event_datetime: datetime = Field(...)
    ticket_id: str | None = Field(None)
    full_name: str = Field(...)
    email: str = Field(...)
    phone: str | None = Field(None)
    additional_details: dict[str, Any] | None = Field(None)
    auth_token: Token | None = Field(None)


class EventRegistrationRequest(CustomBaseModel):
    full_name: str = Field(..., max_length=100, min_length=3)
    email: EmailStr = Field(...)
    phone: str | None = Field(None)
    additional_details: dict[str, str] | None = Field(None)


class EventRegistrationDetailResponse(CustomBaseModel):
    id: str
    user: UserPublic
    ticket_id: str
    is_paid: bool
    actual_amount: float
    paid_amount: float
    payment_receipt: str | None
    created_at: datetime
    updated_at: datetime
    is_won: bool
    position: int | None
    additional_details: dict | None


class EventRegistrationPublicMin(CustomBaseModel):
    id: str = Field(...)
    ticket_id: str = Field(...)
    event_id: int = Field(...)
    is_paid: bool = Field(...)
    full_name: str = Field(...)
    email: str = Field(...)
    phone: str | None = Field(None)
    actual_amount: float = Field(...)
    paid_amount: float = Field(...)
    is_attended: bool = Field(...)
    is_won: bool = Field(...)
    user: UserPublic = Field(...)
    volunteer: ListVolunteersResponse | None = Field(None)
    created_at: datetime = Field(...)
