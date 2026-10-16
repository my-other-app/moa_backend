"""The club's ledger: what an event cost, and what came in besides tickets, set against the ticket money."""
import uuid

from sqlalchemy import UUID, Column, Date, ForeignKey, Integer, Numeric, String

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class ClubExpenses(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "club_expenses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    # Null means an overhead of the club itself: a domain, a subscription, a banner reused all year.
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True)
    # "expense" or "income": the ledger holds what went out and what came in besides tickets — sponsorship, a grant, a donation.
    kind = Column(String(10), nullable=False, default="expense", server_default="expense")
    title = Column(String(120), nullable=False)
    category = Column(String(40), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    incurred_on = Column(Date, nullable=False)
    note = Column(String(500), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
