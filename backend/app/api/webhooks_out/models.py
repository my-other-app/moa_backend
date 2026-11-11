"""Outgoing webhooks' tables (docs/WEBHOOKS.md).

- webhook_endpoints: an address that wants to be told, owned by a club --
  for its own systems -- or by a partner app acting for it (`partner_app_id`,
  a plain id: the partner apps' table is the partner API's, with no foreign
  key here). Its signing secret is kept encrypted (WEBHOOK_KEYS_SECRET,
  crypto.py): shown once, read back only to sign.
- webhook_events: one change, as it will be sent: its type, the object it is
  about, where it came from (`origin`), what changed (`previous`), and -- once
  built, a moment after the change committed -- the object itself (`data`).
  Kept 30 days.
- webhook_deliveries: one change to one endpoint: how it went (status,
  attempts, when it tries next, the last answer's status, a truncated body,
  how long it took). Kept 30 days; any can be sent again.
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import UUID, BigInteger, Boolean, Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

# Delivery statuses.
PENDING, RETRYING, SUCCEEDED, FAILED = "pending", "retrying", "succeeded", "failed"
DELIVERY_STATUSES = (PENDING, RETRYING, SUCCEEDED, FAILED)
#: Still to be sent (the first try, or a retry waiting).
OPEN = (PENDING, RETRYING)

# Why an endpoint is off.
BY_OWNER, FAILING, BY_ADMIN = "by_owner", "failing", "by_admin"
DISABLED_REASONS = (BY_OWNER, FAILING, BY_ADMIN)

# How an endpoint was added.
VIA_DASHBOARD, VIA_API_KEY, VIA_PARTNER = "dashboard", "api_key", "partner_app"
CREATED_VIA = (VIA_DASHBOARD, VIA_API_KEY, VIA_PARTNER)

ORIGIN_TYPES = ("dashboard", "app", "website", "api_key", "partner_app", "system")


def _check(table: str, column: str, values) -> sa.CheckConstraint:
    listed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=f"ck_{table}_{column}")


class WebhookEndpoints(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        # emit's question, on every change a club makes: does it have any?
        Index("ix_webhook_endpoints_club", "club_id", postgresql_where=sa.text("enabled")),
        Index("ix_webhook_endpoints_partner", "partner_app_id", postgresql_where=sa.text("partner_app_id IS NOT NULL")),
        # The admin's failing endpoints.
        Index("ix_webhook_endpoints_failing", "failing_since", postgresql_where=sa.text("failing_since IS NOT NULL")),
        _check("webhook_endpoints", "disabled_reason", DISABLED_REASONS),
        _check("webhook_endpoints", "created_via", CREATED_VIA),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    #: The partner app acting for the club; null for the club's own endpoint.
    #: No foreign key: partner_apps is the partner API's table.
    partner_app_id = Column(UUID(as_uuid=True), nullable=True)
    #: The client it belongs to, for "no echo": the partner app's client id,
    #: or the API key's when a key added it; null when added in the dashboard
    #: (then every one of the club's keys counts as the club's own).
    owner_client_id = Column(String(120), nullable=True)
    created_via = Column(String(20), nullable=False, default=VIA_DASHBOARD, server_default=VIA_DASHBOARD)
    url = Column(String(2000), nullable=False)
    description = Column(String(200), nullable=True)
    #: The types it wants, or ["*"].
    events = Column(JSONB, nullable=False, server_default=sa.text("'[\"*\"]'::jsonb"))
    include_own_changes = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    #: Live changes, or test-mode ones (moa_test_ keys' objects); never both.
    livemode = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    enabled = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    disabled_reason = Column(String(20), nullable=True)
    disabled_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: A club's own endpoint gets people's names, emails, phones and answers
    #: only when the club ticked that its registration form says they are
    #: shared with this endpoint's system (DPDP). Who ticked it, and when.
    share_attendee_details = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    dpdp_confirmed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    dpdp_confirmed_by = Column(String(120), nullable=True)
    #: Fernet-encrypted (crypto.py); `secret_hint` is its last four characters.
    secret_enc = Column(Text, nullable=False)
    secret_hint = Column(String(8), nullable=True)
    #: After a rotation the old secret signs too, until then (a second v1).
    previous_secret_enc = Column(Text, nullable=True)
    previous_secret_expires_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    secret_rotated_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The first failure since the last success: three days of it and the
    #: endpoint is turned off.
    failing_since = Column(TZAwareDateTime(timezone=True), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    last_success_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_failure_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_name = Column(String(120), nullable=True)


class WebhookEvents(AbstractSQLModel):
    __tablename__ = "webhook_events"
    __table_args__ = (
        # Retention, and the admin's volumes.
        Index("ix_webhook_events_created", "created_at"),
        # In order per object: an older change to the same object still to go.
        Index("ix_webhook_events_object", "club_id", "object_type", "object_id", "created_at"),
        _check("webhook_events", "origin_type", ORIGIN_TYPES),
    )

    #: "evt_" and 24 random hex characters: MOA-Event-Id, for de-duplication.
    id = Column(String(40), primary_key=True)
    #: Ordering among changes made in the same instant.
    seq = Column(BigInteger, sa.Identity(), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(40), nullable=False)
    object_type = Column(String(20), nullable=False)
    object_id = Column(String(64), nullable=False)
    livemode = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    origin_type = Column(String(20), nullable=False, default="system", server_default="system")
    origin_client_id = Column(String(120), nullable=True)
    #: The fields that changed, as they were (updates only).
    previous = Column(JSONB, nullable=True)
    #: The object, built when the change is sent out (or at the change, for
    #: one that will be gone by then: a check-in undone).
    data = Column(JSONB, nullable=True)
    #: When it was built and given to its endpoints; null until then.
    fanned_out_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Only to this endpoint (a test), not to every endpoint that wants the type.
    only_endpoint_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()"))


class WebhookDeliveries(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "endpoint_id", name="uq_webhook_deliveries_event_endpoint"),
        # An endpoint's delivery log, newest first.
        Index("ix_webhook_deliveries_endpoint", "endpoint_id", "created_at"),
        # Retention, and the admin's volumes.
        Index("ix_webhook_deliveries_created", "created_at"),
        # Still to go, per endpoint: what an endpoint turned off gives up.
        Index("ix_webhook_deliveries_open", "endpoint_id", postgresql_where=sa.text("status IN ('pending', 'retrying')")),
        _check("webhook_deliveries", "status", DELIVERY_STATUSES),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(String(40), ForeignKey("webhook_events.id", ondelete="CASCADE"), nullable=False)
    endpoint_id = Column(UUID(as_uuid=True), ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(12), nullable=False, default=PENDING, server_default=PENDING)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_attempt_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    succeeded_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    response_status = Column(Integer, nullable=True)
    #: The first RESPONSE_BODY_MAX characters of the last answer.
    response_body = Column(String(2000), nullable=True)
    error = Column(String(300), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    #: Times it waited for an older change to the same object to go first.
    waits = Column(Integer, nullable=False, default=0, server_default="0")
