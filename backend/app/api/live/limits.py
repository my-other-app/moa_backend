"""The live room's rate limits: per person in the room, with a ceiling per address.

A hall shares one address. A venue's Wi-Fi, a college network or a mobile
carrier's NAT puts hundreds of phones behind it, so a limit keyed on the
address alone treated the whole room as one person: the join limit of 30 a
minute let about 30 people a minute into a quiz, whoever they were.

So each action is limited twice:

- per person, on the key the room's own clients send: the browser's key
  (X-Live-Key) for asking, voting and answering polls, the player's key
  (X-Live-Player) for the quiz, the client's `player_key` for joining -- or
  the account (`u:<user id>`) when the request is signed in. A request
  without one is limited by the ceiling alone (clients from before).
- per address, at a ceiling sized for a full hall, so one machine flooding
  the room is still stopped.

The keys are made by the clients, so a determined script can mint a new one
per request; the ceiling is what bounds it, and it is per address. That is
the trade-off: the per-person limit keeps one phone (or a stuck button) from
spamming, and the ceiling keeps one address from taking the room down.

The counts live in the same store as every other limit (app.core.rate_limit,
in this process) and switch off with it.
"""
from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple, Optional

from limits import parse

from app.core.net import client_address
from app.core.rate_limit import limiter
from app.response import CustomHTTPException


class Rule(NamedTuple):
    #: For one browser or player; None: the ceiling only.
    per_person: Optional[str]
    #: For one address: a full hall's worth.
    per_address: str


RULES = {
    # One phone joins once; a stuck button a few times. A hall of 3,000 joins in about a minute.
    "join": Rule("10/minute", "3000/minute"),
    # One answer a question; at most a few questions a minute.
    "answer": Rule("30/minute", "20000/minute"),
    "ask": Rule("20/hour", "2000/hour"),
    "vote": Rule("120/minute", "30000/minute"),
    "respond": Rule("60/minute", "20000/minute"),
    # Opening a stream (stream.py): a phone reconnects every ten minutes, or
    # after a drop; a whole hall reconnects at once after a deploy.
    "stream": Rule("20/minute", "6000/minute"),
}


@lru_cache(maxsize=None)
def _item(rate: str):
    return parse(rate)


def _hit(rate: str, scope: str, who: str) -> None:
    item = _item(rate)
    if limiter.limiter.hit(item, scope, who):
        return
    try:
        reset_at, _ = limiter.limiter.get_window_stats(item, scope, who)
        import time

        wait = max(1, int(reset_at - time.time()) + 1)
    except Exception:  # noqa: BLE001 — the answer is the refusal; the wait is a courtesy
        wait = 60
    raise CustomHTTPException(
        429, message="Too many tries in a short time. Wait a moment and try again.",
        error_code="RATE_LIMITED", errors={"retry_after": wait},
    )


def check(request, action: str, person: Optional[str]) -> None:
    """Count this request against `action`'s limits: per `person` (a room
    key, when the client sent one) and per address. 429 when either is spent."""
    if not limiter.enabled:
        return
    rule = RULES[action]
    if person and rule.per_person:
        _hit(rule.per_person, f"live:{action}:person", person)
    _hit(rule.per_address, f"live:{action}:address", client_address(request) or "unknown")
