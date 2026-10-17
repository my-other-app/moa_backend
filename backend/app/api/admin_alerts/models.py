"""What the platform tells its admins: a club applied, a payment failed, a
club's grace ran out.

One row per alert, read or not. Shared by every admin: marking one read marks
it read for the team, the way a shared inbox works -- the point is that
somebody looked, not who.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AdminNotifications(AbstractSQLModel):
    __tablename__ = "admin_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: What happened, as a stable machine name (`payment_failed`, ...): see
    #: admin_alerts.service.KINDS.
    kind = Column(String(40), nullable=False)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False, default="", server_default="")

    # Nulled rather than cascaded: an alert outlives what it was about, and
    # "a club we no longer have failed a payment" is still a true line.
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True)
    subscription_id = Column(
        Integer, ForeignKey("club_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    #: Whatever the kind carries: the plan, the amount, the failure reason.
    data = Column(JSON, nullable=True)

    created_at = Column(
        TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    read_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    club = relationship("Clubs", foreign_keys=[club_id])

    __table_args__ = (
        # The bell: unread first, newest first.
        Index("ix_admin_notifications_read_created", "read_at", text("created_at DESC")),
        # The ten-minute duplicate check (admin_alerts.service.notify).
        Index("ix_admin_notifications_dedupe", "kind", "subscription_id", "created_at"),
    )
