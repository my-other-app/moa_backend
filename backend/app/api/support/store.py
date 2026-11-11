"""Where the desk's and the leads' files live: the media bucket, in private
folders.

`support/<case or "uploads">/<token>` and `leads/<lead>/<token>`: never one
of the CDN's public folders (app/core/storage/media.PUBLIC_FOLDERS), never
signed into a public link. A file is read only through the API, by whoever
may see the case or the lead. Deleted with its case's messages (retention)
or by an owner.

Called through the module (`store.put`), so a test swaps it for memory --
the same way as the club mailbox's store, whose writer this reuses.
"""
from __future__ import annotations

import logging
import secrets
from typing import Iterable, Optional

from app.config import settings

logger = logging.getLogger(__name__)

PREFIXES = ("support/", "leads/")


def upload_key() -> str:
    return f"su_{secrets.token_hex(16)}"


def new_key(folder: str, owner, name: str) -> str:
    if folder not in ("support", "leads"):
        raise ValueError(folder)
    return f"{folder}/{owner}/{secrets.token_hex(12)}-{int(len(name or ''))}"


def put(key: str, data: bytes, content_type: str) -> None:
    if not key.startswith(PREFIXES):
        raise ValueError("not a desk file")
    from app.api.club_mailbox import store as mailbox_store

    # The mailbox's writer: private, encrypted at rest where S3 does it.
    import io

    from app.core.storage import clients

    extra = {"ContentType": "application/octet-stream"}
    if not clients.custom_endpoint():
        extra["ServerSideEncryption"] = "AES256"
    mailbox_store._client().upload_fileobj(io.BytesIO(data), settings.S3_BUCKET, key, ExtraArgs=extra)


def get(key: str) -> Optional[bytes]:
    if not key.startswith(PREFIXES):
        return None
    from app.api.club_mailbox import store as mailbox_store

    try:
        response = mailbox_store._client().get_object(Bucket=settings.S3_BUCKET, Key=key)
    except Exception as exc:  # noqa: BLE001
        logger.info("desk file %s could not be read (%s)", key.rsplit("/", 1)[0], type(exc).__name__)
        return None
    return response["Body"].read()


def delete(keys: Iterable[str]) -> int:
    """Never raises: a file left behind is unreferenced, in a private folder."""
    keys = [k for k in keys if k and k.startswith(PREFIXES)]
    if not keys:
        return 0
    from app.api.club_mailbox import store as mailbox_store

    done = 0
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        try:
            mailbox_store._client().delete_objects(Bucket=settings.S3_BUCKET, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
            done += len(chunk)
        except Exception as exc:  # noqa: BLE001
            logger.warning("desk: %d file(s) could not be deleted (%s)", len(chunk), type(exc).__name__)
    return done
