"""What a profile may hold: handles, links, tags, texts, and the QR codes'
tokens (the contract's "Handle rules" and the link rule of PUT /me/profile/links)."""
from __future__ import annotations

import re
import secrets
import unicodedata
from typing import Iterable, Optional
from urllib.parse import urlsplit

from app.response import CustomHTTPException

HANDLE_MIN, HANDLE_MAX = 3, 30
HANDLE_RX = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
#: Words a handle may not be: the contract's, and every top-level path the
#: website serves (moa_events src/app, its public/ files and redirects), so
#: no /u/<handle> ever reads as one of the site's own pages.
RESERVED = frozenset({
    # The contract's.
    "admin", "api", "app", "me", "u", "p", "help", "support", "about", "events", "clubs", "club", "speakers",
    "settings", "login", "signup", "register", "myotherapp", "official", "null", "undefined",
    # The website's top-level paths.
    "answers", "certificates", "claim", "data-deletion", "developers", "feedback", "for-clubs", "media", "og",
    "play", "pricing", "privacy", "requests", "robots", "robots.txt", "sitemap", "sitemap.xml", "sitemaps",
    "terms", "threads", "tickets", "topics", "verify", "manifest", "manifest.json", "manifest.webmanifest",
    "favicon.ico", "apple-touch-icon.png", "brand", "logo.svg", "network", "profiles", "profile", "well-known",
    "tests", "error", "not-found", "global-error", "layout", "page", "providers", "live", "compete",
    # The API's own path under /profiles.
    "by-code",
})

#: A QR code's token: from these, no 0 o 1 l i (they read as one another).
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_LENGTH = 10
CODE_RX = re.compile(rf"^[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")

LINK_URL_MAX = 500
#: Characters a link may not hold anywhere: they are how a link escapes an
#: attribute or a vCard line, or hides what it points at.
_URL_REFUSED = set('\\<>"{}|^`')
_HOST_RX = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}\.?$")
_IPV4_RX = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _bad(code: str, message: str, status: int = 422, **errors) -> CustomHTTPException:
    return CustomHTTPException(status, message=message, error_code=code, errors=errors or None)


# ─── Handles ────────────────────────────────────────────────────────────


def normal_handle(raw) -> str:
    """The handle as stored and matched: trimmed, lower case, a leading @ dropped."""
    text = str(raw or "").strip()
    if text.startswith("@"):
        text = text[1:]
    return text.lower()


def check_handle(raw) -> str:
    """The handle to store, or 422 HANDLE_INVALID / HANDLE_RESERVED."""
    handle = normal_handle(raw)
    if not (HANDLE_MIN <= len(handle) <= HANDLE_MAX):
        raise _bad("HANDLE_INVALID", f"A handle is {HANDLE_MIN} to {HANDLE_MAX} characters.")
    if not HANDLE_RX.match(handle) or ".." in handle:
        raise _bad(
            "HANDLE_INVALID",
            "Use lower-case letters, digits, dots, dashes and underscores, starting and ending with a letter or digit, and no two dots together.",
        )
    if handle in RESERVED:
        raise _bad("HANDLE_RESERVED", "That handle is kept for the site. Pick another.")
    return handle


# ─── Codes ──────────────────────────────────────────────────────────────


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normal_code(raw) -> Optional[str]:
    """The token a scanned or typed code stands for, or None when it cannot be one."""
    text = str(raw or "").strip().lower()
    return text if CODE_RX.match(text) else None


# ─── Links ──────────────────────────────────────────────────────────────


def check_url(raw, *, field: str = "url") -> str:
    """A link a page may show: http(s) only, a real host, no user name or
    password, no spaces, quotes or angle brackets, at most 500 characters.
    `javascript:`, `data:` and every other scheme are refused. 422 LINK_INVALID."""
    if not isinstance(raw, str):
        raise _bad("LINK_INVALID", "A link must be a web address.", **{field: "not text"})
    url = raw.strip()
    why = None
    if not url:
        why = "empty"
    elif len(url) > LINK_URL_MAX:
        why = f"longer than {LINK_URL_MAX} characters"
    elif any(ch.isspace() or unicodedata.category(ch)[0] == "C" or ch in _URL_REFUSED for ch in url):
        why = "it holds spaces or characters a link cannot"
    if why is None:
        try:
            parts = urlsplit(url)
            parts.port  # a port that is not a number raises
        except ValueError:
            parts, why = None, "not a web address"
        if parts is not None:
            if parts.scheme.lower() not in ("http", "https"):
                why = "only https:// and http:// links"
            elif "@" in parts.netloc or parts.username is not None or parts.password is not None:
                why = "a link may not carry a user name or password"
            else:
                host = (parts.hostname or "").rstrip(".")
                try:
                    ascii_host = host.encode("idna").decode("ascii").lower() if host else ""
                except UnicodeError:
                    ascii_host = ""
                if not ascii_host or (not _HOST_RX.match(ascii_host) and not _IPV4_RX.match(ascii_host)):
                    why = "not a web address"
    if why is not None:
        raise _bad("LINK_INVALID", f"That link can't be used: {why}.", **{field: why})
    # The scheme as browsers write it; the rest as typed.
    scheme, _, rest = url.partition(":")
    return f"{scheme.lower()}:{rest}"


# ─── Texts and tags ─────────────────────────────────────────────────────


#: Direction overrides: they make a name read as something else.
_BIDI = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _control(ch: str) -> bool:
    """A control character or a direction override (emoji joiners stay)."""
    return unicodedata.category(ch) == "Cc" or ch in _BIDI


def one_line(raw, limit: int) -> Optional[str]:
    """A short text on one line: spaces collapsed, control characters
    dropped; empty is None. Over `limit` is refused by the schema first."""
    if raw is None:
        return None
    text = "".join(" " if _control(ch) else ch for ch in str(raw))
    text = " ".join(text.split())
    return text[:limit] or None


def paragraph(raw, limit: int) -> Optional[str]:
    """A bio: line breaks kept (at most two in a row), other control characters dropped."""
    if raw is None:
        return None
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or not _control(ch))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:limit] or None


def tags(raw: Optional[Iterable], *, most: int, chars: int) -> list[str]:
    """Tags as kept: trimmed, one line, each once (case-blind), in the order
    given. ValueError (a 422 from the schema) past `most` or `chars`."""
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, str):
            raise ValueError("each tag is text")
        tag = one_line(item, 1000) or ""
        if not tag:
            continue
        if len(tag) > chars:
            raise ValueError(f"a tag is at most {chars} characters")
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    if len(out) > most:
        raise ValueError(f"at most {most} tags")
    return out


def like_pattern(q: str) -> str:
    """`q` as an ILIKE "contains" pattern, its own % and _ taken literally."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
