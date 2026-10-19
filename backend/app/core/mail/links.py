"""Links in an email that open a guest's own file without a login.

A guest reads the email on a phone that has never signed in, so the link
carries its own proof: an HMAC of the file and ticket id under a key only
the server holds. Guessing one is guessing the key; a revoked ticket's link
stops working the moment it is revoked, because the file route checks the
ticket too, not just the signature.

The links never expire, so the key must be able to change under them:
LINK_SIGNING_KEYS is a list, the first key signs and every one verifies, and
a rotation adds the new key in front and drops the old one later
(docs/OPERATIONS.md, section 5). Unset, SECRET_KEY signs, as it always has.
A link whose key has been dropped -- or one signed with a key that leaked,
which is never put back -- is refused; a browser that follows it gets a page
offering a fresh one by email (app/api/emails/link_page.py).
"""
import hashlib
import hmac
from urllib.parse import quote

from app.config import settings

FILE_KINDS = ("ticket", "certificate")
#: Hex characters of the HMAC a link carries.
TOKEN_LENGTH = 32


def _digest(key: str, message: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()[:TOKEN_LENGTH]


def sign(message: str) -> str:
    """`message`'s token under the signing key (the list's first)."""
    return _digest(settings.link_signing_keys[0], message)


def verify(message: str, token: str) -> bool:
    """Whether `token` is `message`'s under any of the keys. Every key is
    tried, whichever matches, so the time taken says nothing about which."""
    if not token or not token.isascii():  # compare_digest refuses other text
        return False
    ok = False
    for key in settings.link_signing_keys:
        ok |= hmac.compare_digest(_digest(key, message), token)
    return ok


def file_token(kind: str, ticket_id: str) -> str:
    return sign(f"file:{kind}:{ticket_id}")


def file_token_ok(kind: str, ticket_id: str, token: str) -> bool:
    return verify(f"file:{kind}:{ticket_id}", token)


def file_url(kind: str, ticket_id: str) -> str:
    base = settings.PUBLIC_API_URL.rstrip("/")
    return f"{base}/api/v1/files/{kind}s/{quote(ticket_id, safe='')}.pdf?s={file_token(kind, ticket_id)}"
