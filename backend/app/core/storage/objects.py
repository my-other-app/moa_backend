"""Plain objects in the bucket, outside the ORM's image columns.

The image columns in `fields.py` upload as a side effect of saving a row,
which suits a logo on a template. A designer's uploads are different: the
file arrives before anything is saved, is referred to by key from inside a
JSON layout, and is read back by the renderer. These three calls are all
that needs: put, presign, get.

Called through the module (`objects.put_bytes`) rather than imported by
name, so a test can swap them without reaching S3.
"""
import io
import logging
from functools import lru_cache
from typing import Optional

import boto3

from app.config import settings

logger = logging.getLogger(__name__)


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


def presigned_url(key: str, expires: int = 3600) -> str:
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": settings.S3_BUCKET, "Key": key}, ExpiresIn=expires
    )


def public_url(key: str) -> str:
    """A link to a picture anyone may see (an agenda's speaker photo): signed
    for PUBLIC_IMAGE_URL_TTL and the same link for half of that, like the
    public image columns (app.core.storage.fields.signed_url)."""
    from app.core.storage.fields import signed_url

    return signed_url(key, public=True)


@lru_cache(maxsize=64)
def get_bytes(key: str) -> Optional[bytes]:
    """The object, or None when it is gone. Cached per process: keys are
    unique per upload, so a key never changes what it holds, and a club's
    template is the same bytes on every ticket of the batch."""
    buf = io.BytesIO()
    try:
        client().download_fileobj(settings.S3_BUCKET, key, buf)
    except Exception as exc:
        logger.warning("layout image %s could not be read: %s", key, exc)
        return None
    return buf.getvalue()
