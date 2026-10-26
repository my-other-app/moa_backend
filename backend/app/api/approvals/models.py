"""Registration requests: "Confirm each registration" (docs/APPROVALS.md).

When a club turns on `events.requires_approval`, the public form makes a
request instead of a registration. A request holds no place: places are taken
only when the club confirms it (a free ticket: registered then) or when the
person pays inside the hold a confirmation gives them (a paid ticket: an offer
in the waiting-list machinery, app/api/ticketing/waitlist.py).

Requests live in their own table, never in `event_registrations_link`, so
everything that reads registrations -- the door, certificates, journeys,
exports, the counts and the ledger -- never sees one until it is confirmed.

A request's life (`status`), forward only but for the club's reopen:

    pending ──confirm──> approved ──(paid: hold runs out)──> lapsed
       │                    │
       ├──decline──> declined          (reopen: declined or lapsed -> pending)
       ├──withdraw─> withdrawn         (approved and unpaid may withdraw too)
       └──event cancelled or over──> closed   (pending or approved)
"""
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

PENDING, APPROVED, DECLINED, WITHDRAWN, LAPSED, CLOSED = "pending", "approved", "declined", "withdrawn", "lapsed", "closed"
STATUSES = (PENDING, APPROVED, DECLINED, WITHDRAWN, LAPSED, CLOSED)
#: A person has at most one of these per event (address and account).
OPEN = (PENDING, APPROVED)
#: The statuses the club may put back in the queue.
REOPENABLE = (DECLINED, LAPSED)

_OPEN_SQL = "status IN ('pending', 'approved')"


class RegistrationRequests(AbstractSQLModel, TimestampsMixin):
    """Someone who filled an approval event's form: waiting for the club."""

    __tablename__ = "registration_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    #: The account, or the guest made at submission (as a registration makes one).
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    full_name = Column(String(100), nullable=False)
    # As wide as the registration's own columns: a confirmed request is one.
    email = Column(String(100), nullable=False)
    phone = Column(String(15), nullable=True)
    ticket_type_id = Column(Integer, ForeignKey("ticket_types.id", ondelete="SET NULL"), nullable=True)
    #: As typed (normalised); checked again when the club confirms.
    discount_code = Column(String(40), nullable=True)
    #: The form's answers (`additional_details`), checked at submission.
    answers = Column(JSONB, nullable=True)
    whatsapp_opt_in = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    #: "Get updates from <club> on WhatsApp", ticked: kept on confirmation.
    club_updates_whatsapp = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    #: The club's tracking link the page carried; credited on confirmation.
    tracking_link_id = Column(UUID(as_uuid=True), ForeignKey("tracking_links.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(12), nullable=False, default=PENDING, server_default=PENDING)
    #: The guest's status link's secret (url-safe).
    token = Column(String(64), nullable=False)
    #: The queue's order is (submitted_at, id).
    submitted_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
    decided_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    decided_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: Who decided, as the dashboard names them (a team member's own name).
    decided_by_name = Column(String(120), nullable=True)
    decline_note = Column(String(300), nullable=True)
    #: What a confirmed paid ticket costs, as confirmed (its code taken off).
    amount = Column(Numeric(10, 2), nullable=True)
    #: The registration, once there is one (free: at confirmation; paid: at the claim).
    registration_id = Column(
        UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True
    )
    #: A paid confirmation's hold: an offer in the waiting-list machinery.
    waitlist_entry_id = Column(UUID(as_uuid=True), ForeignKey("waitlist_entries.id", ondelete="SET NULL"), nullable=True)
    #: When the "request received" email last went again, for a form sent twice.
    resent_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The queue, its pages and its counts: an event's requests by status, oldest first.
        sa.Index("ix_registration_requests_queue", "event_id", "status", "submitted_at", "id"),
        # One open request per person per event: by address, and by account.
        sa.Index(
            "uq_registration_requests_open_email", "event_id", sa.func.lower(sa.func.btrim(email)),
            unique=True, postgresql_where=sa.text(_OPEN_SQL),
        ),
        sa.Index(
            "uq_registration_requests_open_user", "event_id", "user_id",
            unique=True, postgresql_where=sa.text(f"{_OPEN_SQL} AND user_id IS NOT NULL"),
        ),
        sa.Index("uq_registration_requests_token", "token", unique=True),
        # A person's own requests (GET /registration-requests/mine), newest first.
        sa.Index("ix_registration_requests_user", "user_id", "submitted_at", postgresql_where=sa.text("user_id IS NOT NULL")),
        # The holds a confirmation gave: the claim finds its request, the expiry the ones run out.
        sa.Index(
            "ix_registration_requests_hold", "waitlist_entry_id",
            postgresql_where=sa.text("waitlist_entry_id IS NOT NULL"),
        ),
        # The queue itself, the requests still waiting, in order: the club's
        # pending page walks it with no sort, and the sweep that closes them
        # once an event is over finds the events by it (a loose index scan:
        # a step per event, however many requests each has decided).
        sa.Index(
            "ix_registration_requests_pending", "event_id", "submitted_at", "id",
            postgresql_where=sa.text("status = 'pending'"),
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'declined', 'withdrawn', 'lapsed', 'closed')",
            name="ck_registration_requests_status",
        ),
    )
