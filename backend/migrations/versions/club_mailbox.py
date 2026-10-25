"""A club's mailbox: <handle>@mail.myotherapp.in, in the dashboard (app/api/club_mailbox).

- club_mailboxes: one per club -- the name it sends as, its signature, its
  retention (1 to 36 months, 12 by default), when its plan lapsed.
- club_mailbox_addresses: its handles, current and old. A handle is unique
  across every row; the current one per club by a partial unique index; an
  old one forwards 90 days (`forward_until`).
- club_mail_threads, club_mail_messages, club_mail_attachments: the mail.
  One received mail is kept once per club (uq on club, provider and the
  provider's id: SNS delivers again what it got no 200 for).
- plan_features.mailbox: off by default, switched on for pro and enterprise
  (`grant`); admins edit it in Admin -> Subscriptions like every other flag.
- club_members.mail: the Mail permission an owner gives a team member, off
  by default.

Every table is new. The two columns are added with a constant default, a
catalog change (no rewrite), under the lock_timeout set first: club_members
is hot, so that statement waits at most 3s for its lock (env.py retries the
migration when it is not granted). The foreign keys take a brief SHARE ROW
EXCLUSIVE on clubs, users and club_members, under the same limit.
plan_features is a handful of rows.

Downgrade drops the tables and both columns.

Revision ID: club_mailbox
Revises: audit_search
"""
import sqlalchemy as sa
from alembic import op

revision = "club_mailbox"
down_revision = "audit_search"
branch_labels = None
depends_on = None

#: The plans that include a club mailbox, from the start.
PLANS = ("pro", "enterprise")


def grant() -> None:
    """Switch mailbox on for PLANS. Plans that do not exist are skipped; safe to run again."""
    op.get_bind().execute(
        sa.text(
            "UPDATE plan_features SET mailbox = true, updated_at = now() "
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


def _set_null_index(name: str, table: str, column: str) -> None:
    """A column a deleted row elsewhere sets null: indexed where set, so that delete finds its rows."""
    op.create_index(name, table, [column], postgresql_where=sa.text(f"{column} IS NOT NULL"))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("plan_features", sa.Column("mailbox", sa.Boolean(), nullable=False, server_default=sa.false()))
    grant()
    op.add_column("club_members", sa.Column("mail", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "club_mailboxes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("display_name", sa.String(80), nullable=True),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("retention_months", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("handle_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lapsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lapse_told_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expiry_told_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dropped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        sa.CheckConstraint("retention_months BETWEEN 1 AND 36", name="ck_club_mailboxes_retention"),
    )
    _set_null_index("ix_club_mailboxes_created_by", "club_mailboxes", "created_by")
    _set_null_index("ix_club_mailboxes_created_by_member", "club_mailboxes", "created_by_member_id")

    op.create_table(
        "club_mailbox_addresses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mailbox_id", sa.Integer(), sa.ForeignKey("club_mailboxes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("handle", sa.String(30), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("forward_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("handle = lower(handle)", name="ck_club_mailbox_addresses_lower"),
    )
    op.create_index(
        "uq_club_mailbox_addresses_current", "club_mailbox_addresses", ["club_id"], unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_index("ix_club_mailbox_addresses_mailbox", "club_mailbox_addresses", ["mailbox_id"])

    op.create_table(
        "club_mail_threads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
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
        *_stamps(),
        sa.CheckConstraint("folder IN ('inbox', 'sent', 'archive', 'spam')", name="ck_club_mail_threads_folder"),
    )
    op.create_index(
        "ix_club_mail_threads_club_folder_last", "club_mail_threads",
        ["club_id", "folder", sa.text("last_message_at DESC NULLS LAST"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_club_mail_threads_club_sent", "club_mail_threads", ["club_id", sa.text("last_outbound_at DESC")],
        postgresql_where=sa.text("last_outbound_at IS NOT NULL"),
    )
    op.create_index("ix_club_mail_threads_club_subject", "club_mail_threads", ["club_id", "subject_key"])

    op.create_table(
        "club_mail_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), sa.ForeignKey("club_mail_threads.id", ondelete="CASCADE"), nullable=False),
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
        sa.Column("sent_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sent_by_name", sa.String(120), nullable=True),
        sa.Column("error", sa.String(400), nullable=True),
        sa.CheckConstraint("direction IN ('in', 'out')", name="ck_club_mail_messages_direction"),
        sa.CheckConstraint(
            "status IN ('received', 'sending', 'sent', 'failed', 'bounced', 'complained')", name="ck_club_mail_messages_status",
        ),
        sa.UniqueConstraint("club_id", "provider", "provider_message_id", name="uq_club_mail_messages_provider"),
    )
    op.create_index("ix_club_mail_messages_thread", "club_mail_messages", ["thread_id", "id"])
    op.create_index("ix_club_mail_messages_club_created", "club_mail_messages", ["club_id", "created_at"])
    op.create_index(
        "ix_club_mail_messages_club_message_id", "club_mail_messages", ["club_id", "message_id"],
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_club_mail_messages_club_alt_id", "club_mail_messages", ["club_id", "alt_message_id"],
        postgresql_where=sa.text("alt_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_club_mail_messages_provider_id", "club_mail_messages", ["provider_message_id"],
        postgresql_where=sa.text("direction = 'out'"),
    )
    op.create_index(
        "ix_club_mail_messages_sent_by", "club_mail_messages", ["sent_by_user_id"],
        postgresql_where=sa.text("sent_by_user_id IS NOT NULL"),
    )
    op.create_index(
        "ix_club_mail_messages_sent_by_member", "club_mail_messages", ["sent_by_member_id"],
        postgresql_where=sa.text("sent_by_member_id IS NOT NULL"),
    )

    op.create_table(
        "club_mail_attachments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("club_mail_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("content_type", sa.String(120), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(300), nullable=False),
        sa.Column("content_id", sa.String(200), nullable=True),
        sa.Column("inline", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_club_mail_attachments_message", "club_mail_attachments", ["message_id"])
    op.create_index("ix_club_mail_attachments_club", "club_mail_attachments", ["club_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_table("club_mail_attachments")
    op.drop_table("club_mail_messages")
    op.drop_table("club_mail_threads")
    op.drop_table("club_mailbox_addresses")
    op.drop_table("club_mailboxes")
    op.drop_column("club_members", "mail")
    op.drop_column("plan_features", "mailbox")
