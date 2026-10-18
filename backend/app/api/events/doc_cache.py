"""Rendered tickets and certificates, kept in the bucket.

A ticket was drawn again for every download, every resend and every bundle
-- the same registration, the same design, the same bytes. Each rendered
PDF is now kept at

    docs/{kind}/{registration_id}/{design_version}-{fingerprint}.pdf

where the fingerprint is a hash of everything the drawing reads: the values
printed, the design (with its image keys, never its expiring links), the
renderer's version and, for the HTML templates, the template file itself.
Change any of them and the key changes, so a cached file is never stale;
nothing is ever invalidated, an old one just stops being asked for.

A render that could not read one of its images (a logo, the artwork) is
still handed out -- a ticket without its logo beats no ticket -- but it is
not kept, so the next request draws it again with the image.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from functools import lru_cache
from typing import Callable, Optional

from app.config import settings
from app.core.layout import pool
from app.core.storage import objects
from app.core.storage.bucket import prefix

logger = logging.getLogger(__name__)

#: Bump when the renderer changes what an existing design prints like.
RENDER_VERSION = "2026-10-18.1"

# Keys a style carries that say nothing about what is printed (a link signed
# now expires in an hour; the key beside it is what names the image).
_VOLATILE_SUFFIX = "_url"


def _stable(style: dict) -> dict:
    return {k: v for k, v in (style or {}).items() if not (k.endswith(_VOLATILE_SUFFIX) and f"{k[:-len(_VOLATILE_SUFFIX)]}_key" in style)}


def fingerprint(*parts) -> str:
    """A short, stable hash of what a render reads."""
    blob = json.dumps(
        [RENDER_VERSION, *[_stable(p) if isinstance(p, dict) else p for p in parts]],
        sort_keys=True, default=str, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


@lru_cache(maxsize=16)
def template_digest(template_path: str, template_dir: str = "templates/pdf/") -> str:
    """The HTML template's own bytes, hashed once per process: a deploy that
    changes the template changes every key made from it."""
    try:
        with open(f"{template_dir}{template_path}", "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return "missing"


def key_for(kind: str, registration_id, design_version: int, print_: str) -> str:
    return f"{prefix('docs', kind, str(registration_id))}{int(design_version or 0)}-{print_}.pdf"


def _put(key: str, pdf: bytes) -> None:
    try:
        objects.put_bytes(key, pdf, "application/pdf")
    except Exception as exc:  # noqa: BLE001 - a cache that cannot be written is only a cache
        logger.warning("rendered document %s could not be kept: %s", key, exc)


async def lookup(key: str) -> Optional[bytes]:
    if not settings.RENDER_DOC_CACHE:
        return None
    return await asyncio.to_thread(objects.fetch, key, quiet_if_missing=True)


async def cached(key: Optional[str], draw: Callable[[], tuple[bytes, bool]]) -> bytes:
    """The kept PDF at `key`, else `draw()` on the render pool, kept when
    complete. No key (a document for no saved registration): drawn, not kept."""
    if key:
        hit = await lookup(key)
        if hit:
            return hit
    pdf, complete = await pool.run(draw)
    if key and complete and settings.RENDER_DOC_CACHE:
        await asyncio.to_thread(_put, key, pdf)
    return pdf

