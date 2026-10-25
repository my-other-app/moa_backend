"""Which pushes go to an FCM topic and which go device by device.

A push about one person -- their ticket, payment, reminder, check-in,
certificate, a role they were given, an announcement to the people
registered for an event -- goes to that person's devices, as it always has:
it carries their data, or its audience is a list only the server holds.

A broadcast is the same message for everyone it reaches, and its audience is
a set the device itself can name: the followers of a club, the people whose
interests are under a topic, everybody. Those go to an FCM topic -- one send,
however many devices -- when PUSH_TOPICS is on:

    club_{id}       a club's new post (club_thread) and new event (new_event),
                    to the club's followers
    topic_{slug}    a new event to the people whose interests are under one
                    of its topics (nearby_event), but not the club's
                    followers, who had it through club_{id}
    all             the platform's announcement to every device (platform)

The app subscribes to the topics GET /notifications/topics lists for its
person (app.core.notifications.topics), so the server decides who is in each.

A kind not named here goes device by device: a new kind (a payment's, a
guest's) is personal until someone decides otherwise.

Every broadcast push carries a `push_id` in its data, the same however many
times and ways it is sent (a job run again, a topic send and a device send in
the change-over): the app shows and opens a push with a given id once, and
Android's notification tag and APNs' collapse id are set to it, so even a
copy the app never sees replaces the first in the tray rather than stacking.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional


class Delivery(str, Enum):
    TOPIC = "topic"
    DEVICE = "device"


#: The broadcast kinds, and whose topic each goes to.
BROADCASTS: dict[str, str] = {
    "club_thread": "club",
    "new_event": "club",
    "nearby_event": "topic",
    "platform": "all",
}

#: The kinds that stay device by device, each for its reason. Listed so a test
#: can check every kind the app knows was decided, not defaulted.
PERSONAL: dict[str, str] = {
    "event_registered": "your registration",
    "event_reminder": "a reminder of your registration",
    "event_started": "an event you are registered for",
    "event_checkin": "your check-in",
    "certificate_generated": "your certificate",
    "event_invited": "an invitation to you",
    "volunteer_added": "a role you were given",
    "volunteer_removed": "a role you were given",
    # To an event's registrants, attendees or no-shows: a list only the
    # server holds, and the app subscribes to nothing per registration.
    "club_announcement": "an event's registrants",
    "event_live_update": "an event's registrants",
    # The quiz starting or a poll going live, to whoever is checked in: a
    # list only the server holds (app/api/live/notify.py).
    "event_live_join": "an event you are checked in to",
    # The event going live, to everyone registered; Q&A opening, to whoever
    # is checked in (app/api/live/notify.py).
    "event_live_started": "an event you are registered for",
    "event_live_qa": "an event you are checked in to",
}

ALL_TOPIC = "all"

# FCM's topic names: letters, digits and -_.~% only.
_TOPIC_SAFE = re.compile(r"[^A-Za-z0-9\-_.~%]")


def delivery_for(kind: Optional[str]) -> Delivery:
    """How a push of this kind goes: TOPIC for a broadcast, DEVICE otherwise."""
    return Delivery.TOPIC if kind in BROADCASTS else Delivery.DEVICE


def club_topic(club_id: int) -> str:
    return f"club_{int(club_id)}"


def topic_topic(slug: str) -> str:
    """A topic's FCM topic. Slugs are already a-z, 0-9 and hyphens
    (app.api.interests.models.slugify); anything else is dropped here so a
    hand-made slug cannot make a name FCM refuses."""
    return "topic_" + _TOPIC_SAFE.sub("", slug or "")


def push_id(kind: str, ref) -> str:
    """The id of a broadcast and its push: the same for every send of it.
    At most 64 characters (APNs' collapse id)."""
    return f"{kind}:{ref}"[:64]


def condition(topics: list[str], *, unless: Optional[str] = None) -> str:
    """An FCM condition for "in any of `topics`, and not in `unless`". FCM
    takes at most five topics in one condition, so at most four of `topics`
    with `unless` (the caller passes them in order of importance). One send
    to an OR of topics reaches a device once, however many of them it is in."""
    room = 5 - (1 if unless else 0)
    names = [t for t in dict.fromkeys(topics)][:room]
    if not names:
        raise ValueError("a condition needs at least one topic")
    anyof = " || ".join(f"'{t}' in topics" for t in names)
    if unless:
        return f"({anyof}) && !('{unless}' in topics)"
    return anyof
