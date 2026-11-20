"""Doors, and every admission through one.

A door is a place where people are admitted. Check-ins, volunteers and
(later) printers and kiosks point at one, so a door can be renamed, retired
and reported on without its label ever being retyped. A check-in is one
row per guest: where, how and by whom. The registration keeps its
`is_attended` mirror so everything that reads it keeps working.
"""
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import UUID, Boolean, Column, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class DoorKind(enum.Enum):
    door = "door"
    desk = "desk"
    kiosk = "kiosk"


class DoorMode(enum.Enum):
    """Whether the door admits anyone right now."""

    open = "open"
    # Open from a day before the start until `close_after_minutes` past the end.
    auto = "auto"
    closed = "closed"


class CheckInVia(enum.Enum):
    gate = "gate"
    kiosk = "kiosk"
    walkup = "walkup"
    offline_sync = "offline_sync"
    desk = "desk"
    phone = "phone"
    # The volunteer app.
    app = "app"
    # The Integrations API (app/api/partner): a club's own system or a partner's.
    api = "api"


class Doors(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "doors"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    name = Column(String(40), nullable=False)
    kind = Column(Enum(DoorKind), nullable=False, default=DoorKind.door)
    sort_order = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
    # The gate link: a key a volunteer's phone opens the console with. Only
    # its hash is kept; the prefix is shown so the organiser can tell links apart.
    link_hash = Column(String(64), nullable=True, unique=True)
    link_prefix = Column(String(8), nullable=True)
    link_can_walkup = Column(Boolean, nullable=False, default=False)
    link_expires_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # What the door is for: none for an entrance (every door made before
    # quests, and any made without one); "station" (a scan records a visit,
    # app/api/quests) or "swag_desk" (a scan shows the guest's rewards). A
    # station or a swag desk never admits anyone. Set when it is made.
    purpose = Column(String(16), nullable=True)

    event = relationship("Events")

    # One door per name on an event, whatever the case: a door is found by
    # lower(name) ("gate a" is "Gate A"), so that is what is unique.
    __table_args__ = (
        sa.Index("uq_doors_event_lower_name", event_id, sa.func.lower(name), unique=True),
        sa.CheckConstraint("purpose IS NULL OR purpose IN ('station', 'swag_desk')", name="ck_doors_purpose"),
    )


class CheckIns(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "check_ins"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    # By event through ix_check_ins_event_checked_in_at (below): a second
    # index on event_id alone would only cost a write per check-in.
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id"), nullable=False, unique=True)
    door_id = Column(UUID(as_uuid=True), ForeignKey("doors.id", ondelete="SET NULL"), nullable=True, index=True)
    door_label = Column(String(40), nullable=True)
    checked_in_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    via = Column(Enum(CheckInVia), nullable=False, default=CheckInVia.gate)
    # When the guest was admitted: the device's clock for a scan made offline.
    checked_in_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    scanned_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # From the device, so a retried scan admits once.
    client_ref = Column(String(64), nullable=True, unique=True)
    # The kiosk that made it, when one did: "this isn't me" may undo only its own.
    kiosk_device_id = Column(UUID(as_uuid=True), ForeignKey("kiosk_devices.id", ondelete="SET NULL"), nullable=True)

    registration = relationship("EventRegistrationsLink")
    door = relationship("Doors")
    by = relationship("Users")

    __table_args__ = (
        # An event's check-ins, in time order (made by add_doors_check_ins).
        sa.Index("ix_check_ins_event_checked_in_at", "event_id", "checked_in_at"),
    )
