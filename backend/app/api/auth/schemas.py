from datetime import datetime
from typing import Literal

from pydantic import field_validator, BaseModel, Field
from app.core.response.base_model import CustomBaseModel


class Token(CustomBaseModel):
    token_type: str
    access_token: str
    # Empty for an admin's session as someone else, which is never renewed.
    refresh_token: str


class AuthTokenData(CustomBaseModel):
    user_id: int
    token_type: str
    token_version: int = 0
    # Set only on a session an admin started on someone else's behalf. It
    # travels with the token so the session is identifiable for its whole
    # life, not just at the moment it was created.
    impersonated_by: int | None = None
    # Identifies the session this token belongs to, so one can be ended
    # without ending all of them. Absent on tokens issued before sessions
    # existed, which are treated as live rather than signed out on deploy.
    sid: str | None = None
    # Set when a club team member signed in with their own password: the
    # session acts as the club, and this says who is behind it. Checked on
    # every request (still a member; the role allows this) — see club_access.
    member_id: int | None = None
    # A refresh token's place in its session's rotation (sessions.rotate).
    # Absent on refresh tokens from before rotation: read as 0, the
    # generation every session then had.
    gen: int | None = None
    # An impersonation hand-off's one-time id: the id its session will have.
    jti: str | None = None


#: The shortest reason an admin may give for a session as someone else:
#: enough for "ticket 4512: can't publish", too much for "test" or "x".
IMPERSONATION_REASON_MIN = 10
IMPERSONATION_REASON_MAX = 300


class ImpersonationRequest(CustomBaseModel):
    """Why the admin needs the session. Required (IMPERSONATION_REASON_MIN
    characters at least, checked by the endpoint so a short or missing one
    is answered in words rather than as a schema error), and recorded in the
    audit log with the session."""

    reason: str | None = None


class ImpersonationResponse(CustomBaseModel):
    token: str
    expires_in: int
    user_id: int
    full_name: str | None = None
    email: str | None = None
    user_type: str


class AuthUser(CustomBaseModel):
    id: int
    full_name: str
    email: str
    phone: str | None = None
    username: str
    user_type: str
    # Google and Apple accounts always; a password account once its owner
    # typed back the code sent to the address.
    email_verified: bool = False
    # The dashboard needs this to stop offering actions the role will refuse.
    # It is a convenience for the UI, not a control: every endpoint still
    # checks the capability itself, so a client that ignores this gets a 403
    # rather than its way.
    admin_role: str | None = None

    @field_validator("admin_role", mode="before")
    @classmethod
    def _role_to_value(cls, value):
        """`admin_role` is a SQLAlchemy Enum, which serialises as
        `AdminRoles.owner` rather than `owner` unless unwrapped here."""
        return getattr(value, "value", value)


class GoogleSignInRequest(CustomBaseModel):
    id_token: str
    platform: str = Field(
        default="web", description="Platform type: 'web', 'android', or 'ios'"
    )


class DashboardGoogleSignInRequest(CustomBaseModel):
    """The club dashboard's or the main portal's "Continue with Google"."""

    id_token: str = Field(min_length=1, description="The credential Google's sign-in button gave")
    scope: Literal["club", "admin"] = Field(description="club: the club dashboard; admin: the main portal")
    totp_code: str | None = Field(
        default=None, description="After 401 TOTP_REQUIRED: the authenticator (or a recovery) code, sent with the same id_token",
    )


class PasswordResetRequest(CustomBaseModel):
    """Request schema for admin password reset"""
    email_or_username: str = Field(..., description="Email or username of the user")
    new_password: str = Field(..., min_length=6, description="New password (min 6 characters)")


class PasswordResetResponse(CustomBaseModel):
    """Response schema for password reset"""
    success: bool
    message: str
    email: str | None = None
    username: str | None = None


class ChangePasswordRequest(CustomBaseModel):
    """Request schema for user changing their own password"""
    old_password: str = Field(..., description="Current password")
    new_password: str = Field(..., min_length=6, description="New password (min 6 characters)")


class ChangePasswordResponse(CustomBaseModel):
    success: bool
    message: str


class EmailCodeSent(CustomBaseModel):
    """A verification code is on its way to `email`."""

    sent: bool = True
    email: str
    # Seconds the code stays good.
    expires_in: int
    # Seconds before another code may be asked for.
    retry_after: int


class EmailVerifyRequest(CustomBaseModel):
    code: str = Field(..., min_length=1, max_length=20, description="The six-digit code from the email")


class EmailVerified(CustomBaseModel):
    email_verified: bool = True
    email_verified_at: datetime
    # Whether tickets and the rest booked as a guest under the address were
    # brought into the account just now, and how many registrations moved.
    merged: bool = False
    registrations_transferred: int = 0


class PasswordForgotRequest(CustomBaseModel):
    email: str = Field(..., min_length=1, max_length=254, description="The account's email address")


class PasswordForgotResponse(CustomBaseModel):
    """Always the same, whether or not the address has an account."""

    sent: bool = True


class PasswordResetConfirm(CustomBaseModel):
    email: str = Field(..., min_length=1, max_length=254)
    code: str = Field(..., min_length=1, max_length=20, description="The six-digit code from the email")
    # The same rule as signing up (/user/register) and the app: 6 or more.
    new_password: str = Field(..., min_length=6)
    # Only for an account with two-factor on: its authenticator or a recovery code.
    totp_code: str | None = Field(None, max_length=64)


class AppleSignInRequest(CustomBaseModel):
    """Request schema for Apple Sign In"""
    identity_token: str = Field(..., description="JWT identity token from Apple")
    authorization_code: str = Field(..., description="Authorization code from Apple")
    user_name: str | None = Field(None, description="User's name (only on first sign in)")
    user_email: str | None = Field(None, description="User's email (only if shared)")


