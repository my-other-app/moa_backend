from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


def _blank(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


class VariantIn(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    stock: int = Field(ge=0, le=1_000_000)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Give the variant a label.")
        return v


class VariantPatch(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=40)
    stock: Optional[int] = Field(default=None, ge=0, le=1_000_000)


class MerchItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    photo_key: Optional[str] = Field(default=None, max_length=300)
    stock: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    eligibility_field: Optional[str] = Field(default=None, max_length=80)
    eligibility_value: Optional[str] = Field(default=None, max_length=80)
    variant_field: Optional[str] = Field(default=None, max_length=80)
    variants: list[VariantIn] = Field(default_factory=list, max_length=40)
    sponsor: Optional[str] = Field(default=None, max_length=120)
    handout_rule: Optional[str] = Field(default=None, max_length=40)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name the item.")
        return v

    @field_validator("photo_key", "eligibility_field", "eligibility_value", "variant_field", mode="before")
    @classmethod
    def _blanks(cls, v):
        return _blank(v)

    @model_validator(mode="after")
    def _rules(self):
        if self.variant_field and self.stock is not None:
            raise ValueError("An item has its own stock or variants, never both.")
        if not self.variant_field and self.stock is None:
            raise ValueError("Without variants, the item needs its own stock.")
        if bool(self.eligibility_field) != bool(self.eligibility_value):
            raise ValueError("Set both the field and the value, or neither.")
        labels = [v.label.lower() for v in self.variants]
        if len(set(labels)) != len(labels):
            raise ValueError("Two variants share a label.")
        return self


class MerchItemPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    sponsor: Optional[str] = Field(default=None, max_length=120)
    handout_rule: Optional[str] = Field(default=None, max_length=40)
    photo_key: Optional[str] = Field(default=None, max_length=300)
    stock: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    eligibility_field: Optional[str] = Field(default=None, max_length=80)
    eligibility_value: Optional[str] = Field(default=None, max_length=80)
    variant_field: Optional[str] = Field(default=None, max_length=80)

    @field_validator("photo_key", "eligibility_field", "eligibility_value", "variant_field", mode="before")
    @classmethod
    def _blanks(cls, v):
        return _blank(v)


class VariantOut(BaseModel):
    id: UUID
    label: str
    stock: int
    collected: int


class MerchItemOut(BaseModel):
    id: UUID
    event_id: int
    name: str
    photo_key: Optional[str]
    photo_url: Optional[str]
    stock: Optional[int]
    collected: int
    total: int
    eligibility_field: Optional[str]
    eligibility_value: Optional[str]
    variant_field: Optional[str]
    variants: list[VariantOut]
    sponsor: Optional[str] = None
    handout_rule: Optional[str] = None
    manual_handed_out: int = 0
    handed_out: int = 0
    created_at: Optional[datetime]


class FieldOut(BaseModel):
    name: str
    key: str
    kind: str  # role | choice | text
    values: list[str]


class MerchStatusRow(BaseModel):
    item_id: UUID
    name: str
    photo_url: Optional[str]
    status: str
    variant_label: Optional[str]
    remaining: Optional[int]
    # What the guest has on file for the size field; shown when it matches no variant.
    on_file: Optional[str]
    # The sizes the volunteer can pick when none is on file or it matches nothing.
    choices: list[dict]


class AttendeeMerch(BaseModel):
    attendee: dict
    items: list[MerchStatusRow]


class CollectBody(BaseModel):
    registration_id: UUID
    # A size picked at the door, only honoured when the guest's record has none that matches.
    variant_label: Optional[str] = Field(default=None, max_length=40)
    door: Optional[str] = Field(default=None, max_length=40)


class CollectResult(BaseModel):
    status: str
    collected: bool
    variant_label: Optional[str]
    remaining: Optional[int]


class MerchLogRow(BaseModel):
    id: UUID
    registration_id: UUID
    name: str
    ticket_id: str
    role: str
    item: dict
    variant: Optional[dict]
    door: Optional[dict]
    collected_at: datetime
    by: dict


class MerchLog(BaseModel):
    items: list[MerchLogRow]
    total: int
    page: int
    page_size: int
    facets: dict


class MerchAnalytics(BaseModel):
    collectors: int
    handovers: int
    by_item: list[dict]
    by_door: list[dict]
    by_hour: list[dict]
