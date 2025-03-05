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
