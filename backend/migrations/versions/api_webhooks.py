"""Outgoing webhooks (app/api/webhooks_out, docs/WEBHOOKS.md,
feature-register/integrations-api-contract.md).

New tables, every one empty:

- webhook_endpoints: an address a club (or a partner app acting for it)
  wants changes sent to; its signing secret Fernet-encrypted under
  WEBHOOK_KEYS_SECRET. `partner_app_id` is a plain uuid, no foreign key (the
  partner apps' table is the partner API's). ix_webhook_endpoints_club is
  what every change asks ("does this club have any?").
- webhook_events: one change to send (evt_...), its object built a moment
  after the change committed; kept 30 days. ix_webhook_events_object keeps
  changes to one object in order.
- webhook_deliveries: one change to one endpoint, with how it went;
  uq_webhook_deliveries_event_endpoint keeps it to one.

Nothing existing changes. Lock-safe: the tables are new, so their indexes
are built on nothing; the foreign keys take a brief SHARE ROW EXCLUSIVE on
clubs and users, under lock_timeout 3s.

Downgrade drops the three tables (every endpoint, change and delivery with them).

Revision ID: api_webhooks
Revises: partner_api
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "api_webhooks"
down_revision = "partner_api"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")
UUID_PK = dict(primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _stamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    ]


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("partner_app_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_client_id", sa.String(120), nullable=True),
        sa.Column("created_via", sa.String(20), nullable=False, server_default="dashboard"),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("description", sa.String(200), nullable=True),
        sa.Column("events", postgresql.JSONB(), nullable=False, server_default=sa.text("'[\"*\"]'::jsonb")),
        sa.Column("include_own_changes", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("disabled_reason", sa.String(20), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("share_attendee_details", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dpdp_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dpdp_confirmed_by", sa.String(120), nullable=True),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column("secret_hint", sa.String(8), nullable=True),
        sa.Column("previous_secret_enc", sa.Text(), nullable=True),
        sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secret_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_name", sa.String(120), nullable=True),
        *_stamps(),
        sa.CheckConstraint(_in("disabled_reason", ("by_owner", "failing", "by_admin")), name="ck_webhook_endpoints_disabled_reason"),
        sa.CheckConstraint(_in("created_via", ("dashboard", "api_key", "partner_app")), name="ck_webhook_endpoints_created_via"),
        if_not_exists=True,
    )
    op.create_index("ix_webhook_endpoints_club", "webhook_endpoints", ["club_id"], postgresql_where=sa.text("enabled"), if_not_exists=True)
    op.create_index("ix_webhook_endpoints_partner", "webhook_endpoints", ["partner_app_id"],
                    postgresql_where=sa.text("partner_app_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_webhook_endpoints_failing", "webhook_endpoints", ["failing_since"],
                    postgresql_where=sa.text("failing_since IS NOT NULL"), if_not_exists=True)

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("object_type", sa.String(20), nullable=False),
        sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("livemode", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("origin_type", sa.String(20), nullable=False, server_default="system"),
        sa.Column("origin_client_id", sa.String(120), nullable=True),
        sa.Column("previous", postgresql.JSONB(), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("fanned_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("only_endpoint_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()")),
        sa.CheckConstraint(_in("origin_type", ("dashboard", "app", "website", "api_key", "partner_app", "system")),
                           name="ck_webhook_events_origin_type"),
        if_not_exists=True,
    )
    op.create_index("ix_webhook_events_created", "webhook_events", ["created_at"], if_not_exists=True)
    op.create_index("ix_webhook_events_object", "webhook_events", ["club_id", "object_type", "object_id", "created_at"], if_not_exists=True)

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("event_id", sa.String(40), sa.ForeignKey("webhook_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.String(2000), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("waits", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
        sa.UniqueConstraint("event_id", "endpoint_id", name="uq_webhook_deliveries_event_endpoint"),
        sa.CheckConstraint(_in("status", ("pending", "retrying", "succeeded", "failed")), name="ck_webhook_deliveries_status"),
        if_not_exists=True,
    )
    op.create_index("ix_webhook_deliveries_endpoint", "webhook_deliveries", ["endpoint_id", "created_at"], if_not_exists=True)
    op.create_index("ix_webhook_deliveries_created", "webhook_deliveries", ["created_at"], if_not_exists=True)
    op.create_index("ix_webhook_deliveries_open", "webhook_deliveries", ["endpoint_id"],
                    postgresql_where=sa.text("status IN ('pending', 'retrying')"), if_not_exists=True)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    for table in ("webhook_deliveries", "webhook_events", "webhook_endpoints"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
