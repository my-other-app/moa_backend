"""What the platform tells a club in its dashboard: the bell.

One row per notice, read or not. Shared by everyone who signs in to the
club's dashboard (the owner and the team), the way the admins' bell is
shared: the point is that somebody at the club saw it.

`dedupe_key` makes a notice once-only: the same key for the same club is
one row however often it is raised (a monthly message quota's 80% warning
is `messages:2026-10:email:80`).
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ClubAlerts(AbstractSQLModel):
    __tablename__ = "club_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    #: What it is about, as a stable machine name (club_alerts.service.KINDS).
    kind = Column(String(40), nullable=False)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False, default="", server_default="")
    #: Where in the dashboard it leads ("/billing"), or null.
    link = Column(String(300), nullable=True)
    data = Column(JSON, nullable=True)
    #: Once per club: a second notice with the same key is not written.
    dedupe_key = Column(String(160), nullable=True)
    created_at = Column(
        TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()")
    )
    read_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("club_id", "dedupe_key", name="uq_club_alerts_once"),
        # The bell: one club's notices, newest first.
        Index("ix_club_alerts_club_created", "club_id", text("created_at DESC")),
    )
