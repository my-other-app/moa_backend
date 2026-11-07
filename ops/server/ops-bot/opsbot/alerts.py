"""De-duplication: one alert when something goes wrong, one when it recovers.

Conditions -- states that are either fine or not (a container unhealthy, a
disk over 80%): an alert on the change to bad, one on the change back, and
nothing while it stays the same.

Bursts -- events that come in numbers (error lines, restarts, OOM kills):
at most one alert per key per `every` seconds, carrying the count since the
last one and a sample; then one "quiet again" message once `every` seconds
pass without any.
"""

from __future__ import annotations

from dataclasses import dataclass


class Conditions:
    def __init__(self, state: dict | None = None):
        #: key -> True (bad) / False (fine). Kept in the state file so a
        #: restart of the bot does not alert again about the same thing.
        self.state: dict[str, bool] = state if state is not None else {}

    def update(self, key: str, bad: bool | None, bad_text: str, ok_text: str | None) -> str | None:
        """The message to send for this observation, or None. `bad=None`
        means "cannot tell" and changes nothing."""
        if bad is None:
            return None
        before = self.state.get(key)
        self.state[key] = bad
        if bad and before is not True:
            return bad_text
        if not bad and before is True:
            return ok_text
        return None

    def keys(self, prefix: str) -> list[str]:
        return [k for k in self.state if k.startswith(prefix)]

    def forget(self, key: str) -> None:
        self.state.pop(key, None)


@dataclass
class _Burst:
    active: bool = False
    last_alert: float | None = None
    last_event: float = 0.0
    first_event: float = 0.0
    pending: int = 0
    total: int = 0
    sample: str = ""


class Bursts:
    def __init__(self, every: float = 600.0, threshold: int = 1):
        self.every = every
        self.threshold = threshold
        self.keys: dict[str, _Burst] = {}

    def feed(self, key: str, count: int, sample: str, now: float) -> tuple[int, str] | None:
        """Report `count` new events for `key` at `now`. Returns (events
        since the last alert, a sample) when an alert is due, else None. A key that
        is not yet active needs `threshold` events in one feed to start."""
        burst = self.keys.setdefault(key, _Burst())
        if count <= 0:
            return None
        if not burst.active and count < self.threshold:
            return None
        burst.last_event = now
        burst.pending += count
        burst.total += count
        if sample and not burst.sample:
            burst.sample = sample
        if not burst.active:
            burst.active = True
            burst.first_event = now
        if burst.last_alert is not None and now - burst.last_alert < self.every:
            return None
        burst.last_alert = now
        count, burst.pending = burst.pending, 0
        sample, burst.sample = burst.sample, ""
        return count, sample

    def quiet(self, now: float) -> list[tuple[str, int, float]]:
        """Keys that were active and have had no event for `every` seconds:
        (key, total events in the burst, when it began). Each is reset."""
        done = []
        for key, burst in self.keys.items():
            if burst.active and now - burst.last_event >= self.every:
                done.append((key, burst.total, burst.first_event))
                self.keys[key] = _Burst()
        return done
