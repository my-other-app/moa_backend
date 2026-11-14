"""The thread pools each process waits in for blocking calls.

Python's default pool, the one `asyncio.to_thread` and
`run_in_executor(None, ...)` use, is sized cpu_count + 4 by the interpreter:
8 threads on a 4-vCPU box, shared by every blocking call the process makes
off the event loop -- bcrypt, uploads to the bucket, after-commit mail, site
revalidation, FCM sends and Razorpay. Eight Razorpay calls hanging toward
their timeout left no thread for a sign-in or an upload on that worker.

So each process (doth.lifespan for the API's workers, app.worker.main for
the job worker) installs a default pool of settings.DEFAULT_THREADS, and
Razorpay gets a pool of its own (settings.RAZORPAY_THREADS): a slow gateway
can only hold up payments. Mail (app/core/email/pace.py) and renders
(app/core/layout/pool.py) already have theirs.

Threads are made as work arrives, never ahead, so an idle process holds none.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.config import settings

_razorpay: Optional[ThreadPoolExecutor] = None
_razorpay_lock = threading.Lock()


def default_pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=max(4, int(settings.DEFAULT_THREADS)), thread_name_prefix="default")


def install_default_executor(loop: Optional[asyncio.AbstractEventLoop] = None) -> ThreadPoolExecutor:
    """Give `loop` (the running one by default) a default pool of
    DEFAULT_THREADS. asyncio.run and uvicorn shut it down with the loop."""
    loop = loop or asyncio.get_running_loop()
    pool = default_pool()
    loop.set_default_executor(pool)
    return pool


def razorpay_pool() -> ThreadPoolExecutor:
    """The process's Razorpay threads (app/api/payments/rzp.py), made on first use."""
    global _razorpay
    if _razorpay is None:
        with _razorpay_lock:
            if _razorpay is None:
                _razorpay = ThreadPoolExecutor(max_workers=max(1, int(settings.RAZORPAY_THREADS)), thread_name_prefix="razorpay")
    return _razorpay
