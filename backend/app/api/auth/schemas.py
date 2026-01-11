from pydantic import BaseModel, Field
from app.core.response.base_model import CustomBaseModel


class Token(CustomBaseModel):
    token_type: str
    access_token: str
    refresh_token: str


class AuthTokenData(CustomBaseModel):
    user_id: int
    token_type: str


class AuthUser(CustomBaseModel):
    id: int
    full_name: str
    email: str
    phone: str | None = None
    username: str
    user_type: str


class GoogleSignInRequest(CustomBaseModel):
    id_token: str
    platform: str = Field(
        default="web", description="Platform type: 'web', 'android', or 'ios'"
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

