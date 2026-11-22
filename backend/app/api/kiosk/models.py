"""Kiosks: a tablet at a door that guests check themselves in on, and the
phone sessions a kiosk's QR starts."""
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import UUID, Boolean, Column, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class SelfCheckInVerify(enum.Enum):
    """What a guest must prove to check themselves in from a phone."""

    name = "name"
    email = "email"
    phone = "phone"


class KioskDevices(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "kiosk_devices"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    name = Column(String(60), nullable=False)
    door_id = Column(UUID(as_uuid=True), ForeignKey("doors.id", ondelete="SET NULL"), nullable=True)
    door_label = Column(String(40), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    token_prefix = Column(String(8), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    last_seen_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    revoked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # Staff-PIN tries in the current window (kiosk/pins.py): counted by a
    # conditional UPDATE, so parallel guesses cannot slip past the limit.
    pin_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    pin_window_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # "kiosk" (a tablet guests use) or "printer" (a print station that claims card print requests).
    role = Column(String(16), nullable=False, default="kiosk", server_default="kiosk")

    event = relationship("Events")
    door = relationship("Doors")


class SelfCheckInSessions(AbstractSQLModel, TimestampsMixin):
    """A phone that scanned a kiosk's QR: ten minutes to name themselves,
    prove it as the event asks, and be admitted at the kiosk's door."""

    __tablename__ = "self_check_in_sessions"

    id = Column(String(64), primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    kiosk_id = Column(UUID(as_uuid=True), ForeignKey("kiosk_devices.id", ondelete="CASCADE"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id"), nullable=True)
    verified = Column(Boolean, nullable=False, default=False)
    lookups = Column(Integer, nullable=False, default=0)
    code_hash = Column(String(64), nullable=True)
    code_channel = Column(String(16), nullable=True)
    code_attempts = Column(Integer, nullable=False, default=0)
    code_sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    code_resent = Column(Boolean, nullable=False, default=False)
    checked_in = Column(Boolean, nullable=False, default=False)
    # Asked to print from the phone; the kiosk picks it up on its next poll.
    print_requested = Column(Boolean, nullable=False, default=False)
    printed = Column(Boolean, nullable=False, default=False)
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)

    registration = relationship("EventRegistrationsLink")
    kiosk = relationship("KioskDevices")


class CardPrintRequests(AbstractSQLModel, TimestampsMixin):
    """A guest's ID card waiting to be printed by a print station: queued by
    a kiosk, the club dashboard or the door console; claimed on a lease by a
    paired printer, which prints it and confirms."""

    __tablename__ = "card_print_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id"), nullable=False)
    # kiosk | dashboard | door
    source = Column(String(16), nullable=False)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    requested_by_device_id = Column(UUID(as_uuid=True), ForeignKey("kiosk_devices.id", ondelete="SET NULL"), nullable=True)
    # A chosen printer; null is any printer of the event.
    printer_id = Column(UUID(as_uuid=True), ForeignKey("kiosk_devices.id", ondelete="SET NULL"), nullable=True)
    # queued -> printing -> printed | failed; cancelled
    status = Column(String(16), nullable=False, default="queued", server_default="queued")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    claimed_by = Column(UUID(as_uuid=True), ForeignKey("kiosk_devices.id", ondelete="SET NULL"), nullable=True)
    claimed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    lease_until = Column(TZAwareDateTime(timezone=True), nullable=True)
    printed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    override = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    error = Column(String(400), nullable=True)

    registration = relationship("EventRegistrationsLink")
    printer = relationship("KioskDevices", foreign_keys=[printer_id])
    claimer = relationship("KioskDevices", foreign_keys=[claimed_by])

    __table_args__ = (
        sa.CheckConstraint("status IN ('queued', 'printing', 'printed', 'failed', 'cancelled')", name="ck_card_print_requests_status"),
        sa.Index("ix_card_print_requests_event_status", "event_id", "status", "created_at"),
        sa.Index("ix_card_print_requests_registration", "registration_id"),
        sa.Index("uq_card_print_requests_open", "event_id", "registration_id", unique=True, postgresql_where=sa.text("status IN ('queued', 'printing')")),
    )
