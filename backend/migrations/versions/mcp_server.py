"""Clubs connecting AI assistants over MCP (app/api/mcp, docs/MCP.md).

- mcp_clients: the assistants, registered by themselves (RFC 7591) or known
  by the URL of their metadata document.
- mcp_grants: a connected app -- a client, a club, the person who approved it
  (the club's account or a team member) and the scopes. Revoked, never deleted.
- mcp_auth_codes, mcp_tokens: authorization codes and access/refresh tokens,
  each stored only as its sha256 digest.
- mcp_pending_actions: what an assistant asked for and a person must approve
  in the dashboard (publish an event, send an announcement), by a one-time
  token's digest, for a day.
- announcement_drafts: an announcement written but not sent.
- plan_features.mcp, off by default, switched on for growth, pro, campus and
  enterprise (`grant`); admins edit it in Admin -> Subscriptions like every
  other flag.

Every table is new, so nothing here waits on a live table except the brief
SHARE ROW EXCLUSIVE the foreign keys take on clubs, users, club_members and
events, under the lock_timeout set first (env.py retries the migration when
it is not granted in time). plan_features is a handful of rows.

Downgrade drops the tables and the column.

Revision ID: mcp_server
Revises: club_merch

Another branch also follows club_merch; whichever merges second is
re-chained onto the other at merge time.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "mcp_server"
down_revision = "club_merch"
branch_labels = None
depends_on = None

#: The plans that include connecting AI assistants, from the start.
PLANS = ("growth", "pro", "campus", "enterprise")


def grant() -> None:
    """Switch mcp on for PLANS. Plans that do not exist are skipped; safe to run again."""
    op.get_bind().execute(
        sa.text(
            "UPDATE plan_features SET mcp = true, updated_at = now() "
            "WHERE plan_id IN (SELECT id FROM plans WHERE key = ANY(:keys))"
        ),
        dict(keys=list(PLANS)),
    )


def _now() -> sa.Column:
    return sa.text("now()")


def _stamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    ]


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "mcp_clients",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", sa.String(512), nullable=False, unique=True),
        sa.Column("kind", sa.String(8), nullable=False, server_default="dcr"),
        sa.Column("client_name", sa.String(200), nullable=True),
        sa.Column("client_uri", sa.String(500), nullable=True),
        sa.Column("logo_uri", sa.String(500), nullable=True),
        sa.Column("redirect_uris", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("grant_types", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("token_endpoint_auth_method", sa.String(32), nullable=False, server_default="none"),
        sa.Column("client_secret_hash", sa.String(64), nullable=True),
        sa.Column("software_id", sa.String(200), nullable=True),
        sa.Column("software_version", sa.String(60), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fresh_until", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.CheckConstraint("kind IN ('dcr', 'cimd')", name="ck_mcp_clients_kind"),
    )

    op.create_table(
        "mcp_grants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True),
        sa.Column("client_pk", UUID(as_uuid=True), sa.ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scopes", sa.String(400), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(40), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revoked_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_mcp_grants_club_id", "mcp_grants", ["club_id"])
    op.create_index("ix_mcp_grants_member_id", "mcp_grants", ["member_id"])
    op.create_index("ix_mcp_grants_club_live", "mcp_grants", ["club_id"], postgresql_where=sa.text("revoked_at IS NULL"))

    op.create_table(
        "mcp_auth_codes",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column("client_pk", UUID(as_uuid=True), sa.ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=True),
        sa.Column("redirect_uri", sa.String(2000), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("scopes", sa.String(400), nullable=False),
        sa.Column("resource", sa.String(500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grant_id", UUID(as_uuid=True), sa.ForeignKey("mcp_grants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index("ix_mcp_auth_codes_expires_at", "mcp_auth_codes", ["expires_at"])

    op.create_table(
        "mcp_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("grant_id", UUID(as_uuid=True), sa.ForeignKey("mcp_grants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scopes", sa.String(400), nullable=False),
        sa.Column("resource", sa.String(500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.CheckConstraint("kind IN ('access', 'refresh')", name="ck_mcp_tokens_kind"),
    )
    op.create_index("ix_mcp_tokens_grant_id", "mcp_tokens", ["grant_id"])
    op.create_index("ix_mcp_tokens_expires_at", "mcp_tokens", ["expires_at"])

    op.create_table(
        "mcp_pending_actions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("grant_id", UUID(as_uuid=True), sa.ForeignKey("mcp_grants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requested_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("client_name", sa.String(200), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        *_stamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected', 'expired', 'superseded', 'failed')",
            name="ck_mcp_pending_actions_status",
        ),
    )
    op.create_index("ix_mcp_pending_actions_club_status", "mcp_pending_actions", ["club_id", "status"])
    op.create_index(
        "ix_mcp_pending_actions_target", "mcp_pending_actions", ["kind", "target_id"], postgresql_where=sa.text("status = 'pending'")
    )

    op.create_table(
        "announcement_drafts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.String(1000), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False, server_default="all"),
        sa.Column("status", sa.String(10), nullable=False, server_default="draft"),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_via", sa.String(16), nullable=False, server_default="mcp"),
        sa.Column("client_name", sa.String(200), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.CheckConstraint("status IN ('draft', 'sent', 'discarded')", name="ck_announcement_drafts_status"),
        sa.CheckConstraint("audience IN ('all', 'attendees', 'non_attendees')", name="ck_announcement_drafts_audience"),
    )
    op.create_index("ix_announcement_drafts_event_id", "announcement_drafts", ["event_id"])
    op.create_index("ix_announcement_drafts_club_status", "announcement_drafts", ["club_id", "status"])

    op.add_column("plan_features", sa.Column("mcp", sa.Boolean(), nullable=False, server_default=sa.false()))
    grant()


def downgrade() -> None:
    op.drop_column("plan_features", "mcp")
    op.drop_table("announcement_drafts")
    op.drop_table("mcp_pending_actions")
    op.drop_table("mcp_tokens")
    op.drop_table("mcp_auth_codes")
    op.drop_table("mcp_grants")
    op.drop_table("mcp_clients")
