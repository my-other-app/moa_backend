"""One interface for every platform: "post this text and picture to this
account, and say where it is"; "find a post of ours since then"; sign-in;
token refresh; revoke.

An adapter is stateless: what it needs of an account comes in `Creds`
(decrypted for the call alone, never logged), and what it learns goes back
in its answer for the caller to save. A refusal is http.PlatformError with
its kind (http.AUTH, RATE, ...), which the scheduler turns into what the
club sees.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional


@dataclass
class Creds:
    """An account's keys for one call. `repr` never shows a token."""

    token: str
    external_id: str
    handle: Optional[str] = None
    refresh_token: Optional[str] = None
    connector_id: Optional[str] = None

    def __repr__(self) -> str:
        return f"Creds(external_id={self.external_id!r})"


@dataclass
class Picture:
    """A post's picture: its public address (Meta fetches it) and its key
    in the bucket (LinkedIn and X are sent the bytes)."""

    url: str
    key: str


@dataclass
class Published:
    external_id: str
    url: Optional[str] = None


@dataclass
class Candidate:
    """An account a sign-in can connect: a Page, an Instagram account, the
    member, an organisation, an X user."""

    external_id: str
    name: Optional[str]
    handle: Optional[str]
    picture: Optional[str]
    token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    refresh_expires_at: Optional[datetime] = None

    def __repr__(self) -> str:
        return f"Candidate(external_id={self.external_id!r}, name={self.name!r})"

    def choice(self) -> dict:
        return {"external_id": self.external_id, "name": self.name, "picture": self.picture}


@dataclass
class SignIn:
    """What a finished sign-in gave: the accounts it can connect, who signed
    in (Meta's app-scoped user id), the permissions granted."""

    candidates: list[Candidate] = field(default_factory=list)
    connector_id: Optional[str] = None
    scopes: Optional[str] = None
    #: Why nothing can be connected, when that is so (`no_pages`, ...).
    empty_reason: Optional[str] = None


@dataclass
class Tokens:
    token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    refresh_expires_at: Optional[datetime] = None

    def __repr__(self) -> str:
        return "Tokens(...)"


#: Called with Instagram's container id the moment it is made, so a retry
#: picks the same container up rather than making a second.
Progress = Callable[[str], Awaitable[None]]


_URL = re.compile(r"https?://\S+")
_SPACE = re.compile(r"\s+")


def same_text(ours: Optional[str], theirs: Optional[str]) -> bool:
    """Whether a post found on the platform is the one we sent: the words
    the same, once links (X rewrites them to t.co), case and spacing are set
    aside; a platform that shortened ours still matches by its start."""

    def norm(value: Optional[str]) -> str:
        value = _URL.sub(" ", value or "")
        value = value.replace("\\", "")
        return _SPACE.sub(" ", value).strip().lower()

    a, b = norm(ours), norm(theirs)
    if not a or not b:
        return False
    n = min(len(a), len(b), 200)
    return n >= 10 and a[:n] == b[:n]
