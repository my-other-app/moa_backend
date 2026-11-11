"""A guest giving their ticket back by the link in their email
(app/api/ticket_cancel, feature-register/ticket-self-cancel-contract.md).

One row a cancelled registration: who, when and why, kept after the
registration is taken off the list (soft deleted, as the club removing it
does), so the club sees it in its list of tickets given back.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel


class TicketCancellations(AbstractSQLModel):
    __tablename__ = "ticket_cancellations"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    registration_id = Column(
        UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False, unique=True,
    )
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: As the registration had them when it was given back.
    full_name = Column(String(100), nullable=False)
    email = Column(String(100), nullable=False)
    ticket_id = Column(String(60), nullable=False)
    #: What the guest wrote, if anything.
    reason = Column(String(300), nullable=True)
    cancelled_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("now()"))

    __table_args__ = (sa.Index("ix_ticket_cancellations_event", "event_id", "cancelled_at"),)
