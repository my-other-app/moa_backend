from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class ContactOut(BaseModel):
    id: UUID
    name: str
    email: Optional[str]
    phone: Optional[str]
    role: str
    attended: bool
    event_id: int
    event_name: str
    event_ended_at: Optional[datetime]
    archived_at: Optional[datetime]


class ContactGroup(BaseModel):
    event_id: int
    event_name: str
    event_ended_at: Optional[datetime]
    archived_at: Optional[datetime]
    contacts: int


class ContactsPage(BaseModel):
    items: list[ContactOut]
    total: int
    page: int
    page_size: int
    groups: list[ContactGroup]
