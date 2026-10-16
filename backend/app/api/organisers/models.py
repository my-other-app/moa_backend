"""The people from the club's team who run one event, as its public page lists them."""
from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class EventTeamMembers(AbstractSQLModel, TimestampsMixin):
    """One of the club's team named as an organiser of an event, with what they do at it.

    The set is replaced whole each time it is saved, so a row is deleted, not soft-deleted."""

    __tablename__ = "event_team_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("club_team_members.id", ondelete="CASCADE"), nullable=False)
    # Their part in this event ("Event lead"); without one, the page shows their club role.
    role = Column(String(80), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)

    member = relationship("ClubTeamMembers")

    __table_args__ = (UniqueConstraint("event_id", "member_id", name="uq_event_team_members_event_member"),)
