"""The tables the club tools keep (app/api/club_ops).

- competition_placings: a team's 1st, 2nd or 3rd place within its track.
  Its members' registrations carry the placing too (`position`), which is
  what certificates print and the badge rules count.
- event_cancellations: an event the club cancelled, once: why, by whom, and
  how far telling the guests got.
- club_refunds: money a club gave back, whole or in part, through the
  account the payment was taken on -- asked by the club on the event's
  Payments, or by cancelling the event. A refund Razorpay refused stays here
  as failed, with the reason, until someone tries it again.
- guest_notices: one row per guest, channel and thing told (a cancellation,
  a refund), claimed before the send: so each guest is told once.
- club_password_reset_codes: a forgotten password's six-digit code, for the
  clubs dashboard's logins (the club's own account, or a team member).
"""
import uuid

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class CompetitionPlacings(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "competition_placings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    competition_id = Column(Integer, ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True)
    team_id = Column(Integer, ForeignKey("competition_teams.id", ondelete="CASCADE"), nullable=False, unique=True)
    #: The team's track when it was placed (a place is within a track).
    track_id = Column(Integer, ForeignKey("competition_tracks.id", ondelete="SET NULL"), nullable=True)
    position = Column(Integer, nullable=False)
    set_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (sa.CheckConstraint("position BETWEEN 1 AND 3", name="ck_competition_placings_position"),)


#: club_refunds.source
CLUB = "club"
CANCELLATION = "cancellation"
#: club_refunds.status. PROCESSING: Razorpay is being asked right now (shown
#: as pending); one left there by a crash is retried once it is STALE.
PENDING = "pending"
PROCESSING = "processing"
SUCCEEDED = "succeeded"
FAILED = "failed"


class ClubRefunds(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_refunds"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id"), nullable=True)
    payment_log_id = Column(UUID(as_uuid=True), ForeignKey("payment_logs.id"), nullable=False, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    reason = Column(String(500), nullable=False)
    source = Column(String(16), nullable=False, default=CLUB)
    status = Column(String(16), nullable=False, default=PENDING)
    razorpay_refund_id = Column(String(64), nullable=True)
    error = Column(String(300), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    #: The dialog's own id: the same click twice gives one refund.
    client_ref = Column(UUID(as_uuid=True), nullable=True, unique=True)
    #: Who asked: the club's account (or the admin acting as it), and the
    #: team member when it was one.
    requested_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    impersonated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    requested_by_name = Column(String(120), nullable=True)
    processed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The guest was told (claimed before the mail goes: once).
    notified_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="ck_club_refunds_amount"),
        # Cancelling refunds each payment once, however often it is asked.
        Index("uq_club_refunds_cancellation_payment", "payment_log_id", unique=True,
              postgresql_where=sa.text("source = 'cancellation'")),
        Index("ix_club_refunds_open", "event_id", postgresql_where=sa.text("status <> 'succeeded'")),
    )


class EventCancellations(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "event_cancellations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, unique=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=True, index=True)
    reason = Column(String(500), nullable=False)
    previous_status = Column(String(16), nullable=True)
    cancelled_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    cancelled_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    cancelled_by_name = Column(String(120), nullable=True)
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    impersonated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    registrations = Column(Integer, nullable=False, default=0)
    #: The email to every guest, in the event's Messages.
    message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    #: The app push went (claimed first: once), and to how many devices.
    push_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    push_sent = Column(Integer, nullable=True)


#: guest_notices.channel
PUSH = "push"
WHATSAPP = "whatsapp"
EMAIL = "email"


class GuestNotices(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "guest_notices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: What was told: "cancel:<event id>" or "refund:<refund id>".
    ref = Column(String(80), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id"), nullable=False)
    channel = Column(String(16), nullable=False)
    #: sending -> sent | skipped | failed
    status = Column(String(16), nullable=False, default="sending")
    error = Column(String(300), nullable=True)
    provider_id = Column(String(200), nullable=True)

    __table_args__ = (UniqueConstraint("ref", "registration_id", "channel", name="uq_guest_notices_ref_registration_channel"),)


class ClubPasswordResetCodes(AbstractSQLModel):
    """One code for an address on the clubs dashboard. As password_reset_codes
    (app/api/auth/password_reset.py): only a keyed hash of the address and of
    the code, a row whether or not there is an account behind the address."""

    __tablename__ = "club_password_reset_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_key = Column(String(64), nullable=False)
    #: The club account the code was sent to, or the team member; neither
    #: when the address has no club login (the code went nowhere).
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True)
    code_hash = Column(String(64), nullable=False)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    expires_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    closed_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_club_password_reset_codes_key_sent", "email_key", "sent_at"),
        Index("ix_club_password_reset_codes_sent", "sent_at"),
    )
