"""Telling the admins: a row for the bell, a mail, and a Discord post.

`notify` is the one call. It writes the row in the caller's transaction (so an
alert about a change that is rolled back never appears) and, once that
commits, mails ADMIN_ALERT_EMAILS and posts to ADMIN_ALERT_DISCORD_WEBHOOK in
the background. It never raises: an alert that cannot be written must not
fail the webhook or the request it describes.

Identical alerts -- the same kind about the same subscription (or, with none,
the same club) -- within ten minutes are one alert. Razorpay sends
`payment.failed` and `subscription.pending` for one failed charge, seconds
apart, and the admin needs to hear about it once. An alert about something
else names what it is about with `dedupe_on` (a reported comment: its type
and id), and the same kind about the same thing is one alert in the window.
"""
from __future__ import annotations

import logging
import threading
import time
import zlib
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import joinedload

from app.api.admin_alerts.models import AdminNotifications
from app.config import settings
from app.core.after_commit import run_after_commit
from app.core.email import plain

logger = logging.getLogger(__name__)

#: Every kind, and what it means. The admin app may group or colour by kind;
#: a new one is added here first.
KINDS: dict[str, str] = {
    "club_applied": "A club applied for an account",
    "subscription_trial_started": "A club's trial began",
    "subscription_activated": "A club's subscription was paid for the first time",
    "payment_received": "A renewal was paid",
    "payment_failed": "A charge failed",
    "subscription_past_due": "A subscription fell past due",
    "grace_started": "Razorpay stopped retrying; the club's grace began",
    "grace_ending": "A club's grace ends within two days",
    "subscription_downgraded": "A grace ran out and the club moved to Free",
    "subscription_paused": "A subscription was paused",
    "subscription_resumed": "A subscription was resumed",
    "subscription_cancelled": "A subscription was cancelled or completed",
    "plan_assigned": "An admin changed a club's plan (data.complimentary: given for nothing)",
    "comp_ending": "A club's complimentary plan ends within seven days",
    "comp_ended": "A club's complimentary plan ended; it is on Free or its earlier plan",
    "payment_gateway_connected": "A club connected its own Razorpay account",
    "payment_gateway_failed": "A club's own Razorpay account stopped working, or a webhook for it had a bad signature",
    "payment_gateway_disconnected": "A club's own Razorpay account was disconnected or disabled",
    "club_domain_active": "A club's own domain was verified and its website is live there",
    "content_reported": "Someone reported a thread, a comment, a user or a club (data: target_type, target_id, reason, report_count)",
}

DEDUPE_WINDOW = timedelta(minutes=10)

#: First key of the advisory lock two alerts of one kind about one thing take.
_DEDUPE_LOCK = 7_420_311


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(value) -> int | None:
    if value is None or isinstance(value, int):
        return value
    return getattr(value, "id", None)


def _lock_key(kind: str, subscription_id: int | None, club_id: int | None, dedupe_on: dict | None = None) -> int:
    about = ":".join(f"{k}={dedupe_on[k]}" for k in sorted(dedupe_on)) if dedupe_on else f"{subscription_id}:{club_id}"
    raw = zlib.crc32(f"{kind}:{about}".encode())
    return raw - 2**32 if raw >= 2**31 else raw


async def notify(
    session,
    kind: str,
    *,
    title: str,
    body: str = "",
    club=None,
    subscription=None,
    data: dict | None = None,
    dedupe: bool = True,
    dedupe_on: dict | None = None,
) -> AdminNotifications | None:
    """
    Record an alert and, after commit, mail and post it. Returns the row, or
    None when it was a duplicate or could not be written.

    `club` and `subscription` are rows or ids. `dedupe=False` is for an
    admin's own action: two deliberate changes ten minutes apart are two
    things that happened, not one said twice. `dedupe_on` names what the
    alert is about by keys of `data` (`{"target_type": "comment",
    "target_id": "…"}`): the same kind with the same values in its data is
    the duplicate, whatever club it names.
    """
    try:
        subscription_id = _id(subscription)
        club_id = _id(club) or getattr(subscription, "club_id", None)
        club_name = getattr(club, "name", None)
        now = _now()
        # Every statement inside a SAVEPOINT: one that fails rolls back only
        # the alert, and the caller's transaction carries on (audit.record).
        async with session.begin_nested():
            if club_name is None and club_id is not None:
                from app.api.clubs.models import Clubs

                club_name = await session.scalar(select(Clubs.name).where(Clubs.id == club_id))
            if dedupe:
                # Two webhooks about one failure arrive in the same second.
                # The one that cannot take the lock is the duplicate: the
                # other is writing the same alert right now.
                got = await session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:ns, :key)"),
                    {"ns": _DEDUPE_LOCK, "key": _lock_key(kind, subscription_id, club_id, dedupe_on)},
                )
                if not got:
                    return None
                same = [AdminNotifications.kind == kind, AdminNotifications.created_at >= now - DEDUPE_WINDOW]
                if dedupe_on:
                    same += [AdminNotifications.data[k].as_string() == str(v) for k, v in dedupe_on.items()]
                elif subscription_id is not None:
                    same.append(AdminNotifications.subscription_id == subscription_id)
                elif club_id is not None:
                    same += [AdminNotifications.subscription_id.is_(None), AdminNotifications.club_id == club_id]
                else:
                    same += [AdminNotifications.subscription_id.is_(None), AdminNotifications.club_id.is_(None),
                             AdminNotifications.title == title[:200]]
                if await session.scalar(select(AdminNotifications.id).where(*same).limit(1)):
                    return None
            row = AdminNotifications(
                kind=kind,
                title=title[:200],
                body=body or "",
                club_id=club_id,
                subscription_id=subscription_id,
                data=data or {},
                created_at=now,
            )
            session.add(row)
            await session.flush()
        run_after_commit(session, deliver, {
            "id": row.id, "kind": kind, "title": row.title, "body": row.body,
            "club_id": club_id, "club_name": club_name, "subscription_id": subscription_id,
            "data": data or {}, "created_at": now.isoformat(),
        })
        return row
    except Exception:  # noqa: BLE001
        logger.warning("admin alert %s could not be recorded", kind, exc_info=True)
        return None


# ─── Delivery, after commit ─────────────────────────────────────────────


class Throttle:
    """At most `limit` sends in any `per` seconds, in this process. Past it,
    the send is skipped and logged -- the row is still in the bell."""

    def __init__(self, limit: int, per: float):
        self.limit, self.per = limit, per
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._sent and self._sent[0] <= now - self.per:
                self._sent.popleft()
            if len(self._sent) >= self.limit:
                return False
            self._sent.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._sent.clear()


#: A run of failures (a Razorpay outage, a bad deploy) must not bury the inbox.
EMAIL_THROTTLE = Throttle(limit=20, per=600)
#: Discord allows 30 posts a minute per webhook; stay under it.
DISCORD_THROTTLE = Throttle(limit=20, per=60)


def reset_for_tests() -> None:
    EMAIL_THROTTLE.reset()
    DISCORD_THROTTLE.reset()


def recipients() -> list[str]:
    """ADMIN_ALERT_EMAILS as a list: trimmed, lower case, no blanks or repeats."""
    out: list[str] = []
    for part in (settings.ADMIN_ALERT_EMAILS or "").replace(";", ",").split(","):
        address = part.strip().lower()
        if address and "@" in address and address not in out:
            out.append(address)
    return out


def _lines(alert: dict) -> list[str]:
    lines = [alert["body"]] if alert.get("body") else []
    if alert.get("club_id"):
        name = alert.get("club_name")
        lines.append(f"Club: {name} (#{alert['club_id']})" if name else f"Club: #{alert['club_id']}")
    if alert.get("subscription_id"):
        lines.append(f"Subscription: #{alert['subscription_id']}")
    for key, value in (alert.get("data") or {}).items():
        if value not in (None, "", [], {}):
            lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    return lines


def send_email(alert: dict) -> bool:
    """One mail to every address in ADMIN_ALERT_EMAILS. Never raises."""
    to = recipients()
    if not to:
        return False
    if not EMAIL_THROTTLE.allow():
        logger.warning("admin alert email throttled: %s", alert.get("title"))
        return False
    from app.core.email import email as email_mod

    subject = f"[{plain.brand()} admin] {alert['title']}"
    paragraphs = [alert["title"], *_lines(alert)]
    footer = f"Alert #{alert.get('id')} ({alert.get('kind')}). It is also in the admin dashboard's notifications."
    try:
        email_mod.send_email(
            recipients=to, subject=subject,
            text=plain.text(paragraphs, footer=footer), html=plain.render(paragraphs, footer=footer),
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning("admin alert email could not be sent", exc_info=True)
        return False


def post_discord(alert: dict, max_retries: int = 2, retry_delay: float = 1.0) -> bool:
    """The alert in the Discord channel, retried like the error webhook. Never raises."""
    url = settings.ADMIN_ALERT_DISCORD_WEBHOOK
    if not url:
        return False
    if not DISCORD_THROTTLE.allow():
        logger.warning("admin alert Discord post throttled: %s", alert.get("title"))
        return False
    content = "\n".join([f"**{alert['title']}**", *_lines(alert)])[:1900]
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=10.0) as client:
                client.post(url, json=payload).raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("admin alert Discord post attempt %d failed: %s", attempt + 1, e)
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (2**attempt))
    return False


def deliver(alert: dict) -> None:
    send_email(alert)
    post_discord(alert)


# ─── The bell ───────────────────────────────────────────────────────────


def _filters(*, unread: bool | None, kind: str | None) -> list:
    criteria = []
    if unread:
        criteria.append(AdminNotifications.read_at.is_(None))
    if kind:
        criteria.append(AdminNotifications.kind == kind)
    return criteria


async def unread_count(session) -> int:
    return int(
        await session.scalar(select(func.count(AdminNotifications.id)).where(AdminNotifications.read_at.is_(None)))
        or 0
    )


async def list_alerts(session, *, unread: bool | None, kind: str | None, offset: int, limit: int) -> dict:
    criteria = _filters(unread=unread, kind=kind)
    total = await session.scalar(select(func.count(AdminNotifications.id)).where(*criteria))
    rows = (
        await session.execute(
            select(AdminNotifications)
            .where(*criteria)
            .options(joinedload(AdminNotifications.club))
            .order_by(AdminNotifications.created_at.desc(), AdminNotifications.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return {
        "items": [to_dict(row) for row in rows],
        "total": int(total or 0),
        "unread": await unread_count(session),
    }


def to_dict(row: AdminNotifications) -> dict:
    club = row.club
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body or "",
        "club": {"id": club.id, "name": club.name, "slug": club.slug} if club is not None else None,
        "subscription_id": row.subscription_id,
        "data": row.data or {},
        "created_at": row.created_at,
        "read_at": row.read_at,
    }


async def get_alert(session, alert_id: int) -> AdminNotifications | None:
    return await session.scalar(
        select(AdminNotifications)
        .where(AdminNotifications.id == alert_id)
        .options(joinedload(AdminNotifications.club))
    )


async def mark_read(session, row: AdminNotifications) -> None:
    """Read once; reading again keeps the first time."""
    if row.read_at is None:
        row.read_at = _now()


async def mark_all_read(session, *, kind: str | None = None) -> int:
    criteria = [AdminNotifications.read_at.is_(None)]
    if kind:
        criteria.append(AdminNotifications.kind == kind)
    result = await session.execute(
        update(AdminNotifications).where(*criteria).values(read_at=_now()).execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
