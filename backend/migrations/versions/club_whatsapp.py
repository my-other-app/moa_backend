"""A club's own WhatsApp Business number (app/api/club_whatsapp).

- club_whatsapp_accounts: the connection -- WhatsApp Business Account id,
  phone number id, the encrypted access token, app secret and webhook verify
  token (WHATSAPP_KEYS_SECRET), the webhook's token, status, quality rating
  and messaging limit. At most one live (active or failed) per club, and
  one number live in one club at most, by partial unique indexes.
- club_whatsapp_quality_log: each change of the quality rating or limit.
- club_whatsapp_templates: the templates the club's account has, as last read.
- club_whatsapp_conversations, club_whatsapp_messages: the inbox.
- plan_features.whatsapp_own: off by default, switched on for enterprise
  (`grant`); admins edit it in Admin -> Subscriptions like every other flag.
- club_members.whatsapp: the WhatsApp permission an owner gives a team
  member, off by default.

Every table is new. The two columns are added with a constant default, which
is a catalog change (no rewrite), under the lock_timeout set first;
club_members is hot, so that statement waits at most 3s for its lock (env.py
retries the migration when it is not granted). The foreign keys take a brief
SHARE ROW EXCLUSIVE on clubs, users, club_members and
event_registrations_link, under the same limit. plan_features is a handful
of rows.

Downgrade drops the tables and both columns.

Revision ID: club_whatsapp
Revises: message_quotas
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "club_whatsapp"
down_revision = "message_quotas"
branch_labels = None
depends_on = None

#: The plans that include a club's own WhatsApp number, from the start.
PLANS = ("enterprise",)


def grant() -> None:
    """Switch whatsapp_own on for PLANS. Plans that do not exist are skipped; safe to run again."""
    op.get_bind().execute(
        sa.text(
            "UPDATE plan_features SET whatsapp_own = true, updated_at = now() "
            "WHERE plan_id IN (SELECT id FROM plans WHERE key = ANY(:keys))"
        ),
        dict(keys=list(PLANS)),
    )


def _now():
    return sa.text("now()")


def _stamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    ]


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("plan_features", sa.Column("whatsapp_own", sa.Boolean(), nullable=False, server_default=sa.false()))
    grant()
    op.add_column("club_members", sa.Column("whatsapp", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "club_whatsapp_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(12), nullable=False, server_default="keys"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("waba_id", sa.String(32), nullable=False),
        sa.Column("phone_number_id", sa.String(32), nullable=False),
        sa.Column("business_id", sa.String(32), nullable=True),
        sa.Column("app_id", sa.String(32), nullable=True),
        sa.Column("display_phone_number", sa.String(32), nullable=True),
        sa.Column("verified_name", sa.String(200), nullable=True),
        sa.Column("access_token_enc", sa.Text(), nullable=True),
        sa.Column("app_secret_enc", sa.Text(), nullable=True),
        sa.Column("verify_token_enc", sa.Text(), nullable=True),
        sa.Column("token_last4", sa.String(8), nullable=True),
        sa.Column("webhook_token", sa.String(64), nullable=False, unique=True),
        sa.Column("quality_rating", sa.String(16), nullable=True),
        sa.Column("messaging_limit_tier", sa.String(32), nullable=True),
        sa.Column("quality_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("templates_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_webhook_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("last_error_code", sa.String(40), nullable=True),
        sa.Column("failed_by", sa.String(8), nullable=True),
        sa.Column("number_status", sa.String(32), nullable=True),
        sa.Column("name_status", sa.String(32), nullable=True),
        sa.Column("restrictions", sa.JSON(), nullable=True),
        sa.Column("routing", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("retention_months", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        sa.CheckConstraint("status IN ('active', 'failed', 'disconnected')", name="ck_club_whatsapp_accounts_status"),
        sa.CheckConstraint("mode IN ('keys', 'embedded')", name="ck_club_whatsapp_accounts_mode"),
        sa.CheckConstraint("retention_months BETWEEN 1 AND 36", name="ck_club_whatsapp_accounts_retention"),
        sa.CheckConstraint("failed_by IS NULL OR failed_by IN ('read', 'send', 'webhook')", name="ck_club_whatsapp_accounts_failed_by"),
    )
    op.create_index("ix_club_whatsapp_accounts_club_id", "club_whatsapp_accounts", ["club_id"])
    op.create_index(
        "uq_club_whatsapp_accounts_live", "club_whatsapp_accounts", ["club_id"], unique=True,
        postgresql_where=sa.text("status IN ('active', 'failed')"),
    )
    op.create_index(
        "uq_club_whatsapp_accounts_live_number", "club_whatsapp_accounts", ["phone_number_id"], unique=True,
        postgresql_where=sa.text("status IN ('active', 'failed')"),
    )

    op.create_table(
        "club_whatsapp_quality_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("club_whatsapp_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quality_rating", sa.String(16), nullable=True),
        sa.Column("messaging_limit_tier", sa.String(32), nullable=True),
        sa.Column("previous_rating", sa.String(16), nullable=True),
        sa.Column("previous_tier", sa.String(32), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index("ix_club_whatsapp_quality_log_account_id", "club_whatsapp_quality_log", ["account_id"])

    op.create_table(
        "club_whatsapp_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("club_whatsapp_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("meta_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("meta_status", sa.String(32), nullable=True),
        sa.Column("flag", sa.String(16), nullable=True),
        sa.Column("rejected_reason", sa.String(200), nullable=True),
        sa.Column("quality", sa.String(16), nullable=True),
        sa.Column("components", sa.JSON(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint("account_id", "name", "language", name="uq_club_whatsapp_templates_name"),
    )
    op.create_index("ix_club_whatsapp_templates_club_id", "club_whatsapp_templates", ["club_id"])

    op.create_table(
        "club_whatsapp_conversations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("club_whatsapp_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("business_phone_id", sa.String(32), nullable=False),
        sa.Column("wa_id", sa.String(20), nullable=True),
        sa.Column("bsuid", sa.String(160), nullable=True),
        sa.Column("profile_name", sa.String(120), nullable=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("registration_id", UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contact_name", sa.String(120), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(8), nullable=False, server_default="open"),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_direction", sa.String(3), nullable=True),
        sa.Column("last_message_type", sa.String(16), nullable=True),
        sa.Column("last_message_preview", sa.String(160), nullable=True),
        sa.Column("last_message_status", sa.String(12), nullable=True),
        sa.Column("awaiting_since", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("club_id", "business_phone_id", "wa_id", name="uq_club_whatsapp_conversations_contact"),
        sa.UniqueConstraint("club_id", "business_phone_id", "bsuid", name="uq_club_whatsapp_conversations_bsuid"),
        sa.CheckConstraint("wa_id IS NOT NULL OR bsuid IS NOT NULL", name="ck_club_whatsapp_conversations_who"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_club_whatsapp_conversations_status"),
    )
    op.create_index("ix_club_whatsapp_conversations_club_last", "club_whatsapp_conversations", ["club_id", "last_message_at"])

    op.create_table(
        "club_whatsapp_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.BigInteger(), sa.ForeignKey("club_whatsapp_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("media_id", sa.String(128), nullable=True),
        sa.Column("media_mime", sa.String(100), nullable=True),
        sa.Column("media_filename", sa.String(255), nullable=True),
        sa.Column("location", sa.JSON(), nullable=True),
        sa.Column("template_name", sa.String(512), nullable=True),
        sa.Column("template_language", sa.String(16), nullable=True),
        sa.Column("template_params", sa.JSON(), nullable=True),
        sa.Column("context_wamid", sa.String(128), nullable=True),
        sa.Column("wamid", sa.String(128), nullable=True, unique=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(16), nullable=True),
        sa.Column("error_title", sa.String(200), nullable=True),
        sa.Column("via", sa.String(12), nullable=True),
        sa.Column("delivery_id", UUID(as_uuid=True), nullable=True),
        sa.Column("sent_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sent_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sent_by_name", sa.String(120), nullable=True),
        sa.Column("response_seconds", sa.Integer(), nullable=True),
        sa.Column("wa_timestamp", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.CheckConstraint("direction IN ('in', 'out')", name="ck_club_whatsapp_messages_direction"),
    )
    op.create_index("ix_club_whatsapp_messages_conversation", "club_whatsapp_messages", ["conversation_id", "id"])
    op.create_index("ix_club_whatsapp_messages_thread", "club_whatsapp_messages", ["conversation_id", "wa_timestamp", "id"])
    op.create_index("ix_club_whatsapp_messages_club_created", "club_whatsapp_messages", ["club_id", "created_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_table("club_whatsapp_messages")
    op.drop_table("club_whatsapp_conversations")
    op.drop_table("club_whatsapp_templates")
    op.drop_table("club_whatsapp_quality_log")
    op.drop_table("club_whatsapp_accounts")
    op.drop_column("club_members", "whatsapp")
    op.drop_column("plan_features", "whatsapp_own")
