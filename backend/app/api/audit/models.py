"""Record of what admins did.

The admin dashboard can delete clubs and events, approve and reject
registrations, award and revoke badges, block organisations, record winners
and refund money -- and until this table there was no record of any of it. An
incident ("who deleted that club?") had no answer, and none could be
reconstructed, because nothing was written down.

Deliberately append-only: nothing in the application updates or deletes a row.
A log an admin can edit answers the question it exists to answer only when
nobody wants it to.
"""

from sqlalchemy import Column, ForeignKey, Integer, String, JSON, Index
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class AdminAuditLogs(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "admin_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Kept alongside the foreign key on purpose: an account can be deleted and
    # the log still has to say who acted. A dangling id is not an answer.
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_email = Column(String(100), nullable=True)

    action = Column(String(60), nullable=False, index=True)
    entity_type = Column(String(40), nullable=True, index=True)
    entity_id = Column(String(64), nullable=True)

    # One line an admin can read without expanding anything.
    summary = Column(String(255), nullable=True)
    # Whatever the action needs: amounts, before/after, the impersonated user.
    details = Column(JSON, nullable=True)

    ip_address = Column(String(45), nullable=True)

    actor = relationship("Users", foreign_keys=[actor_id])

    __table_args__ = (
        # The two reads this table gets: the newest page, and everything that
        # happened to one record.
        Index("ix_audit_created_at", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )
