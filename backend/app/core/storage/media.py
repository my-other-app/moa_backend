"""Public pictures' addresses: through the media CDN when it is on.

A public picture is one anyone may see: an event's poster, a club's logo
and cover, a speaker's, team member's or partner's photo, a post's image, a
stock avatar, a store item's photo. They were only ever handed out as
presigned S3 links, signed in us-east-1 and good for a week: a different
address every few days (so nothing downstream could cache them for long),
~280 ms a hop from India to Virginia on every view, and S3's egress on every
one.

With APP_MEDIA_CDN_URL set (https://media.myotherapp.in), `public_url`
answers `<cdn>/<key>`: a Cloudflare Worker in front of the bucket (the media
Worker, infra/cloudflare/media in the app repo) fetches the object once per
edge and keeps it. Unset, it answers exactly what it did before: the
week-signed link. Every public picture in an API response, on a club's site,
in an email or behind a link preview is made here -- the image columns
(fields.S3ImageField, `public=True`), the agenda's speaker photos and the
designer's public pictures (objects.public_url), store and merch photos, the
club site's image route.

What may have a CDN address is a fixed list of folders, PUBLIC_FOLDERS, and
nothing else, whatever asks. The Worker holds the same list and refuses
anything outside it, and the key it signs with can read nothing else (its
README). Tickets and certificates (docs/), template art and signatures
(templates/), designer uploads (events/{id}/layout/, clubs/{id}/layout/),
email pictures, event files, close-out reports, profile pictures and
organisation logos are never on it.

Keys. Every upload already had a key of its own (a uuid4), but nothing said
so: the Worker could not tell a key that will never change from one that
might, and so could not let browsers keep it. A public picture stored now has
a *versioned* key -- its file name is `v-` and 32 hex digits, a fresh uuid4,
variants beside it as `v-<hex>.<size>.<ext>` -- written once and never
again, so the CDN serves it as `immutable` for a year. Keys from before keep
working, with a short life.
"""
from __future__ import annotations

import re
import uuid
from typing import Callable, Optional
from urllib.parse import quote, unquote, urlsplit

from app.config import settings

#: The folders a CDN address may point into, under the bucket's base path.
#: `{id}` is a number. The media Worker's own list (infra/cloudflare/media/
#: media.js) must say the same; tests/test_media_cdn.py holds the backend to it.
PUBLIC_FOLDERS = (
    "clubs/logos/",       # club logos -- and event posters, which have always been stored here
    "clubs/covers/",
    "clubs/team/",
    "clubs/partners/",
    "clubs/threads/",     # a post's image
    "events/speakers/",
    "avatars/",           # the stock avatars people pick from
    "events/{id}/merch/",  # store and merch photos, shown on the event's store
)

#: What a picture's file name may end in. Never SVG: a picture served from
#: our own host must not be able to carry a script.
IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "webp", "gif")

_FOLDERS = "|".join(re.escape(f.rstrip("/")).replace(re.escape("{id}"), "[0-9]{1,12}") for f in PUBLIC_FOLDERS)
_EXTS = "|".join(IMAGE_EXTENSIONS)
# One file directly inside one of the folders: no deeper path, no dot-dot.
_PUBLIC = re.compile(rf"^(?:{_FOLDERS})/[A-Za-z0-9][A-Za-z0-9_.-]{{0,199}}\.(?:{_EXTS})$")
# A versioned file name, an original or one of its sizes.
_VERSIONED = re.compile(rf"^v-[0-9a-f]{{32}}(?:\.[a-z0-9_]{{1,24}})?\.(?:{_EXTS})$")


def enabled() -> bool:
    return bool(settings.MEDIA_CDN_URL)


def _base_path() -> str:
    return (settings.S3_BASE_PATH or "").rstrip("/")


def relative(key: Optional[str]) -> Optional[str]:
    """The key without the bucket's base path: what follows the CDN's host.
    None for a key stored under some other base path."""
    if not key or not isinstance(key, str):
        return None
    base = _base_path()
    if not base:
        return key
    if not key.startswith(base + "/"):
        return None
    return key[len(base) + 1:]


def is_public(key: Optional[str]) -> bool:
    """Whether `key` is a picture the CDN may serve: one file, an image, in
    one of PUBLIC_FOLDERS."""
    rest = relative(key)
    return rest is not None and ".." not in rest and bool(_PUBLIC.match(rest))


def is_versioned(key: Optional[str]) -> bool:
    """Whether `key` was stored under a versioned name (never overwritten)."""
    return bool(key) and bool(_VERSIONED.match(str(key).rpartition("/")[2]))


def versioned_name(ext: str) -> str:
    """A new file name for an upload: `v-<uuid4 hex>.<ext>`."""
    return f"v-{uuid.uuid4().hex}.{ext}"


def cdn_url(key: Optional[str]) -> Optional[str]:
    """`<cdn>/<key>` when the CDN is on and `key` is public; else None."""
    base = settings.MEDIA_CDN_URL
    if not base or not is_public(key):
        return None
    return f"{base}/{quote(relative(key), safe='/')}"


def public_url(key: str, *, otherwise: Optional[Callable[[str], str]] = None) -> str:
    """The address of a picture anyone may see -- the one builder.

    On the CDN when it is on and the key is in a public folder; else
    `otherwise(key)`, by default today's week-signed link (fields.signed_url).
    A caller whose picture used to have an hour-signed link passes that, so
    with the CDN off nothing changes.
    """
    url = cdn_url(key)
    if url is not None:
        return url
    if otherwise is None:
        from app.core.storage.fields import signed_url

        return signed_url(key, public=True)
    return otherwise(key)


def key_from_url(url: Optional[str]) -> Optional[str]:
    """The key behind one of our CDN addresses, else None.

    Code that reads a picture's bytes by key (the ticket renderer, the
    wallet pass, an email's inline images) is handed addresses, and an
    address on the CDN must read the same object a signed link would have:
    by key, from the process's cache, never over HTTP. Only a public key is
    answered -- the same ones the CDN would serve -- and only while the CDN
    is on, when its host is known.
    """
    base = settings.MEDIA_CDN_URL
    if not base or not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.netloc or f"{parts.scheme}://{parts.netloc.lower()}" != base:
        return None
    rest = unquote(parts.path or "").lstrip("/")
    if ".." in rest or not _PUBLIC.match(rest):
        return None
    base_path = _base_path()
    return f"{base_path}/{rest}" if base_path else rest


def srcset(image) -> Optional[str]:
    """`<url> <width>w, ...` for a picture read from an image column, from
    the sizes made of it at upload -- or None when the CDN is off or the
    picture has no sizes of its own (see fields.S3ImageField.has_variants).

    The widths are each size's box, an upper bound: a size is never wider,
    and is narrower when the picture is taller than the box's shape. Only
    for a picture shown in a small or fluid frame (a logo, a photo, a
    poster's card); a full-width cover keeps its original.
    """
    if not enabled() or image is None:
        return None
    paths = getattr(image, "variations", None) or {}
    widths = getattr(image, "widths", None) or {}
    out = []
    for name, width in sorted(widths.items(), key=lambda kv: kv[1]):
        url = cdn_url(paths.get(name))
        if url is None:
            return None
        out.append(f"{url} {int(width)}w")
    return ", ".join(out) or None
