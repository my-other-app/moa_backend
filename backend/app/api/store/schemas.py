from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

ROLES = ("attendee", "speaker", "staff")


def _roles(v):
    if v is None:
        return None
    out = []
    for r in v:
        r = (r or "").strip().lower()
        if r not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if r not in out:
            out.append(r)
    return out


class VariantIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=40)
    stock: int = Field(0, ge=0, le=1_000_000)


class StoreItemIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=300)
    photo_key: Optional[str] = Field(None, max_length=300)
    price: Decimal = Field(..., ge=0, max_digits=10, decimal_places=2)
    stock: Optional[int] = Field(None, ge=0, le=1_000_000)
    variants: list[VariantIn] = Field(default_factory=list, max_length=20)
    available_roles: Optional[list[str]] = None
    free_roles: list[str] = Field(default_factory=list)
    on_sale: bool = True

    _a = field_validator("available_roles")(_roles)
    _f = field_validator("free_roles")(_roles)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Name the item")
        return v

    @model_validator(mode="after")
    def _stock_xor_variants(self):
        if self.variants and self.stock is not None:
            raise ValueError("Give stock per size, or one stock for the item — not both")
        if not self.variants and self.stock is None:
            raise ValueError("Give the item a stock, or sizes with a stock each")
        labels = [v.label.strip().lower() for v in self.variants]
        if len(set(labels)) != len(labels):
            raise ValueError("Two sizes have the same label")
        return self


class StoreItemPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=300)
    photo_key: Optional[str] = Field(None, max_length=300)
    price: Optional[Decimal] = Field(None, ge=0, max_digits=10, decimal_places=2)
    stock: Optional[int] = Field(None, ge=0, le=1_000_000)
    variants: Optional[list[VariantIn]] = Field(None, max_length=20)
    available_roles: Optional[list[str]] = None
    free_roles: Optional[list[str]] = None
    on_sale: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0, le=10000)

    _a = field_validator("available_roles")(_roles)
    _f = field_validator("free_roles")(_roles)


class VariantOut(BaseModel):
    id: str
    label: str
    stock: int
    sold: int
    #: Held for checkouts not paid yet (they come back if the hold runs out).
    held: int = 0
    left: int


class StoreItemOut(BaseModel):
    id: str
    event_id: int
    name: str
    description: Optional[str]
    photo_key: Optional[str]
    photo_url: Optional[str]
    price: float
    stock: Optional[int]
    sold: int
    held: int = 0
    total: int
    left: int
    earned: float
    available_roles: Optional[list[str]]
    free_roles: list[str]
    on_sale: bool
    sold_out: bool
    sort_order: int
    variants: list[VariantOut]
    created_at: str


class StorePick(BaseModel):
    item_id: UUID
    variant_id: Optional[UUID] = None
    qty: int = Field(1, ge=1, le=10)


class LineOut(BaseModel):
    item_id: str
    item_name: str
    variant_label: Optional[str]
    qty: int
    unit_price: float
    line_total: float
    is_free: bool


class OrderOut(BaseModel):
    id: str
    registration_id: str
    guest: str
    ticket_id: Optional[str]
    status: str
    total: float
    lines: list[LineOut]
    summary: str
    paid_at: Optional[str]
    handed_over_at: Optional[str]
    #: Who handed it over, when it has been.
    handed_over_by: Optional[int] = None
    created_at: str


class OrdersPage(BaseModel):
    items: list[OrderOut]
    total: int
    page: int
    page_size: int


class SizeRow(BaseModel):
    label: str
    sold: int


class StoreSummary(BaseModel):
    enabled: bool
    items_total: int
    items_on_sale: int
    sold_out: int
    units_sold: int
    orders_paid: int
    revenue: float
    ticket_revenue: float
    to_hand_over: int
    #: Paid for when the pieces had gone: the organiser hands something over
    #: anyway (fulfil) or refunds.
    oversold: int = 0
    sizes: list[SizeRow]
    sizes_item: Optional[str]
    latest_orders: list[OrderOut]


class CatalogVariant(BaseModel):
    id: str
    label: str
    left: int


class CatalogItem(BaseModel):
    id: str
    name: str
    description: Optional[str]
    photo_url: Optional[str]
    price: float
    left: Optional[int]
    variants: list[CatalogVariant]


class Catalog(BaseModel):
    enabled: bool
    items: list[CatalogItem]


class SettingsPatch(BaseModel):
    merch_enabled: Optional[bool] = None
    swag_enabled: Optional[bool] = None
