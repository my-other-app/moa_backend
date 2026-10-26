"""Ticketing: an event's ticket types, a club's discount codes, and the
waiting lists for what has sold out (docs/TICKETING.md).

Every event has at least one ticket type. The migration `ticketing` gave each
event that existed one, its default, at the event's own price and capacity;
an event made since gets its default the first time anything asks for its
types (types.ensure). While an event has only that one type it *follows the
event*: its price is the event's fee and its capacity the event's
`max_participants`, exactly as before, so nothing changes for an event, or a
client, that never adds a second type.

A registration names its type (`event_registrations_link.ticket_type_id`).
A type's places taken are counted by the database (`taken_count`, kept by
the trigger below, as `events.going_count` and `paid_count` are): a
registration holds a place on its type while it is live and either paid or
owes nothing -- free, or made free by a discount code.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy import ARRAY, Boolean, CheckConstraint, Column, DDL, ForeignKey, Integer, Numeric, String
from sqlalchemy import event as sa_event
from sqlalchemy.dialects.postgresql import UUID

from app.api.events.models import EventRegistrationsLink
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class TicketTypes(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """One kind of ticket an event sells: "Early bird", "Student", "VIP"."""

    __tablename__ = "ticket_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(80), nullable=False)
    description = Column(String(500), nullable=True)
    #: Rupees; 0 is free.
    price = Column(Numeric(10, 2), nullable=False, default=0, server_default="0")
    #: Places of this type; null for no cap of its own (the event's total still holds).
    capacity = Column(Integer, nullable=True)
    #: When it is on sale; null for "with the event's registration".
    sales_start = Column(TZAwareDateTime(timezone=True), nullable=True)
    sales_end = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Not listed on the event's page; sold only to a request that names it.
    is_hidden = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    #: How many of this type one person may hold: one account, one email
    #: address or one phone number. Null for no limit beyond the one
    #: registration per account an event always allows.
    per_person_limit = Column(Integer, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    #: The type the event had before it had types (the migration's, or the
    #: first one asked for). One per event.
    is_default = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    #: Registrations holding a place of this type, kept by the database
    #: (TAKEN_COUNT_SQL): live, and paid or owing nothing.
    taken_count = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        sa.Index("ix_ticket_types_event", "event_id", "sort_order", "id", postgresql_where=sa.text("NOT is_deleted")),
        sa.Index(
            "uq_ticket_types_event_default", "event_id", unique=True,
            postgresql_where=sa.text("is_default AND NOT is_deleted"),
        ),
        CheckConstraint("price >= 0", name="ck_ticket_types_price"),
        CheckConstraint("capacity IS NULL OR capacity >= 0", name="ck_ticket_types_capacity"),
        CheckConstraint("per_person_limit IS NULL OR per_person_limit >= 1", name="ck_ticket_types_per_person"),
    )


#: A discount code takes a share off, or an amount off.
PERCENT, AMOUNT = "percent", "amount"


class DiscountCodes(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """A code a guest types at registration for a lower price. One event's
    (`event_id`), or every event of the club's (null)."""

    __tablename__ = "discount_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=True)
    #: As typed by the club, kept upper-case; matched ignoring case.
    code = Column(String(40), nullable=False)
    kind = Column(String(10), nullable=False)
    #: A percentage (0 to 100) or rupees off.
    value = Column(Numeric(10, 2), nullable=False)
    #: Only these of the event's types; null for any. An event's codes only.
    ticket_type_ids = Column(ARRAY(Integer), nullable=True)
    starts_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    ends_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Paid or confirmed registrations it may be used on; null for no limit.
    max_uses = Column(Integer, nullable=True)
    #: Uses by one person (an account, an email address, a phone number).
    per_person_limit = Column(Integer, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        sa.Index("uq_discount_codes_club_code", "club_id", "code", unique=True, postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ix_discount_codes_event", "event_id", postgresql_where=sa.text("NOT is_deleted")),
        CheckConstraint("kind IN ('percent', 'amount')", name="ck_discount_codes_kind"),
        CheckConstraint("value >= 0 AND (kind <> 'percent' OR value <= 100)", name="ck_discount_codes_value"),
        CheckConstraint("max_uses IS NULL OR max_uses >= 1", name="ck_discount_codes_max_uses"),
        CheckConstraint("per_person_limit IS NULL OR per_person_limit >= 1", name="ck_discount_codes_per_person"),
    )


# A waiting list entry's life: waiting in line, offered a place (a claim link
# that runs out), and then claimed (registered), expired (the link ran out;
# the place went to the next person), removed by the club, or left by the
# person themselves.
WAITING, OFFERED, CLAIMED, EXPIRED, REMOVED, LEFT = "waiting", "offered", "claimed", "expired", "removed", "left"
OPEN_STATUSES = (WAITING, OFFERED)


class WaitlistEntries(AbstractSQLModel, TimestampsMixin):
    """Someone waiting for a ticket of a sold-out type."""

    __tablename__ = "waitlist_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    ticket_type_id = Column(Integer, ForeignKey("ticket_types.id", ondelete="CASCADE"), nullable=False)
    #: When they joined signed in.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    full_name = Column(String(100), nullable=False)
    email = Column(String(254), nullable=True)
    phone = Column(String(20), nullable=True)
    #: "Tell me on WhatsApp too", ticked: the offer may go there as well.
    whatsapp_opt_in = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    status = Column(String(12), nullable=False, default=WAITING, server_default=WAITING)
    #: The claim link's secret, while an offer stands (a new one for each offer).
    offer_token = Column(String(64), nullable=True)
    offered_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    offer_expires_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Offers made to this entry so far (an offer the club made again counts).
    offers_made = Column(Integer, nullable=False, default=0, server_default="0")
    #: The registration a claim made (unpaid until its payment lands on a paid type).
    registration_id = Column(
        UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True
    )
    closed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The hold a club's confirmation gave a paid registration request
    #: (app/api/approvals): an offer like any other -- it holds its place,
    #: runs out, is claimed and paid -- but never one of the line's. The
    #: club's waiting list, its counts and "my places in line" leave it out.
    via_request = Column(Boolean, nullable=False, default=False, server_default=sa.false())

    __table_args__ = (
        # The line, in order, per type: who is next.
        sa.Index(
            "ix_waitlist_line", "event_id", "ticket_type_id", "created_at", "id",
            postgresql_where=sa.text("status IN ('waiting', 'offered')"),
        ),
        # The sweep's walk: offers that run out.
        sa.Index("ix_waitlist_offer_expiry", "offer_expires_at", postgresql_where=sa.text("status = 'offered'")),
        # The club's list of an event's entries, newest first.
        sa.Index("ix_waitlist_event_created", "event_id", "created_at"),
        sa.Index("uq_waitlist_offer_token", "offer_token", unique=True, postgresql_where=sa.text("offer_token IS NOT NULL")),
        # One place in line per address and type.
        sa.Index(
            "uq_waitlist_open_email", "ticket_type_id", sa.func.lower(sa.func.btrim(email)), unique=True,
            postgresql_where=sa.text("email IS NOT NULL AND status IN ('waiting', 'offered')"),
        ),
        CheckConstraint(
            "status IN ('waiting', 'offered', 'claimed', 'expired', 'removed', 'left')", name="ck_waitlist_status"
        ),
        CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="ck_waitlist_contact"),
    )


#: How long a claim link stands unless the club says otherwise.
DEFAULT_OFFER_HOURS = 24


class WaitlistSettings(AbstractSQLModel, TimestampsMixin):
    """An event's waiting list, as the club set it up. No row: off."""

    __tablename__ = "waitlist_settings"

    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    offer_hours = Column(Integer, nullable=False, default=DEFAULT_OFFER_HOURS, server_default=str(DEFAULT_OFFER_HOURS))

    __table_args__ = (CheckConstraint("offer_hours BETWEEN 1 AND 168", name="ck_waitlist_offer_hours"),)


# ─── ticket_types.taken_count, kept by the database ──────────────────────
#
# As events.going_count and paid_count are (events.models), for the same
# reason: registrations are written from many places, refunds and removals
# included, and a trigger sees every one in the writer's own transaction. A
# registration holds a place on its type while it is live and either paid or
# owes nothing (actual_amount 0: a free type, or a code that took it all).
# It fires after the event's own two counters (triggers of one kind run in
# name order), so a registration always takes the event's row, then the
# type's -- the order every path here takes them in.
# The migration `ticketing` installs the same function and trigger.

TAKEN_COUNT_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_ticket_type_taken() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      old_holds boolean := false;
      new_holds boolean := false;
    BEGIN
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        old_holds := OLD.ticket_type_id IS NOT NULL AND NOT OLD.is_deleted AND (OLD.is_paid OR OLD.actual_amount = 0);
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') THEN
        new_holds := NEW.ticket_type_id IS NOT NULL AND NOT NEW.is_deleted AND (NEW.is_paid OR NEW.actual_amount = 0);
      END IF;
      IF TG_OP = 'UPDATE' AND old_holds = new_holds AND OLD.ticket_type_id IS NOT DISTINCT FROM NEW.ticket_type_id THEN
        RETURN NULL;
      END IF;
      IF old_holds THEN
        UPDATE ticket_types SET taken_count = taken_count - 1 WHERE id = OLD.ticket_type_id;
      END IF;
      IF new_holds THEN
        UPDATE ticket_types SET taken_count = taken_count + 1 WHERE id = NEW.ticket_type_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_registrations_type_taken
    AFTER INSERT OR DELETE OR UPDATE OF ticket_type_id, is_deleted, is_paid, actual_amount ON event_registrations_link
    FOR EACH ROW EXECUTE FUNCTION moa_ticket_type_taken()
    """,
)

for _sql in TAKEN_COUNT_SQL:
    sa_event.listen(EventRegistrationsLink.__table__, "after_create", DDL(_sql).execute_if(dialect="postgresql"))
