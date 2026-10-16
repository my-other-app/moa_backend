from slowapi import Limiter
from slowapi.util import get_remote_address

# In-memory store (no Redis in this stack today, and WORKERS=1 in production
# currently, so per-process limits are accurate). If the app is ever scaled to
# multiple workers/instances, switch to a Redis-backed storage_uri so limits
# are shared across processes.
limiter = Limiter(key_func=get_remote_address)


def account_or_address(request) -> str:
    """The limiter's key for an endpoint only a signed-in account calls: that
    account, from its own (verified) token; the address when there is none.

    The address alone is a poor key behind this deployment's proxy: nginx on
    the host reaches the container through Docker's published port, so the
    address the app sees can be the same bridge address for every caller,
    and a per-address limit becomes one budget the whole platform shares --
    one person could spend it for everyone.
    """
    auth = request.headers.get("authorization") or ""
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get("access_token")
    if token:
        try:
            from app.core.auth.jwt import decode_jwt_token

            user_id = decode_jwt_token(token).get("user_id")
            if user_id:
                return f"user:{user_id}"
        except Exception:
            pass
    return get_remote_address(request)
