"""Every club's address, and the platform's own inbox (feature-register/mail-and-slugs-contract.md, Part M).

- club_mailbox_addresses.automatic (boolean, false): an address the
  platform made from the club's name (app/api/club_mailbox/service.py
  ensure_address), not one the club picked. Its first change is not the
  month's. Every club gets one when it is created; the clubs before this
  get theirs from the backfill (scripts/mailbox_backfill.py, or the admin's
  POST /admin/club-mailbox/backfill) -- data, not this migration.
- platform_mail_threads, platform_mail_messages, platform_mail_attachments:
  the admin portal's Mail for admin@, developer@, developers@ and info@
  (app/api/platform_mail): the club mailbox's tables without a club, each
  thread and message filed under its `mailbox` (the address's local part).
  A received mail is kept once per (mailbox, provider, provider's id).

The column is added with a constant default, a catalog change (no rewrite),
under lock_timeout 3s (club_mailbox_addresses is small; env.py retries a
lock not granted). The tables are new.

Downgrade drops the three tables and the column.

Revision ID: platform_mail
Revises: certificate_design
"""
import sqlalchemy as sa
from alembic import op

revision = "platform_mail"
down_revision = "certificate_design"
branch_labels = None
depends_on = None


def _now():
    return sa.text("now()")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("club_mailbox_addresses", sa.Column("automatic", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "platform_mail_threads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("mailbox", sa.String(64), nullable=False),
        sa.Column("folder", sa.String(8), nullable=False, server_default="inbox"),
        sa.Column("subject", sa.String(300), nullable=True),
        sa.Column("subject_key", sa.String(300), nullable=True),
        sa.Column("participants", sa.JSON(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_from", sa.String(200), nullable=True),
        sa.Column("snippet", sa.String(200), nullable=True),
        sa.Column("has_attachments", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.CheckConstraint("folder IN ('inbox', 'sent', 'archive', 'spam')", name="ck_platform_mail_threads_folder"),
    )
    op.create_index(
        "ix_platform_mail_threads_mailbox_folder_last", "platform_mail_threads",
        ["mailbox", "folder", sa.text("last_message_at DESC NULLS LAST"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_platform_mail_threads_folder_last", "platform_mail_threads",
        ["folder", sa.text("last_message_at DESC NULLS LAST"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_platform_mail_threads_sent", "platform_mail_threads", [sa.text("last_outbound_at DESC")],
        postgresql_where=sa.text("last_outbound_at IS NOT NULL"),
    )
    op.create_index("ix_platform_mail_threads_mailbox_subject", "platform_mail_threads", ["mailbox", "subject_key"])

    op.create_table(
        "platform_mail_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("thread_id", sa.BigInteger(), sa.ForeignKey("platform_mail_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mailbox", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("message_id", sa.String(400), nullable=True),
        sa.Column("alt_message_id", sa.String(400), nullable=True),
        sa.Column("in_reply_to", sa.String(400), nullable=True),
        sa.Column("references", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(16), nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("delivered_to", sa.String(320), nullable=True),
        sa.Column("from_address", sa.String(320), nullable=True),
        sa.Column("from_name", sa.String(200), nullable=True),
        sa.Column("to_addresses", sa.JSON(), nullable=True),
        sa.Column("cc_addresses", sa.JSON(), nullable=True),
        sa.Column("bcc_addresses", sa.JSON(), nullable=True),
        sa.Column("reply_to", sa.String(320), nullable=True),
        sa.Column("subject", sa.String(300), nullable=True),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("has_remote_images", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("snippet", sa.String(200), nullable=True),
        sa.Column("verdicts", sa.JSON(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("unread", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("sent_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sent_by_name", sa.String(120), nullable=True),
        sa.Column("error", sa.String(400), nullable=True),
        sa.CheckConstraint("direction IN ('in', 'out')", name="ck_platform_mail_messages_direction"),
        sa.CheckConstraint(
            "status IN ('received', 'sending', 'sent', 'failed', 'bounced', 'complained')", name="ck_platform_mail_messages_status",
        ),
        sa.UniqueConstraint("mailbox", "provider", "provider_message_id", name="uq_platform_mail_messages_provider"),
    )
    op.create_index("ix_platform_mail_messages_thread", "platform_mail_messages", ["thread_id", "id"])
    op.create_index(
        "ix_platform_mail_messages_mailbox_message_id", "platform_mail_messages", ["mailbox", "message_id"],
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_platform_mail_messages_mailbox_alt_id", "platform_mail_messages", ["mailbox", "alt_message_id"],
        postgresql_where=sa.text("alt_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_platform_mail_messages_provider_id", "platform_mail_messages", ["provider_message_id"],
        postgresql_where=sa.text("direction = 'out'"),
    )
    op.create_index(
        "ix_platform_mail_messages_out_created", "platform_mail_messages", ["created_at"],
        postgresql_where=sa.text("direction = 'out'"),
    )
    op.create_index(
        "ix_platform_mail_messages_sent_by", "platform_mail_messages", ["sent_by_user_id"],
        postgresql_where=sa.text("sent_by_user_id IS NOT NULL"),
    )

    op.create_table(
        "platform_mail_attachments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("platform_mail_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("content_type", sa.String(120), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(300), nullable=False),
        sa.Column("content_id", sa.String(200), nullable=True),
        sa.Column("inline", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_platform_mail_attachments_message", "platform_mail_attachments", ["message_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_table("platform_mail_attachments")
    op.drop_table("platform_mail_messages")
    op.drop_table("platform_mail_threads")
    op.drop_column("club_mailbox_addresses", "automatic")
