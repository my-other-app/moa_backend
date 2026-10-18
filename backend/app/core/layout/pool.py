"""Where documents are drawn: a few threads of their own, off the event loop.

Production is one process with one event loop. A ticket drawn on that loop
held every other request in the process -- a check-in scan, a payment
webhook, a page of the feed -- behind it, and a certificate bundle or a
batch of ID cards held them for minutes. Every render now goes through
`run`, which draws on RENDER_THREADS threads kept for this alone.

Not the loop's default pool: the Razorpay SDK, SES and the bucket calls wait
in that one, and a burst of renders queued there would starve a payment
behind a hundred certificates. Here a burst of renders queues behind itself.

The work handed to `run` must not touch the database: whatever a render
needs is read on the loop first (the registration, its event, the design),
and only the drawing comes here.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, TypeVar

from app.config import settings

T = TypeVar("T")

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()

THREAD_PREFIX = "render"


def size() -> int:
    """RENDER_THREADS, kept within 1..16 whatever the environment says."""
    return max(1, min(16, int(settings.RENDER_THREADS or 1)))


def executor() -> ThreadPoolExecutor:
    """The render pool, made the first time it is needed."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=size(), thread_name_prefix=THREAD_PREFIX)
        return _executor


async def run(fn: Callable[..., T], /, *args, **kwargs) -> T:
    """`fn(*args, **kwargs)` on a render thread; the caller's loop is free meanwhile."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await loop.run_in_executor(executor(), call)


def on_render_thread() -> bool:
    return threading.current_thread().name.startswith(THREAD_PREFIX)


def shutdown() -> None:
    """Let the pool go (tests, and a process on its way out)."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None


class _Guard:
    """A process-wide lock that knows whether this thread holds it.

    pypdfium2 is not thread-safe: PDFium keeps global state, and two threads
    inside it at once can crash the process. Every call into pdfium -- a PNG
    from a PDF, the text of a page -- holds `PDFIUM`. Re-entrant, so a helper
    that takes it may be called by code already holding it.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self) -> "_Guard":
        self._lock.acquire()
        self._local.depth = getattr(self._local, "depth", 0) + 1
        return self

    def __exit__(self, *exc) -> None:
        self._local.depth -= 1
        self._lock.release()

    def held(self) -> bool:
        return getattr(self._local, "depth", 0) > 0


PDFIUM = _Guard()
