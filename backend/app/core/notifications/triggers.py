"""
Notification trigger functions for different scenarios.

This module provides trigger functions that are called when specific
events occur in the application (new event, check-in, etc.).

Each trigger:
1. Sends push notification via FCM
2. Saves notification to database for in-app notification history
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs.models import ClubUsersLink
from app.api.events.models import Events, EventRegistrationsLink
from app.api.notifications import service as db_notification_service
from app.core import jobs
from app.core.notifications import delivery, fanout
from app.core.notifications.topics import settings_off
from app.core.notifications.service import (
    send_notification_to_user,
    send_notification_to_users,
)

logger = logging.getLogger(__name__)


async def notify_followers_of_new_event(
    session: AsyncSession,
    club_id: int,
    club_name: str,
    event_id: int,
    event_name: str,
) -> None:
    """
    Notify all followers of a club about a new event.

    Trigger 1: When a club posts a new event, notify all users following the club.

    A job (`notifications.new_event`), written and committed here -- the
    event is committed by then -- and run after: the inbox rows are one
    INSERT ... SELECT over the followers, and the pushes go in batches
    (app.core.notifications.fanout). Once per event: a second call while the
    first is waiting adds nothing, and a run again tells nobody twice.
    """
    await jobs.enqueue(
        session, "notifications.new_event",
        {"club_id": club_id, "club_name": club_name, "event_id": event_id, "event_name": event_name},
        dedupe_key=f"notify:new_event:{event_id}",
    )
    await session.commit()


def broadcasts_on() -> bool:
    """Whether broadcasts go through fanout.announce (one row, a topic send)
    rather than a row and a push per recipient, as before."""
    from app.config import settings

    return bool(settings.PUSH_TOPICS or settings.INBOX_SHARED)


@jobs.handler("notifications.new_event", max_attempts=6, timeout=600.0)
async def _new_event_job(p: dict) -> None:
    club_id, event_id = int(p["club_id"]), int(p["event_id"])
    title, body = f"🎉 New from {p['club_name']}!", f"Check out: {p['event_name']}"
    data = {"club_name": p["club_name"], "event_name": p["event_name"]}
    push_data = {"type": "new_event", "event_id": str(event_id), "club_id": str(club_id)}
    if broadcasts_on():
        async with jobs.scope() as s:
            await fanout.announce(s, fanout.Broadcast(
                kind="new_event", ref=str(event_id), title=title, body=body, data=data, push_data=push_data,
                from_club_id=club_id, event_id=event_id, club_id=club_id,
            ), topic=delivery.club_topic(club_id))
        return
    audience = (
        select(ClubUsersLink.user_id)
        .where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.is_following == True,  # noqa: E712
            ClubUsersLink.is_deleted == False,  # noqa: E712
            ClubUsersLink.is_muted == False,  # noqa: E712
            ~settings_off("clubs", ClubUsersLink.user_id),
        )
        .distinct()
    )
    async with jobs.scope() as s:
        await _tell(
            s, audience, title=title, body=body, type="new_event",
            from_club_id=club_id, event_id=event_id, data=data, push_data=push_data,
        )


async def notify_users_by_interest(
    session: AsyncSession,
    category_id: int | Sequence[int],
    category_name: str,
    club_id: int,
    event_id: int,
    event_name: str,
) -> Optional[int]:
    """
    Notify users who picked an interest under the event's topics.

    Trigger 2. `category_id`: a topic (interest category) id, or the
    event's topics' ids; `category_name` names the first in the push.
    Excludes users who are already followers (they got Trigger 1).

    Capped, so one event cannot push to every user on the platform: at most
    INTEREST_PUSH_MAX_PER_EVENT people (the newest accounts first: index
    order, and the people most likely still finding their way in), and
    nobody who has already had INTEREST_PUSH_MAX_PER_USER_PER_DAY of these in
    the last day. Followers are never capped: they asked. A job, written and
    committed here and run after, as notify_followers_of_new_event's is.
    """
    from app.config import settings

    per_event = max(0, int(settings.INTEREST_PUSH_MAX_PER_EVENT))
    per_day = max(0, int(settings.INTEREST_PUSH_MAX_PER_USER_PER_DAY))
    if per_event == 0 or per_day == 0:
        return 0

    topic_ids = [category_id] if isinstance(category_id, int) else list(category_id)
    if not topic_ids:
        return 0
    await jobs.enqueue(
        session, "notifications.interest",
        {"topic_ids": topic_ids, "category_name": category_name, "club_id": club_id, "event_id": event_id, "event_name": event_name},
        dedupe_key=f"notify:interest:{event_id}",
    )
    await session.commit()
    return None


@jobs.handler("notifications.interest", max_attempts=6, timeout=600.0)
async def _interest_job(p: dict) -> None:
    from app.api.interests.models import Interests
    from app.api.notifications.models import Notifications
    from app.api.users.models import UserInterests
    from app.config import settings

    per_event = max(0, int(settings.INTEREST_PUSH_MAX_PER_EVENT))
    per_day = max(0, int(settings.INTEREST_PUSH_MAX_PER_USER_PER_DAY))
    if per_event == 0 or per_day == 0:
        return
    topic_ids = [int(t) for t in p["topic_ids"]]
    club_id, event_id, category_name, event_name = int(p["club_id"]), int(p["event_id"]), p["category_name"], p["event_name"]
    title = f"📌 Event in {category_name}"
    body = f"New event matching your interests: {event_name}"
    data = {"category_name": category_name, "event_name": event_name}
    push_data = {"type": "interest_match", "event_id": str(event_id), "category_id": str(topic_ids[0])}
    if broadcasts_on():
        async with jobs.scope() as s:
            await _announce_by_topic(
                s, topic_ids, club_id=club_id, event_id=event_id, title=title, body=body, data=data, push_data=push_data,
            )
        return
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    in_category = select(Interests.id).where(Interests.category_id.in_(topic_ids), Interests.is_deleted == False)  # noqa: E712
    follows = (
        select(ClubUsersLink.id)
        .where(
            ClubUsersLink.user_id == UserInterests.user_id,
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.is_following == True,  # noqa: E712
            ClubUsersLink.is_deleted == False,  # noqa: E712
        )
        .exists()
    )
    told_today = (
        select(func.count())
        .select_from(Notifications)
        .where(
            Notifications.user_id == UserInterests.user_id,
            Notifications.type == "nearby_event",
            Notifications.is_deleted == False,  # noqa: E712
            Notifications.created_at >= day_ago,
        )
        .scalar_subquery()
    )
    audience = (
        select(UserInterests.user_id)
        .where(
            UserInterests.interest_id.in_(in_category),
            UserInterests.is_deleted == False,  # noqa: E712
            ~follows,
            told_today < per_day,
            ~settings_off("topics", UserInterests.user_id),
        )
        .group_by(UserInterests.user_id)
        .order_by(UserInterests.user_id.desc())
        .limit(per_event)
    )

    async with jobs.scope() as s:
        await _tell(
            s, audience, title=title, body=body, type="nearby_event", from_club_id=club_id, event_id=event_id,
            data=data, push_data=push_data,
        )


async def _announce_by_topic(
    session: AsyncSession, topic_ids: list[int], *, club_id: int, event_id: int, title: str, body: str, data: dict, push_data: dict,
) -> bool:
    """A new event to the people of its topics, as a broadcast: one send to
    the topics' FCM condition, but not to the club's followers (they had
    new_event through club_{id}). A topic push reaches whoever is subscribed,
    so it cannot be capped per person: each topic takes at most
    INTEREST_TOPIC_PUSH_MAX_PER_DAY of these a day instead, and a topic past
    it is left out of this one. FCM takes five topics in a condition: the
    club's and the event's first four, in topic order."""
    from app.api.interests.models import InterestCategory
    from app.api.notifications.models import NotificationBroadcasts
    from app.config import settings

    cap = max(0, int(settings.INTEREST_TOPIC_PUSH_MAX_PER_DAY))
    topics = (await session.execute(
        select(InterestCategory.id, InterestCategory.slug)
        .where(InterestCategory.id.in_(topic_ids), InterestCategory.is_deleted == False)  # noqa: E712
        .order_by(InterestCategory.sort_order, InterestCategory.name, InterestCategory.id)
    )).all()
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    sent_today = Counter(
        int(t)
        for audience in (await session.scalars(
            select(NotificationBroadcasts.audience).where(
                NotificationBroadcasts.type == "nearby_event", NotificationBroadcasts.created_at >= day_ago,
                NotificationBroadcasts.push_id != delivery.push_id("nearby_event", event_id),
            )
        )).all()
        for t in (audience or {}).get("topics") or ()
    )
    room = [t for t in topics if sent_today[t.id] < cap][:4]
    if not room:
        logger.info("No topic of event %s has room for another push today", event_id)
        await session.commit()
        return False
    return await fanout.announce(session, fanout.Broadcast(
        kind="nearby_event", ref=str(event_id), title=title, body=body, data=data, push_data=push_data,
        from_club_id=club_id, event_id=event_id, topic_ids=[t.id for t in room], except_club_id=club_id,
    ), condition=delivery.condition([delivery.topic_topic(t.slug) for t in room], unless=delivery.club_topic(club_id)))


async def _tell(
    session: AsyncSession,
    audience,
    *,
    title: str,
    body: str,
    type: str,
    data: dict,
    push_data: dict,
    from_club_id: Optional[int] = None,
    event_id: Optional[int] = None,
) -> int:
    """The inbox rows in one statement, kept, with the job that pushes to
    everyone holding one (`notifications.push`), committed together. The
    rows skip anyone already told about this event (`once`), so the job run
    again adds none; the push job, run again, pushes again -- a second
    buzz rather than none."""
    reached = await fanout.insert_for_audience(
        session, audience, title=title, description=body, type=type, data=data,
        from_club_id=from_club_id, event_id=event_id, once=True,
    )
    if reached:
        await fanout.enqueue_push(session, type=type, event_id=event_id, title=title, body=body, data=push_data)
    await session.commit()
    if not reached:
        logger.debug("Nobody new to tell about %s for event %s", type, event_id)
        return 0
    logger.info("Told %d people about %s for event %s; pushing", len(reached), type, event_id)
    return len(reached)


async def notify_user_check_in(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    event_name: str,
    club_id: Optional[int] = None,
) -> bool:
    """
    Notify a user when they successfully check in to an event.
    
    Trigger 3: Confirmation notification after event check-in.
    """
    logger.info(f"Sending check-in confirmation to user {user_id} for event {event_id}")
    
    title = "✅ Checked in successfully!"
    body = f"Welcome to {event_name}. Enjoy the event!"
    
    # Save to database
    await db_notification_service.create_notification(
        session=session,
        user_id=user_id,
        title=title,
        description=body,
        type="event_checkin",
        event_id=event_id,
        from_club_id=club_id,
        data={"event_name": event_name},
    )
    
    # Send push notification
    return await send_notification_to_user(
        session=session,
        user_id=user_id,
        title=title,
        body=body,
        data={
            "type": "check_in",
            "event_id": str(event_id),
        },
    )


async def notify_event_participants(
    session: AsyncSession,
    event_id: int,
    title: str,
    body: str,
    user_ids: Optional[list[int]] = None,
    audience: str = "all",
    image_url: Optional[str] = None,
    club_id: Optional[int] = None,
) -> int:
    """
    Send notification to event participants from club dashboard.
    
    Trigger 4: Allow clubs to send custom notifications to event participants.
    """
    # Get club_id from event if not provided
    if club_id is None:
        event_query = select(Events.club_id).where(Events.id == event_id)
        event_result = await session.execute(event_query)
        club_id = event_result.scalar_one_or_none()
    
    if user_ids is None:
        # Build query based on audience targeting
        query = select(EventRegistrationsLink.user_id).where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
        )
        
        # Apply audience filter
        if audience == "attendees":
            query = query.where(EventRegistrationsLink.is_attended == True)
        elif audience == "non_attendees":
            query = query.where(EventRegistrationsLink.is_attended == False)
        
        result = await session.execute(query)
        user_ids = [row[0] for row in result.fetchall()]
    
    if not user_ids:
        logger.debug(f"No participants to notify for event {event_id} (audience: {audience})")
        return 0, []
    
    logger.info(f"Sending club notification to {len(user_ids)} {audience} of event {event_id}")
    
    # Save to database
    await db_notification_service.create_notifications_batch(
        session=session,
        user_ids=user_ids,
        title=title,
        description=body,
        type="club_announcement",
        from_club_id=club_id,
        event_id=event_id,
        data={"audience": audience, "has_image": image_url is not None},
    )
    
    # Send push notification
    sent_count = await send_notification_to_users(
        session=session,
        user_ids=user_ids,
        title=title,
        body=body,
        data={
            "type": "club_announcement",
            "event_id": str(event_id),
            "image_url": image_url or "",
        },
        image_url=image_url,
    )
    
    return sent_count, user_ids


async def notify_user_added_as_volunteer(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    event_name: str,
    club_name: str,
    club_id: Optional[int] = None,
) -> bool:
    """
    Notify a user when they are added as a volunteer to an event.
    
    Trigger 5: When a user is added as a volunteer, notify them.
    """
    logger.info(f"Sending volunteer notification to user {user_id} for event {event_id}")
    
    title = f"🎖️ You're now a volunteer!"
    body = f"{club_name} added you as a volunteer for {event_name}"
    
    # Save to database
    await db_notification_service.create_notification(
        session=session,
        user_id=user_id,
        title=title,
        description=body,
        type="volunteer_added",
        event_id=event_id,
        from_club_id=club_id,
        data={"club_name": club_name, "event_name": event_name},
    )
    
    # Send push notification
    return await send_notification_to_user(
        session=session,
        user_id=user_id,
        title=title,
        body=body,
        data={
            "type": "volunteer_assigned",
            "event_id": str(event_id),
        },
    )


async def notify_certificate_generated(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    event_name: str,
    certificate_id: Optional[str] = None,
    club_id: Optional[int] = None,
) -> bool:
    """
    Notify a user when their certificate is generated.
    
    Trigger 6: When a certificate is generated for a user after event completion.
    """
    logger.info(f"Sending certificate notification to user {user_id} for event {event_id}")
    
    title = "🏆 Certificate Ready!"
    body = f"Your certificate for {event_name} is now available!"
    
    # Save to database
    await db_notification_service.create_notification(
        session=session,
        user_id=user_id,
        title=title,
        description=body,
        type="certificate_generated",
        event_id=event_id,
        from_club_id=club_id,
        data={"event_name": event_name, "certificate_id": certificate_id},
    )
    
    # Send push notification
    return await send_notification_to_user(
        session=session,
        user_id=user_id,
        title=title,
        body=body,
        data={
            "type": "certificate_generated",
            "event_id": str(event_id),
            "certificate_id": certificate_id or "",
        },
    )
