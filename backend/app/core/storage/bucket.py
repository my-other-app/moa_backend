"""Our bucket's addresses: the key behind a signed URL.

A signed URL is good for an hour; the key is good for ever. Anything that is
kept -- a template's config, a cache entry -- holds the key and signs it when
it is shown. This reads the key back out of a URL we signed earlier, for the
configs saved before that rule existed.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional
from urllib.parse import unquote, urlsplit

from app.config import settings
from app.core.storage import clients, media


def _bucket() -> str:
    return settings.S3_BUCKET


def _on_our_store(netloc: str, path: str, bucket: str) -> Optional[str]:
    """The key in a path-style address on our own store, `{S3_PUBLIC_URL or
    S3_ENDPOINT_URL}/{bucket}/{key}`; None for any other host or bucket."""
    for endpoint in clients.endpoints():
        base = urlsplit(endpoint)
        if netloc.lower() != base.netloc.lower():
            continue
        prefix = f"{base.path.rstrip('/')}/{bucket}/"
        if path.startswith(prefix):
            return path[len(prefix):] or None
    return None


def key_from_url(url: Optional[str]) -> Optional[str]:
    """The object key in our bucket that `url` points at, else None.

    Understands the shapes boto3 signs: virtual-hosted
    (`{bucket}.s3[.{region}].amazonaws.com/{key}`) and path style
    (`s3[.{region}].amazonaws.com/{bucket}/{key}`), and, on a store of our
    own (S3_ENDPOINT_URL), path style on its public or internal address
    (`https://media.myotherapp.in/{bucket}/{key}`). The query (the signature)
    is ignored, so an expired URL still names its key. And, while the media
    CDN is on, its addresses (`{APP_MEDIA_CDN_URL}/{key}`, public pictures
    only: media.key_from_url), so a poster handed out on the CDN is read by
    its key like any other -- from the process's cache, never over HTTP.
    """
    if not url or not isinstance(url, str):
        return None
    on_cdn = media.key_from_url(url)
    if on_cdn:
        return on_cdn
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.hostname or ""
    bucket = _bucket()
    path = unquote(parts.path or "")
    ours = _on_our_store(parts.netloc, path, bucket)
    if ours:
        return ours
    if not host.endswith(".amazonaws.com"):
        return None
    if host.startswith(f"{bucket}.s3.") or host.startswith(f"{bucket}.s3-"):
        key = path[1:] if path.startswith("/") else path
    elif host.startswith("s3.") or host.startswith("s3-"):
        prefix = f"/{bucket}/"
        if not path.startswith(prefix):
            return None
        key = path[len(prefix):]
    else:
        return None
    return key or None


def under(key: Optional[str], prefixes: Iterable[str]) -> bool:
    """Whether `key` lives inside one of the folders (no `..` tricks)."""
    if not key or ".." in key.split("/"):
        return False
    return any(key.startswith(p) for p in prefixes)


def prefix(*parts: str) -> str:
    """A folder under the bucket's base path, ending in a slash."""
    return os.path.join(settings.S3_BASE_PATH, *parts).rstrip("/") + "/"
