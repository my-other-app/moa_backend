"""One delivered mail, shared by every inbox it may be for.

Both mailbox hooks (router.py) ask three inboxes in turn: the platform's own
(app/api/platform_mail), the team's desk (app/api/support) and the clubs'.
They are handed one `Incoming`, so the mail is:

- read at most once (from the inbound bucket, or the Worker's body);
- bounded before it is parsed: its MIME part lines are counted in the raw
  bytes, and a mail with more than MAILBOX_INBOUND_MAX_PARTS parts is
  treated as unparseable without being parsed;
- parsed at most once (parse.py), in a worker thread, never on the event
  loop: each inbox gets the same `Parsed`.

Each inbox checks whether it already kept this delivery (by the provider's
id, which for Cloudflare comes from the Message-ID header alone) before it
asks for the parse, so a delivery that comes again costs a query, not a
parse.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional, Union

from app.api.club_mailbox import parse, providers
from app.config import settings

Reader = Callable[[providers.Received], bytes]


class Unparseable(Exception):
    """The mail can't be read as MIME, or has more parts than we parse."""


def part_lines(raw: bytes, stop: Optional[int] = None) -> int:
    """How many lines of `raw` begin with `--`: every MIME boundary line
    does, so this is an upper bound on the number of parts, counted without
    parsing. A line may end in CR, LF or both, as the parser reads them.
    Counting ends past `stop`."""
    count = 1 if raw.startswith(b"--") else 0
    for sep in (b"\n--", b"\r--"):
        count += raw.count(sep)
        if stop is not None and count > stop:
            break
    return count


def max_parts() -> int:
    return max(1, int(settings.MAILBOX_INBOUND_MAX_PARTS or 300))


def _parse_bounded(raw: bytes) -> parse.Parsed:
    limit = max_parts()
    if part_lines(raw, stop=limit) > limit:
        raise Unparseable("too many parts")
    try:
        return parse.parse(raw)
    except Exception as exc:  # noqa: BLE001 - a mail that can't be read at all
        raise Unparseable(type(exc).__name__) from exc


class Incoming:
    """A delivery's raw mail and its parse, each made once, off the loop."""

    def __init__(self, received: providers.Received, reader: Optional[Reader] = None):
        self.received = received
        self._reader = reader or providers.read
        self._raw: Optional[bytes] = None
        self._too_large: Optional[str] = None
        self._parsed: Optional[parse.Parsed] = None
        self._unparseable: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._raw) if self._raw is not None else 0

    async def raw(self) -> bytes:
        """The raw MIME. providers.TooLarge past MAILBOX_INBOUND_MAX_MB;
        providers.Unreadable when it can't be read now (not remembered: the
        hook answers so the mail comes again)."""
        async with self._lock:
            if self._raw is not None:
                return self._raw
            if self._too_large is not None:
                raise providers.TooLarge(self._too_large)
            try:
                self._raw = await asyncio.to_thread(self._reader, self.received)
            except providers.TooLarge as exc:
                self._too_large = str(exc)
                raise
            return self._raw

    async def parsed(self) -> parse.Parsed:
        """The parsed mail. providers.TooLarge, providers.Unreadable (as
        raw()), or Unparseable."""
        raw = await self.raw()
        async with self._lock:
            if self._parsed is not None:
                return self._parsed
            if self._unparseable is not None:
                raise Unparseable(self._unparseable)
            try:
                self._parsed = await asyncio.to_thread(_parse_bounded, raw)
            except Unparseable as exc:
                self._unparseable = str(exc)
                raise
            return self._parsed


def shared(received: providers.Received, source: Union[Incoming, Reader, None]) -> Incoming:
    """The delivery's Incoming: the one the hook made, or one around a reader."""
    if isinstance(source, Incoming):
        return source
    return Incoming(received, source)
