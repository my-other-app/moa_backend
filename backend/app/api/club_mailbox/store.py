"""Where a mailbox's files live: the media bucket, in a private folder.

`mailbox/<club>/<message>/<n>`: never one of the CDN's public folders
(app/core/storage/media.PUBLIC_FOLDERS), never signed into a public link.
The dashboard reads a file only through the API's attachment proxy, as the
signed-in team. Deleted with its message (retention, a thread deleted, the
mailbox wiped).

Called through the module (`store.put`), so a test swaps it for memory.
"""
from __future__ import annotations

import io
import logging
from typing import Iterable, Optional

from app.config import settings

logger = logging.getLogger(__name__)

PREFIX = "mailbox"


def key_for(club_id: int, message_id: int, n: int) -> str:
    return f"{PREFIX}/{int(club_id)}/{int(message_id)}/{int(n)}"


def _client():
    from app.core.storage import objects

    return objects.client()


def put(key: str, data: bytes, content_type: str) -> None:
    _client().upload_fileobj(
        io.BytesIO(data), settings.S3_BUCKET, key,
        ExtraArgs={"ContentType": "application/octet-stream", "ServerSideEncryption": "AES256"},
    )


def get(key: str) -> Optional[bytes]:
    try:
        response = _client().get_object(Bucket=settings.S3_BUCKET, Key=key)
    except Exception as exc:  # noqa: BLE001 - missing, or the bucket unreachable: the proxy says so
        logger.info("mailbox file %s could not be read (%s)", key.rsplit("/", 1)[0], type(exc).__name__)
        return None
    return response["Body"].read()


def delete(keys: Iterable[str]) -> int:
    """Delete these objects, a thousand a request. Never raises: a file left
    behind is deleted by the next sweep's retention run of its folder."""
    keys = [k for k in keys if k and k.startswith(f"{PREFIX}/")]
    done = 0
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        try:
            _client().delete_objects(Bucket=settings.S3_BUCKET, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
            done += len(chunk)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mailbox: %d file(s) could not be deleted (%s)", len(chunk), type(exc).__name__)
    return done
