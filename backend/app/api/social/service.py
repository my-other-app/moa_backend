"""An event's posts: drafted, edited, scheduled, approved, cancelled,
retried -- and what the Promote tab shows of them.

A club's posts are its own: a co-host plans the shared event's posts from
its own accounts and sees only those; the host never sees or uses a
co-host's, nor a co-host the host's. Scheduling needs the plan
(`social_autopost`, `social_x` for X) and a configured platform; a draft
needs neither. With the club's approval setting on, an editor's post waits
as `needs_approval` until an owner or admin approves it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.api.social import alerts, content, links, platforms, posters, scheduler
from app.api.social.access import Who, still_hosts
from app.api.social.models import (
    CANCELLED, DISCONNECTED, DONE, DRAFT, FAILED, HELD, NEEDS_APPROVAL, PAUSED, POSTED, POSTING, SCHEDULED, SKIPPED,
    T_CANCELLED, T_SCHEDULED, WAITING, PosterVariants, SocialAccounts, SocialPosts, SocialPostTargets, SocialSettings,
)
from app.response import CustomHTTPException

#: Posts one event may carry per club, and targets one post.
MAX_POSTS = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Settings ─────────────────────────────────────────────────────────────


async def settings_of(session, club_id: int) -> dict:
    row = await session.get(SocialSettings, club_id)
    return {"approval_required": bool(row and row.approval_required)}


async def save_settings(session, who: Who, approval_required: bool, request=None) -> dict:
    row = await session.get(SocialSettings, who.club.id)
    if row is None:
        row = SocialSettings(club_id=who.club.id)
        session.add(row)
    before = bool(row.approval_required)
    row.approval_required, row.updated_at = bool(approval_required), _now()
    if before != row.approval_required:
        await alerts.record(session, actor=who.actor, action="settings", club_id=who.club.id, request=request,
                            summary=f"Turned social post approval {'on' if row.approval_required else 'off'}",
                            details={"approval_required": row.approval_required})
    await session.commit()
    return await settings_of(session, who.club.id)


# ─── Reading ──────────────────────────────────────────────────────────────


def _person(name, user_id, member_id) -> Optional[dict]:
    if not (name or user_id or member_id):
        return None
    return {"id": member_id, "name": name}


def target_out(t: SocialPostTargets, variant: Optional[PosterVariants]) -> dict:
    error = None
    if t.error_code and t.status in (FAILED, HELD, SKIPPED, "waiting_limit", T_SCHEDULED):
        error = {"code": t.error_code, "message": t.error_message, "action": t.error_action}
    return {
        "id": t.id, "platform": t.platform, "account_id": t.account_id, "text": t.text,
        "variant_id": t.variant_id, "image_url": posters.url_of(variant), "status": t.status, "post_url": t.post_url,
        "error": error, "attempts": int(t.attempts or 0), "posted_at": t.posted_at,
        "scheduled_for": t.next_attempt_at if t.next_attempt_at and t.status in WAITING else t.scheduled_for,
    }


async def _variants(session, ids) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    return {v.id: v for v in await session.scalars(select(PosterVariants).where(PosterVariants.id.in_(ids)))}


async def post_out(session, post: SocialPosts, targets: Optional[list] = None) -> dict:
    targets = targets if targets is not None else list(await session.scalars(
        select(SocialPostTargets).where(SocialPostTargets.post_id == post.id).order_by(SocialPostTargets.created_at)
    ))
    variants = await _variants(session, [t.variant_id for t in targets])
    return {
        "id": post.id, "event_id": post.event_id, "name": post.name, "trigger": post.trigger, "status": post.status,
        "status_reason": post.status_reason, "template": post.template,
        "created_by": _person(post.created_by_name, post.created_by_user_id, post.created_by_member_id),
        "approved_by": _person(post.approved_by_name, post.approved_by_user_id, post.approved_by_member_id),
        "approved_at": post.approved_at, "created_at": post.created_at,
        "targets": [target_out(t, variants.get(t.variant_id)) for t in targets],
    }


async def event_posts(session, who: Who, event) -> dict:
    posts = list(await session.scalars(
        select(SocialPosts).where(SocialPosts.event_id == event.id, SocialPosts.club_id == who.club.id)
        .order_by(SocialPosts.created_at.desc())
    ))
    targets: dict = {}
    if posts:
        for t in await session.scalars(
            select(SocialPostTargets).where(SocialPostTargets.post_id.in_([p.id for p in posts])).order_by(SocialPostTargets.created_at)
        ):
            targets.setdefault(t.post_id, []).append(t)
    return {
        "attribution": await links.attribution(session, event.id),
        "posts": [await post_out(session, p, targets.get(p.id, [])) for p in posts],
    }


async def post_for(session, who: Who, post_id) -> SocialPosts:
    post = await session.get(SocialPosts, post_id)
    if post is None or post.club_id != who.club.id:
        raise CustomHTTPException(404, message="No such post", error_code="SOCIAL_POST_NOT_FOUND")
    return post


async def target_for(session, who: Who, target_id) -> SocialPostTargets:
    t = await session.get(SocialPostTargets, target_id)
    if t is None or t.club_id != who.club.id:
        raise CustomHTTPException(404, message="No such post", error_code="SOCIAL_POST_NOT_FOUND")
    return t


async def _event(session, event_id: int):
    from app.api.events.models import Events

    event = await session.get(Events, event_id)
    if event is None or event.is_deleted:
        raise CustomHTTPException(404, message="Event not found")
    return event


# ─── Drafts and checks ────────────────────────────────────────────────────


async def drafts(session, who: Who, event, wanted: Optional[list[str]] = None) -> dict:
    """A drafted post per platform: its text (with fill-in words), hashtag
    ideas, the poster made to its size, and its tracking link."""
    keys = [p for p in (wanted or list(platforms.PLATFORMS)) if p in platforms.PLATFORMS]
    f = await content.facts(session, event, None)
    made = await posters.ensure(session, event, sorted({platforms.get(k).default_size for k in keys}))
    out = []
    for key in keys:
        p = platforms.get(key)
        link = await links.link_for(session, event, key)
        variant = made.get(p.default_size)
        out.append({
            "platform": key, "text": content.draft(key, f), "hashtags": [f"#{t}" for t in f.hashtags],
            "variant": posters.out(variant) if variant else None, "link": links.short_url(link), "link_id": link.id,
            "limit": p.text_limit,
        })
    await session.commit()
    return {"fill_ins": list(content.FILL_INS), "platforms": out}


async def preview(session, who: Who, event, platform: str, text: str, variant_id=None) -> dict:
    f = await content.facts(session, event, None)
    link = await links.link_for(session, event, platform)
    await session.commit()
    rendered = content.render(text, f.words(link=links.short_url(link)))
    variant = None
    if variant_id is not None:
        variant = await session.get(PosterVariants, variant_id)
        if variant is None or variant.event_id != event.id:
            raise CustomHTTPException(404, message="No such poster size for this event", error_code="POSTER_NOT_FOUND")
    elif platforms.get(platform).needs_image:
        size = platforms.get(platform).default_size
        variant = (await posters.ensure(session, event, [size])).get(size)
        await session.commit()
    size = (variant.width, variant.height) if variant and variant.key else None
    warnings, counts = content.check(platform, text, rendered, picture_size=size, has_picture=bool(variant and variant.key))
    return {"platform": platform, "text_rendered": rendered, "warnings": warnings, "counts": counts,
            "image_url": posters.url_of(variant), "account": None}


async def rewrite(session, request, who: Who, event, platform: str, text: str, tone: Optional[str]) -> dict:
    """The club's AI assist (its own key when it has one) rewrites the text
    for the platform, keeping the fill-in words."""
    from app.api.ai import access as ai_access
    from app.api.ai import write
    from app.core.ai.base import AIRequest
    from app.core.ai.prompting import DATA_RULES, data_block

    p = platforms.get(platform)
    ai = await ai_access.resolve(session, who.club.id, who.user)
    tone_words = write.TONES.get(tone or "friendly", write.TONES["friendly"])
    system = (
        "You rewrite a social media post for a college club in India that runs its events on MyOtherApp. " + DATA_RULES + "\n\n"
        f"Rewrite it for {p.label}: at most {p.text_limit} characters"
        + (", as short and punchy as a post on X, one or two hashtags at most" if platform == "x" else "")
        + (", ending with up to eight relevant hashtags; links in Instagram captions cannot be tapped, so say \"link in bio\"" if platform == "instagram" else "")
        + (", professional and a little fuller" if platform in ("linkedin", "linkedin_page") else "")
        + f". Tone: {tone_words}.\n"
        "Keep every fill-in word in curly braces exactly as it is ({event_name}, {date}, {time}, {venue}, {fee}, "
        "{seats_left}, {speakers}, {link}): they are filled in when the post goes out. Never invent a date, time, price, "
        "venue, name, link or number."
    )
    ask = AIRequest(
        system=system,
        text="\n".join(["The event:", data_block("event", write.event_facts(event, who.club.name)),
                        "The post to rewrite:", data_block("post", text), "Write it as JSON."]),
        schema=write.SCHEMA, max_output_tokens=4096,
    )
    outcome = await ai_access.run(session, ai, action="write", request=ask, parse=write.parse, user=who.user, member_id=who.member_id)
    new = outcome.value.text.strip()[: p.text_limit * 2]
    rendered = content.render(new, (await content.facts(session, event, None)).words(link="https://myotherapp.in/l/xxxxxxxx"))
    warnings, counts = content.check(platform, new, rendered, has_picture=True)
    return {"platform": platform, "text": new, "warnings": warnings, "counts": counts}


# ─── Making and changing posts ────────────────────────────────────────────


async def _check_target(session, who: Who, event, spec: dict, *, scheduling: bool) -> dict:
    platform = spec["platform"]
    account = await session.get(SocialAccounts, spec["account_id"])
    if account is None or account.club_id != who.club.id or account.status == DISCONNECTED:
        raise CustomHTTPException(404, message="No such connected account", error_code="SOCIAL_ACCOUNT_NOT_FOUND")
    if account.platform != platform:
        raise CustomHTTPException(422, message="That account is on another platform.", error_code="SOCIAL_PLATFORM_MISMATCH")
    variant_id = spec.get("variant_id")
    if spec.get("image_key"):
        variant = await session.scalar(select(PosterVariants).where(
            PosterVariants.event_id == event.id, PosterVariants.key == spec["image_key"]))
        if variant is None:
            raise CustomHTTPException(422, message="That picture isn't one of this event's poster sizes.", error_code="POSTER_NOT_FOUND")
        variant_id = variant.id
    elif variant_id is not None:
        variant = await session.get(PosterVariants, variant_id)
        if variant is None or variant.event_id != event.id:
            raise CustomHTTPException(404, message="No such poster size for this event", error_code="POSTER_NOT_FOUND")
    text = (spec.get("text") or "").strip()
    if not text:
        raise CustomHTTPException(422, message=f"Write something for {platforms.get(platform).label}.", error_code="SOCIAL_TEXT_EMPTY")
    return {"platform": platform, "account": account, "text": text, "variant_id": variant_id}


async def _validate_for_schedule(session, who: Who, event, specs: list[dict]) -> None:
    """What must hold before anything is put on the clock."""
    f = await content.facts(session, event, None)
    for s in specs:
        key = s["platform"]
        p = platforms.get(key)
        reason = platforms.unavailable_reason(key)
        if reason:
            raise CustomHTTPException(409, message=f"Automatic posting to {p.label} isn't available yet.",
                                      error_code="SOCIAL_PLATFORM_UNAVAILABLE", errors={"platform": key, "reason": reason})
        await platforms.require_plan(session, who.club.id, key, actor=who.user)
        rendered = content.render(s["text"], f.words(link="https://api.myotherapp.in/l/xxxxxxxx"))
        variant = await session.get(PosterVariants, s["variant_id"]) if s.get("variant_id") else None
        if variant is None:
            # Not chosen: the platform's own size of the poster, when the event has one.
            variant = (await posters.ensure(session, event, [p.default_size])).get(p.default_size)
            s["variant_id"] = variant.id if variant else None
        warnings, counts = content.check(key, s["text"], rendered, picture_size=(variant.width, variant.height) if variant and variant.key else None,
                                         has_picture=bool(variant and variant.key))
        blocking = [w for w in warnings if w["code"] in ("x_over_280", "ig_hashtags_over_30", "image_missing", "image_shape", "unknown_fill_in")
                    or w["code"].endswith(f"_over_{p.text_limit}")]
        if blocking:
            raise CustomHTTPException(422, message=blocking[0]["message"], error_code="SOCIAL_POST_INVALID",
                                      errors={"platform": key, "warnings": blocking, "counts": counts})


async def create_post(session, who: Who, event, body: dict, request=None) -> dict:
    if not await still_hosts(session, event, who.club.id):
        raise CustomHTTPException(403, message="Only the event's host and co-hosts post about it.")
    count = len(list(await session.scalars(select(SocialPosts.id).where(SocialPosts.event_id == event.id, SocialPosts.club_id == who.club.id))))
    if count >= MAX_POSTS:
        raise CustomHTTPException(409, message=f"An event can have {MAX_POSTS} posts at most.", error_code="SOCIAL_TOO_MANY_POSTS")
    specs = [await _check_target(session, who, event, t, scheduling=not body.get("draft")) for t in body["targets"]]
    seen = set()
    for s in specs:
        if s["account"].id in seen:
            raise CustomHTTPException(422, message="Each account once per post.", error_code="SOCIAL_DUPLICATE_ACCOUNT")
        seen.add(s["account"].id)
    trigger = body["trigger"]
    if not body.get("draft"):
        await _validate_for_schedule(session, who, event, specs)
        scheduler.check_due(trigger, event, _now())
    post = SocialPosts(
        event_id=event.id, club_id=who.club.id, name=body["name"].strip()[:80], trigger=trigger, status=DRAFT,
        template=body.get("template"), created_by_user_id=getattr(who.user, "id", None), created_by_member_id=who.member_id,
        created_by_name=who.name,
    )
    session.add(post)
    await session.flush()
    for s in specs:
        link = await links.link_for(session, event, s["platform"])
        session.add(SocialPostTargets(
            post_id=post.id, event_id=event.id, club_id=who.club.id, platform=s["platform"], account_id=s["account"].id,
            text=s["text"], variant_id=s["variant_id"], link_id=link.id, status=T_SCHEDULED,
        ))
    await session.flush()
    if not body.get("draft"):
        await _schedule(session, who, post, event, request=request)
    await session.commit()
    return await post_out(session, post)


async def update_post(session, who: Who, post: SocialPosts, body: dict, request=None) -> dict:
    if post.status in (DONE, CANCELLED):
        raise CustomHTTPException(409, message="This post is over; make a new one.", error_code="SOCIAL_POST_OVER")
    event = await _event(session, post.event_id)
    if "name" in body and body["name"] is not None:
        post.name = body["name"].strip()[:80]
    if body.get("trigger") is not None:
        post.trigger = body["trigger"]
    if body.get("targets") is not None:
        specs = [await _check_target(session, who, event, t, scheduling=post.status != DRAFT) for t in body["targets"]]
        current = list(await session.scalars(select(SocialPostTargets).where(SocialPostTargets.post_id == post.id)))
        by_account = {t.account_id: t for t in current}
        keep = set()
        for s in specs:
            t = by_account.get(s["account"].id)
            if t is not None and t.status not in WAITING:
                keep.add(t.id)  # sent, sending or over: never edited
                continue
            if t is None:
                link = await links.link_for(session, event, s["platform"])
                t = SocialPostTargets(post_id=post.id, event_id=event.id, club_id=who.club.id, platform=s["platform"],
                                      account_id=s["account"].id, link_id=link.id, status=T_SCHEDULED)
                session.add(t)
            t.text, t.variant_id = s["text"], s["variant_id"]
            t.error_code = t.error_message = t.error_action = None
            if t.status == HELD and s["account"].status == "connected":
                t.status = T_SCHEDULED
            await session.flush()
            keep.add(t.id)
        for t in current:
            if t.id not in keep and t.status in WAITING:
                await session.delete(t)
        await session.flush()
    if post.status in (SCHEDULED, NEEDS_APPROVAL, PAUSED):
        # A change to a post on the clock is scheduled again: its times, and an editor's change waits for approval again.
        await _schedule(session, who, post, event, request=request, quiet=True)
    await session.commit()
    return await post_out(session, post)


async def _schedule(session, who: Who, post: SocialPosts, event, *, request=None, quiet: bool = False) -> None:
    targets = await scheduler.waiting_targets(session, post.id)
    if not targets:
        raise CustomHTTPException(409, message="Nothing in this post is waiting to go out.", error_code="SOCIAL_NOTHING_TO_SEND")
    specs = [{"platform": t.platform, "text": t.text, "variant_id": t.variant_id} for t in targets]
    await _validate_for_schedule(session, who, event, specs)
    for t, s in zip(targets, specs):
        t.variant_id = s["variant_id"]
    needs = (await settings_of(session, who.club.id))["approval_required"] and not who.decides
    post.event_snapshot = content.snapshot(event)
    post.status_reason = None
    if needs:
        post.status = NEEDS_APPROVAL
        post.approved_by_user_id = post.approved_by_member_id = post.approved_by_name = post.approved_at = None
        scheduler.check_due(post.trigger, event, _now())
        await scheduler.place(session, post, event)
    else:
        post.status = SCHEDULED
        if (await settings_of(session, who.club.id))["approval_required"]:
            post.approved_by_user_id, post.approved_by_member_id = getattr(who.user, "id", None), who.member_id
            post.approved_by_name, post.approved_at = who.name, _now()
        await scheduler.place(session, post, event)
    await alerts.record(session, actor=who.actor, action="schedule", club_id=who.club.id, request=request,
                        summary=f"{'Asked approval for' if needs else 'Scheduled'} \"{post.name}\" ({', '.join(sorted({t.platform for t in targets}))})",
                        details={"post_id": str(post.id), "event_id": event.id, "trigger": post.trigger, "status": post.status})


async def schedule(session, who: Who, post: SocialPosts, request=None) -> dict:
    if post.status in (DONE, CANCELLED):
        raise CustomHTTPException(409, message="This post is over; make a new one.", error_code="SOCIAL_POST_OVER")
    event = await _event(session, post.event_id)
    if not await still_hosts(session, event, who.club.id):
        raise CustomHTTPException(403, message="Only the event's host and co-hosts post about it.")
    await _schedule(session, who, post, event, request=request)
    await session.commit()
    return await post_out(session, post)


async def approve(session, who: Who, post: SocialPosts, request=None) -> dict:
    if not who.decides:
        raise CustomHTTPException(403, message="Only the club's owner or an admin approves posts.", error_code="ROLE_FORBIDDEN")
    if post.status != NEEDS_APPROVAL:
        raise CustomHTTPException(409, message="This post isn't waiting for approval.", error_code="SOCIAL_NOT_AWAITING_APPROVAL")
    event = await _event(session, post.event_id)
    await _validate_for_schedule(session, who, event, [
        {"platform": t.platform, "text": t.text, "variant_id": t.variant_id} for t in await scheduler.waiting_targets(session, post.id)
    ])
    post.status = SCHEDULED
    post.approved_by_user_id, post.approved_by_member_id, post.approved_by_name, post.approved_at = (
        getattr(who.user, "id", None), who.member_id, who.name, _now())
    await scheduler.place(session, post, event)
    await alerts.record(session, actor=who.actor, action="approve", club_id=who.club.id, request=request,
                        summary=f"Approved \"{post.name}\"", details={"post_id": str(post.id), "event_id": event.id})
    await session.commit()
    return await post_out(session, post)


async def cancel(session, who: Who, post: SocialPosts, request=None) -> dict:
    """Everything of the post still waiting is cancelled; what went out stays."""
    from sqlalchemy import update

    if post.status in (DONE, CANCELLED):
        raise CustomHTTPException(409, message="This post is over already.", error_code="SOCIAL_POST_OVER")
    await session.execute(
        update(SocialPostTargets).where(SocialPostTargets.post_id == post.id, SocialPostTargets.status.in_(WAITING))
        .values(status=T_CANCELLED, error_code="by_club", error_message=None, error_action=None)
        .execution_options(synchronize_session=False)
    )
    post.status, post.status_reason = CANCELLED, "by_club"
    await alerts.record(session, actor=who.actor, action="cancel", club_id=who.club.id, request=request,
                        summary=f"Cancelled \"{post.name}\"", details={"post_id": str(post.id), "event_id": post.event_id})
    await session.commit()
    return await post_out(session, post)


async def delete_post(session, who: Who, post: SocialPosts) -> None:
    if post.status != DRAFT:
        raise CustomHTTPException(409, message="Only a draft can be deleted; cancel a scheduled post instead.", error_code="SOCIAL_NOT_A_DRAFT")
    await session.delete(post)
    await session.commit()


async def retry(session, who: Who, target: SocialPostTargets, request=None) -> dict:
    """A failed, skipped, held or cancelled target, sent again now -- never
    one the platform already has (its post id is kept)."""
    if target.external_post_id or target.status == POSTED:
        raise CustomHTTPException(409, message="This one is already posted.", error_code="SOCIAL_ALREADY_POSTED")
    if target.status == POSTING:
        raise CustomHTTPException(409, message="This one is being sent now.", error_code="SOCIAL_POSTING")
    post = await session.get(SocialPosts, target.post_id)
    event = await _event(session, target.event_id)
    if not await still_hosts(session, event, who.club.id):
        raise CustomHTTPException(403, message="Only the event's host and co-hosts post about it.")
    account = await session.get(SocialAccounts, target.account_id) if target.account_id else None
    if account is None or account.status != "connected":
        raise CustomHTTPException(409, message=f"Reconnect {alerts.LABELS.get(target.platform, target.platform)} first.",
                                  error_code="SOCIAL_RECONNECT_FIRST")
    await _validate_for_schedule(session, who, event, [{"platform": target.platform, "text": target.text, "variant_id": target.variant_id}])
    now = _now()
    target.status, target.scheduled_for, target.next_attempt_at = T_SCHEDULED, now, None
    target.first_attempt_at = None
    target.error_code = target.error_message = target.error_action = None
    if post.status in (DONE, CANCELLED, PAUSED, DRAFT):
        post.status, post.status_reason = SCHEDULED, None
    await session.flush()
    await scheduler.enqueue_due(session, target)
    await alerts.record(session, actor=who.actor, action="retry", club_id=who.club.id, request=request,
                        summary=f"Sent \"{post.name}\" to {alerts.LABELS.get(target.platform, target.platform)} again",
                        details={"post_id": str(post.id), "target_id": str(target.id)})
    await session.commit()
    return await post_out(session, post)


# ─── Ready-made posts ─────────────────────────────────────────────────────

#: The standard reminder: two days before the event starts.
REMINDER_MINUTES = 2 * 24 * 60


async def campaign(session, who: Who, event, wanted: list[str], account_ids: list, request=None) -> dict:
    """"Announce it": the Announcement (on publish) and the Reminder (two
    days before) from the drafts, to the accounts ticked -- the reminder
    left out when its time has passed."""
    q = select(SocialAccounts).where(SocialAccounts.club_id == who.club.id, SocialAccounts.status != DISCONNECTED)
    chosen = [a for a in await session.scalars(q) if (not account_ids or a.id in set(account_ids)) and (not wanted or a.platform in wanted)]
    if not chosen:
        raise CustomHTTPException(422, message="Connect an account (or tick one) to announce the event.", error_code="SOCIAL_NO_ACCOUNTS")
    f = await content.facts(session, event, None)
    made = await posters.ensure(session, event, sorted({platforms.get(a.platform).default_size for a in chosen}))
    targets = [{"platform": a.platform, "account_id": a.id, "text": content.draft(a.platform, f),
                "variant_id": (made.get(platforms.get(a.platform).default_size).id if made.get(platforms.get(a.platform).default_size) else None)}
               for a in chosen]
    out = [await create_post(session, who, event, {"name": "Announcement", "trigger": {"mode": "on_publish"}, "targets": targets,
                                                    "template": "announcement"}, request=request)]
    reminder = {"mode": "before_start", "minutes": REMINDER_MINUTES}
    try:
        due = scheduler.due_for(reminder, event, _now())
    except CustomHTTPException:
        due = None
    if due is not None and due > _now():
        out.append(await create_post(session, who, event, {"name": "Reminder", "trigger": reminder, "targets": targets,
                                                            "template": "reminder"}, request=request))
    return {"posts": out}


async def cancellation_drafts(session, event) -> int:
    """A cancelled event whose posts went out: a "Cancellation" draft for
    each club that posted, to the accounts it posted from."""
    from app.api.social.models import SocialAccounts as A

    rows = list(await session.scalars(
        select(SocialPostTargets).where(SocialPostTargets.event_id == event.id, SocialPostTargets.status == POSTED)
    ))
    by_club: dict = {}
    for t in rows:
        by_club.setdefault(t.club_id, {})[t.account_id] = t.platform
    made = 0
    for club_id, accounts in by_club.items():
        exists_ = await session.scalar(select(SocialPosts.id).where(
            SocialPosts.event_id == event.id, SocialPosts.club_id == club_id, SocialPosts.template == "cancellation").limit(1))
        if exists_ is not None:
            continue
        post = SocialPosts(event_id=event.id, club_id=club_id, name="Cancellation", trigger={"mode": "at", "at": _now().isoformat()},
                           status=DRAFT, template="cancellation", created_by_name="MyOtherApp")
        session.add(post)
        await session.flush()
        for account_id, platform in accounts.items():
            account = await session.get(A, account_id) if account_id else None
            if account is None or account.status == DISCONNECTED:
                continue
            text = (f"Update: {{event_name}} on {{date}} is cancelled. We're sorry — "
                    "everyone registered has been told, and any payments are being refunded.")
            session.add(SocialPostTargets(post_id=post.id, event_id=event.id, club_id=club_id, platform=platform,
                                          account_id=account_id, text=text, status=T_SCHEDULED))
        made += 1
    await session.flush()
    return made
