"""Clubs co-hosting another club's event (docs/COHOSTS.md).

The event stays the host's (`events.club_id`); a row here is one other
club's invite to it, and that club's standing on it: invited, then accepted
(a co-host), or declined, left, removed, cancelled or expired. A club that
said no, left or was removed can be asked again: that is a new row. At most
one live row (invited or accepted) per event and club, which the partial
unique index holds whatever the races.
"""
from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, String, text

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

INVITED = "invited"
ACCEPTED = "accepted"
DECLINED = "declined"
LEFT = "left"
REMOVED = "removed"
CANCELLED = "cancelled"
EXPIRED = "expired"
STATUSES = (INVITED, ACCEPTED, DECLINED, LEFT, REMOVED, CANCELLED, EXPIRED)
#: A row that counts against the limit and blocks another invite.
LIVE = (INVITED, ACCEPTED)

#: Live invites (pending and accepted) an event may have.
MAX_PER_EVENT = 5
#: An invite lapses this many days after it is sent, or when the event ends.
INVITE_DAYS = 14
NOTE_MAX = 300


class EventCohosts(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "event_cohosts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    #: The invited club, a co-host once it accepts.
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(16), nullable=False, default=INVITED, server_default=INVITED)
    note = Column(String(NOTE_MAX), nullable=True)
    #: Who sent it: the host's account, and the team member when one did.
    invited_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    invited_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    invited_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    #: Accepted or declined.
    responded_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Left, removed, cancelled or expired.
    ended_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When the co-host's followers were told about the event (once).
    followers_told_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('invited', 'accepted', 'declined', 'left', 'removed', 'cancelled', 'expired')",
            name="ck_event_cohosts_status",
        ),
        Index(
            "uq_event_cohosts_live", "event_id", "club_id", unique=True,
            postgresql_where=text("status IN ('invited', 'accepted')"),
        ),
        # A club's co-hosted events, and its pending invitations.
        Index("ix_event_cohosts_club_status", "club_id", "status"),
    )
