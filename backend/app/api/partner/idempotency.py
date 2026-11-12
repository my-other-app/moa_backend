"""Idempotency-Key on every POST and DELETE.

The first answer to a key is kept for 24 hours, per client: the same key
again with the same request (method, path, query and body) gets that answer
back, marked `Idempotent-Replayed: true`, and nothing is done twice; the same
key with a different request is 422 IDEMPOTENCY_MISMATCH. While the first is
still being answered, a second answers 409 IDEMPOTENCY_IN_PROGRESS. A 5xx is
not kept, so the request may be tried again with its key.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from datetime import timedelta
from typing import Optional

from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.partner.common import aware, now
from app.api.partner.errors import PartnerError
from app.api.partner.models import PartnerIdempotency

logger = logging.getLogger(__name__)

TTL = timedelta(hours=24)
#: A first request this old with no answer kept is taken to have died.
STALE = timedelta(minutes=2)
MAX_KEY = 255
METHODS = frozenset({"POST", "DELETE"})
KEPT_HEADERS = ("etag", "location")
#: How often a request also clears its client's expired keys.
SWEEP_CHANCE = 0.02


class Replay(Exception):
    """The first answer to this key: answered again, as it was."""

    def __init__(self, response: Response):
        super().__init__("idempotent replay")
        self.response = response


async def fingerprint(request) -> str:
    body = await request.body()
    raw = b"\n".join([request.method.encode(), request.url.path.encode(), str(request.url.query).encode(), body or b""])
    return hashlib.sha256(raw).hexdigest()


async def begin(session, request, client_id: str) -> None:
    """Before the handler: replay, refuse, or hold the key for this request."""
    if request.method not in METHODS:
        return
    key = request.headers.get("idempotency-key")
    if key is None:
        return
    key = key.strip()
    if not key or len(key) > MAX_KEY:
        raise PartnerError("VALIDATION", f"Idempotency-Key is 1 to {MAX_KEY} characters.", details={"fields": {"Idempotency-Key": "1 to 255 characters."}})
    digest = await fingerprint(request)
    table = PartnerIdempotency.__table__
    row = await session.scalar(select(PartnerIdempotency).where(PartnerIdempotency.client_id == client_id, PartnerIdempotency.key == key))
    if row is not None and (aware(row.expires_at) <= now() or (row.status_code is None and aware(row.created_at) <= now() - STALE)):
        await session.execute(delete(PartnerIdempotency).where(PartnerIdempotency.id == row.id))
        await session.commit()
        row = None
    if row is not None:
        if row.request_hash != digest:
            raise PartnerError(
                "IDEMPOTENCY_MISMATCH", "This Idempotency-Key was used for a different request. Use a new key for a new request.",
            )
        if row.status_code is None:
            raise PartnerError("IDEMPOTENCY_IN_PROGRESS", "A request with this Idempotency-Key is still being answered. Try again in a moment.")
        headers = {k: v for k, v in (row.response_headers or {}).items()}
        headers["Idempotent-Replayed"] = "true"
        raise Replay(Response(content=row.response_body or "", status_code=row.status_code, headers=headers, media_type="application/json"))
    stamp = now()
    if random.random() < SWEEP_CHANCE:
        # Now and then, this client's keys older than a day go.
        await sweep(session, client_id=client_id)
    inserted = (await session.execute(
        pg_insert(table).values(
            client_id=client_id, key=key, request_hash=digest, method=request.method, path=request.url.path[:500],
            created_at=stamp, expires_at=stamp + TTL,
        ).on_conflict_do_nothing(index_elements=[table.c.client_id, table.c.key]).returning(table.c.id)
    )).scalar_one_or_none()
    await session.commit()
    if inserted is None:
        raise PartnerError("IDEMPOTENCY_IN_PROGRESS", "A request with this Idempotency-Key is still being answered. Try again in a moment.")
    request.state.partner_idempotency_id = inserted


async def finish(session, request, response: Response) -> None:
    """After the handler: keep the answer (not a 5xx, which may be tried again)."""
    row_id = getattr(request.state, "partner_idempotency_id", None)
    if row_id is None:
        return
    try:
        if response.status_code >= 400 and session.in_transaction():
            # Whatever a refused request had begun is not kept with its answer.
            await session.rollback()
        row = await session.get(PartnerIdempotency, row_id)
        if row is None:
            return
        if response.status_code >= 500:
            await session.delete(row)
        else:
            body = getattr(response, "body", b"") or b""
            row.status_code = response.status_code
            row.response_body = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else json.dumps(body)
            row.response_headers = {k: v for k, v in response.headers.items() if k.lower() in KEPT_HEADERS}
        await session.commit()
    except Exception:  # noqa: BLE001 - the answer itself must still go
        logger.warning("could not keep the answer for an Idempotency-Key", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass


async def sweep(session, *, client_id: Optional[str] = None) -> int:
    """Forget the keys older than a day."""
    q = delete(PartnerIdempotency).where(PartnerIdempotency.expires_at < now())
    if client_id is not None:
        q = q.where(PartnerIdempotency.client_id == client_id)
    done = (await session.execute(q)).rowcount or 0
    await session.commit()
    return done
