"""The inbox worked as a team (inbox_router.py): alerts, opt-outs, blocks,
attachments, assignment, notes and saved replies."""
from datetime import datetime
from typing import Optional

from pydantic import Field

from app.core.response.base_model import CustomBaseModel


class AssigneeOut(CustomBaseModel):
    #: owner | member
    kind: str
    member_id: Optional[int] = None
    name: Optional[str] = None
    #: "owner" or "member:<id>": what ?assignee= takes.
    key: Optional[str] = None


class BlockedOut(CustomBaseModel):
    at: datetime
    #: True: blocked at Meta too (they can't write); false: here only.
    meta: bool = False
    by: Optional[str] = None


class OptOutOut(CustomBaseModel):
    #: all | marketing
    scope: str
    #: keyword | user_preferences | meta_131050 | team
    source: str
    at: Optional[datetime] = None
    by: Optional[str] = None


class QuotedOut(CustomBaseModel):
    """The message a reply (or a reaction) is about, as the quoted bubble shows it."""

    id: int
    direction: str
    type: str
    preview: Optional[str] = None


class SummaryOut(CustomBaseModel):
    #: Unread messages, blocked conversations aside: the nav's badge and the tab's "(n)".
    unread_total: int = 0
    unread_conversations: int = 0
    waiting: int = 0
    mine: int = 0


class TeamPerson(CustomBaseModel):
    key: str
    #: owner | member
    kind: str
    member_id: Optional[int] = None
    name: str
    me: bool = False


class TeamOut(CustomBaseModel):
    items: list[TeamPerson]
    #: The viewer's own key ("owner" or "member:<id>").
    me: str


class AssignIn(CustomBaseModel):
    """The owner (`owner` true), a member (`member_id`), or nobody (neither)."""

    owner: bool = False
    member_id: Optional[int] = None


class BlockIn(CustomBaseModel):
    #: Block here only: no call to Meta (what is left after 24 hours).
    local_only: bool = False


class OptOutIn(CustomBaseModel):
    #: all | marketing
    scope: str = Field("all", pattern="^(all|marketing)$")


class NoteIn(CustomBaseModel):
    body: str = Field(..., max_length=2000)


class NoteOut(CustomBaseModel):
    id: int
    conversation_id: int
    body: str
    author: str
    created_at: datetime
    mine: bool = False
    can_delete: bool = False


class NoteList(CustomBaseModel):
    items: list[NoteOut]


class SavedReplyIn(CustomBaseModel):
    title: str = Field(..., max_length=60)
    body: str = Field(..., max_length=4096)
    #: Without the "/"; letters, digits, - and _.
    shortcut: Optional[str] = Field(None, max_length=32)


class SavedReplyOut(CustomBaseModel):
    id: int
    title: str
    body: str
    shortcut: Optional[str] = None
    updated_at: Optional[datetime] = None


class SavedReplyList(CustomBaseModel):
    items: list[SavedReplyOut]


class InboxSettingsIn(CustomBaseModel):
    alert_after_hours: Optional[int] = Field(None, ge=1, le=48)
    #: The automatic answer to STOP and to START; "" puts the default back.
    stop_reply: Optional[str] = Field(None, max_length=1000)
    start_reply: Optional[str] = Field(None, max_length=1000)


class MyNotifications(CustomBaseModel):
    #: Mail me when a message waits unread (15 minutes, at most hourly) or a conversation waits too long.
    email_messages: bool = True
    #: Mail me when a conversation is assigned to me.
    email_assigned: bool = True


class MyNotificationsIn(CustomBaseModel):
    email_messages: Optional[bool] = None
    email_assigned: Optional[bool] = None


class InboxSettingsOut(CustomBaseModel):
    alert_after_hours: int = 4
    stop_reply: Optional[str] = None
    start_reply: Optional[str] = None
    #: The words used when the club's own are empty ({club} is its name).
    stop_reply_default: str = ""
    start_reply_default: str = ""
    me: MyNotifications = Field(default_factory=MyNotifications)


class OptOutResult(CustomBaseModel):
    changed: bool = False
