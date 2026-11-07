"""Redaction: everything that goes to Telegram from a log or an error passes
through redact() first.

What it removes (tests/test_redact.py has one case per pattern):
  - credentials in URLs (scheme://user:password@host)
  - Authorization / Cookie headers, Bearer tokens, JWTs, Telegram bot tokens
  - the values of token=, key=, secret=, password=, s=, k= query parameters
    (and of any parameter or key=value pair whose name contains token,
    secret, password, passwd or key)
  - the path segment after /tickets/, /certificates/, /registration-requests/,
    /waitlist/offers/, /clubs/access/invite/, /check-in/ (capability links)
  - ticket codes (MOA-XXXXX-XXXXX, and the older MOA-<hex>-XXXXX)
  - UUIDs, email addresses (also URL-encoded), phone numbers

Over-redaction is fine; a leak is not. The text is also capped in length.
"""

from __future__ import annotations

import re

MASK = "***"

#: Path prefixes whose next segment is a secret link token.
TOKEN_PATHS = (
    "tickets",
    "certificates",
    "registration-requests",
    "waitlist/offers",
    "clubs/access/invite",
    "check-in",
)

_SECRET_WORDS = r"(?:token|secret|password|passwd|pwd|api[_-]?key|access[_-]?key|private[_-]?key|key|signature|sig)"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # scheme://user:password@host -> scheme://user:***@host
    (re.compile(r"(://[^/:@\s]*:)[^@/\s]+@"), r"\1" + MASK + "@"),
    # Headers carrying credentials, whatever follows.
    (re.compile(r"(?i)\b((?:proxy-)?authorization|cookie|set-cookie|x-api-key|x-auth-token)(\s*[:=]\s*)[^\r\n]+"), r"\1\2" + MASK),
    # Bearer / Basic / Token schemes.
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    # JWTs (header.payload[.signature]).
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]*)?"), "<jwt>"),
    # Telegram bot tokens.
    (re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"), "<bot-token>"),
    # Query parameters: the short names exactly, the long ones by substring.
    (re.compile(r"(?i)([?&;](?:s|k|t|sig|code)=)[^&\s\"'#<>]*"), r"\1" + MASK),
    (re.compile(r"(?i)([?&;][\w.-]*" + _SECRET_WORDS + r"[\w.-]*=)[^&\s\"'#<>]*"), r"\1" + MASK),
    # JSON: "...token...": "value"
    (re.compile(r"(?i)(\"[\w.-]*" + _SECRET_WORDS + r"[\w.-]*\"\s*:\s*)\"[^\"]*\""), r'\1"' + MASK + '"'),
    # key=value / key: value in plain log text (not KeyError: ..., which is
    # an exception's name, not a credential).
    (
        re.compile(
            r"(?i)\b([\w.-]*" + _SECRET_WORDS + r"[\w.-]*)(?<!error)(?<!exception)(?<!warning)"
            r"(\s*[=:]\s*)(?!\*\*\*)[\"']?[^\s\"',;&]+[\"']?"
        ),
        r"\1\2" + MASK,
    ),
    # Capability links: the segment after the prefix.
    (re.compile(r"(/(?:" + "|".join(re.escape(p) for p in TOKEN_PATHS) + r")/)[^/\s?#\"'<>]+"), r"\1" + MASK),
    # Ticket codes: MOA-XXXXX-XXXXX, and the older MOA-<1-8 hex>-XXXXX.
    (re.compile(r"(?i)\bMOA-[A-Z0-9]{1,8}-[A-Z0-9]{4,8}\b"), "MOA-" + MASK),
    # UUIDs.
    (re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    # Email addresses, plain and URL-encoded.
    (re.compile(r"[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), "<email>"),
    # Phone numbers: international (+ or %2B, 8-15 digits, separators
    # allowed), and Indian mobiles with or without 91/0 in front.
    (re.compile(r"(?:\+|%2B)\d[\d\s().-]{6,17}\d"), "<phone>"),
    (re.compile(r"(?<![\w.:/-])(?:(?:91|0)[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\w.])"), "<phone>"),
]

#: Hard cap for any text that leaves the box.
MAX_CHARS = 3500


def redact(text: str, limit: int | None = MAX_CHARS) -> str:
    """The text with every pattern above masked, cut to `limit` characters
    (the end is kept: for logs and tracebacks it matters most)."""
    if not text:
        return ""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    if limit is not None and len(text) > limit:
        text = "[...cut...]\n" + text[-(limit - 13) :]
    return text


def scrub_secret(text: str, *secrets: str) -> str:
    """`text` without any of `secrets` (a token in an exception message)."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return text
