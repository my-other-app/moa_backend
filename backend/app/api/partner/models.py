"""What the Integrations API keeps (migration partner_api).

- `PartnerAllocations`: the platform admin's decision, per club: API access
  on or off, the rate tier, how many keys, notes. No row: the plan's
  `api_access` decides (off on every plan for now).
- `PartnerApiKeys`: a club's own keys, by their sha256 only. `client_id` is
  the key's stable identity (it survives a rotation), what webhooks name as
  a change's origin.
- `PartnerApps`: platforms a platform admin registered (no self-sign-up),
  with the scopes each may ever ask for.
- `PartnerConnections`: a club connected a partner app (OAuth consent, or
  the partner created the club); `PartnerAuthCodes` and `PartnerTokens` are
  the OAuth pieces, digests only, as app/api/mcp keeps its own.
- `PartnerExternalIds`: each client's own ids for our objects, unique per
  (client, resource): a POST with one already used answers ALREADY_EXISTS.
- `PartnerIdempotency`: the first answer to each Idempotency-Key, 24 hours.
- `PartnerExternalPayments`: a registration paid outside MyOtherApp, as the
  partner said; never in payouts or the ledger.
- `PartnerCertificateJobs`: an issue asked for through the API.
- `PartnerUsageDaily`: requests, errors and attendee reads per client per day.
- `PartnerSandboxObjects`: what test credentials make (sandbox.py).
- `DeveloperDocsLinks`: signed links to the private developer docs
  (docs_links.py; migration developer_docs_links), by their sha256 only.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Column, Date, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

RATE_TIERS = ("standard", "high")
APP_STATUSES = ("active", "suspended")
TOKEN_KINDS = ("access", "refresh")
SANDBOX_KINDS = ("event", "registration", "checkin", "certificate")
JOB_STATUSES = ("queued", "running", "completed", "failed")
DOCS_LINK_KINDS = ("club", "partner_app", "admin")
DOCS_LINK_PURPOSES = ("self", "share")


def _uuid_pk() -> Column:
    return Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _when(nullable: bool = True) -> Column:
    return Column(TZAwareDateTime(timezone=True), nullable=nullable)


class PartnerAllocations(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "partner_allocations"

    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    rate_tier = Column(String(16), nullable=False, default="standard", server_default="standard")
    max_keys = Column(Integer, nullable=False, default=5, server_default="5")
    notes = Column(String(1000), nullable=True)
    # admin: set in the admin portal; partner_created: a partner app with
    # clubs:create made the club (the admin can still turn it off).
    source = Column(String(20), nullable=False, default="admin", server_default="admin")
    updated_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        CheckConstraint("rate_tier IN ('standard', 'high')", name="ck_partner_allocations_tier"),
        CheckConstraint("max_keys >= 0 AND max_keys <= 100", name="ck_partner_allocations_max_keys"),
    )


class PartnerApiKeys(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "partner_api_keys"

    id = _uuid_pk()
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    # The key's identity across rotations: the origin webhooks name.
    client_id = Column(String(40), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    # What the list shows: moa_live_ and the first eight characters after it.
    prefix = Column(String(24), nullable=False)
    key_hash = Column(String(64), nullable=False, unique=True)
    livemode = Column(Boolean, nullable=False, default=True)
    scopes = Column(String(400), nullable=False)
    expires_at = _when()
    # Addresses or CIDR ranges; empty: any address.
    ip_allowlist = Column(JSON, nullable=False, default=list)
    last_used_at = _when()
    last_used_ip = Column(String(64), nullable=True)
    rotated_at = _when()
    revoked_at = _when()
    revoked_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    created_by_name = Column(String(120), nullable=True)
    # The club ticked "Our registration form tells people their details are
    # shared with <this key's name>" (attendees:read; DPDP Act 2023).
    attendees_ack_at = _when()

    __table_args__ = (Index("ix_partner_api_keys_club_live", "club_id", postgresql_where=text("revoked_at IS NULL")),)


class PartnerApps(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "partner_apps"

    id = _uuid_pk()
    name = Column(String(120), nullable=False)
    client_id = Column(String(64), nullable=False, unique=True)
    client_secret_hash = Column(String(64), nullable=False)
    description = Column(String(500), nullable=True)
    website = Column(String(500), nullable=True)
    logo_url = Column(String(500), nullable=True)
    contact_email = Column(String(200), nullable=True)
    redirect_uris = Column(JSON, nullable=False, default=list)
    # The scopes it may ever ask for; clubs:create only when the admin says.
    allowed_scopes = Column(String(400), nullable=False)
    rate_tier = Column(String(16), nullable=False, default="standard", server_default="standard")
    status = Column(String(12), nullable=False, default="active", server_default="active")
    suspended_at = _when()
    suspended_reason = Column(String(300), nullable=True)
    secret_rotated_at = _when()
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended')", name="ck_partner_apps_status"),
        CheckConstraint("rate_tier IN ('standard', 'high')", name="ck_partner_apps_tier"),
    )


class PartnerConnections(AbstractSQLModel, TimestampsMixin):
    """A club's connection to a partner app, in live or test mode: what the
    Connected apps list shows and disconnects. One live one per (app, club, mode)."""

    __tablename__ = "partner_connections"

    id = _uuid_pk()
    app_id = Column(PGUUID(as_uuid=True), ForeignKey("partner_apps.id", ondelete="CASCADE"), nullable=False, index=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    scopes = Column(String(400), nullable=False)
    livemode = Column(Boolean, nullable=False, default=True)
    # consent: a club's owner or admin allowed it; clubs_create: the partner made the club.
    via = Column(String(16), nullable=False, default="consent", server_default="consent")
    connected_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    connected_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    connected_by_name = Column(String(120), nullable=True)
    attendees_ack_at = _when()
    last_used_at = _when()
    revoked_at = _when()
    revoked_reason = Column(String(40), nullable=True)
    revoked_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        Index("ix_partner_connections_club_live", "club_id", postgresql_where=text("revoked_at IS NULL")),
        Index(
            "uq_partner_connections_live", "app_id", "club_id", "livemode", unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class PartnerAuthCodes(AbstractSQLModel):
    """Between the consent and the token request: good once, for minutes,
    only with the verifier behind its challenge."""

    __tablename__ = "partner_auth_codes"

    code_hash = Column(String(64), primary_key=True)
    app_id = Column(PGUUID(as_uuid=True), ForeignKey("partner_apps.id", ondelete="CASCADE"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True)
    connected_by_name = Column(String(120), nullable=True)
    redirect_uri = Column(String(2000), nullable=False)
    code_challenge = Column(String(128), nullable=False)
    scopes = Column(String(400), nullable=False)
    livemode = Column(Boolean, nullable=False, default=True)
    attendees_ack = Column(Boolean, nullable=False, default=False)
    expires_at = _when(False)
    used_at = _when()
    connection_id = Column(PGUUID(as_uuid=True), ForeignKey("partner_connections.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (Index("ix_partner_auth_codes_expires_at", "expires_at"),)


class PartnerTokens(AbstractSQLModel):
    """An access or refresh token, by its digest. A connection's tokens act
    for its club; an app's own (client credentials, no connection) only
    list and create clubs. A refresh token is spent when used; presented
    again, its connection ends."""

    __tablename__ = "partner_tokens"

    id = _uuid_pk()
    app_id = Column(PGUUID(as_uuid=True), ForeignKey("partner_apps.id", ondelete="CASCADE"), nullable=False)
    connection_id = Column(PGUUID(as_uuid=True), ForeignKey("partner_connections.id", ondelete="CASCADE"), nullable=True, index=True)
    kind = Column(String(8), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    scopes = Column(String(400), nullable=False)
    livemode = Column(Boolean, nullable=False, default=True)
    expires_at = _when(False)
    revoked_at = _when()
    rotated_at = _when()
    parent_id = Column(PGUUID(as_uuid=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("kind IN ('access', 'refresh')", name="ck_partner_tokens_kind"),
        Index("ix_partner_tokens_expires_at", "expires_at"),
    )


class PartnerExternalIds(AbstractSQLModel):
    __tablename__ = "partner_external_ids"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    client_id = Column(String(64), nullable=False)
    # event | registration | checkin | club
    resource = Column(String(20), nullable=False)
    external_id = Column(String(128), nullable=False)
    object_id = Column(String(64), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("uq_partner_external_ids", "client_id", "resource", "external_id", unique=True),
        Index("ix_partner_external_ids_object", "client_id", "resource", "object_id"),
    )


class PartnerIdempotency(AbstractSQLModel):
    __tablename__ = "partner_idempotency"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    client_id = Column(String(64), nullable=False)
    key = Column(String(255), nullable=False)
    request_hash = Column(String(64), nullable=False)
    method = Column(String(8), nullable=False)
    path = Column(String(500), nullable=False)
    # Null while the first request is still being answered.
    status_code = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    response_headers = Column(JSON, nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))
    expires_at = _when(False)

    __table_args__ = (
        Index("uq_partner_idempotency_key", "client_id", "key", unique=True),
        Index("ix_partner_idempotency_expires_at", "expires_at"),
    )


class PartnerExternalPayments(AbstractSQLModel):
    """A registration the partner says was paid outside MyOtherApp. Its
    registration is marked paid at nothing (finance's income and the ledger
    never see it, and payouts never include it); the amount lives here."""

    __tablename__ = "partner_external_payments"

    registration_id = Column(
        PGUUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), primary_key=True,
    )
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    amount_paise = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="INR", server_default="INR")
    reference = Column(String(128), nullable=True)
    client_id = Column(String(64), nullable=False)
    recorded_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))
    cancelled_at = _when()

    __table_args__ = (CheckConstraint("amount_paise >= 0", name="ck_partner_external_payments_amount"),)


class PartnerCertificateJobs(AbstractSQLModel):
    __tablename__ = "partner_certificate_jobs"

    id = _uuid_pk()
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    # The event's id as the API names it (a number, or a sandbox id).
    event_ref = Column(String(64), nullable=False)
    client_id = Column(String(64), nullable=False)
    livemode = Column(Boolean, nullable=False, default=True)
    status = Column(String(12), nullable=False, default="queued")
    request = Column(JSON, nullable=False, default=dict)
    result = Column(JSON, nullable=True)
    error = Column(String(500), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))
    finished_at = _when()

    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="ck_partner_certificate_jobs_status"),
        Index("ix_partner_certificate_jobs_club", "club_id", "created_at"),
    )


class PartnerUsageDaily(AbstractSQLModel):
    __tablename__ = "partner_usage_daily"

    day = Column(Date, primary_key=True)
    client_id = Column(String(64), primary_key=True)
    # 0 for an app's own token, which acts for no club.
    club_id = Column(Integer, primary_key=True, default=0)
    client_type = Column(String(16), nullable=False)
    livemode = Column(Boolean, nullable=False, default=True)
    requests = Column(Integer, nullable=False, default=0, server_default="0")
    writes = Column(Integer, nullable=False, default=0, server_default="0")
    errors = Column(Integer, nullable=False, default=0, server_default="0")
    rate_limited = Column(Integer, nullable=False, default=0, server_default="0")
    attendee_reads = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (Index("ix_partner_usage_daily_club", "club_id", "day"),)


class PartnerSandboxObjects(AbstractSQLModel):
    """An object test credentials made: an event, a registration, a
    check-in, a certificate. Its API shape is `data`; nothing of it is in
    the real tables."""

    __tablename__ = "partner_sandbox_objects"

    id = Column(String(40), primary_key=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(16), nullable=False)
    # A registration's, check-in's or certificate's event.
    parent_id = Column(String(40), nullable=True)
    data = Column(JSONB, nullable=False)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("kind IN ('event', 'registration', 'checkin', 'certificate')", name="ck_partner_sandbox_objects_kind"),
        Index("ix_partner_sandbox_objects_list", "club_id", "kind", "parent_id", "updated_at"),
    )


class DeveloperDocsLinks(AbstractSQLModel, TimestampsMixin):
    """A link to the private developer docs (myotherapp.in/developers/...):
    `moa_docs_` and 32 random bytes, kept only as a sha256 digest, so the
    link exists in one answer (create) and nowhere else (docs_links.py).

    - kind `club`: made by a club's owner or admin, for themselves (purpose
      `self`, 7 days) or to share with their developer (`share`, 30 days);
    - kind `partner_app`: made by a platform admin for a partner app's
      developers (`share`, 30 days);
    - kind `admin`: a platform admin's own (`self`, 7 days).

    Each is checked afresh on every use (docs_links.check): who made it, the
    club's API access, the app's status. A team member who made one is
    `created_by_member_id`; deleting that member's row deletes the link, and
    deleting the club or the app deletes theirs.
    """

    __tablename__ = "developer_docs_links"

    id = _uuid_pk()
    token_hash = Column(String(64), nullable=False)
    kind = Column(String(12), nullable=False)
    purpose = Column(String(8), nullable=False, default="self", server_default="self")
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=True)
    partner_app_id = Column(PGUUID(as_uuid=True), ForeignKey("partner_apps.id", ondelete="CASCADE"), nullable=True)
    label = Column(String(80), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True)
    created_by_name = Column(String(120), nullable=True)
    expires_at = _when(nullable=False)
    revoked_at = _when()
    revoked_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_used_at = _when()
    use_count = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("uq_developer_docs_links_token_hash", "token_hash", unique=True),
        Index("ix_developer_docs_links_club", "club_id", "created_at", postgresql_where=text("club_id IS NOT NULL")),
        Index("ix_developer_docs_links_partner_app", "partner_app_id", postgresql_where=text("partner_app_id IS NOT NULL")),
        Index("ix_developer_docs_links_created_at", "created_at"),
        CheckConstraint("kind IN ('club', 'partner_app', 'admin')", name="ck_developer_docs_links_kind"),
        CheckConstraint("purpose IN ('self', 'share')", name="ck_developer_docs_links_purpose"),
        CheckConstraint("kind <> 'club' OR club_id IS NOT NULL", name="ck_developer_docs_links_club"),
        CheckConstraint("kind <> 'partner_app' OR partner_app_id IS NOT NULL", name="ck_developer_docs_links_partner_app"),
        CheckConstraint("use_count >= 0", name="ck_developer_docs_links_use_count"),
    )
