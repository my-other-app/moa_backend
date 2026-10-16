"""Resend signs its webhooks the Svix way: HMAC-SHA256 over
`{svix-id}.{svix-timestamp}.{raw body}` with the base64 secret after
`whsec_`, sent as space-separated `v1,<base64>` signatures. A timestamp
more than five minutes off is refused, so an old capture cannot be replayed."""
import base64
import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300


def verify(secret: str, msg_id: str, timestamp: str, signatures: str, body: bytes, now: float | None = None) -> bool:
    if not (secret and msg_id and timestamp and signatures):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > TOLERANCE_SECONDS:
        return False
    try:
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except Exception:  # noqa: BLE001
        return False
    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
    for part in signatures.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return True
    return False
