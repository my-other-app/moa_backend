from enum import Enum as PyEnum
from typing import Dict, Optional
import uuid
from pydantic import BaseModel, Field
from app.core.response.base_model import CustomBaseModel


class PaymentSources(str, PyEnum):
    event_registration = "event_registration"


class OrderCreateRequest(CustomBaseModel):
    source: PaymentSources = Field(..., description="Source of the payment order")
    payload: Dict[str, str | int] = Field(
        ..., description="Metadata for the payment order"
    )


class PaymentVerifyRequest(CustomBaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str


class OrderCreateResponse(CustomBaseModel):
    id: uuid.UUID = Field(...)
    razorpay_order_id: str = Field(...)
    amount: int = Field(...)
    currency: str = Field(...)
    status: str = Field(...)


class PaymentVerifyResponse(CustomBaseModel):
    payment_status: str = Field(...)
    payment_amount: int = Field(...)
    remining_amount: Optional[int] = Field(None)
    payment_method: Optional[str] = Field(None)
