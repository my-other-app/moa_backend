from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

EXPENSE_CATEGORIES = ("venue", "food", "prizes", "printing", "marketing", "equipment", "travel", "speakers", "other")
INCOME_CATEGORIES = ("sponsorship", "grant", "donation", "sales", "fees", "other")
KINDS = ("expense", "income")
CATEGORIES = {"expense": EXPENSE_CATEGORIES, "income": INCOME_CATEGORIES}


def _clean_title(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("Give the expense a title")
    return v


def _known_category(v: str, kind: str = "expense") -> str:
    v = v.strip().lower()
    allowed = CATEGORIES[kind]
    if v not in allowed:
        raise ValueError(f"an {kind}'s category must be one of: {', '.join(allowed)}")
    return v


class ExpenseCreate(BaseModel):
    event_id: Optional[int] = None
    kind: Literal["expense", "income"] = "expense"
    title: str = Field(..., min_length=1, max_length=120)
    category: str = Field("other", max_length=40)
    amount: Decimal = Field(..., gt=0, max_digits=10, decimal_places=2)
    incurred_on: Optional[date] = None
    note: Optional[str] = Field(None, max_length=500)

    _title = field_validator("title")(_clean_title)

    @model_validator(mode="after")
    def _category_fits_kind(self):
        self.category = _known_category(self.category, self.kind)
        return self


class ExpensePatch(BaseModel):
    event_id: Optional[int] = None
    kind: Optional[Literal["expense", "income"]] = None
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
        # Checked against the entry's kind in the service, where the kind is known.
        return None if v is None else v.strip().lower()


class ExpenseOut(BaseModel):
    id: str
    event_id: Optional[int]
    event_name: Optional[str]
    kind: str = "expense"
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
    merch: float = 0
    other_income: float = 0
    other_income_count: int = 0
    refunds: float
    refund_count: int
    online: float = 0
    online_count: int = 0
    cash: float = 0
    cash_count: int = 0
    expenses: float
    expense_count: int
    net: float
    margin: Optional[float] = None
    outstanding: float
    outstanding_count: int
    avg_ticket: Optional[float]
    cost_per_head: Optional[float] = None


class Deltas(BaseModel):
    gross: Optional[float]
    tickets: Optional[float]
    merch: Optional[float] = None
    refunds: Optional[float]
    expenses: Optional[float]
    net: Optional[float]


class MonthRow(BaseModel):
    month: str
    tickets: float
    merch: float = 0
    other: float = 0
    refunds: float
    expenses: float
    net: float


class TypeRow(BaseModel):
    type: str
    events: int
    tickets: float
    merch: float = 0
    other: float = 0
    expenses: float
    net: float


class EventRow(BaseModel):
    id: int
    name: str
    starts_at: str
    type: str
    status: str
    held: bool = False
    has_fee: bool = False
    fee: float = 0
    regs: int
    paying: int
    tickets: float
    merch: float = 0
    other_income: float = 0
    online: float = 0
    cash: float = 0
    refunds: float
    expenses: float
    expense_count: int = 0
    net: float
    margin: Optional[float] = None
    outstanding: float = 0
    outstanding_count: int = 0


class CategoryRow(BaseModel):
    category: str
    amount: float
    count: int


class ChannelRow(BaseModel):
    key: str
    label: str
    amount: float
    count: int


class LossRow(BaseModel):
    id: int
    name: str
    net: float
    tickets: float
    expenses: float


class OwedRow(BaseModel):
    id: int
    name: str
    outstanding: float
    outstanding_count: int


class UnloggedRow(BaseModel):
    id: int
    name: str
    tickets: float


class Attention(BaseModel):
    at_a_loss: list[LossRow] = []
    owed: list[OwedRow] = []
    unlogged: list[UnloggedRow] = []


class Ahead(BaseModel):
    """What is still to come inside the period: events not yet held, what they have sold so far, what they are owed."""

    events: int = 0
    paid_events: int = 0
    regs: int = 0
    tickets: float = 0
    outstanding: float = 0
    outstanding_count: int = 0
    expenses: float = 0


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
    by_category: list[CategoryRow] = []
    income_by_category: list[CategoryRow] = []
    channels: list[ChannelRow] = []
    attention: Attention = Attention()
    ahead: Ahead = Ahead()
    categories: list[str]
    income_categories: list[str] = []
    generated_at: str
