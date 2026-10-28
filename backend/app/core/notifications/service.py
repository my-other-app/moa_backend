"""
Firebase Cloud Messaging: sending pushes to people's devices.

Sends go out in batches of at most FCM_BATCH tokens. firebase-admin refuses
more than 500 in one `send_each_for_multicast` (a ValueError, before anything
is sent), and this module used to catch that and return 0 -- so no club with
more than about 500 follower devices ever had a push delivered. Each batch is
sent on a worker thread: the SDK is synchronous and opens a thread per token,
and on the event loop it stopped every other request until FCM answered.

Tokens are looked up with one array parameter (`user_id = ANY(:ids)`), never
one bind per user: Postgres takes at most 32,767 binds in a statement, so a
list of followers past that failed outright.

A person can have several devices; each has its own row (one per token), and
every one of them is sent to.

A broadcast can go to an FCM topic instead (`send_to_topic`, with PUSH_TOPICS:
app.core.notifications.delivery says which pushes are broadcasts): one request
however many devices are subscribed. A push whose data carries a `push_id`
gets it as Android's notification tag and APNs' collapse id, whichever way it
is sent, so two copies of one push show as one.

The number on an iPhone's app icon is APNs' `badge`. It was 1 in every push,
and the app never set it, so after one push the icon said 1 for good. Now it
is the person's unread inbox count, the number GET
/notifications/unread-count gives them, read as the push goes (their new
line included, as every caller writes the line first):

- a push to one person (send_notification_to_user): their count;
- a push to many (send_notification_to_users, a broadcast device by device):
  each device its person's count, from one grouped count per LOOKUP_BATCH
  people (unread_badges). A batch of devices whose counts are all the same
  is one multicast, as before; a batch with different counts is one
  `send_each` of a message per device -- the call firebase-admin makes for
  a multicast anyway, FCM_BATCH at a time. With INBOX_SHARED on, the shared
  broadcasts' part of each count is a query per person
  (app.core.notifications.inbox), so a push to many carries no badge;
- a send to an FCM topic is one message for every device subscribed: no
  badge.

No badge leaves the number on the icon as it was; the app sets it from the
same count whenever it opens or comes to the front. A wrong number is never
sent: when a count cannot be read, the push goes without one.
"""

import asyncio
import logging
from typing import Iterable, Mapping, Optional

import firebase_admin
import sqlalchemy as sa
from firebase_admin import credentials, exceptions as firebase_exceptions, messaging
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.notifications.models import Notifications, NotificationStatus
from app.api.users.models import UserDeviceTokens, Users
from app.config import settings

logger = logging.getLogger(__name__)

#: The most tokens firebase-admin takes in one multicast.
FCM_BATCH = 500
#: User ids per token lookup: one array bind each, so this only bounds how
#: much one round trip carries.
LOOKUP_BATCH = 5000

# Initialize Firebase Admin SDK
_firebase_app = None


def initialize_firebase():
    """Initialize Firebase Admin SDK with service account credentials."""
    global _firebase_app

    if _firebase_app is not None:
        return _firebase_app

    try:
        cred = None

        # Option 1: Path to service account JSON file
        if settings.FIREBASE_SERVICE_ACCOUNT_PATH:
            import os
            if not os.path.exists(settings.FIREBASE_SERVICE_ACCOUNT_PATH):
                logger.error(f"Firebase service account file not found at: {settings.FIREBASE_SERVICE_ACCOUNT_PATH}")
                return None

            logger.info(f"Using Firebase credentials from file: {settings.FIREBASE_SERVICE_ACCOUNT_PATH}")
            cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_PATH)

        # Option 2: Base64 encoded service account JSON
        elif settings.FIREBASE_SERVICE_ACCOUNT_BASE64:
            import base64
            import json
            logger.info("Using Firebase credentials from base64 encoded environment variable")
            decoded = base64.b64decode(settings.FIREBASE_SERVICE_ACCOUNT_BASE64)
            service_account_info = json.loads(decoded)
            cred = credentials.Certificate(service_account_info)

        if cred:
            _firebase_app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin SDK initialized successfully")
        else:
            logger.warning(
                "No Firebase credentials configured. "
                "Set APP_FIREBASE_SERVICE_ACCOUNT_PATH or APP_FIREBASE_SERVICE_ACCOUNT_BASE64"
            )
            return None

        return _firebase_app
    except Exception as e:
        logger.error(f"Failed to initialize Firebase Admin SDK: {e}")
        return None


def int_array(ids: Iterable[int]):
    """One bind for a whole list of ids, for `column == sa.any_(int_array(ids))`."""
    return sa.literal(sorted({int(i) for i in ids}), ARRAY(sa.Integer))


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def reachable(user_id_column):
    """SQL: the account behind `user_id_column` may be pushed -- its address
    is not at a suppressed domain (settings.MAIL_SUPPRESS_DOMAINS, the
    demo's invented people). Every device query a push reads goes through it."""
    from app.core.mail import suppression

    return ~sa.exists().where(Users.id == user_id_column, suppression.domain_suppressed(Users.email))


async def get_fcm_devices_for_users(
    session: AsyncSession,
    user_ids: list[int],
) -> list[tuple[int, str]]:
    """Every live device of these users, as (user id, token), each token
    once, oldest first."""
    if not user_ids:
        return []
    ids = sorted({int(u) for u in user_ids})
    devices: list[tuple[int, str]] = []
    seen: set[str] = set()
    for part in _chunks(ids, LOOKUP_BATCH):
        rows = await session.execute(
            select(UserDeviceTokens.user_id, UserDeviceTokens.fcm_token)
            .where(
                UserDeviceTokens.user_id == sa.any_(int_array(part)),
                UserDeviceTokens.is_deleted == False,  # noqa: E712
                reachable(UserDeviceTokens.user_id),
            )
            .order_by(UserDeviceTokens.id)
        )
        for user_id, token in rows.all():
            if token and token not in seen:
                seen.add(token)
                devices.append((int(user_id), token))
    logger.info("Found %d FCM tokens for %d users", len(devices), len(ids))
    return devices


async def get_fcm_tokens_for_users(
    session: AsyncSession,
    user_ids: list[int],
) -> list[str]:
    """Every live device token of these users, each once, oldest first."""
    return [token for _, token in await get_fcm_devices_for_users(session, user_ids)]


# ─── The number on the app's icon ───────────────────────────────────────────


async def unread_badge(session: AsyncSession, user_id: int) -> Optional[int]:
    """The badge for a push to this person: their unread inbox count, as GET
    /notifications/unread-count gives it (with INBOX_SHARED, the shared
    lines' part included). None when it cannot be read: the push then goes
    without a badge. In a savepoint, so a count that fails leaves the
    caller's transaction usable."""
    from app.api.notifications.service import get_unread_count

    try:
        async with session.begin_nested():
            return max(0, int(await get_unread_count(session, int(user_id))))
    except Exception:  # noqa: BLE001 -- a push is never stopped by its badge
        logger.warning("No badge for user %s: the unread count could not be read", user_id, exc_info=True)
        return None


async def unread_badges(session: AsyncSession, user_ids: Iterable[int]) -> Optional[dict[int, int]]:
    """Each person's unread inbox count, for a push to many: one grouped
    count (ix_notifications_user_unread) per LOOKUP_BATCH people, the same
    number GET /notifications/unread-count gives each of them; 0 for whoever
    has none. None -- no badge -- with INBOX_SHARED on, whose shared lines
    are counted by a query per person, or when the counts cannot be read."""
    if settings.INBOX_SHARED:
        return None
    ids = sorted({int(u) for u in user_ids})
    counts = dict.fromkeys(ids, 0)
    try:
        async with session.begin_nested():
            for part in _chunks(ids, LOOKUP_BATCH):
                rows = await session.execute(
                    select(Notifications.user_id, sa.func.count())
                    .where(
                        Notifications.user_id == sa.any_(int_array(part)),
                        Notifications.status == NotificationStatus.unread,
                        Notifications.is_deleted == False,  # noqa: E712
                    )
                    .group_by(Notifications.user_id)
                )
                counts.update({int(u): int(n) for u, n in rows.all()})
    except Exception:  # noqa: BLE001 -- a push is never stopped by its badge
        logger.warning("No badges for %d users: the unread counts could not be read", len(ids), exc_info=True)
        return None
    return counts


def badges_by_token(devices: list[tuple[int, str]], counts: Optional[dict[int, int]]) -> Optional[dict[str, int]]:
    """Each device's badge, its person's count; None when there are no counts."""
    if counts is None:
        return None
    return {token: counts[user_id] for user_id, token in devices if user_id in counts}


async def get_fcm_token_for_user(
    session: AsyncSession,
    user_id: int
) -> Optional[str]:
    """One of the user's tokens (the newest), or None. Sends go to every
    device: see get_fcm_tokens_for_users."""
    tokens = await get_fcm_tokens_for_users(session, [user_id])
    return tokens[-1] if tokens else None


def is_dead_token(exc: BaseException | None) -> bool:
    """Whether FCM's answer means this token will never take a message again:
    UNREGISTERED (the app was uninstalled, or the token rotated), or
    INVALID_ARGUMENT about the token itself. INVALID_ARGUMENT is also FCM's
    answer to a bad message (a payload too big, a bad image URL) -- every
    token fails that one the same way -- so only one that names the
    registration token condemns it."""
    if isinstance(exc, messaging.UnregisteredError):
        return True
    if isinstance(exc, firebase_exceptions.InvalidArgumentError):
        return "registration token" in str(exc).lower()
    return False


async def prune_tokens(session: AsyncSession, tokens: list[str]) -> int:
    """Delete the rows for tokens FCM said are dead, for every account that
    has them, in one statement however many there are. Never raises: a push
    must not fail what it was telling about. Commits, so the rows stay gone
    even when the caller has nothing left to commit (a push is sent after the
    change it announces is kept)."""
    tokens = sorted(set(t for t in tokens if t))
    if not tokens:
        return 0
    try:
        async with session.begin_nested():
            result = await session.execute(
                delete(UserDeviceTokens)
                .where(UserDeviceTokens.fcm_token == sa.any_(sa.literal(tokens, ARRAY(sa.String))))
                .execution_options(include_deleted=True, synchronize_session=False)
            )
        await session.commit()
        removed = int(result.rowcount or 0)
        logger.info("Removed %d dead FCM token rows", removed)
        return removed
    except Exception:  # noqa: BLE001
        logger.warning("Dead FCM tokens could not be removed", exc_info=True)
        return 0


async def send_notification_to_user(
    session: AsyncSession,
    user_id: int,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """A push to every device the user is signed in on, its badge their
    unread count (unread_badge: the push's own inbox line included when it
    was written first, as every caller does). True when at least one device
    took it."""
    tokens = await get_fcm_tokens_for_users(session, [user_id])
    if not tokens:
        logger.debug("No FCM token found for user %s", user_id)
        return False

    badge = await unread_badge(session, user_id)
    dead: list[str] = []
    if len(tokens) == 1:
        sent = 1 if await _send_to_token(tokens[0], title, body, data, dead=dead, badge=badge) else 0
    else:
        badges = None if badge is None else dict.fromkeys(tokens, badge)
        sent = await _send_to_tokens(tokens, title, body, data, dead=dead, badges=badges)
    await prune_tokens(session, dead)
    return sent > 0


async def send_notification_to_users(
    session: AsyncSession,
    user_ids: list[int],
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> int:
    """A push to every device of these users, each device's badge its
    person's unread count (unread_badges; none with INBOX_SHARED on).
    Returns how many devices took it.

    The token lookups, the counts and the sends go in batches, however many
    users there are; a batch that fails is logged and the rest still go."""
    if not user_ids:
        return 0
    ids = sorted({int(u) for u in user_ids})
    sent = tried = 0
    dead: list[str] = []
    for part in _chunks(ids, LOOKUP_BATCH):
        devices = await get_fcm_devices_for_users(session, part)
        tried += len(devices)
        if devices:
            counts = await unread_badges(session, {user_id for user_id, _ in devices})
            tokens = [token for _, token in devices]
            sent += await _send_to_tokens(
                tokens, title, body, data, image_url, dead=dead, badges=badges_by_token(devices, counts),
            )
    if not tried:
        logger.debug("No FCM tokens found for %d users", len(ids))
        return 0
    await prune_tokens(session, dead)
    report_outcome(tried, sent, what=title)
    return sent


def report_outcome(tried: int, sent: int, *, what: str) -> None:
    """Says, at a level someone will see, when devices were there and nothing
    reached any of them -- the failure that went unnoticed for months."""
    if tried and not sent:
        logger.error("PUSH FAILED: none of %d devices took %r (the warnings above say why)", tried, what)
    else:
        logger.info("Push %r: %d of %d devices", what, sent, tried)


def _tag(data: Optional[dict]) -> Optional[str]:
    """A broadcast's push_id (app.core.notifications.delivery), when the data
    has one: Android's notification tag and APNs' collapse id, so a second
    copy of the same push replaces the first in the tray instead of stacking."""
    value = (data or {}).get("push_id")
    return str(value)[:64] if value else None


def _apns(image_url: Optional[str], tag: Optional[str], badge: Optional[int] = None) -> messaging.APNSConfig:
    """`badge`: the number for the app's icon, the person's unread count; None
    leaves the key out, and the icon as it was."""
    return messaging.APNSConfig(
        headers={"apns-collapse-id": tag} if tag else None,
        payload=messaging.APNSPayload(aps=messaging.Aps(sound="default", badge=None if badge is None else max(0, int(badge)))),
        fcm_options=messaging.APNSFCMOptions(image=image_url) if image_url else None,
    )


def _android(image_url: Optional[str], tag: Optional[str]) -> messaging.AndroidConfig:
    return messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(
            sound="default",
            click_action="FLUTTER_NOTIFICATION_CLICK",
            image=image_url,
            tag=tag,
        ),
    )


def _message(
    token: str,
    title: str,
    body: str,
    data: Optional[dict],
    image_url: Optional[str] = None,
    badge: Optional[int] = None,
) -> messaging.Message:
    """One device's message: what _multicast sends each of its devices, with
    this device's badge."""
    tag = _tag(data)
    return messaging.Message(
        notification=messaging.Notification(title=title, body=body, image=image_url),
        data=data or {},
        token=token,
        android=_android(image_url, tag),
        apns=_apns(image_url, tag, badge),
    )


async def _send_to_token(
    token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    dead: Optional[list] = None,
    badge: Optional[int] = None,
) -> bool:
    """Send notification to a single FCM token, on a worker thread. A token
    FCM says is dead is added to `dead`, for the caller to remove
    (prune_tokens). `badge`: its person's unread count (unread_badge), or
    None for none."""
    if not initialize_firebase():
        logger.error("Firebase not initialized, cannot send notification")
        return False

    try:
        response = await asyncio.to_thread(messaging.send, _message(token, title, body, data, badge=badge))
        logger.info(f"Successfully sent notification: {response}")
        return True
    except Exception as e:
        if is_dead_token(e):
            logger.warning(f"Token is dead ({type(e).__name__}), removing it: {token[:20]}...")
            if dead is not None:
                dead.append(token)
        else:
            logger.error(f"Failed to send notification: {e}")
        return False


def _multicast(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict],
    image_url: Optional[str],
    badge: Optional[int] = None,
) -> messaging.MulticastMessage:
    tag = _tag(data)
    return messaging.MulticastMessage(
        notification=messaging.Notification(title=title, body=body, image=image_url),
        data=data or {},
        tokens=tokens,
        android=_android(image_url, tag),
        apns=_apns(image_url, tag, badge),
    )


async def _send_to_tokens(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
    dead: Optional[list] = None,
    badges: Optional[Mapping[str, int]] = None,
) -> int:
    """Send to any number of tokens, FCM_BATCH at a time, each batch on a
    worker thread. Returns how many devices took it. Tokens FCM says are dead
    are added to `dead`, for the caller to remove.

    `badges`: each token's badge, its person's unread count; a token not in
    it (or no `badges`) goes without one. A batch whose tokens all have the
    same badge is one multicast; one with different badges is one
    `send_each` of a message per token -- what firebase-admin makes of a
    multicast itself -- so it is still one call per FCM_BATCH devices."""
    if not tokens:
        return 0

    if not initialize_firebase():
        logger.error("Firebase not initialized, cannot send notifications to %d devices", len(tokens))
        return 0

    sent = 0
    for batch in _chunks(list(tokens), FCM_BATCH):
        counts = {badges.get(token) for token in batch} if badges else {None}
        try:
            if len(counts) == 1:
                response = await asyncio.to_thread(
                    messaging.send_each_for_multicast, _multicast(batch, title, body, data, image_url, badge=counts.pop())
                )
            else:
                response = await asyncio.to_thread(
                    messaging.send_each,
                    [_message(token, title, body, data, image_url, badge=badges.get(token)) for token in batch],
                )
        except Exception:  # noqa: BLE001
            logger.exception("A batch of %d pushes could not be sent", len(batch))
            continue
        sent += response.success_count
        if response.failure_count > 0:
            for idx, send_response in enumerate(response.responses):
                if not send_response.success:
                    logger.warning("Failed to send to token %d: %s", idx, send_response.exception)
                    if dead is not None and is_dead_token(send_response.exception):
                        dead.append(batch[idx])
    logger.info("Batch send complete: %d of %d", sent, len(tokens))
    return sent


# ─── Topics (feat/w2-push) ──────────────────────────────────────────────────


class TopicSendFailed(Exception):
    """FCM did not take a topic send, for a reason that may pass (it was
    unavailable, over quota, answered with an internal error): the job that
    sent it is tried again (app.core.jobs)."""


#: FCM's answers that a retry cannot change: the message itself is wrong.
_PERMANENT = (
    firebase_exceptions.InvalidArgumentError, firebase_exceptions.PermissionDeniedError,
    firebase_exceptions.NotFoundError, firebase_exceptions.UnauthenticatedError,
)


async def send_to_topic(
    *,
    topic: Optional[str] = None,
    condition: Optional[str] = None,
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> Optional[str]:
    """One push to every device subscribed to `topic`, or matching
    `condition` ("('topic_tech' in topics) && !('club_12' in topics)"): one
    request, however many devices, on a worker thread. No badge: one message
    for everyone cannot carry each person's count. Returns FCM's message
    id, or None when Firebase is not set up or FCM refused the message as
    wrong (logged; trying again would not help). Raises TopicSendFailed for a
    failure that may pass, so the job retries it -- with the same push_id, so
    a device that did get the first shows it once."""
    if (topic is None) == (condition is None):
        raise ValueError("a topic send needs exactly one of topic and condition")
    if not initialize_firebase():
        logger.error("Firebase not initialized, cannot send %r to %s", title, topic or condition)
        return None
    tag = _tag(data)
    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body, image=image_url),
        data=data or {},
        topic=topic,
        condition=condition,
        android=_android(image_url, tag),
        apns=_apns(image_url, tag),
    )
    try:
        message_id = await asyncio.to_thread(messaging.send, message)
    except _PERMANENT as e:
        logger.error("PUSH FAILED: FCM refused %r to %s: %s", title, topic or condition, e)
        return None
    except Exception as e:  # noqa: BLE001 -- unavailable, quota, internal, a network error
        raise TopicSendFailed(f"{type(e).__name__}: {e}") from e
    logger.info("Push %r to %s: %s", title, topic or condition, message_id)
    return message_id


#: Tokens firebase-admin takes in one subscribe or unsubscribe call.
TOPIC_MANAGEMENT_BATCH = 1000


async def unsubscribe_from_topic(tokens: list[str], topic: str) -> int:
    """Take these devices out of `topic` (the server's side of a mute, an
    unfollow, a switch turned off: the person's other devices leave at once,
    not when each next opens the app). Returns how many FCM took out. Raises
    TopicSendFailed when a batch could not be sent at all, for the job to retry."""
    if not tokens or not initialize_firebase():
        return 0
    done = 0
    for batch in _chunks(list(tokens), TOPIC_MANAGEMENT_BATCH):
        try:
            response = await asyncio.to_thread(messaging.unsubscribe_from_topic, batch, topic)
        except Exception as e:  # noqa: BLE001
            raise TopicSendFailed(f"{type(e).__name__}: {e}") from e
        done += response.success_count
        for error in response.errors or ():
            logger.info("Token %d not taken out of %s: %s", error.index, topic, error.reason)
    return done
