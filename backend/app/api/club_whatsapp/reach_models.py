"""What a club's own WhatsApp number reaches beyond the inbox: what Meta
says it cost, who said yes to the club's updates, and the broadcasts it sends.

- `club_whatsapp_spend`: Meta's pricing analytics for one number's account
  and one month, as the sweep last read them (at most daily): volume and
  approximate cost by pricing category, in the account's currency. The
  settings page reads this, never Meta.
- `club_whatsapp_consents`: a person's yes to "Get updates from <club> on
  WhatsApp" -- from a registration form or a follow -- with the words they
  saw (`consent_text`, naming the club as Meta's opt-in rule asks) and their
  version. One live row per person per club (by user and by number);
  withdrawing (an unfollow, Meta's 131050) stamps `withdrawn_at`. A
  promotion goes only to people with a live one.
- `club_whatsapp_broadcasts`: an event update (an approved UTILITY template,
  to one event's registrants who asked for WhatsApp) or a promotion (an
  approved MARKETING template, to the people who consented), now or at
  `scheduled_at`. `variables` says what fills each of the template's values
  -- the person's name, the event's name or date, or the club's own words
  -- never per person. `results` keeps the counts once the recipients are
  gone (retention).
- `club_whatsapp_broadcast_recipients`: one row per person a broadcast is
  for, as its sender works through them: queued (perhaps `not_before`
  tomorrow, `held` by the messaging limit), sent, delivered, read, failed
  with Meta's code, or skipped. Deleted with the club's inbox retention.

Phone numbers and names here are personal data: shown only to the club's
team with the WhatsApp permission, never logged.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Spend ───────────────────────────────────────────────────────────────


class ClubWhatsAppSpend(AbstractSQLModel):
    __tablename__ = "club_whatsapp_spend"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("club_whatsapp_accounts.id", ondelete="CASCADE"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    #: The first day of the month it covers (India's calendar, as the dashboard's).
    month = Column(Date, nullable=False)
    #: The WhatsApp Business Account's currency, as Meta reports it (INR, USD ...).
    currency = Column(String(8), nullable=True)
    #: [{"category": "MARKETING", "volume": 120, "cost": 103.57}], Meta's figures summed by pricing category.
    categories = Column(JSON, nullable=False, default=list)
    volume = Column(BigInteger, nullable=False, default=0, server_default="0")
    cost = Column(Numeric(14, 4), nullable=False, default=0, server_default="0")
    fetched_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (UniqueConstraint("account_id", "month", name="uq_club_whatsapp_spend_month"),)


# ─── Consent to a club's updates ─────────────────────────────────────────

SOURCE_REGISTRATION = "registration"
SOURCE_FOLLOW = "follow"
CONSENT_SOURCES = (SOURCE_REGISTRATION, SOURCE_FOLLOW)


class ClubWhatsAppConsents(AbstractSQLModel):
    __tablename__ = "club_whatsapp_consents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True)
    #: Their number in digits with its country code, as WhatsApp names them;
    #: null for a follower whose profile has no number yet (their profile's is
    #: read when a promotion goes).
    wa_id = Column(String(20), nullable=True)
    bsuid = Column(String(160), nullable=True)
    #: registration | follow
    source = Column(String(16), nullable=False)
    #: The words they ticked, naming the club, and the version of them.
    text_version = Column(String(32), nullable=False)
    consent_text = Column(String(300), nullable=False)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    withdrawn_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: unfollow | follow | meta_131050 | ...
    withdrawn_source = Column(String(32), nullable=True)

    __table_args__ = (
        CheckConstraint("source IN ('registration', 'follow')", name="ck_club_whatsapp_consents_source"),
        # One live yes per person per club, by number and by account.
        Index("uq_club_whatsapp_consents_live_wa", "club_id", "wa_id", unique=True,
              postgresql_where=text("withdrawn_at IS NULL AND wa_id IS NOT NULL")),
        Index("uq_club_whatsapp_consents_live_user", "club_id", "user_id", unique=True,
              postgresql_where=text("withdrawn_at IS NULL AND user_id IS NOT NULL")),
        # A promotion's audience: the club's live consents.
        Index("ix_club_whatsapp_consents_club_live", "club_id", "id", postgresql_where=text("withdrawn_at IS NULL")),
        Index("ix_club_whatsapp_consents_user", "user_id", postgresql_where=text("user_id IS NOT NULL")),
        Index("ix_club_whatsapp_consents_registration", "registration_id", postgresql_where=text("registration_id IS NOT NULL")),
    )


# ─── Broadcasts ──────────────────────────────────────────────────────────

EVENT_UPDATE = "event_update"
PROMOTION = "promotion"
KINDS = (EVENT_UPDATE, PROMOTION)

SCHEDULED = "scheduled"
SENDING = "sending"
DONE = "done"
CANCELLED = "cancelled"
FAILED = "failed"
BROADCAST_STATUSES = (SCHEDULED, SENDING, DONE, CANCELLED, FAILED)

#: A recipient's way through the sender.
QUEUED = "queued"
CLAIMED = "sending"
SENT = "sent"
DELIVERED = "delivered"
READ = "read"
RECIPIENT_FAILED = "failed"
SKIPPED = "skipped"
#: The order a status only moves forward in (Meta's late "delivered" never undoes "read").
STATUS_ORDER = {QUEUED: 0, CLAIMED: 0, SENT: 1, DELIVERED: 2, READ: 3}

ENDPOINT_MESSAGES = "messages"
ENDPOINT_MARKETING = "marketing_messages"


class ClubWhatsAppBroadcasts(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_whatsapp_broadcasts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("club_whatsapp_accounts.id", ondelete="SET NULL"), nullable=True)
    #: event_update (UTILITY) | promotion (MARKETING): the template's category decides.
    kind = Column(String(16), nullable=False)
    template_name = Column(String(512), nullable=False)
    template_language = Column(String(16), nullable=False)
    category = Column(String(32), nullable=False)
    #: The event an update is about (and whose name or date a promotion may name).
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    #: What fills each of the template's values: {"header": [v], "body": [v], "buttons": {"0": v}},
    #: each v {"source": "name" | "event_name" | "event_date" | "text", "value": "..."}.
    variables = Column(JSON, nullable=False, default=dict)
    status = Column(String(12), nullable=False)
    scheduled_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    started_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    finished_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When its audience was read and its recipients written (as it starts sending).
    materialized_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: messages | marketing_messages: where a MARKETING template goes (Meta's MM API once the account is onboarded).
    endpoint = Column(String(24), nullable=True)
    #: What the composer showed before sending: {"recipients", "rate", "currency", "subtotal", "gst", "total"}.
    estimate = Column(JSON, nullable=True)
    #: Left out when the audience was read: they asked to stop, never said yes, or have no number WhatsApp reaches.
    skipped_opted_out = Column(Integer, nullable=False, default=0, server_default="0")
    skipped_no_consent = Column(Integer, nullable=False, default=0, server_default="0")
    skipped_no_number = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(String(300), nullable=True)
    #: The counts as they stood when the recipients were deleted (retention).
    results = Column(JSON, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    created_by_name = Column(String(120), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('event_update', 'promotion')", name="ck_club_whatsapp_broadcasts_kind"),
        CheckConstraint("status IN ('scheduled', 'sending', 'done', 'cancelled', 'failed')", name="ck_club_whatsapp_broadcasts_status"),
        Index("ix_club_whatsapp_broadcasts_club", "club_id", text("id DESC")),
        Index("ix_club_whatsapp_broadcasts_account", "account_id", postgresql_where=text("account_id IS NOT NULL")),
        Index("ix_club_whatsapp_broadcasts_event", "event_id", postgresql_where=text("event_id IS NOT NULL")),
        Index("ix_club_whatsapp_broadcasts_created_by", "created_by", postgresql_where=text("created_by IS NOT NULL")),
        Index("ix_club_whatsapp_broadcasts_created_by_member", "created_by_member_id",
              postgresql_where=text("created_by_member_id IS NOT NULL")),
    )


class ClubWhatsAppBroadcastRecipients(AbstractSQLModel):
    __tablename__ = "club_whatsapp_broadcast_recipients"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    broadcast_id = Column(BigInteger, ForeignKey("club_whatsapp_broadcasts.id", ondelete="CASCADE"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    wa_id = Column(String(20), nullable=True)
    bsuid = Column(String(160), nullable=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: What fills "name": their first name, or "there".
    first_name = Column(String(60), nullable=True)
    status = Column(String(12), nullable=False, default=QUEUED, server_default=QUEUED)
    #: opted_out | no_consent | cancelled | not_sent
    skip_reason = Column(String(16), nullable=True)
    #: Not sent before this: the messaging limit's next day, a retry's wait, Meta's 24 hours after 131049.
    not_before = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Put back to the next day by the messaging limit at least once.
    held = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    tries = Column(Integer, nullable=False, default=0, server_default="0")
    #: Times Meta said 131049 (its per-person marketing limit): tried again once, a day later.
    capped = Column(Integer, nullable=False, default=0, server_default="0")
    wamid = Column(String(128), nullable=True, unique=True)
    error_code = Column(String(16), nullable=True)
    error_title = Column(String(200), nullable=True)
    claimed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    status_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("broadcast_id", "wa_id", name="uq_club_whatsapp_broadcast_recipients_wa"),
        UniqueConstraint("broadcast_id", "bsuid", name="uq_club_whatsapp_broadcast_recipients_bsuid"),
        CheckConstraint("wa_id IS NOT NULL OR bsuid IS NOT NULL", name="ck_club_whatsapp_broadcast_recipients_who"),
        # The sender's next batch: a broadcast's queued, due rows.
        Index("ix_club_whatsapp_broadcast_recipients_due", "broadcast_id", "status", "not_before"),
        # The messaging limit's moving day, and this month's counts: a club's sends by time.
        Index("ix_club_whatsapp_broadcast_recipients_sent", "club_id", "sent_at", postgresql_where=text("sent_at IS NOT NULL")),
        Index("ix_club_whatsapp_broadcast_recipients_registration", "registration_id",
              postgresql_where=text("registration_id IS NOT NULL")),
        Index("ix_club_whatsapp_broadcast_recipients_user", "user_id", postgresql_where=text("user_id IS NOT NULL")),
    )
