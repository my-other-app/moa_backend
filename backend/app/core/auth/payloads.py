"""Short-lived signed payloads: a kiosk's rotating QR, a redemption a
guest can undo for a minute. HMAC under the app secret, base64url, with
an expiry inside; nothing is stored and nothing can be forged."""
import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from app.config import settings


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sig(body: str) -> str:
    return _b64(hmac.new(settings.SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())[:32]


def sign_payload(payload: dict, ttl_seconds: int, *, slot: Optional[int] = None) -> str:
    """`slot` pins the issue time (a rotating QR is the same token for a
    whole minute, so a kiosk's poll does not redraw it on every tick)."""
    issued = slot if slot is not None else int(time.time())
    body = _b64(json.dumps({**payload, "iat": issued, "exp": issued + ttl_seconds}, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sig(body)}"


def verify_payload(token: str) -> Optional[dict]:
    """The payload, or None when the token is malformed, forged or expired."""
    try:
        body, sig = token.strip().split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sig(body), sig):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload
