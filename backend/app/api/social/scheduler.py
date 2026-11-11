"""When posts go out, and sending them exactly once.

Triggers (a post's `trigger`):
- `on_publish`: the moment the event is published (now, for one that is);
- `at`: a date and time the club chose (India time when it gave none);
- `before_start`, `before_reg_close`: so many minutes before the event
  starts or its registrations close; `after_end`: so many after it ends.
  A relative time in quiet hours (23:00-07:00 IST) moves to 07:00 -- or,
  should that be after what it announces, to 22:55 the evening before. A
  time the club picked itself (`at`) is never moved.

What the event does to them (hooks from events, closeout and club_ops):
- published: `on_publish` posts are due now; the team gets the share kit;
- date, time, venue or fee changed: relative times move with the event,
  and a waiting post whose words mention the old detail as written (not by
  a fill-in word, which is filled at posting) is paused, `event_changed`;
- cancelled, unpublished or deleted: everything waiting is cancelled; a
  cancelled event whose posts went out gets a "Cancellation" draft.

Exactly once. Each target is claimed by one conditional UPDATE (`posting`,
with a claim id) before anything is sent; whoever loses that race does
nothing. The platform's post id is saved the moment the platform answers.
A target whose send may have landed without its answer (a timeout after
sending, or a claim that went stale) is `uncertain` and is looked up on the
platform before it may be sent again; a platform with no way to look
(LinkedIn's member posts) asks the club instead of guessing.

Failures: a dead token holds the account's posts for a reconnect; a limit
(Instagram's 100 a day, X's rate limits) waits by itself; an outage is
retried with growing gaps for six hours; a refused picture or a refusal
stops with what to do. The X month's cap (X_MONTHLY_POST_CAP) stops X
posts with "share manually".

The job `social.post_target` (one per target and due time) runs it; the
minute sweep (sweep.py) re-queues anything due that has no job.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, case, exists, func, or_, select, text, update

from app.api.closeout.models import EventStatus
from app.api.events.models import Events
from app.api.social import accounts as accounts_mod
from app.api.social import alerts, content, crypto, links, platforms, posters
from app.api.social import http as h
from app.api.social.adapters import for_platform
from app.api.social.adapters.base import Picture, Published
from app.api.social.models import (
    CANCELLED, DISCONNECTED, DONE, FAILED, FINISHED, HELD, NEEDS_APPROVAL, NEEDS_RECONNECT, PAUSED, POSTED, POSTING,
    SCHEDULED, SKIPPED, T_CANCELLED, T_SCHEDULED, WAITING, WAITING_LIMIT,
    PosterVariants, SocialAccounts, SocialPosts, SocialPostTargets,
)
from app.config import settings
from app.core import jobs
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

JOB = "social.post_target"
IST = timezone(timedelta(hours=5, minutes=30))
QUIET_START, QUIET_END, LAST_BEFORE_QUIET = time(23, 0), time(7, 0), time(22, 55)
RELATIVE = ("before_start", "before_reg_close", "after_end")
#: A claim older than this was a runner that died mid-send: its target is uncertain.
STALE = timedelta(minutes=10)
#: How long an outage is retried before the club is asked.
RETRY_WINDOW = timedelta(hours=6)
#: How long a limit is waited out.
LIMIT_WINDOW = timedelta(hours=24)
#: Minutes between tries of an outage: 1, 2, 5, 10, 20, 40, then hourly.
GAPS = (1, 2, 5, 10, 20, 40, 60)
#: An uncertain send is looked up this long after it.
LOOKUP_AFTER = timedelta(minutes=2)
#: A limit's shortest wait.
LIMIT_WAIT = timedelta(minutes=15)

T = SocialPostTargets


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def lifecycle(event) -> EventStatus:
    return event.lifecycle or EventStatus.published


def is_published(event) -> bool:
    return event is not None and not event.is_deleted and lifecycle(event) not in (EventStatus.draft, EventStatus.cancelled)


def ends_at(event) -> datetime:
    return _aware(event.event_datetime) + timedelta(hours=float(event.duration or 0))


def quiet(dt: datetime, anchor: Optional[datetime] = None) -> datetime:
    """`dt`, out of quiet hours: to 07:00 IST -- or to 22:55 the evening
    before when 07:00 would be at or after `anchor` (what it announces)."""
    local = dt.astimezone(IST)
    t = local.time()
    if not (t >= QUIET_START or t < QUIET_END):
        return dt
    morning = local.date() + (timedelta(days=1) if t >= QUIET_START else timedelta())
    moved = datetime.combine(morning, QUIET_END, tzinfo=IST)
    if anchor is not None and moved >= anchor:
        evening = local.date() - (timedelta() if t >= QUIET_START else timedelta(days=1))
        moved = datetime.combine(evening, LAST_BEFORE_QUIET, tzinfo=IST)
    return moved.astimezone(timezone.utc)


def due_for(trigger: dict, event, now: datetime) -> Optional[datetime]:
    """When a post with `trigger` goes out; None while it waits for the event to be published."""
    mode = trigger.get("mode")
    minutes = timedelta(minutes=int(trigger.get("minutes") or 0))
    if mode == "on_publish":
        return now if is_published(event) else None
    if mode == "at":
        at = trigger.get("at")
        when = datetime.fromisoformat(at) if isinstance(at, str) else at
        return _aware(when).astimezone(timezone.utc)
    if mode == "before_start":
        start = _aware(event.event_datetime)
        return quiet(start - minutes, anchor=start)
    if mode == "before_reg_close":
        close = _aware(event.reg_enddate)
        if close is None:
            raise CustomHTTPException(422, message="This event has no registration closing time, so nothing can go before it.",
                                      error_code="NO_REG_CLOSE")
        return quiet(close - minutes, anchor=close)
    if mode == "after_end":
        return quiet(ends_at(event) + minutes)
    raise CustomHTTPException(422, message="Pick when the post goes out.", error_code="TRIGGER_INVALID")


def check_due(trigger: dict, event, now: datetime) -> Optional[datetime]:
    due = due_for(trigger, event, now)
    if due is not None and due < now - timedelta(minutes=1):
        local = due.astimezone(IST)
        raise CustomHTTPException(
            422, message=f"That time has passed ({local:%a} {local.day} {local:%b}, {local:%I:%M %p}). Pick a later one.",
            error_code="TRIGGER_IN_PAST", errors={"due": due.isoformat()},
        )
    return due


# ─── Placing posts on the clock ──────────────────────────────────────────


async def enqueue_due(session, t: SocialPostTargets) -> None:
    """A job for the target at its due time (one per target and time)."""
    if t.status not in (T_SCHEDULED, WAITING_LIMIT) or t.scheduled_for is None:
        return
    due = max(_aware(t.scheduled_for), _aware(t.next_attempt_at) or _aware(t.scheduled_for))
    await jobs.enqueue(session, JOB, {"target_id": str(t.id)}, run_at=due, dedupe_key=f"{JOB}:{t.id}:{int(due.timestamp())}")


async def waiting_targets(session, post_id) -> list[SocialPostTargets]:
    return list(await session.scalars(select(T).where(T.post_id == post_id, T.status.in_(WAITING)).order_by(T.created_at)))


async def place(session, post: SocialPosts, event, now: Optional[datetime] = None, *, strict: bool = True) -> Optional[datetime]:
    """Give the post's waiting targets their due time and their jobs.
    `strict`: refuse a time that has passed (422)."""
    now = now or _now()
    due = check_due(post.trigger, event, now) if strict else due_for(post.trigger, event, now)
    for t in await waiting_targets(session, post.id):
        t.scheduled_for, t.next_attempt_at = due, None
    await session.flush()
    if post.status == SCHEDULED:
        for t in await waiting_targets(session, post.id):
            await enqueue_due(session, t)
    return due


async def on_published(session, event) -> None:
    """The event is out: its `on_publish` posts are due now, the team gets
    its share kit, and its poster sizes are made."""
    now = _now()
    posts = list(await session.scalars(select(SocialPosts).where(SocialPosts.event_id == event.id, SocialPosts.status.in_((SCHEDULED, NEEDS_APPROVAL)))))
    for post in posts:
        if (post.trigger or {}).get("mode") != "on_publish":
            continue
        for t in await waiting_targets(session, post.id):
            if t.scheduled_for is None:
                t.scheduled_for = now
        await session.flush()
        if post.status == SCHEDULED:
            for t in await waiting_targets(session, post.id):
                await enqueue_due(session, t)
    from app.api.social import sharekit

    await jobs.enqueue(session, sharekit.PUSH_JOB, {"event_id": event.id}, dedupe_key=f"{sharekit.PUSH_JOB}:{event.id}")
    await jobs.enqueue(session, sharekit.POSTERS_JOB, {"event_id": event.id}, dedupe_key=f"{sharekit.POSTERS_JOB}:{event.id}")


_DETAILS = {
    "date": ("event_datetime",), "time": ("event_datetime",), "venue": ("location_name", "is_online"),
    "fee": ("has_fee", "reg_fee"),
}


def _changed(before: dict, after: dict) -> set[str]:
    out = set()
    b, a = before.get("event_datetime"), after.get("event_datetime")
    if b != a and b and a:
        bd, ad = datetime.fromisoformat(b).astimezone(IST), datetime.fromisoformat(a).astimezone(IST)
        if bd.date() != ad.date():
            out.add("date")
        if bd.time() != ad.time():
            out.add("time")
    for detail in ("venue", "fee"):
        if any(before.get(k) != after.get(k) for k in _DETAILS[detail]):
            out.add(detail)
    if before.get("duration") != after.get("duration"):
        out.add("duration")
    if before.get("reg_enddate") != after.get("reg_enddate"):
        out.add("reg_close")
    return out


async def on_changed(session, event, before: dict) -> None:
    """The event's date, time, venue or fee changed (see the module's doc)."""
    after = content.snapshot(event)
    changed = _changed(before, after)
    if not changed:
        return
    now = _now()
    literal = content.literal_mentions(before)
    needles = [n for d in ("date", "time", "venue", "fee") if d in changed for n in literal.get(d, [])]
    timing = bool(changed & {"date", "time", "duration", "reg_close"})
    posts = list(await session.scalars(select(SocialPosts).where(SocialPosts.event_id == event.id, SocialPosts.status.in_((SCHEDULED, NEEDS_APPROVAL)))))
    for post in posts:
        targets = await waiting_targets(session, post.id)
        if not targets:
            continue
        if needles and any(content.mentions(t.text, needles) for t in targets):
            post.status, post.status_reason = PAUSED, "event_changed"
        mode = (post.trigger or {}).get("mode")
        if timing and mode in RELATIVE:
            try:
                due = due_for(post.trigger, event, now)
            except CustomHTTPException:
                due = None
            for t in targets:
                if due is None:
                    t.status, t.error_code, t.error_message, t.error_action = SKIPPED, "no_reg_close", "The event no longer has a registration closing time.", "retry"
                elif due < now and now >= _aware(event.event_datetime) and mode != "after_end":
                    t.status, t.error_code, t.error_message, t.error_action = SKIPPED, "event_over", "The event's new time made this post too late.", None
                else:
                    t.scheduled_for, t.next_attempt_at = max(due, now), None
        post.event_snapshot = after
        await session.flush()
        if post.status == SCHEDULED:
            for t in await waiting_targets(session, post.id):
                await enqueue_due(session, t)
        await settle_post(session, post.id)


async def stop_posts(session, event_id: int, reason: str, *, club_id: Optional[int] = None) -> int:
    """Cancel every waiting target of the event's posts (one club's, or all)."""
    q = select(SocialPosts).where(SocialPosts.event_id == event_id, SocialPosts.status.in_((SCHEDULED, NEEDS_APPROVAL, PAUSED)))
    if club_id is not None:
        q = q.where(SocialPosts.club_id == club_id)
    n = 0
    for post in list(await session.scalars(q)):
        res = await session.execute(
            update(T).where(T.post_id == post.id, T.status.in_(WAITING))
            .values(status=T_CANCELLED, error_code=reason, error_message=None, error_action=None)
            .execution_options(synchronize_session=False)
        )
        n += int(res.rowcount or 0)
        post.status, post.status_reason = CANCELLED, reason
    await session.flush()
    return n


async def on_stopped(session, event, reason: str) -> None:
    """The event was cancelled (`event_cancelled`), unpublished
    (`event_unpublished`) or deleted (`event_deleted`)."""
    await stop_posts(session, event.id, reason)
    if reason == "event_cancelled":
        from app.api.social import service

        await service.cancellation_drafts(session, event)


# ─── Sending one target ───────────────────────────────────────────────────


async def settle_post(session, post_id) -> None:
    """A scheduled post whose targets are all over is done."""
    open_ = await session.scalar(select(func.count()).where(T.post_id == post_id, T.status.notin_(FINISHED)))
    if not open_:
        await session.execute(
            update(SocialPosts).where(SocialPosts.id == post_id, SocialPosts.status == SCHEDULED).values(status=DONE)
            .execution_options(synchronize_session=False)
        )


async def _set(session, target_id, claim, **values) -> bool:
    """Settle a claimed target, only while the claim is still ours."""
    res = await session.execute(
        update(T).where(T.id == target_id, T.claim == claim).values(**values).execution_options(synchronize_session=False)
    )
    return bool(res.rowcount)


async def _posted(session, target: SocialPostTargets, pub: Published, now: datetime) -> str:
    """Saved the moment the platform answered: what makes a retry find it done."""
    res = await session.execute(
        update(T).where(T.id == target.id, T.external_post_id.is_(None))
        .values(status=POSTED, external_post_id=pub.external_id[:200], post_url=(pub.url or "")[:1000] or None, posted_at=now,
                uncertain_since=None, claim=None, error_code=None, error_message=None, error_action=None, next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if res.rowcount:
        post = await session.get(SocialPosts, target.post_id)
        await alerts.record(session, actor=None, action="post", club_id=target.club_id,
                            summary=f"Posted \"{post.name if post else ''}\" to {alerts.LABELS.get(target.platform, target.platform)}",
                            details={"post_id": str(target.post_id), "target_id": str(target.id), "event_id": target.event_id,
                                     "platform": target.platform, "account_id": str(target.account_id), "post_url": pub.url})
    await settle_post(session, target.post_id)
    await session.commit()
    return "posted"


async def _stop(session, target, claim, status: str, code: str, message: Optional[str], action: Optional[str]) -> str:
    await _set(session, target.id, claim, status=status, claim=None, error_code=code, error_message=(message or "")[:400] or None,
               error_action=action, next_attempt_at=None)
    await session.flush()
    post = await session.get(SocialPosts, target.post_id)
    if status == FAILED and post is not None:
        fresh = await session.get(T, target.id, populate_existing=True)
        await alerts.post_failed(session, fresh, post)
        await alerts.record(session, actor=None, action="post_failed", club_id=target.club_id,
                            summary=f"\"{post.name}\" failed on {alerts.LABELS.get(target.platform, target.platform)}: {code}",
                            details={"post_id": str(post.id), "target_id": str(target.id), "event_id": target.event_id,
                                     "platform": target.platform, "code": code, "action": action})
    await settle_post(session, target.post_id)
    await session.commit()
    return status


async def _again(session, target, claim, when: datetime, *, status: str = T_SCHEDULED, code=None, message=None,
                 uncertain: Optional[datetime] = None) -> str:
    """Wait and try again at `when`."""
    values = dict(status=status, claim=None, next_attempt_at=when, error_code=code, error_message=(message or None) and message[:400],
                  error_action=None)
    if uncertain is not None:
        values["uncertain_since"] = uncertain
    await _set(session, target.id, claim, **values)
    await session.commit()
    fresh = await session.get(T, target.id, populate_existing=True)
    await enqueue_due(session, fresh)
    await session.commit()
    return status


def gap(attempts: int) -> timedelta:
    return timedelta(minutes=GAPS[min(max(attempts, 1) - 1, len(GAPS) - 1)])


async def _failure(session, target, claim, account, exc: h.PlatformError, now: datetime) -> str:
    label = alerts.LABELS.get(target.platform, target.platform)
    first = _aware(target.first_attempt_at) or now
    if exc.kind == h.AUTH:
        await accounts_mod.needs_reconnect(session, account, exc.message)
        await _set(session, target.id, claim, status=HELD, claim=None, error_code="reconnect",
                   error_message=f"Reconnect {label} to send.", error_action="reconnect")
        await session.commit()
        return HELD
    if exc.kind == h.UNKNOWN:
        return await _again(session, target, claim, now + LOOKUP_AFTER, code=exc.code, message=exc.message,
                            uncertain=_aware(target.claimed_at) or now)
    if exc.kind == h.RATE:
        if now - first >= LIMIT_WINDOW:
            return await _stop(session, target, claim, FAILED, exc.code or "rate_limited", f"{label}'s limit held it for a day.", "retry")
        wait = max(LIMIT_WAIT, timedelta(seconds=exc.retry_after or 0))
        return await _again(session, target, claim, now + wait, status=WAITING_LIMIT, code=exc.code or "rate_limited",
                            message=f"Waiting — {label}'s limit.")
    if exc.kind == h.TRANSIENT:
        if now - first >= RETRY_WINDOW:
            return await _stop(session, target, claim, FAILED, exc.code or "unreachable",
                               f"{label} could not be reached for six hours: {exc.message}", "retry")
        return await _again(session, target, claim, now + gap(target.attempts), code=exc.code, message=exc.message)
    if exc.kind == h.IMAGE:
        return await _stop(session, target, claim, FAILED, exc.code or "image", exc.message, "fix_image")
    if exc.kind == h.PERMISSION:
        return await _stop(session, target, claim, FAILED, exc.code or "permission", exc.message, "reconnect")
    return await _stop(session, target, claim, FAILED, exc.code or exc.kind, exc.message, "share_manually")


def _picture(variant: Optional[PosterVariants]) -> Optional[Picture]:
    if variant is None or not variant.key:
        return None
    return Picture(url=posters.url_of(variant), key=variant.key)


async def _instagram_full(session, target, account, creds) -> bool:
    since = _now() - timedelta(hours=24)
    ours = await session.scalar(select(func.count()).where(
        T.account_id == account.id, T.platform == "instagram", T.posted_at >= since,
    )) or 0
    if ours >= platforms.INSTAGRAM_DAILY_LIMIT:
        return True
    try:
        quota = await for_platform("instagram").quota(creds)
    except h.PlatformError as exc:
        if exc.kind == h.AUTH:
            raise
        return False
    return quota is not None and quota[0] >= quota[1]


def month_start(now: datetime) -> datetime:
    local = now.astimezone(IST)
    return datetime(local.year, local.month, 1, tzinfo=IST).astimezone(timezone.utc)


async def x_over_cap(session, target_id, now: datetime) -> bool:
    """The month's X posts (sent, or being sent) have reached X_MONTHLY_POST_CAP.
    Under a transaction lock: two at the cap cannot both pass."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('social_x_monthly_cap'))"))
    used = await session.scalar(select(func.count()).where(
        T.platform == "x", T.id != target_id,
        or_(T.posted_at >= month_start(now), T.status == POSTING),
    )) or 0
    return used >= max(0, int(settings.X_MONTHLY_POST_CAP or 0))


def _stop_for_event(event, post, now: datetime) -> Optional[tuple[str, str, str]]:
    if event is None or event.is_deleted or lifecycle(event) is EventStatus.cancelled:
        return T_CANCELLED, "event_cancelled", "The event was cancelled."
    if lifecycle(event) is EventStatus.draft:
        return SKIPPED, "event_not_published", "The event is not published, so its page is not open yet."
    mode = (post.trigger or {}).get("mode")
    if mode not in ("after_end", "at") and now > ends_at(event):
        return SKIPPED, "event_over", "The event is over."
    return None


async def claim(session, target_id, now: datetime) -> Optional[uuid.UUID]:
    """Take the target for sending, or None when it is not due or someone has it."""
    mine = uuid.uuid4()
    got = await session.scalar(
        update(T)
        .where(
            T.id == target_id,
            or_(
                and_(T.status.in_((T_SCHEDULED, WAITING_LIMIT)), T.scheduled_for.is_not(None), T.scheduled_for <= now,
                     or_(T.next_attempt_at.is_(None), T.next_attempt_at <= now)),
                and_(T.status == POSTING, T.claimed_at < now - STALE),
            ),
            exists().where(SocialPosts.id == T.post_id, SocialPosts.status == SCHEDULED),
        )
        .values(
            status=POSTING, claim=mine, claimed_at=now, attempts=T.attempts + 1,
            first_attempt_at=func.coalesce(T.first_attempt_at, now),
            # A claim taken over from a runner that went quiet mid-send: it may have posted.
            uncertain_since=case((T.status == POSTING, func.coalesce(T.uncertain_since, T.claimed_at)), else_=T.uncertain_since),
        )
        .returning(T.id)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return mine if got is not None else None


async def run_target(session, target_id, now: Optional[datetime] = None) -> str:
    """Send one target if it is due: what the job runs. Returns what became of it."""
    now = now or _now()
    mine = await claim(session, target_id, now)
    if mine is None:
        return "not_due"
    target = await session.get(T, target_id, populate_existing=True)
    post = await session.get(SocialPosts, target.post_id, populate_existing=True)
    if target.external_post_id:
        return await _posted(session, target, Published(target.external_post_id, target.post_url), now)
    event = await session.get(Events, target.event_id, populate_existing=True)
    stop = _stop_for_event(event, post, now)
    if stop:
        return await _stop(session, target, mine, stop[0], stop[1], stop[2], "retry" if stop[1] == "event_not_published" else None)
    account = await session.get(SocialAccounts, target.account_id, populate_existing=True) if target.account_id else None
    label = alerts.LABELS.get(target.platform, target.platform)
    if account is None or account.status in (DISCONNECTED, NEEDS_RECONNECT):
        await _set(session, target.id, mine, status=HELD, claim=None, error_code="reconnect",
                   error_message=f"Reconnect {label} to send.", error_action="reconnect")
        await session.commit()
        return HELD
    if platforms.unavailable_reason(target.platform) == "not_configured":
        return await _stop(session, target, mine, FAILED, "not_configured", f"Automatic posting to {label} is not set up.", "share_manually")
    if not await platforms.plan_allows(session, post.club_id, target.platform):
        return await _stop(session, target, mine, FAILED, "plan", f"Your plan no longer includes automatic posting to {label}.", "share_manually")
    adapter = for_platform(target.platform)
    try:
        creds = await accounts_mod.fresh_creds(session, account)
    except crypto.Unavailable:
        return await _stop(session, target, mine, FAILED, "not_configured", "The account's keys can't be read on this server.", "reconnect")
    except h.PlatformError as exc:
        return await _failure(session, target, mine, account, exc, now)

    # An answer that may have been lost: look before sending again.
    if target.uncertain_since is not None and target.rendered_text:
        try:
            found = await adapter.find_recent(creds, target.rendered_text, _aware(target.uncertain_since))
        except h.LookupUnsupported:
            return await _stop(session, target, mine, FAILED, "unknown_outcome",
                               f"We couldn't tell whether {label} took this post. Check it there, then Retry if it isn't.", "retry")
        except h.PlatformError as exc:
            if exc.kind == h.UNKNOWN:
                exc = h.PlatformError(h.TRANSIENT, exc.message, code=exc.code)
            return await _failure(session, target, mine, account, exc, now)
        if found is not None:
            return await _posted(session, target, found, now)
        await _set(session, target.id, mine, uncertain_since=None, container_id=None if target.platform != "instagram" else target.container_id)
        await session.commit()
        target = await session.get(T, target_id, populate_existing=True)

    try:
        if target.platform == "instagram" and await _instagram_full(session, target, account, creds):
            return await _again(session, target, mine, now + timedelta(hours=1), status=WAITING_LIMIT, code="ig_daily_limit",
                                message="Waiting — Instagram's daily limit.")
    except h.PlatformError as exc:
        return await _failure(session, target, mine, account, exc, now)
    if target.platform == "x":
        if await x_over_cap(session, target.id, now):
            await session.commit()
            return await _stop(session, target, mine, FAILED, "x_monthly_cap",
                               "MyOtherApp's posts to X for this month have all gone out. Share this one by hand.", "share_manually")
        await session.commit()

    event_facts = await content.facts(session, event)
    link = await session.get(links.TrackingLinks, target.link_id) if target.link_id else None
    if link is None or link.is_deleted:
        link = await links.link_for(session, event, target.platform)
        target.link_id = link.id
    words = event_facts.words(link=links.short_url(link))
    rendered = content.render(target.text, words)
    target.rendered_text = rendered
    variant = await session.get(PosterVariants, target.variant_id) if target.variant_id else None
    if variant is None and platforms.get(target.platform).needs_image:
        made = await posters.ensure(session, event, [platforms.get(target.platform).default_size])
        variant = made.get(platforms.get(target.platform).default_size)
        target.variant_id = variant.id if variant else None
    await session.commit()

    async def keep_container(container_id: str) -> None:
        await session.execute(update(T).where(T.id == target_id).values(container_id=container_id).execution_options(synchronize_session=False))
        await session.commit()

    try:
        pub = await adapter.publish(creds, rendered, _picture(variant), container=target.container_id, progress=keep_container)
    except h.PlatformError as exc:
        return await _failure(session, target, mine, account, exc, now)
    except Exception as exc:  # noqa: BLE001 - after a send began, its outcome is unknown
        logger.warning("social: target %s raised %s while sending", target_id, type(exc).__name__)
        return await _failure(session, target, mine, account, h.PlatformError(h.UNKNOWN, "The send stopped unexpectedly.", code="raised"), now)
    return await _posted(session, target, pub, now)


@jobs.handler(JOB, max_attempts=3, timeout=240.0, concurrency=2, priority=jobs.HIGH)
async def _post_job(p: dict) -> None:
    async with jobs.scope() as session:
        await run_target(session, uuid.UUID(str(p["target_id"])))


# ─── The minute's sweep ──────────────────────────────────────────────────


async def dispatch(session, now: Optional[datetime] = None, limit: int = 200) -> int:
    """Queue every due target that may have lost its job (a deploy, a moved
    time), and every stale claim. Returns how many were queued."""
    now = now or _now()
    rows = list(await session.scalars(
        select(T).join(SocialPosts, SocialPosts.id == T.post_id)
        .where(
            SocialPosts.status == SCHEDULED,
            or_(
                and_(T.status.in_((T_SCHEDULED, WAITING_LIMIT)), T.scheduled_for <= now,
                     or_(T.next_attempt_at.is_(None), T.next_attempt_at <= now)),
                and_(T.status == POSTING, T.claimed_at < now - STALE),
            ),
        )
        .order_by(T.scheduled_for).limit(limit)
    ))
    for t in rows:
        if t.status == POSTING:
            await jobs.enqueue(session, JOB, {"target_id": str(t.id)}, dedupe_key=f"{JOB}:{t.id}:stale:{int(_aware(t.claimed_at).timestamp())}")
        else:
            await enqueue_due(session, t)
    await session.commit()
    return len(rows)
