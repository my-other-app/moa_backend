"""Links in an email that open a guest's own file without a login.

A guest reads the email on a phone that has never signed in, so the link
carries its own proof: an HMAC of the file and ticket id under the app
secret. Guessing one is guessing the secret; a revoked ticket's link stops
working the moment it is revoked, because the file route checks the ticket
too, not just the signature.
"""
import hashlib
import hmac
from urllib.parse import quote

from app.config import settings

FILE_KINDS = ("ticket", "certificate")


def file_token(kind: str, ticket_id: str) -> str:
    msg = f"file:{kind}:{ticket_id}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:32]


def file_token_ok(kind: str, ticket_id: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(file_token(kind, ticket_id), token)


def file_url(kind: str, ticket_id: str) -> str:
    base = settings.PUBLIC_API_URL.rstrip("/")
    return f"{base}/api/v1/files/{kind}s/{quote(ticket_id, safe='')}.pdf?s={file_token(kind, ticket_id)}"
