"""The media bucket's S3 clients: the one place they are made.

Every upload, read, copy, delete and listing in the media bucket
(settings.S3_BUCKET) goes through `media_client()`, and every signed link
handed to a browser through `media_client(public=True)`.

With S3_ENDPOINT_URL unset -- AWS S3 -- both are made exactly as they always
were: `boto3.client("s3", aws_access_key_id=..., aws_secret_access_key=...)`,
plus `region_name` when S3_REGION is set.

With it set -- a store of our own that speaks S3, such as Garage on the
production server -- the clients address it path-style
(`<endpoint>/<bucket>/<key>`) with SigV4 in S3_REGION:

- `media_client()` talks to S3_ENDPOINT_URL, the address the API reaches
  the store at (inside Docker, http://media:3900);
- `media_client(public=True)` signs links for S3_PUBLIC_URL, the address
  browsers reach it at (https://media.myotherapp.in), or S3_ENDPOINT_URL
  when that is unset. A SigV4 signature covers the host, so a link must be
  signed for the host that will receive it: the store sees the browser's
  Host header and checks the signature against it.

Only the media bucket. Off-site backups (BACKUP_S3_*), the club mailbox's
inbound bucket (MAILBOX_*) and SES make their own clients, always on AWS:
that is why this is not boto3's global AWS_ENDPOINT_URL_S3.

Made once per process and shared (clients are thread-safe): making one
loads and validates the S3 service model, about 4 ms.
"""
from __future__ import annotations

from functools import lru_cache

import boto3
from botocore.config import Config

from app.config import settings


def endpoint(*, public: bool = False) -> str:
    """The store's address for this client; empty on AWS S3."""
    internal = settings.S3_ENDPOINT_URL or ""
    if public:
        return settings.S3_PUBLIC_URL or internal
    return internal


def custom_endpoint() -> bool:
    """Whether the media bucket is on a store of our own rather than AWS S3."""
    return bool(settings.S3_ENDPOINT_URL)


def endpoints() -> tuple[str, ...]:
    """Every address the store is reached at (public first), for reading a
    key back out of a link (app.core.storage.bucket.key_from_url)."""
    found = []
    for url in (endpoint(public=True), endpoint()):
        if url and url not in found:
            found.append(url)
    return tuple(found)


def build(*, public: bool = False):
    """A new client for the media bucket (uncached: see `media_client`)."""
    kwargs = {
        "aws_access_key_id": settings.S3_ACCESS_KEY,
        "aws_secret_access_key": settings.S3_SECRET_KEY,
    }
    if settings.S3_REGION:
        kwargs["region_name"] = settings.S3_REGION
    url = endpoint(public=public)
    if url:
        kwargs["endpoint_url"] = url
        kwargs["config"] = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # Checksums only where the operation demands one, as boto3 did
            # before 1.36: S3-compatible stores differ in which of its newer
            # checksum headers and trailers they accept.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
    return boto3.client("s3", **kwargs)


@lru_cache(maxsize=2)
def _cached(public: bool):
    return build(public=public)


def media_client(*, public: bool = False):
    """The process's client for the media bucket: the internal endpoint for
    every operation, or (`public=True`) the one that signs browsers' links."""
    return _cached(bool(public))
