"""Rendered tickets and certificates, kept in the bucket.

A ticket was drawn again for every download, every resend and every bundle
-- the same registration, the same design, the same bytes. Each rendered
PDF is now kept at

    docs/{kind}/{registration_id}/{design_version}-{fingerprint}.pdf

where the fingerprint is a hash of everything the drawing reads: the values
printed, the design (with its image keys, never its expiring links), the
renderer's version and, for the HTML templates, the template file itself.
Change any of them and the key changes, so a cached file is never stale;
nothing is ever invalidated, an old one just stops being asked for (and the
sweep in doc_cache_sweep.py deletes it once it is APP_DOC_CACHE_DAYS old).

A render that could not read one of its images (a logo, the artwork) is
still handed out -- a ticket without its logo beats no ticket -- but it is
not kept, so the next request draws it again with the image.

**One render per document.** Two first downloads of one document at the same
moment both drew it (an email blast's "Download your ticket", a guest
tapping twice, a resend beside a bundle). Now:

- In one process, the callers asking for the same key share one drawing
  (`_inflight`): the first starts it, the rest await it. It runs as a task
  of its own, so a caller who goes away does not cancel it for the others.
- Across processes, that drawing first claims the document: a Postgres
  advisory lock, two ints (RENDER_LOCK, a hash of the key), session-level, on
  a connection of its own in autocommit -- no transaction is held open. The
  holder looks in the bucket again (another process may have just kept it),
  draws, keeps, and lets go. Whoever did not get the claim asks again after
  a short pause, 50 ms doubling to a second; once the claim is free it finds
  the document kept and reads it. After DOC_RENDER_WAIT_SECONDS (20) of
  waiting it draws the document itself rather than fail.
- With APP_REDIS_URL set the claim is a Redis key instead (SET NX with an
  expiry, released only by its holder), and no database connection is held
  while a document is drawn. Redis unset, down or slow: the Postgres lock.
  The two never disagree about correctness, only about who draws: at worst
  a document is drawn twice, the same bytes kept twice under the same key.
- A Postgres claim holds one pooled connection for as long as the holder
  takes to look, draw and keep the document: tens of milliseconds for a
  layered design, up to RENDER_TIMEOUT for a wkhtmltopdf one. At most
  CLAIM_SLOTS documents per process are drawn under a claim at once (the
  rest queue for a slot, as they would for a render thread), so a burst of
  first downloads never holds more than that many connections. Waiting
  holds none.
- A claim that cannot be asked for (the database unreachable) is not a
  reason to fail a download: the document is drawn without one.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import AsyncIterator, Callable, Optional

from sqlalchemy import func, select

from app.config import settings
from app.core.layout import pool
from app.core.storage import objects
from app.core.storage.bucket import prefix
from app.db import lock_keys

logger = logging.getLogger(__name__)

#: Bump when the renderer changes what an existing design prints like.
RENDER_VERSION = "2026-10-18.1"

#: What the code keeps under docs/: these kinds, these extensions (the sweep
#: deletes nothing else).
KINDS = ("ticket", "certificate")
EXTENSIONS = ("pdf",)

#: Two ints, (this, a hash of the document's key): one process draws a document.
RENDER_LOCK = lock_keys.DOC_RENDER
#: The first pause before asking for a claim again, and the longest.
FIRST_PAUSE = 0.05
MAX_PAUSE = 1.0
#: How long a Redis claim lives if its holder dies with it, in ms: past any render.
REDIS_CLAIM_MS = 120_000

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
    """A kind the code keeps is one of KINDS (a test reads every caller)."""
    return f"{prefix('docs', kind, str(registration_id))}{int(design_version or 0)}-{print_}.{EXTENSIONS[0]}"


def _put(key: str, pdf: bytes) -> None:
    try:
        objects.put_bytes(key, pdf, "application/pdf")
    except Exception as exc:  # noqa: BLE001 - a cache that cannot be written is only a cache
        logger.warning("rendered document %s could not be kept: %s", key, exc)


async def lookup(key: str) -> Optional[bytes]:
    if not settings.RENDER_DOC_CACHE:
        return None
    return await asyncio.to_thread(objects.fetch, key, quiet_if_missing=True)


async def _draw_and_keep(key: str, draw: Callable[[], tuple[bytes, bool]]) -> bytes:
    pdf, complete = await pool.run(draw)
    if complete:
        await asyncio.to_thread(_put, key, pdf)
    return pdf


async def cached(key: Optional[str], draw: Callable[[], tuple[bytes, bool]]) -> bytes:
    """The kept PDF at `key`, else `draw()` on the render pool -- once, however
    many ask at the same moment -- kept when complete. No key (a document for
    no saved registration), or the cache off: drawn, not kept."""
    if not key or not settings.RENDER_DOC_CACHE:
        return (await pool.run(draw))[0]
    hit = await lookup(key)
    if hit:
        return hit
    return await _joined(key, draw)


# ─── One drawing per document ───────────────────────────────────────────────

#: This process's drawings in progress, by key. A later caller's `draw` is
#: dropped: the same key is the same document.
_inflight: dict[str, asyncio.Task] = {}


def _forget(key: str, task: asyncio.Task) -> None:
    if _inflight.get(key) is task:
        del _inflight[key]
    if not task.cancelled():
        task.exception()  # retrieved: every caller may have gone


async def _joined(key: str, draw: Callable[[], tuple[bytes, bool]]) -> bytes:
    loop = asyncio.get_running_loop()
    task = _inflight.get(key)
    if task is None or task.get_loop() is not loop:
        task = loop.create_task(_fill(key, draw), name=f"doc-cache {key}")
        _inflight[key] = task
        task.add_done_callback(functools.partial(_forget, key))
    return await asyncio.shield(task)


async def _fill(key: str, draw: Callable[[], tuple[bytes, bool]]) -> bytes:
    """Draw and keep `key` under its claim, or wait for whoever holds it."""
    deadline = time.monotonic() + max(0.0, float(settings.DOC_RENDER_WAIT_SECONDS))
    pause = FIRST_PAUSE
    while True:
        async with _slot(), _claim(key) as mine:
            if mine is not False:
                # Held (or no claim could be asked for). Kept by someone else
                # while this one waited for it?
                if mine:
                    hit = await lookup(key)
                    if hit:
                        return hit
                return await _draw_and_keep(key, draw)
        if time.monotonic() + pause > deadline:
            break
        await asyncio.sleep(pause)
        pause = min(pause * 2, MAX_PAUSE)
    logger.warning("waited %.0f s for another process to draw %s; drawing it here", settings.DOC_RENDER_WAIT_SECONDS, key)
    hit = await lookup(key)
    return hit or await _draw_and_keep(key, draw)


#: Documents one process draws under a claim at once (each Postgres claim is
#: a pooled connection held while it is drawn).
def claim_slots() -> int:
    return 2 * pool.size()


_slots: Optional[tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = None


def _slot() -> asyncio.Semaphore:
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(claim_slots()))
    return _slots[1]


def lock_id(key: str) -> int:
    """The second int of a document's lock: a signed 32-bit hash of its key."""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big", signed=True)


def lock_engine():
    """Where a document's Postgres claim is taken: the app's engine (tests point it at theirs)."""
    from app.db.core import engine

    return engine


@asynccontextmanager
async def _claim(key: str) -> AsyncIterator[Optional[bool]]:
    """True: this process holds `key`'s claim for the block. False: another
    does. None: no claim could be asked for; draw anyway."""
    token = await _redis_claim(key)
    if token is not None:
        try:
            yield bool(token)
        finally:
            if token:
                await _redis_release(key, token)
        return
    async with _pg_claim(key) as got:
        yield got


@asynccontextmanager
async def _pg_claim(key: str) -> AsyncIterator[Optional[bool]]:
    keys = (RENDER_LOCK, lock_id(key))
    try:
        conn = await lock_engine().connect()
    except Exception as exc:  # noqa: BLE001 - never a reason to fail a download
        logger.warning("no claim for %s (%s); drawing it without", key, type(exc).__name__)
        yield None
        return
    held = False
    try:
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            held = bool(await conn.scalar(select(func.pg_try_advisory_lock(*keys))))
            got: Optional[bool] = held
        except Exception as exc:  # noqa: BLE001
            logger.warning("no claim for %s (%s); drawing it without", key, type(exc).__name__)
            got = None
        yield got
    finally:
        if held:
            try:
                await conn.scalar(select(func.pg_advisory_unlock(*keys)))
            except Exception:  # noqa: BLE001 - a lock left on a connection must not go back to the pool
                try:
                    await conn.invalidate()
                except Exception:  # noqa: BLE001
                    pass
        try:
            await conn.close()
        except Exception:  # noqa: BLE001
            pass


_REDIS_UNLOCK = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def _redis_key(key: str) -> str:
    from app.core import redis

    return f"{redis.PREFIX}doc-claim:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"


async def _redis_claim(key: str):
    """A token: claimed. False: another process holds it. None: no Redis to ask."""
    from app.core import redis

    if redis.available() is None:
        return None
    token = secrets.token_hex(8)
    got = await redis.run(lambda c: c.set(_redis_key(key), token, nx=True, px=REDIS_CLAIM_MS), "down", what="doc claim")
    if got == "down":
        return None
    return token if got else False


async def _redis_release(key: str, token: str) -> None:
    from app.core import redis

    await redis.run(lambda c: c.eval(_REDIS_UNLOCK, 1, _redis_key(key), token), None, what="doc claim release")
