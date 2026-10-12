from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

EXPENSE_CATEGORIES = ("venue", "food", "prizes", "printing", "marketing", "equipment", "travel", "speakers", "other")


def _clean_title(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("Give the expense a title")
    return v


def _known_category(v: str) -> str:
    v = v.strip().lower()
    if v not in EXPENSE_CATEGORIES:
        raise ValueError(f"category must be one of: {', '.join(EXPENSE_CATEGORIES)}")
    return v


class ExpenseCreate(BaseModel):
    event_id: Optional[int] = None
    title: str = Field(..., min_length=1, max_length=120)
    category: str = Field("other", max_length=40)
    amount: Decimal = Field(..., gt=0, max_digits=10, decimal_places=2)
    incurred_on: Optional[date] = None
    note: Optional[str] = Field(None, max_length=500)

    _title = field_validator("title")(_clean_title)
    _category = field_validator("category")(_known_category)


class ExpensePatch(BaseModel):
    event_id: Optional[int] = None
    title: Optional[str] = Field(None, min_length=1, max_length=120)
    category: Optional[str] = Field(None, max_length=40)
    amount: Optional[Decimal] = Field(None, gt=0, max_digits=10, decimal_places=2)
    incurred_on: Optional[date] = None
    note: Optional[str] = Field(None, max_length=500)

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return None if v is None else _clean_title(v)

    @field_validator("category")
    @classmethod
    def _category(cls, v):
        return None if v is None else _known_category(v)


class ExpenseOut(BaseModel):
    id: str
    event_id: Optional[int]
    event_name: Optional[str]
    title: str
    category: str
    amount: float
    incurred_on: date
    note: Optional[str]
    created_at: str


class EventsFigure(BaseModel):
    conducted: int
    upcoming: int
    delta: Optional[float]


class Figures(BaseModel):
    events: Optional[EventsFigure] = None
    regs: int
    paying: int
    gross: float
    tickets: float
    refunds: float
    refund_count: int
    expenses: float
    expense_count: int
    net: float
    outstanding: float
    outstanding_count: int
    avg_ticket: Optional[float]


class Deltas(BaseModel):
    gross: Optional[float]
    tickets: Optional[float]
    refunds: Optional[float]
    expenses: Optional[float]
    net: Optional[float]


class MonthRow(BaseModel):
    month: str
    tickets: float
    refunds: float
    expenses: float
    net: float


class TypeRow(BaseModel):
    type: str
    events: int
    tickets: float
    expenses: float
    net: float


class EventRow(BaseModel):
    id: int
    name: str
    starts_at: str
    type: str
    status: str
    regs: int
    paying: int
    tickets: float
    refunds: float
    expenses: float
    net: float


class Period(BaseModel):
    start: date
    end: date


class FinanceSummary(BaseModel):
    scope: Literal["club", "event"]
    period: Period
    figures: Figures
    deltas: Deltas
    by_month: list[MonthRow]
    by_type: list[TypeRow]
    by_event: list[EventRow]
    expenses: list[ExpenseOut]
    categories: list[str]
    generated_at: str
