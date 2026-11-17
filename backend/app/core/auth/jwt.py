from datetime import datetime, timedelta, timezone
from app.config import settings
from app.core.auth.authentication import ALGORITHM
import jwt


#: Claims left out when they are None: JWT registers them, and a decoder
#: refuses a null one ("jti" must be a string).
_OMIT_WHEN_NONE = ("jti", "gen")


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    """A token signed with the current signing key (APP_SIGNING_KEYS' first,
    else APP_SECRET_KEY)."""
    to_encode = {k: v for k, v in data.items() if not (k in _OMIT_WHEN_NONE and v is None)}
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.signing_key, algorithm=ALGORITHM)
    return encoded_jwt


def decode_jwt_token(token: str):
    """The claims of a token signed with any of the verifying keys (so one
    signed before APP_SIGNING_KEYS was set, with APP_SECRET_KEY, still
    works); the error of the last key tried otherwise. An expired token
    is refused as soon as its signature is found good."""
    keys = settings.verifying_keys
    for key in keys[:-1]:
        try:
            return jwt.decode(token, key, algorithms=[ALGORITHM], verify=True)
        except jwt.InvalidSignatureError:
            continue
    return jwt.decode(token, keys[-1], algorithms=[ALGORITHM], verify=True)
