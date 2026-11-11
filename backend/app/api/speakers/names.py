"""How a speaker's name, link and photo are compared, and how a profile's
address is made. Pure functions: the backfill, the event form and the
picker all read names the same way."""
from __future__ import annotations

import re
import unicodedata
from typing import Optional
from urllib.parse import urlsplit

#: Titles that say nothing about who someone is: "Dr. Rao" and "Rao" are one name.
TITLES = frozenset({
    "dr", "prof", "professor", "mr", "mrs", "ms", "miss", "mx", "er", "adv", "advocate", "shri", "sri", "smt", "kum",
    "capt", "col", "lt", "maj", "gen", "rev", "fr", "sir", "madam", "ar",
})
_NOT_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
_SLUG_DROP = re.compile(r"[^a-z0-9\s-]")
_SLUG_JOIN = re.compile(r"[\s-]+")

LINK_KINDS = ("website", "linkedin", "x", "github", "other")
#: Hosts that say which kind a link is.
_HOST_KINDS = {
    "linkedin.com": "linkedin", "lnkd.in": "linkedin",
    "x.com": "x", "twitter.com": "x",
    "github.com": "github",
}


def _plain(text: str) -> str:
    """Without accents, case folded: "Évora" and "evora" read the same."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def name_key(name: Optional[str]) -> str:
    """The name as matching compares it: case folded, accents and
    punctuation gone, leading titles dropped, single spaces. Never empty for
    a name with a letter in it: a name that is only a title keeps it."""
    words = _SPACES.sub(" ", _NOT_WORD.sub(" ", _plain(name or ""))).strip().split(" ")
    words = [w for w in words if w]
    kept = list(words)
    while len(kept) > 1 and kept[0] in TITLES:
        kept.pop(0)
    return " ".join(kept)[:120]


def search_key(q: Optional[str]) -> str:
    """A search box's words, read as name_key reads a name (titles kept: a
    search for "dr" means it)."""
    return _SPACES.sub(" ", _NOT_WORD.sub(" ", _plain(q or ""))).strip()[:120]


def link_kind(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for known, kind in _HOST_KINDS.items():
        if host == known or host.endswith("." + known):
            return kind
    return "website"


def link_key(url: Optional[str]) -> Optional[str]:
    """A link as matching compares it: host without www, path without its
    trailing slash, no query or fragment, lower case. None for no link."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/").lower()
    return f"{host}{path}"


def profile_link_keys(links: Optional[dict]) -> set:
    return {k for k in (link_key(v) for v in (links or {}).values() if isinstance(v, str)) if k}


def links_from(url: Optional[str]) -> dict:
    """An event row's one link, as a profile's links: under its kind."""
    if not url:
        return {}
    return {link_kind(url): url}


def first_link(links: Optional[dict]) -> Optional[str]:
    """The link an event copy shows from a profile's links: the website first."""
    for kind in LINK_KINDS:
        v = (links or {}).get(kind)
        if v:
            return v
    return None


def slug_base(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii").lower()
    words = name_key(plain).split(" ")
    base = _SLUG_JOIN.sub("-", _SLUG_DROP.sub("", " ".join(words))).strip("-")
    return (base or "speaker")[:100]


def initials(name: str) -> str:
    """For a missing photo: up to two letters."""
    words = [w for w in name_key(name).split(" ") if w]
    return "".join(w[0] for w in words[:2]).upper()


def names_alike(a: str, b: str) -> bool:
    """Two name keys that are plainly one name: equal, or one the other with
    a word more ("Meera" and "Meera Nair"). For a shared link only."""
    if not a or not b:
        return False
    if a == b:
        return True
    wa, wb = set(a.split(" ")), set(b.split(" "))
    return bool(wa) and bool(wb) and (wa <= wb or wb <= wa)


#: How agenda free text names several people: "Meera, Rao & Kiran", "A and B", "A / B".
_SPLIT = re.compile(r"\s*(?:,|&|\+|/|;|\band\b|\|)\s*", re.I)


def agenda_names(text: Optional[str]) -> list[str]:
    """The names an agenda session's free-text speaker lists, as keys."""
    if not text:
        return []
    out: list[str] = []
    for part in _SPLIT.split(text):
        key = name_key(part)
        if key and key not in out:
            out.append(key)
    return out
