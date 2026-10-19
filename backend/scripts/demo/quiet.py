"""Quiet mode: nothing leaves the process while the demo is being seeded.

The seed goes through the app's own service functions, and some of those
tell people things: a new event pushes to its club's followers and to
everyone whose interests match, a check-in pushes "you're in", a store
order that oversells mails the owner and alerts the admins. For invented
clubs none of that may reach anyone. Inside `quiet()`:

- **Mail.** `transport.deliver`, the one function every mail passes through
  (account mail, alerts, tickets, journeys, surveys), only records the mail.
- **Push.** Firebase is never initialised, and the three FCM senders
  (one token, many tokens, a topic) only record what they were given.
- **Jobs.** `jobs.enqueue`, `enqueue_many` and `add` record the job and
  write no row. A row would be claimed within a second by the API's or the
  worker's runner, in a process where none of this is switched off, so the
  only safe job is one that is never written.
- **Admin alerts** (`admin_alerts.notify`, the bell row and its mail and
  Discord post) and **Discord error reports** only record.
- **After-commit calls** other than the public site's revalidation (a cache
  purge, not a message) only record.
- **The outside world:** no geocoder, no map-link following, no IP lookup,
  no Cloudflare, and Razorpay refuses outright.
- **Interest pushes** for a new event are capped at zero, so the job is
  not even asked for.

Everything is put back as it was on the way out, whatever happened inside.
`Quiet.sent` lists every mail, push, alert and post that was stopped;
`Quiet.dropped_jobs` the jobs that were not written. The seed prints both,
and the tests assert that `sent` is empty.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class QuietViolation(RuntimeError):
    """Something tried to reach the outside world in quiet mode."""


@dataclass
class Quiet:
    mails: list[dict] = field(default_factory=list)
    pushes: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    posts: list[dict] = field(default_factory=list)
    dropped_jobs: list[str] = field(default_factory=list)
    dropped_calls: list[str] = field(default_factory=list)

    @property
    def sent(self) -> list[dict]:
        """Everything that would have reached a person: mail, pushes, alerts, posts."""
        return [*self.mails, *self.pushes, *self.alerts, *self.posts]

    def summary(self) -> dict:
        jobs: dict[str, int] = {}
        for name in self.dropped_jobs:
            jobs[name] = jobs.get(name, 0) + 1
        return {
            "mails_stopped": len(self.mails),
            "pushes_stopped": len(self.pushes),
            "alerts_stopped": len(self.alerts),
            "posts_stopped": len(self.posts),
            "jobs_not_written": jobs,
            "after_commit_calls_dropped": len(self.dropped_calls),
        }


class _Patches:
    """setattr with a memory: everything set is put back by `restore`."""

    def __init__(self) -> None:
        self._saved: list[tuple[Any, str, Any]] = []

    def set(self, target: Any, name: str, value: Any) -> None:
        self._saved.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def restore(self) -> None:
        while self._saved:
            target, name, value = self._saved.pop()
            setattr(target, name, value)


@contextlib.contextmanager
def quiet():
    """Switch every outbound path off for the block; answers the `Quiet` record."""
    import razorpay.client

    from app.api.admin_alerts import service as admin_alerts
    from app.api.discover import geo
    from app.config import settings
    from app.core import after_commit, jobs, site_revalidate
    from app.core.email import transport
    from app.core.notifications import service as push
    from app.core.utils import discord

    record = Quiet()
    p = _Patches()

    def deliver(mail) -> dict:
        record.mails.append({"to": list(mail.to), "bcc": list(mail.bcc), "subject": mail.subject})
        return {"MessageId": None, "Provider": "quiet"}

    async def send_to_token(token, title, body, data=None, dead=None) -> bool:
        record.pushes.append({"tokens": 1, "title": title})
        return False

    async def send_to_tokens(tokens, title, body, data=None, image_url=None, dead=None) -> int:
        record.pushes.append({"tokens": len(tokens or []), "title": title})
        return 0

    async def send_to_topic(*, topic=None, condition=None, title, body, data=None, image_url=None):
        record.pushes.append({"topic": topic or condition, "title": title})
        return None

    async def enqueue(session, name, payload, *, run_at=None, dedupe_key=None, priority=None) -> None:
        record.dropped_jobs.append(name)

    async def enqueue_many(session, name, items, *, priority=None, chunk=1000) -> int:
        record.dropped_jobs.extend(name for _ in list(items))
        return 0

    def add(session, name, payload, *, priority=None):
        # The row is made but never added to the session: nothing is written.
        record.dropped_jobs.append(name)
        return jobs.Jobs(name=name, payload=jobs.as_json(payload))

    async def notify(session, kind, *, title, **kwargs):
        record.alerts.append({"kind": kind, "title": title})
        return None

    async def send_discord(error_details, max_retries=2, retry_delay=1.0):
        record.posts.append({"discord": str(error_details)[:200]})

    real_dispatch: Callable = after_commit._dispatch

    def dispatch(calls) -> None:
        keep = []
        for func, args, kwargs in calls:
            if getattr(func, "__module__", "") == site_revalidate.__name__:
                keep.append((func, args, kwargs))
            else:
                record.dropped_calls.append(getattr(func, "__qualname__", repr(func)))
        if keep:
            real_dispatch(keep)

    async def no_link(url):
        return None

    def razorpay_refuses(self, method, path, **options):
        raise QuietViolation(f"quiet mode: Razorpay is off ({method.upper()} {path})")

    try:
        p.set(transport, "deliver", deliver)
        p.set(push, "initialize_firebase", lambda: None)
        p.set(push, "_send_to_token", send_to_token)
        p.set(push, "_send_to_tokens", send_to_tokens)
        p.set(push, "send_to_topic", send_to_topic)
        p.set(jobs, "enqueue", enqueue)
        p.set(jobs, "enqueue_many", enqueue_many)
        p.set(jobs, "add", add)
        p.set(admin_alerts, "notify", notify)
        p.set(discord, "send_discord_notification", send_discord)
        p.set(after_commit, "_dispatch", dispatch)
        p.set(geo, "resolve_link", no_link)
        p.set(razorpay.client.Client, "request", razorpay_refuses)
        p.set(settings, "GEOCODER_ENABLED", False)
        p.set(settings, "GEOIP_URL", "")
        p.set(settings, "CLOUDFLARE_API_TOKEN", "")
        p.set(settings, "DISCORD_ERROR_WEBHOOK", None)
        p.set(settings, "ADMIN_ALERT_DISCORD_WEBHOOK", None)
        p.set(settings, "ADMIN_ALERT_EMAILS", "")
        p.set(settings, "INTEREST_PUSH_MAX_PER_EVENT", 0)
        p.set(settings, "LIVE_NOTIFY", False)
        yield record
    finally:
        p.restore()
        if record.sent:
            logger.warning("quiet mode stopped %d outbound message(s)", len(record.sent))
