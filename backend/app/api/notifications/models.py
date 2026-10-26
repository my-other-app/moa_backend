import uuid

from sqlalchemy import JSON, UUID, Boolean, Column, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin

from sqlalchemy.orm import relationship
import sqlalchemy as sa
from enum import Enum as PyEnum


class NotificationType(PyEnum):
    """All notification types supported by the system."""
    # Event-related
    certificate_generated = "certificate_generated"  # Certificate ready for download
    event_started = "event_started"                  # Event is starting soon
    event_invited = "event_invited"                  # User invited to event
    event_checkin = "event_checkin"                  # Check-in confirmation
    event_registered = "event_registered"            # Registration confirmation
    event_reminder = "event_reminder"                # Reminder before event
    
    # Club-related
    new_event = "new_event"                          # Followed club posted new event
    nearby_event = "nearby_event"                    # Event matching user interests
    club_announcement = "club_announcement"          # Club sent custom message
    
    # Volunteer-related
    volunteer_added = "volunteer_added"              # User added as volunteer
    volunteer_removed = "volunteer_removed"          # User removed as volunteer
    club_thread = "club_thread"

    # Registration requests (app/api/approvals): confirmed (free: opens the
    # ticket; paid: opens the claim to pay), and declined (an inbox item).
    request_approved = "request_approved"
    request_approved_pay = "request_approved_pay"
    request_declined = "request_declined"


class NotificationStatus(PyEnum):
    read = "read"
    unread = "unread"


class Notifications(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "notifications"

    # Made in Python, so the ORM can write a batch of these in one statement:
    # with the SQL default it had to fetch each new key back, one INSERT per row.
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    type = Column(String(50), nullable=False)
    from_club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    from_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Indexed since add_event_id_notifications (ix_notifications_event_id); declared here too.
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)  # Link to relevant event
    status = Column(
        Enum(NotificationStatus), nullable=False, default=NotificationStatus.unread
    )
    data = Column(JSON, nullable=True)  # Additional data like certificate_id, etc.

    # Relationships
    user = relationship("Users", back_populates="notifications", foreign_keys=[user_id])
    from_club = relationship("Clubs", foreign_keys=[from_club_id])
    from_user = relationship("Users", foreign_keys=[from_user_id])
    event = relationship("Events", foreign_keys=[event_id])

    __table_args__ = (
        # The inbox, newest first, and its unread count: both by person. Without
        # these every inbox read, count and mark-all-read walked the whole table.
        sa.Index(
            "ix_notifications_user_created",
            "user_id",
            sa.text("created_at DESC"),
            postgresql_where=sa.text("NOT is_deleted"),
        ),
        sa.Index(
            "ix_notifications_user_unread",
            "user_id",
            postgresql_where=sa.text("status = 'unread' AND NOT is_deleted"),
        ),
    )


# ─── Broadcasts (feat/w2-push) ──────────────────────────────────────────────


class NotificationBroadcasts(AbstractSQLModel, TimestampsMixin):
    """One broadcast -- a club's post or new event to its followers, a new
    event to people by topic, an announcement to everyone
    (app.core.notifications.delivery) -- written once, however many it
    reaches. Written whenever PUSH_TOPICS or INBOX_SHARED is on.

    With `shared`, this row is every recipient's inbox line for it, read
    through their follows and interests (notification_broadcast_targets,
    app.core.notifications.inbox); without, each recipient also got a row in
    `notifications`, and this one only records the send."""

    __tablename__ = "notification_broadcasts"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    # The push's id, the same for every send of it ("new_event:42",
    # "club_thread:<uuid>"). Unique: a job run again writes nothing twice.
    push_id = Column(String(64), nullable=False, unique=True)
    type = Column(String(50), nullable=False)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    from_club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True)
    from_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True)
    # The inbox line's data, as a row in `notifications` would hold it.
    data = Column(JSON, nullable=True)
    # Whom it went to: {"club": 12}, {"topics": [3, 5], "except_club": 12}, {"all": true}.
    audience = Column(JSONB, nullable=False)
    shared = Column(Boolean, nullable=False, default=False, server_default=sa.false())

    from_club = relationship("Clubs", foreign_keys=[from_club_id])
    from_user = relationship("Users", foreign_keys=[from_user_id])
    event = relationship("Events", foreign_keys=[event_id])

    __table_args__ = (
        # A topic's new-event pushes in the last day (INTEREST_TOPIC_PUSH_MAX_PER_DAY).
        sa.Index("ix_notification_broadcasts_type_created", "type", "created_at"),
    )


class NotificationBroadcastTargets(AbstractSQLModel):
    """Who reads a shared broadcast: one row per audience it went to
    ("club:12", "topic:5"), with the broadcast's time. A person's inbox walks,
    for each of their targets, that target's newest rows here (the feed index)
    and merges them with their own rows in `notifications`."""

    __tablename__ = "notification_broadcast_targets"

    broadcast_id = Column(UUID(as_uuid=True), ForeignKey("notification_broadcasts.id", ondelete="CASCADE"), primary_key=True)
    target = Column(String(40), primary_key=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    # A new event by topic is not for the club's followers: they had it as
    # the club's (new_event). Read: skipped for whoever followed this club
    # before the broadcast.
    except_club_id = Column(Integer, nullable=True)

    __table_args__ = (
        # One target's broadcasts, newest first, from the index alone.
        sa.Index(
            "ix_notification_broadcast_targets_feed", "target", sa.text("created_at DESC"), sa.text("broadcast_id DESC"),
            postgresql_include=["except_club_id"],
        ),
    )


class NotificationBroadcastStates(AbstractSQLModel):
    """What one person did with one shared broadcast: read it, dismissed it.
    Written only when they do; no row is unread (unless the broadcast is
    older than their notification_settings.read_through)."""

    __tablename__ = "notification_broadcast_states"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    broadcast_id = Column(UUID(as_uuid=True), ForeignKey("notification_broadcasts.id", ondelete="CASCADE"), primary_key=True)
    read_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    dismissed_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.Index("ix_notification_broadcast_states_broadcast", "broadcast_id"),
    )


class NotificationSettings(AbstractSQLModel):
    """A person's notification switches, and where they last read everything
    up to. One row, written only when they change a switch or mark all read;
    no row is every switch on and nothing read."""

    __tablename__ = "notification_settings"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # The followed clubs' new posts and events (their club_{id} topics).
    clubs = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    # New events under the topics of the person's interests (topic_{slug}).
    topics = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    # The platform's announcements (all).
    platform = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    # Every shared broadcast up to here counts as read (POST /notifications/read-all).
    read_through = Column(TZAwareDateTime(timezone=True), nullable=True)
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()"))
