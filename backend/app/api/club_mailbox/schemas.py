"""What the mailbox's routes take and answer (app/api/club_mailbox/router.py)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class PreviousAddress(BaseModel):
    address: str
    #: Mail to it still arrives here until then.
    forward_until: Optional[datetime] = None


class Limits(BaseModel):
    per_hour: int
    per_day: int
    recipients: int
    attachments_mb: int


class MailboxOut(BaseModel):
    #: The feature is on on this server (APP_CLUB_MAILBOX).
    available: bool
    domain: str
    plan_allows: bool
    #: When the plan lacks it: the cheapest plan with it, and where to upgrade.
    upgrade: Optional[dict] = None
    set_up: bool
    address: Optional[str] = None
    handle: Optional[str] = None
    previous: list[PreviousAddress] = []
    display_name: Optional[str] = None
    #: Only to those who may write mail.
    signature: Optional[str] = None
    retention_months: int = 12
    #: The address can't change again before this.
    next_change_at: Optional[datetime] = None
    #: open; lapsed (the plan dropped it: readable until `readable_until`); expired (export or delete).
    state: str = "open"
    lapsed_at: Optional[datetime] = None
    readable_until: Optional[datetime] = None
    can_manage: bool = False
    can_mail: bool = False
    is_owner: bool = False
    impersonated: bool = False
    #: Email credits left (null: no limit, or not known).
    credits_left: Optional[int] = None
    limits: Limits


class AddressIn(BaseModel):
    handle: str = Field(..., min_length=1, max_length=64)


class HandleCheckOut(BaseModel):
    handle: str
    address: str
    available: bool
    #: Why not: HANDLE_INVALID, HANDLE_RESERVED, HANDLE_TAKEN.
    code: Optional[str] = None
    message: Optional[str] = None


class SettingsIn(BaseModel):
    display_name: Optional[str] = Field(None, max_length=80)
    retention_months: Optional[int] = Field(None, ge=1, le=36)


class SignatureIn(BaseModel):
    signature: Optional[str] = Field(None, max_length=2000)


class SummaryOut(BaseModel):
    unread_total: int
    unread_threads: int
    spam_unread: int
    archive_unread: int = 0


class ThreadOut(BaseModel):
    id: int
    folder: str
    subject: str
    participants: list[str]
    message_count: int
    unread_count: int
    last_message_at: Optional[datetime] = None
    last_inbound_at: Optional[datetime] = None
    last_outbound_at: Optional[datetime] = None
    last_from: Optional[str] = None
    snippet: str = ""
    has_attachments: bool = False


class ThreadList(BaseModel):
    items: list[ThreadOut]
    total: int
    counts: SummaryOut


class AttachmentOut(BaseModel):
    id: int
    filename: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: int
    content_id: Optional[str] = None
    inline: bool = False
    #: Through the API, for the signed-in team only.
    url: str


class MessageOut(BaseModel):
    id: int
    thread_id: int
    direction: str
    status: str
    from_address: Optional[str] = None
    from_name: Optional[str] = None
    to: list[str] = []
    cc: list[str] = []
    bcc: list[str] = []
    reply_to: Optional[str] = None
    subject: str = ""
    text: str = ""
    #: Sanitised; shown in a sandboxed frame, remote pictures blocked until asked for.
    html: Optional[str] = None
    has_remote_images: bool = False
    snippet: str = ""
    unread: bool = False
    spam: bool = False
    verdicts: dict = {}
    delivered_to: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    sent_by: Optional[str] = None
    error: Optional[str] = None
    attachments: list[AttachmentOut] = []


class ThreadDetail(BaseModel):
    thread: ThreadOut
    messages: list[MessageOut]
    #: The club's addresses (a reply-all leaves them out).
    own_addresses: list[str] = []


class ThreadPatch(BaseModel):
    folder: str = Field(..., pattern="^(inbox|archive|spam)$")


class AdminMailboxOut(BaseModel):
    club_id: int
    available: bool
    plan_allows: bool
    set_up: bool
    address: Optional[str] = None
    state: str
    lapsed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    retention_months: Optional[int] = None
    forwarding: int = 0
    threads: int = 0
    received: int = 0
    sent: int = 0
    spam: int = 0
    failed: int = 0
    received_this_month: int = 0
    sent_this_month: int = 0
    dropped_while_closed: int = 0
    last_received_at: Optional[datetime] = None
    last_sent_at: Optional[datetime] = None
