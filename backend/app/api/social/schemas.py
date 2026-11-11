"""What the social routes take and give (feature-register/social-posting-contract.md)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

Platform = Literal["facebook", "instagram", "linkedin", "linkedin_page", "x"]
Size = Literal["ig_portrait", "ig_square", "facebook", "linkedin", "linkedin_square", "x"]
Mode = Literal["on_publish", "at", "before_start", "before_reg_close", "after_end"]
#: Up to 60 days either side of what it is relative to.
MAX_MINUTES = 60 * 24 * 60


class Trigger(BaseModel):
    mode: Mode = "on_publish"
    #: For `at`: when (India time if no offset is given).
    at: Optional[datetime] = None
    #: For before_start, before_reg_close and after_end.
    minutes: Optional[int] = Field(None, ge=0, le=MAX_MINUTES)

    @model_validator(mode="after")
    def _complete(self):
        if self.mode == "at" and self.at is None:
            raise ValueError("Say when: `at` is needed for a post at a set time.")
        if self.mode in ("before_start", "before_reg_close", "after_end") and self.minutes is None:
            raise ValueError("Say how long before or after: `minutes` is needed.")
        return self

    def stored(self) -> dict:
        out: dict = {"mode": self.mode}
        if self.mode == "at":
            out["at"] = self.at.isoformat()
        elif self.mode != "on_publish":
            out["minutes"] = int(self.minutes)
        return out


class TargetIn(BaseModel):
    platform: Platform
    account_id: UUID
    text: str = Field(..., min_length=1, max_length=10000)
    variant_id: Optional[UUID] = None
    #: One of this event's poster sizes' picture keys (the club's own image), instead of variant_id.
    image_key: Optional[str] = Field(None, max_length=500)


class PostIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    trigger: Trigger
    targets: list[TargetIn] = Field(..., min_length=1, max_length=5)
    #: Save only: nothing is put on the clock.
    draft: bool = False


class PostPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    trigger: Optional[Trigger] = None
    targets: Optional[list[TargetIn]] = Field(None, min_length=1, max_length=5)


class CampaignIn(BaseModel):
    #: The platforms to announce on (empty: every connected one).
    platforms: list[Platform] = Field(default_factory=list, max_length=5)
    #: The accounts ticked (empty: every connected one on those platforms).
    account_ids: list[UUID] = Field(default_factory=list, max_length=10)


class PreviewIn(BaseModel):
    platform: Platform
    text: str = Field(..., max_length=10000)
    variant_id: Optional[UUID] = None


class RewriteIn(BaseModel):
    platform: Platform
    text: str = Field(..., min_length=1, max_length=10000)
    tone: Optional[Literal["friendly", "formal", "hype"]] = None


class ConnectIn(BaseModel):
    #: Where the dashboard wants the browser back (the club dashboard only).
    return_to: Optional[str] = Field(None, max_length=500)


class ChoiceIn(BaseModel):
    external_id: str = Field(..., min_length=1, max_length=100)


class SettingsIn(BaseModel):
    approval_required: bool


class PosterIn(BaseModel):
    mode: Literal["crop", "fit"] = "crop"
    #: Where the important part is, 0 to 1 across and down (crop).
    focus: Optional[dict[Literal["x", "y"], float]] = None
    #: "#rrggbb" behind a fitted poster.
    background: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")


# ─── Out ──────────────────────────────────────────────────────────────────


class PlatformOut(BaseModel):
    platform: str
    label: str
    available: bool
    #: not_configured | review_pending | plan
    reason: Optional[str] = None
    plan_feature: str


class PersonRef(BaseModel):
    #: The team member's id (club_members.id); null for the club's own account.
    id: Optional[int] = None
    name: Optional[str] = None


class AccountOut(BaseModel):
    id: UUID
    platform: str
    name: Optional[str] = None
    handle: Optional[str] = None
    picture_url: Optional[str] = None
    connected_by: PersonRef
    connected_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    status: str
    waiting_posts: int = 0
    last_error: Optional[str] = None


class ConnectOut(BaseModel):
    authorize_url: str


class ChoiceOut(BaseModel):
    external_id: str
    name: Optional[str] = None
    picture: Optional[str] = None


class TargetError(BaseModel):
    code: str
    message: Optional[str] = None
    #: retry | reconnect | share_manually | fix_image
    action: Optional[str] = None


class TargetOut(BaseModel):
    id: UUID
    platform: str
    account_id: Optional[UUID] = None
    text: str
    variant_id: Optional[UUID] = None
    image_url: Optional[str] = None
    status: str
    post_url: Optional[str] = None
    error: Optional[TargetError] = None
    attempts: int = 0
    posted_at: Optional[datetime] = None
    scheduled_for: Optional[datetime] = None


class PostOut(BaseModel):
    id: UUID
    event_id: int
    name: str
    trigger: dict
    status: str
    status_reason: Optional[str] = None
    template: Optional[str] = None
    created_by: Optional[PersonRef] = None
    approved_by: Optional[PersonRef] = None
    approved_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    targets: list[TargetOut]


class EventPostsOut(BaseModel):
    #: Registrations each platform brought (its social and share links): {"instagram": 42, ...}.
    attribution: dict[str, int]
    posts: list[PostOut]


class CampaignOut(BaseModel):
    posts: list[PostOut]


class PosterOut(BaseModel):
    id: UUID
    size: str
    width: int
    height: int
    mode: str
    focus: dict[str, float]
    background: Optional[str] = None
    own: bool = False
    url: Optional[str] = None


class DraftOut(BaseModel):
    platform: str
    text: str
    hashtags: list[str]
    variant: Optional[PosterOut] = None
    link: str
    link_id: UUID
    limit: int


class DraftsOut(BaseModel):
    fill_ins: list[str]
    platforms: list[DraftOut]


class Warning_(BaseModel):
    code: str
    message: str


class PreviewOut(BaseModel):
    platform: str
    text_rendered: str
    warnings: list[Warning_]
    counts: dict[str, int]
    image_url: Optional[str] = None


class RewriteOut(BaseModel):
    platform: str
    text: str
    warnings: list[Warning_]
    counts: dict[str, int]


class SettingsOut(BaseModel):
    approval_required: bool


class KitPlatform(BaseModel):
    platform: str
    text: str
    image_url: Optional[str] = None
    link: str


class KitEvent(BaseModel):
    id: int
    name: str
    date: Optional[str] = None
    slug: str


class ShareKitOut(BaseModel):
    event: KitEvent
    platforms: list[KitPlatform]


class ShareKitListItem(BaseModel):
    id: int
    name: str
    date: Optional[str] = None
    slug: str
    image_url: Optional[str] = None


class ShareRowItem(BaseModel):
    platform: Literal["x", "linkedin", "facebook", "whatsapp", "copy"]
    text: str
    #: The tracking link to share (its own per platform, utm_medium=share).
    url: str
    #: The platform's own share address with the text and link filled; null for copy.
    intent_url: Optional[str] = None


class DeletionOut(BaseModel):
    url: str
    confirmation_code: str


class DeletionStatusOut(BaseModel):
    code: str
    status: str
    requested_at: datetime
    completed_at: Optional[datetime] = None
    accounts: int
    what: str
