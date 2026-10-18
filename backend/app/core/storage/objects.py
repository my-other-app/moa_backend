"""Plain objects in the bucket, outside the ORM's image columns.

The image columns in `fields.py` upload as a side effect of saving a row,
which suits a logo on a template. A designer's uploads are different: the
file arrives before anything is saved, is referred to by key from inside a
JSON layout, and is read back by the renderer. These calls are all that
needs: put, presign, get.

Called through the module (`objects.put_bytes`) rather than imported by
name, so a test can swap them without reaching S3.
"""
import io
import logging
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Callable, Optional

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

MB = 1024 * 1024


@lru_cache(maxsize=1)
def client():
    return boto3.client(
        "s3",
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
    )


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    client().upload_fileobj(
        io.BytesIO(data), settings.S3_BUCKET, key, ExtraArgs={"ContentType": content_type}
    )
    with _missing_lock:
        _missing.pop(key, None)


def presigned_url(key: str, expires: int = 3600) -> str:
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": settings.S3_BUCKET, "Key": key}, ExpiresIn=expires
    )


class ByteCache:
    """Least recently used, capped by the bytes it holds rather than by count.

    The renderer's caches used to be `lru_cache(maxsize=64)`: 64 entries of
    whatever size -- 64 background images of 5 MB are 320 MB in a process
    that has one worker -- and a `None` answer was kept like any other, so
    one timeout blanked a club's logo on every ticket until the process
    restarted. This keeps only bytes, never a failure, up to a budget.
    Thread-safe: renders read it from several threads at once.
    """

    def __init__(self, max_bytes: Callable[[], int], max_item: Callable[[], int]):
        self._max_bytes = max_bytes
        self._max_item = max_item
        self._items: "OrderedDict[str, bytes]" = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            data = self._items.get(key)
            if data is not None:
                self._items.move_to_end(key)
            return data

    def put(self, key: str, data: Optional[bytes]) -> None:
        if not data or len(data) > self._max_item():
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._items[key] = data
            self._size += len(data)
            budget = self._max_bytes()
            while self._size > budget and self._items:
                _, gone = self._items.popitem(last=False)
                self._size -= len(gone)

    @property
    def size(self) -> int:
        return self._size

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0


_images = ByteCache(
    lambda: max(0, settings.RENDER_IMAGE_CACHE_MB) * MB,
    lambda: max(0, settings.RENDER_IMAGE_CACHE_ITEM_MB) * MB,
)

# A key the bucket says it does not have, for a few minutes: keys are never
# reused, so "not found" is an answer, not an accident -- but only a short
# while, in case the upload that makes it is still on its way.
_MISSING_FOR = 300.0
_MISSING_MAX = 5000
_missing: "OrderedDict[str, float]" = OrderedDict()
_missing_lock = threading.Lock()


def _not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    return code in ("404", "NoSuchKey", "NotFound")


def _known_missing(key: str) -> bool:
    with _missing_lock:
        until = _missing.get(key)
        if until is None:
            return False
        if until > time.monotonic():
            return True
        del _missing[key]
        return False


def _remember_missing(key: str) -> None:
    with _missing_lock:
        _missing[key] = time.monotonic() + _MISSING_FOR
        _missing.move_to_end(key)
        while len(_missing) > _MISSING_MAX:
            _missing.popitem(last=False)


def fetch(key: str, *, quiet_if_missing: bool = False) -> Optional[bytes]:
    """The object straight from the bucket, kept nowhere; None when it is
    not there or could not be read. `quiet_if_missing` is for a lookup that
    expects to miss (a rendered document not made yet)."""
    if not key:
        return None
    buf = io.BytesIO()
    try:
        client().download_fileobj(settings.S3_BUCKET, key, buf)
    except Exception as exc:
        if _not_found(exc):
            if quiet_if_missing:
                return None
            _remember_missing(key)
        logger.warning("object %s could not be read: %s", key, exc)
        return None
    return buf.getvalue()


def get_bytes(key: str) -> Optional[bytes]:
    """The object, or None when it is gone or could not be read just now.

    Kept per process within RENDER_IMAGE_CACHE_MB: keys are unique per
    upload, so a key never changes what it holds, and a club's template is
    the same bytes on every ticket of the batch. A failure is never kept --
    the next render asks again -- except "no such key", for a few minutes.
    """
    if not key:
        return None
    hit = _images.get(key)
    if hit is not None:
        return hit
    if _known_missing(key):
        return None
    data = fetch(key)
    if data is not None:
        _images.put(key, data)
    return data


def cache_clear() -> None:
    _images.clear()
    with _missing_lock:
        _missing.clear()
