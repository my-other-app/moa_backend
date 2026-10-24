"""A club's own WhatsApp Business number: the connection, its quality, its
templates, and the conversations it holds.

An Enterprise feature (plan flag `whatsapp_own`). The club pastes the keys
of its own Meta app -- the WhatsApp Business Account id, the phone number
id, a permanent access token and the app secret -- and points the app's
webhook at the address we give it. Its inbox then holds every message people
send that number, and its team (those given the WhatsApp permission) replies
from the dashboard. The club pays Meta for what its number sends.

- `club_whatsapp_accounts`: one row per connection. At most one per club is
  live (`active`, or `failed` and waiting to be fixed), held by a partial
  unique index. The token, the app secret and the webhook verify token are
  Fernet under settings.WHATSAPP_KEYS_SECRET (app/core/keyring.py), never
  returned, logged or audited; disconnecting wipes them (the row stays, for
  the history and the audit log). `mode` is `keys` today; `embedded` is
  reserved for Meta's Embedded Signup, which fills the same row from an
  OAuth code instead of pasted keys (business_id with it).
- `club_whatsapp_quality_log`: every change of the number's quality rating
  (GREEN, YELLOW, RED) or messaging limit, as the sweep, a check or Meta's
  webhook found it.
- `club_whatsapp_templates`: what the club's WhatsApp Business Account has
  approved, as last read from Meta (the dashboard lists live; this is what a
  ticket send decides by, without asking Meta each time).
- `club_whatsapp_conversations`: one per person (their WhatsApp id) per
  business number, linked to the user or registration their number matches,
  with the 24-hour customer-service window (`last_inbound_at`), what is
  unread and since when a message has waited for an answer.
- `club_whatsapp_messages`: each message, in or out: its kind, its text (or
  caption, button title, reaction), the media id Meta gave it (fetched only
  through the dashboard's authenticated proxy, never a public address), its
  status and Meta's error. Deleted by the retention sweep after the club's
  retention period (12 months unless it chose otherwise).

Message content is personal data: it is shown only to the club's own
account and the team members it gave the WhatsApp permission, never to a
platform admin (impersonating or not), and never written to a log.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

MODE_KEYS = "keys"
#: Reserved: Meta's Embedded Signup, once MyOtherApp is a Tech Provider.
MODE_EMBEDDED = "embedded"
MODES = (MODE_KEYS, MODE_EMBEDDED)

ACTIVE = "active"
#: Meta refused the keys or the number (a check, or a send with an
#: account-level error). Still the club's, but nothing is sent from it until
#: it is fixed; tickets go from the platform's number meanwhile.
FAILED = "failed"
#: The club disconnected it; its keys are wiped.
DISCONNECTED = "disconnected"
STATUSES = (ACTIVE, FAILED, DISCONNECTED)
LIVE = (ACTIVE, FAILED)

#: Meta's quality ratings, best first. UNKNOWN: Meta has not rated it yet.
QUALITY = ("GREEN", "YELLOW", "RED")

IN = "in"
OUT = "out"

#: A message's status: an inbound one is `received`; an outbound one moves
#: pending -> sent -> delivered -> read, or to failed.
RECEIVED = "received"
PENDING = "pending"
SENT = "sent"
DELIVERED = "delivered"
READ = "read"
FAILED_MESSAGE = "failed"
#: The order an outbound status only moves forward in (a late "delivered"
#: never undoes "read"); failed may land at any point.
STATUS_ORDER = {PENDING: 0, SENT: 1, DELIVERED: 2, READ: 3}

#: How an outbound message was sent: replied in the inbox, or a ticket,
#: certificate or reminder the deliveries sent from the club's number.
VIA_INBOX = "inbox"
VIA_DELIVERY = "delivery"

OPEN = "open"
CLOSED = "closed"

DEFAULT_RETENTION_MONTHS = 12


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ClubWhatsAppAccounts(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_whatsapp_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    mode = Column(String(12), nullable=False, default=MODE_KEYS, server_default=MODE_KEYS)
    status = Column(String(16), nullable=False, default=ACTIVE, server_default=ACTIVE)
    #: The WhatsApp Business Account: its templates live there.
    waba_id = Column(String(32), nullable=False)
    #: The number's id at Meta: messages are sent from it.
    phone_number_id = Column(String(32), nullable=False)
    #: The Meta Business portfolio, from Embedded Signup (reserved).
    business_id = Column(String(32), nullable=True)
    #: The club's Meta app: needed to submit a template with a document header.
    app_id = Column(String(32), nullable=True)
    #: As Meta reports them: "+91 98470 12345", and the business name approved for the number.
    display_phone_number = Column(String(32), nullable=True)
    verified_name = Column(String(200), nullable=True)
    #: Fernet, with settings.WHATSAPP_KEYS_SECRET. Never returned, logged or
    #: audited; wiped (null) when the club disconnects.
    access_token_enc = Column(Text, nullable=True)
    app_secret_enc = Column(Text, nullable=True)
    verify_token_enc = Column(Text, nullable=True)
    #: The token's last four characters: all anyone sees of it again.
    token_last4 = Column(String(8), nullable=True)
    #: The last part of the club's webhook URL. Random, url-safe.
    webhook_token = Column(String(64), nullable=False, unique=True)
    #: GREEN, YELLOW, RED or UNKNOWN; TIER_250, TIER_1K ... TIER_UNLIMITED.
    quality_rating = Column(String(16), nullable=True)
    messaging_limit_tier = Column(String(32), nullable=True)
    quality_checked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    verified_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_webhook_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_error = Column(String(300), nullable=True)
    #: Meta's code for last_error, when it gave one.
    last_error_code = Column(String(16), nullable=True)
    #: Whether the club's tickets, certificates and reminders go from this number.
    routing = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    #: Months the inbox keeps a message before the sweep deletes it.
    retention_months = Column(Integer, nullable=False, default=DEFAULT_RETENTION_MONTHS, server_default=str(DEFAULT_RETENTION_MONTHS))
    #: When it stopped: disconnected or disabled.
    ended_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)

    club = relationship("Clubs", foreign_keys=[club_id])

    __table_args__ = (
        Index(
            "uq_club_whatsapp_accounts_live",
            "club_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'failed')"),
        ),
        CheckConstraint("status IN ('active', 'failed', 'disconnected')", name="ck_club_whatsapp_accounts_status"),
        CheckConstraint("mode IN ('keys', 'embedded')", name="ck_club_whatsapp_accounts_mode"),
        CheckConstraint("retention_months BETWEEN 1 AND 36", name="ck_club_whatsapp_accounts_retention"),
    )


class ClubWhatsAppQualityLog(AbstractSQLModel):
    __tablename__ = "club_whatsapp_quality_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("club_whatsapp_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    quality_rating = Column(String(16), nullable=True)
    messaging_limit_tier = Column(String(32), nullable=True)
    previous_rating = Column(String(16), nullable=True)
    previous_tier = Column(String(32), nullable=True)
    #: connect | check | sweep | webhook
    source = Column(String(16), nullable=False)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))


class ClubWhatsAppTemplates(AbstractSQLModel):
    __tablename__ = "club_whatsapp_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("club_whatsapp_accounts.id", ondelete="CASCADE"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    #: Meta's id for this name and language (the "hsm" id).
    meta_id = Column(String(64), nullable=True)
    name = Column(String(512), nullable=False)
    language = Column(String(16), nullable=False)
    category = Column(String(32), nullable=True)
    #: APPROVED, PENDING, REJECTED, PAUSED, DISABLED, ... as Meta says it.
    status = Column(String(32), nullable=False)
    rejected_reason = Column(String(200), nullable=True)
    quality = Column(String(16), nullable=True)
    components = Column(JSON, nullable=True)
    synced_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (UniqueConstraint("account_id", "name", "language", name="uq_club_whatsapp_templates_name"),)


class ClubWhatsAppConversations(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_whatsapp_conversations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    #: The connection it arrived on; null once that row is gone.
    account_id = Column(Integer, ForeignKey("club_whatsapp_accounts.id", ondelete="SET NULL"), nullable=True)
    #: The business number's id at Meta: the 24-hour window is per number.
    business_phone_id = Column(String(32), nullable=False)
    #: The person's WhatsApp id: their number in digits, with its country code.
    wa_id = Column(String(20), nullable=False)
    #: The name on their WhatsApp profile, as Meta passes it on.
    profile_name = Column(String(120), nullable=True)
    #: Who this number is to the club, when it matches a registration for one
    #: of its events (or the account behind one).
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True)
    contact_name = Column(String(120), nullable=True)
    linked_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    status = Column(String(8), nullable=False, default=OPEN, server_default=OPEN)
    unread_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_message_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The 24-hour window: free text may be sent until this plus 24 hours.
    last_inbound_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_outbound_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The list's line for the latest message. Message content: deleted with it.
    last_message_direction = Column(String(3), nullable=True)
    last_message_type = Column(String(16), nullable=True)
    last_message_preview = Column(String(160), nullable=True)
    last_message_status = Column(String(12), nullable=True)
    #: When the oldest unanswered message from them arrived; null when answered.
    awaiting_since = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("club_id", "business_phone_id", "wa_id", name="uq_club_whatsapp_conversations_contact"),
        CheckConstraint("status IN ('open', 'closed')", name="ck_club_whatsapp_conversations_status"),
        # The inbox's list: a club's conversations, latest first.
        Index("ix_club_whatsapp_conversations_club_last", "club_id", "last_message_at"),
    )


class ClubWhatsAppMessages(AbstractSQLModel):
    __tablename__ = "club_whatsapp_messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id = Column(BigInteger, ForeignKey("club_whatsapp_conversations.id", ondelete="CASCADE"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    direction = Column(String(3), nullable=False)
    #: text, image, document, audio, video, sticker, location, reaction,
    #: button, interactive, contacts, template, unsupported
    type = Column(String(16), nullable=False)
    #: The text, the caption, the button's title, the reaction's emoji.
    body = Column(Text, nullable=True)
    #: Meta's media id: fetched only through the inbox's authenticated proxy.
    media_id = Column(String(128), nullable=True)
    media_mime = Column(String(100), nullable=True)
    media_filename = Column(String(255), nullable=True)
    #: {"latitude", "longitude", "name", "address"}
    location = Column(JSON, nullable=True)
    template_name = Column(String(512), nullable=True)
    template_language = Column(String(16), nullable=True)
    #: {"header": [...], "body": [...], "buttons": [...]}
    template_params = Column(JSON, nullable=True)
    #: The message a reply or a reaction is about (its wamid).
    context_wamid = Column(String(128), nullable=True)
    wamid = Column(String(128), nullable=True, unique=True)
    status = Column(String(12), nullable=False)
    status_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    error_code = Column(String(16), nullable=True)
    error_title = Column(String(200), nullable=True)
    #: inbox | delivery, for what was sent; null for what arrived.
    via = Column(String(12), nullable=True)
    delivery_id = Column(UUID(as_uuid=True), nullable=True)
    sent_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sent_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    sent_by_name = Column(String(120), nullable=True)
    #: For a reply: seconds since the message it answered first waited.
    response_seconds = Column(Integer, nullable=True)
    #: When it happened: Meta's time for an inbound message, the send's for
    #: an outbound one. A thread is in this order (then id).
    wa_timestamp = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    conversation = relationship("ClubWhatsAppConversations", foreign_keys=[conversation_id])

    __table_args__ = (
        Index("ix_club_whatsapp_messages_conversation", "conversation_id", "id"),
        # A thread, in the order things happened.
        Index("ix_club_whatsapp_messages_thread", "conversation_id", "wa_timestamp", "id"),
        CheckConstraint("direction IN ('in', 'out')", name="ck_club_whatsapp_messages_direction"),
        # The retention sweep and the stats: a club's messages by age.
        Index("ix_club_whatsapp_messages_club_created", "club_id", "created_at"),
    )
