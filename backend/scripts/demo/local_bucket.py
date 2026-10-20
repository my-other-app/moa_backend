"""A folder standing in for the S3 bucket, for a rehearsal on a laptop and
for the tests. Never used on the server, where the seed uploads to the
real bucket like any upload from the dashboard.

Inside `local_bucket(root)` every write the app makes -- an image column's
upload, `objects.put_bytes` (store photos, design artwork) -- lands under
`root`, and every read (`objects.fetch`, which the document renderer goes
through) comes from there. Signing a link stays as it is: it is local work.
"""

from __future__ import annotations

import contextlib
from pathlib import Path


def _path(root: Path, key: str) -> Path:
    safe = key.replace("://", "/").lstrip("/")
    return root / safe


@contextlib.contextmanager
def local_bucket(root: Path):
    """Point the app's storage at the folder `root` for the block; answers it."""
    from app.core.storage import objects
    from app.db.base import AbstractSQLModel
    from app.db import registry  # noqa: F401 (every model's columns)

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    saved: list[tuple[object, str, object]] = []

    def patch(target, name, value):
        saved.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def write(key: str, data: bytes) -> None:
        path = _path(root, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def upload_fileobj(fileobj, bucket, key, ExtraArgs=None, **kwargs):
        fileobj.seek(0) if hasattr(fileobj, "seek") else None
        write(key, fileobj.read())

    def put_bytes(key: str, data: bytes, content_type: str) -> None:
        write(key, bytes(data))

    def fetch(key: str, *, quiet_if_missing: bool = False):
        path = _path(root, key) if key else None
        return path.read_bytes() if path is not None and path.is_file() else None

    def delete_object(**kwargs):
        path = _path(root, kwargs.get("Key", ""))
        if path.is_file():
            path.unlink()
        return {}

    clients = {}
    for table in AbstractSQLModel.metadata.tables.values():
        for column in table.columns:
            client = getattr(column.type, "s3_client", None)
            if client is not None:
                clients[id(client)] = client
    try:
        for client in clients.values():
            patch(client, "upload_fileobj", upload_fileobj)
            patch(client, "delete_object", delete_object)
            patch(client, "head_object", lambda **kw: {})
        patch(objects, "put_bytes", put_bytes)
        patch(objects, "fetch", fetch)
        objects.cache_clear()
        yield root
    finally:
        while saved:
            target, name, value = saved.pop()
            setattr(target, name, value)
        objects.cache_clear()


def read(root: Path, key: str) -> bytes | None:
    path = _path(Path(root), key)
    return path.read_bytes() if path.is_file() else None


__all__ = ["local_bucket", "read"]
