"""Calling Razorpay without stopping the server.

The razorpay SDK is synchronous (requests underneath), and it was called
straight from async code: making an order, fetching a payment, refunding,
creating and cancelling subscriptions, checking a club's keys. Each call
held the worker's event loop -- every other request on that worker -- for as
long as Razorpay took to answer, and with no timeout a hung connection held
it for good.

Now every call goes through `call`: it runs in a worker thread, and the
caller stops waiting after RAZORPAY_TIMEOUT seconds (RazorpayTimeout). The
clients made here also give requests that timeout, so a thread whose caller
has given up does not hang on for ever either.

`call` takes the SDK method itself, looked up when the call is made, so a
test that swaps `razorpay_client.order.create` (or the whole client) for a
fake is still the one called.
"""
from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Callable, Optional

import razorpay
import requests

from app.config import settings


class RazorpayTimeout(TimeoutError):
    """Razorpay did not answer within RAZORPAY_TIMEOUT seconds."""


class _TimedSession(requests.Session):
    """A requests session whose requests time out unless told otherwise."""

    def __init__(self, timeout: float):
        super().__init__()
        self._timeout = timeout

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return super().request(method, url, **kwargs)


def new_client(key_id: str, key_secret: str, *, timeout: Optional[float] = None) -> razorpay.Client:
    """A Razorpay client whose every HTTP request times out."""
    return razorpay.Client(session=_TimedSession(timeout or settings.RAZORPAY_TIMEOUT), auth=(key_id, key_secret))


async def call(fn: Callable[..., Any], *args, timeout: Optional[float] = None, **kwargs) -> Any:
    """`fn(*args, **kwargs)` in a worker thread; RazorpayTimeout when it
    has not answered in `timeout` seconds (RAZORPAY_TIMEOUT by default).
    Whatever the SDK raises is raised here unchanged."""
    limit = timeout if timeout is not None else settings.RAZORPAY_TIMEOUT
    try:
        return await asyncio.wait_for(asyncio.to_thread(partial(fn, *args, **kwargs)), limit)
    except asyncio.TimeoutError:
        name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or "call"
        raise RazorpayTimeout(f"Razorpay did not answer {name} within {limit:g} s") from None
