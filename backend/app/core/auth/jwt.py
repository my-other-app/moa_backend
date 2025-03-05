from datetime import datetime, timedelta, timezone
from app.config import settings
from app.core.auth.authentication import ALGORITHM
import jwt


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_jwt_token(token: str):
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM], verify=True)
