from datetime import datetime

from pydantic import Field

from app.core.response.base_model import CustomBaseModel


class AlertClub(CustomBaseModel):
    id: int
    name: str
    slug: str


class AlertOut(CustomBaseModel):
    id: int
    #: See admin_alerts.service.KINDS.
    kind: str
    title: str
    body: str = ""
    club: AlertClub | None = None
    subscription_id: int | None = None
    data: dict = Field(default_factory=dict)
    created_at: datetime
    #: Null while unread. Shared by every admin.
    read_at: datetime | None = None


class AlertsPage(CustomBaseModel):
    items: list[AlertOut]
    #: How many match the filters.
    total: int
    #: Unread across every kind, for the bell's badge.
    unread: int


class UnreadCount(CustomBaseModel):
    unread: int


class ReadAllOut(CustomBaseModel):
    #: How many were marked read by this call.
    updated: int
    unread: int
