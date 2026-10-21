from typing import Any
from datetime import datetime
from fastapi import Body, File, Form, UploadFile
from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator, model_validator

from app.api.users.models import UserAvatarTypes
from app.api.orgs.schema import OrganizationPublicMin
from app.api.auth.schemas import Token
from app.core.response.base_model import CustomBaseModel


class AvatarPublic(CustomBaseModel):
    id: int
    name: str
    image: dict | None = None


class UserProfileBase(CustomBaseModel):
    whatsapp: str | None = Field(None)


# class UserProfileCreate(CustomBaseModel):
#     whatsapp: str | None = Form(None)
#     org_id: int | None = Form(None)
#     avatar_id: int | None = Form(None)
#     profile_pic: UploadFile | None = File(None)


class UserProfilePublic(CustomBaseModel):
    org: OrganizationPublicMin | None = None
    avatar: AvatarPublic | None = None
    profile_pic: dict | None = None


class UserProfilePrivate(CustomBaseModel):
    whatsapp: str | None
    org: OrganizationPublicMin | None
    avatar: AvatarPublic | None
    profile_pic: dict | None = Field(None)


class UserBase(CustomBaseModel):
    full_name: str = Field(...)
    email: EmailStr = Field(...)
    phone: str | None = Field(None)
    username: str = Field(...)


class UserCreate(CustomBaseModel):
    full_name: str = Field(...)
    email: EmailStr = Field(...)
    phone: str = Field(None)
    password: str = Field(...)


class UserPublic(UserBase):
    id: int = Field(...)
    user_type: str = Field(...)
    profile: UserProfilePublic | None = Field(None)


class UserPrivate(UserBase):
    id: int = Field(...)
    user_type: str = Field(...)
    profile: UserProfilePrivate | None = Field(None)


class UserInDB(UserCreate):
    id: int = Field(...)
    password: str = Field(..., min_length=8)


class UserAvatarSelect(CustomBaseModel):
    avatar_id: int | None = Body(None)


class UserInterestSelect(CustomBaseModel):
    interest_ids: list[int] = Body(...)


# RESPONSE MODELS


class UserOrganizationDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    type: str = Field(...)
    is_verified: bool = Field(...)
    logo: dict | None = Field(None)


class UserAvatarDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    image: dict | None = Field(None)


class UserInterestDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class UserRegisterResponse(Token):
    username: str


class UserCreateResponse(CustomBaseModel):
    full_name: str = Field(...)
    whatsapp: str | None = Field(None)
    org_id: int | None = Field(None)
    avatar_id: int | None = Field(None)
    profile_pic: dict | None = Field(None)


class UserProfileDetailResponse(CustomBaseModel):
    full_name: str = Field(...)
    whatsapp: str | None = Field(None)
    org: UserOrganizationDetail | None = Field(None)
    avatar: UserAvatarDetail | None = Field(None)
    profile_pic: dict | None = Field(None)


class UserDetailResponse(CustomBaseModel):
    id: int = Field(...)
    full_name: str = Field(...)
    email: str = Field(...)
    phone: str | None = Field(None)
    username: str = Field(...)
    user_type: str = Field(...)
    provider: str | None = Field(None)
    # Google and Apple accounts always; a password account once its owner
    # typed back the code sent to the address. The app asks the rest to
    # verify, since tickets booked on the website reach only verified ones.
    email_verified: bool = Field(False)
    # "Show me in People Like You": whether others checked in at the same
    # event see this person in its networking list. On unless turned off
    # (PATCH /user/preferences).
    networking_visible: bool = Field(True)
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)
    profile: UserProfileDetailResponse | None = Field(None)
    interests: list[UserInterestDetail] | None = Field(None)


class AdminUserListResponse(UserDetailResponse):
    """
    One row of the admin user list.

    These two fields are here rather than on UserDetailResponse because that
    one also answers GET /user/profile: putting an account's admin role on it
    would publish the platform's privilege map to every signed-in user.

    `admin_role` is reported as None unless the account is actually an admin.
    The column is NOT NULL and defaults to `owner`, so every app user carries
    `owner` in the database -- read literally it would badge the entire user
    base as administrators.
    """

    is_active: bool = Field(True)
    admin_role: str | None = Field(None)

    @model_validator(mode="after")
    def _only_admins_have_a_role(self) -> "AdminUserListResponse":
        # Done here rather than at the call site so it holds for every route
        # that returns this schema, including the CSV export.
        if self.user_type != "admin":
            self.admin_role = None
        return self


# Response Model


class UserEventCategoryDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class UserEventClubDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    logo: dict | None = Field(None)


class UserEventList(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    poster: dict | None = Field(None)
    event_datetime: datetime = Field(...)
    duration: float = Field(...)
    location_name: str | None = Field(None)
    has_fee: bool = Field(...)
    has_prize: bool = Field(True)
    prize_amount: float | None = Field(None)
    is_online: bool = Field(False)

    @field_validator("is_online", mode="before")
    @classmethod
    def _default_is_online(cls, value: Any) -> Any:
        """
        `events.is_online` is nullable and most rows predate the column, so it
        comes back as None. `Field(False)` does not cover that -- a default
        only applies when the key is absent, not when it is explicitly None --
        so serialising any such event raised a ValidationError and returned
        500 from /user/my-events. An event with no value set is not online.
        """
        return False if value is None else value

    reg_startdate: datetime = Field(...)
    reg_enddate: datetime | None = Field(None)
    club: UserEventClubDetail = Field(...)
    category: UserEventCategoryDetail = Field(...)


class UserRegisteredEvents(CustomBaseModel):
    full_name: str = Field(...)
    email: str = Field(...)
    phone: str | None = Field(None)
    ticket_id: str = Field(...)
    is_paid: bool = Field(...)
    actual_amount: float | None = Field(None)
    paid_amount: float | None = Field(None)
    event: UserEventList = Field(...)


class AdminUserStats(CustomBaseModel):
    """Aggregates for one user. Counted in SQL, not by loading the rows."""

    clubs_following: int = Field(0)
    events_registered: int = Field(0)
    events_attended: int = Field(0)
    events_won: int = Field(0)
    badges_earned: int = Field(0)
    total_paid: float = Field(0)


class AdminUserDetailResponse(UserDetailResponse):
    """
    UserDetailResponse plus the activity an admin is actually looking for.

    The lists behind these counts (followed clubs, registered events) have
    their own paginated endpoints rather than being embedded, so this stays
    one cheap query however active the account is.
    """

    stats: AdminUserStats = Field(...)
    is_active: bool = Field(True)
    suspended_reason: str | None = Field(None)
    # Only meaningful for an admin account, and only reported for one -- see
    # AdminUserListResponse for why reporting the raw column would describe
    # every user on the platform as an owner.
    admin_role: str | None = Field(None)

    @model_validator(mode="after")
    def _only_admins_have_a_role(self) -> "AdminUserDetailResponse":
        if self.user_type != "admin":
            self.admin_role = None
        return self


class FCMTokenRequest(CustomBaseModel):
    """Request schema for registering FCM token"""
    fcm_token: str = Field(..., description="Firebase Cloud Messaging token")
    platform: str = Field(..., description="Device platform (ios/android)")
    # 1 from an app that subscribes to its person's FCM topics (GET
    # /notifications/topics); absent from an app from before them.
    topics: int = Field(0, ge=0, le=1, description="1: this app subscribes to its FCM topics")


class FCMTokenDelete(CustomBaseModel):
    """The token to stop sending this account's pushes to: the device's own, at sign-out."""
    token: str = Field(..., min_length=1, max_length=512)


class UserPreferencesPatch(CustomBaseModel):
    """The person's own switches the server keeps. Only those sent change."""
    networking_visible: bool | None = Field(None, description="Show me in People Like You")


class UserPreferences(CustomBaseModel):
    networking_visible: bool = Field(...)


class FCMTokenResponse(CustomBaseModel):
    """Response schema for FCM token registration"""
    success: bool = Field(...)
    message: str = Field(...)

