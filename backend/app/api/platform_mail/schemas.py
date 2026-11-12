"""What the platform inbox's routes take and answer (app/api/platform_mail/router.py).

The club mailbox's shapes (club_mailbox/schemas.py), each with `mailbox`."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from app.api.club_mailbox.schemas import MessageOut as ClubMessageOut
from app.api.club_mailbox.schemas import SummaryOut, ThreadPatch  # noqa: F401 - the same shapes here
from app.api.club_mailbox.schemas import ThreadOut as ClubThreadOut


class AddressOut(BaseModel):
    address: str
    #: Its local part: what threads are filed under and what is sent from.
    mailbox: str
    #: Unread mail in its Inbox.
    unread: int = 0


class InboxOut(BaseModel):
    addresses: list[AddressOut]
    unread_total: int
    #: Which admin may delete a conversation (the owner role).
    can_delete: bool = False


class ThreadOut(ClubThreadOut):
    mailbox: str


class ThreadList(BaseModel):
    items: list[ThreadOut]
    total: int
    page: int = 1
    page_size: int = 25
    counts: SummaryOut


class MessageOut(ClubMessageOut):
    mailbox: str


class ThreadDetail(BaseModel):
    thread: ThreadOut
    messages: list[MessageOut]
    #: The platform's addresses (a reply-all leaves them out).
    own_addresses: list[str] = []


class DeletedOut(BaseModel):
    deleted: int
    detail: Optional[str] = None
