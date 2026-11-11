"""The Integrations API (app/api/partner, docs/INTEGRATIONS_API.md,
feature-register/integrations-api-contract.md).

New tables, every one empty:

- partner_allocations: the platform admin's per-club decision (API access
  on or off, rate tier, max keys, notes). No row: the plan's api_access.
- partner_api_keys: a club's own keys, by sha256 digest only.
- partner_apps: partner platforms a platform admin registered.
- partner_connections, partner_auth_codes, partner_tokens: a club's
  connection to a partner app and its OAuth codes and tokens (digests only).
  uq_partner_connections_live keeps one live connection per (app, club, mode).
- partner_external_ids: each client's own ids for our objects;
  uq_partner_external_ids is the (client, resource, external_id) rule.
- partner_idempotency: the first answer to each Idempotency-Key (24 hours).
- partner_external_payments: registrations paid outside MyOtherApp, as the
  partner reported them (never in payouts or the ledger).
- partner_certificate_jobs, partner_usage_daily, partner_sandbox_objects
  (test mode's objects, never in the real tables).

Changed:

- events gets `external_payments_allowed` ("Accept registrations paid
  outside MyOtherApp"), off;
- plan_features gets `api_access`, off on every plan (a platform admin's
  allocation decides for now);
- the checkinvia enum gets `api`, for check-ins made through the API.

Lock-safe: the tables are new, so their indexes are built on nothing; the
two columns are added with a constant default (a catalog change on events,
which is hot, under lock_timeout 3s); ADD VALUE to the enum is a catalog
change too. The foreign keys take a brief SHARE ROW EXCLUSIVE on clubs,
users, club_members, events and event_registrations_link.

Downgrade drops the tables (every key, app, connection, token, mapping and
sandbox object with them) and the two columns. The enum keeps `api`
(Postgres cannot drop an enum value); nothing writes it once the code that
does is gone.

Revision ID: partner_api
Revises: social_posting
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "partner_api"
down_revision = "social_posting"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")
UUID = postgresql.UUID(as_uuid=True)
UUID_PK = dict(primary_key=True, server_default=sa.text("gen_random_uuid()"))

TABLES = (
    "partner_sandbox_objects", "partner_usage_daily", "partner_certificate_jobs", "partner_external_payments",
    "partner_idempotency", "partner_external_ids", "partner_tokens", "partner_auth_codes", "partner_connections",
    "partner_apps", "partner_api_keys", "partner_allocations",
)


def _stamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    ]


def _fk(table: str, ondelete: str):
    return sa.ForeignKey(f"{table}.id", ondelete=ondelete)


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.add_column("events", sa.Column("external_payments_allowed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("plan_features", sa.Column("api_access", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("ALTER TYPE checkinvia ADD VALUE IF NOT EXISTS 'api'")

    op.create_table(
        "partner_allocations",
        sa.Column("club_id", sa.Integer(), _fk("clubs", "CASCADE"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rate_tier", sa.String(16), nullable=False, server_default="standard"),
        sa.Column("max_keys", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("notes", sa.String(1000), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="admin"),
        sa.Column("updated_by_id", sa.Integer(), _fk("users", "SET NULL"), nullable=True),
        *_stamps(),
        sa.CheckConstraint("rate_tier IN ('standard', 'high')", name="ck_partner_allocations_tier"),
        sa.CheckConstraint("max_keys >= 0 AND max_keys <= 100", name="ck_partner_allocations_max_keys"),
        if_not_exists=True,
    )

    op.create_table(
        "partner_api_keys",
        sa.Column("id", UUID, **UUID_PK),
        sa.Column("club_id", sa.Integer(), _fk("clubs", "CASCADE"), nullable=False),
        sa.Column("client_id", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prefix", sa.String(24), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("scopes", sa.String(400), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_allowlist", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(64), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), _fk("users", "SET NULL"), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), _fk("users", "SET NULL"), nullable=True),
        sa.Column("created_by_member_id", sa.Integer(), _fk("club_members", "SET NULL"), nullable=True),
        sa.Column("created_by_name", sa.String(120), nullable=True),
        sa.Column("attendees_ack_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        if_not_exists=True,
    )
    op.create_index("ix_partner_api_keys_club_id", "partner_api_keys", ["club_id"], if_not_exists=True)
    op.create_index("ix_partner_api_keys_club_live", "partner_api_keys", ["club_id"], postgresql_where=sa.text("revoked_at IS NULL"), if_not_exists=True)

    op.create_table(
        "partner_apps",
        sa.Column("id", UUID, **UUID_PK),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False, unique=True),
        sa.Column("client_secret_hash", sa.String(64), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("website", sa.String(500), nullable=True),
        sa.Column("logo_url", sa.String(500), nullable=True),
        sa.Column("contact_email", sa.String(200), nullable=True),
        sa.Column("redirect_uris", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("allowed_scopes", sa.String(400), nullable=False),
        sa.Column("rate_tier", sa.String(16), nullable=False, server_default="standard"),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_reason", sa.String(300), nullable=True),
        sa.Column("secret_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.Integer(), _fk("users", "SET NULL"), nullable=True),
        *_stamps(),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_partner_apps_status"),
        sa.CheckConstraint("rate_tier IN ('standard', 'high')", name="ck_partner_apps_tier"),
        if_not_exists=True,
    )

    op.create_table(
        "partner_connections",
        sa.Column("id", UUID, **UUID_PK),
        sa.Column("app_id", UUID, _fk("partner_apps", "CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), _fk("clubs", "CASCADE"), nullable=False),
        sa.Column("scopes", sa.String(400), nullable=False),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("via", sa.String(16), nullable=False, server_default="consent"),
        sa.Column("connected_by_user_id", sa.Integer(), _fk("users", "SET NULL"), nullable=True),
        sa.Column("connected_by_member_id", sa.Integer(), _fk("club_members", "SET NULL"), nullable=True),
        sa.Column("connected_by_name", sa.String(120), nullable=True),
        sa.Column("attendees_ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(40), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), _fk("users", "SET NULL"), nullable=True),
        *_stamps(),
        if_not_exists=True,
    )
    op.create_index("ix_partner_connections_app_id", "partner_connections", ["app_id"], if_not_exists=True)
    op.create_index("ix_partner_connections_club_live", "partner_connections", ["club_id"],
                    postgresql_where=sa.text("revoked_at IS NULL"), if_not_exists=True)
    op.create_index("uq_partner_connections_live", "partner_connections", ["app_id", "club_id", "livemode"], unique=True,
                    postgresql_where=sa.text("revoked_at IS NULL"), if_not_exists=True)

    op.create_table(
        "partner_auth_codes",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column("app_id", UUID, _fk("partner_apps", "CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), _fk("clubs", "CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), _fk("users", "CASCADE"), nullable=False),
        sa.Column("member_id", sa.Integer(), _fk("club_members", "CASCADE"), nullable=True),
        sa.Column("connected_by_name", sa.String(120), nullable=True),
        sa.Column("redirect_uri", sa.String(2000), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("scopes", sa.String(400), nullable=False),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("attendees_ack", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connection_id", UUID, _fk("partner_connections", "SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        if_not_exists=True,
    )
    op.create_index("ix_partner_auth_codes_expires_at", "partner_auth_codes", ["expires_at"], if_not_exists=True)

    op.create_table(
        "partner_tokens",
        sa.Column("id", UUID, **UUID_PK),
        sa.Column("app_id", UUID, _fk("partner_apps", "CASCADE"), nullable=False),
        sa.Column("connection_id", UUID, _fk("partner_connections", "CASCADE"), nullable=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scopes", sa.String(400), nullable=False),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parent_id", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("kind IN ('access', 'refresh')", name="ck_partner_tokens_kind"),
        if_not_exists=True,
    )
    op.create_index("ix_partner_tokens_connection_id", "partner_tokens", ["connection_id"], if_not_exists=True)
    op.create_index("ix_partner_tokens_expires_at", "partner_tokens", ["expires_at"], if_not_exists=True)

    op.create_table(
        "partner_external_ids",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("resource", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("club_id", sa.Integer(), _fk("clubs", "CASCADE"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        if_not_exists=True,
    )
    op.create_index("uq_partner_external_ids", "partner_external_ids", ["client_id", "resource", "external_id"], unique=True, if_not_exists=True)
    op.create_index("ix_partner_external_ids_object", "partner_external_ids", ["client_id", "resource", "object_id"], if_not_exists=True)

    op.create_table(
        "partner_idempotency",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("response_headers", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_index("uq_partner_idempotency_key", "partner_idempotency", ["client_id", "key"], unique=True, if_not_exists=True)
    op.create_index("ix_partner_idempotency_expires_at", "partner_idempotency", ["expires_at"], if_not_exists=True)

    op.create_table(
        "partner_external_payments",
        sa.Column("registration_id", UUID, _fk("event_registrations_link", "CASCADE"), primary_key=True),
        sa.Column("club_id", sa.Integer(), _fk("clubs", "CASCADE"), nullable=False),
        sa.Column("event_id", sa.Integer(), _fk("events", "CASCADE"), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_paise >= 0", name="ck_partner_external_payments_amount"),
        if_not_exists=True,
    )
    op.create_index("ix_partner_external_payments_event_id", "partner_external_payments", ["event_id"], if_not_exists=True)

    op.create_table(
        "partner_certificate_jobs",
        sa.Column("id", UUID, **UUID_PK),
        sa.Column("club_id", sa.Integer(), _fk("clubs", "CASCADE"), nullable=False),
        sa.Column("event_ref", sa.String(64), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("request", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed')", name="ck_partner_certificate_jobs_status"),
        if_not_exists=True,
    )
    op.create_index("ix_partner_certificate_jobs_club", "partner_certificate_jobs", ["club_id", "created_at"], if_not_exists=True)

    op.create_table(
        "partner_usage_daily",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("client_id", sa.String(64), primary_key=True),
        sa.Column("club_id", sa.Integer(), primary_key=True, server_default="0"),
        sa.Column("client_type", sa.String(16), nullable=False),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("writes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rate_limited", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attendee_reads", sa.Integer(), nullable=False, server_default="0"),
        if_not_exists=True,
    )
    op.create_index("ix_partner_usage_daily_club", "partner_usage_daily", ["club_id", "day"], if_not_exists=True)

    op.create_table(
        "partner_sandbox_objects",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("club_id", sa.Integer(), _fk("clubs", "CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("parent_id", sa.String(40), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        *_stamps(),
        sa.CheckConstraint("kind IN ('event', 'registration', 'checkin', 'certificate')", name="ck_partner_sandbox_objects_kind"),
        if_not_exists=True,
    )
    op.create_index("ix_partner_sandbox_objects_list", "partner_sandbox_objects", ["club_id", "kind", "parent_id", "updated_at"], if_not_exists=True)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.drop_column("plan_features", "api_access")
    op.drop_column("events", "external_payments_allowed")
