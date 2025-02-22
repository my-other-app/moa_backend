from pydantic import BaseModel


class Token(BaseModel):
    token_type: str
    access_token: str
    refresh_token: str


class AuthTokenData(BaseModel):
    user_id: int
    token_type: str


class AuthUser(BaseModel):
    id: int
    full_name: str
    email: str
    phone: str | None = None
    username: str
    user_type: str
