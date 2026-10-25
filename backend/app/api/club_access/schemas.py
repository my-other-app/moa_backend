from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MemberRow(BaseModel):
    id: int
    email: str
    full_name: Optional[str] = None
    role: str
    role_label: str
    status: str
    invite_expired: bool = False
    invited_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    #: The WhatsApp permission: reads and answers the club's own WhatsApp inbox.
    whatsapp: bool = False
    #: The Mail permission: reads, answers and sends from the club's mailbox.
    mail: bool = False


class RoleOption(BaseModel):
    key: str
    label: str
    description: str


class AccessList(BaseModel):
    owner_email: Optional[str] = None
    members: list[MemberRow]
    roles: list[RoleOption]
    can_manage: bool


class InviteRequest(BaseModel):
    email: str = Field(..., max_length=100)
    full_name: Optional[str] = Field(None, max_length=80)
    role: str


class InviteResponse(BaseModel):
    member: MemberRow
    # Shown once, so the owner can send it another way if the mail goes astray.
    invite_url: str


class RoleChange(BaseModel):
    role: str


class WhatsAppPermission(BaseModel):
    """Whether this member may read and answer the club's WhatsApp inbox."""

    allowed: bool


class MailPermission(BaseModel):
    """Whether this member may read, answer and send from the club's mailbox."""

    allowed: bool


class Me(BaseModel):
    role: str
    role_label: str
    role_description: str
    can: dict[str, bool]
    club_id: int
    club_name: str
    # None for the club's own account.
    member: Optional[MemberRow] = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class InvitePreview(BaseModel):
    club_name: str
    club_logo: Optional[str] = None
    email: str
    full_name: Optional[str] = None
    role: str
    role_label: str
    role_description: str
    expired: bool


class InviteAccept(BaseModel):
    full_name: Optional[str] = Field(None, max_length=80)
    password: str


class Accepted(BaseModel):
    email: str
    club_name: str
