"""A club's WhatsApp inbox, worked as a team (app/api/club_whatsapp).

- club_whatsapp_conversations: who it is assigned to (the owner, or a
  member: assigned_owner / assigned_member_id / assigned_at), whether the
  club blocked them (blocked_at, blocked_meta: at Meta too, blocked_by_name),
  since when a message has been unread (unread_since) and the wait the team
  was last told of (waiting_alerted_since).
- club_whatsapp_optouts: who told the club to stop -- STOP, WhatsApp's own
  marketing setting (user_preferences), Meta's 131050, or the team -- by
  WhatsApp id and/or BSUID, scope all or marketing. Not inbox content:
  retention never deletes it.
- club_whatsapp_notes: the team's notes on a conversation (deleted with it).
- club_whatsapp_saved_replies: the club's canned answers.
- club_whatsapp_inbox_settings: per club, how long a message may wait
  before the team is told (1 to 48 hours, 4 by default), and the automatic
  answers to STOP and START.
- club_whatsapp_person_prefs: per person with the inbox, their mails.

The conversation's eight columns are nullable or have a constant default: a
catalog change, no rewrite, under the lock_timeout set first. Its three
indexes are partial (only the rows where they are set) and small; they are
built under that same limit -- the table is the inbox's alone and young
(#106), so a plain CREATE INDEX, not CONCURRENTLY, inside the migration's
transaction. The foreign keys take a brief SHARE ROW EXCLUSIVE on clubs,
users, club_members and club_whatsapp_conversations.

Downgrade drops the five tables, the columns and their indexes.

Revision ID: club_whatsapp_inbox
Revises: club_whatsapp
"""
import sqlalchemy as sa
from alembic import op

revision = "club_whatsapp_inbox"
down_revision = "club_whatsapp"
branch_labels = None
depends_on = None

CONVERSATIONS = "club_whatsapp_conversations"


def _now():
    return sa.text("now()")


def _set_null_index(name: str, table: str, column: str) -> None:
    """A column a deleted row elsewhere sets null: indexed where set."""
    op.create_index(name, table, [column], postgresql_where=sa.text(f"{column} IS NOT NULL"))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")

    op.add_column(CONVERSATIONS, sa.Column("assigned_owner", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column(CONVERSATIONS, sa.Column(
        "assigned_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True,
    ))
    op.add_column(CONVERSATIONS, sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(CONVERSATIONS, sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(CONVERSATIONS, sa.Column("blocked_meta", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column(CONVERSATIONS, sa.Column("blocked_by_name", sa.String(120), nullable=True))
    op.add_column(CONVERSATIONS, sa.Column("unread_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column(CONVERSATIONS, sa.Column("waiting_alerted_since", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_club_whatsapp_conversations_assignee", CONVERSATIONS, "NOT (assigned_owner AND assigned_member_id IS NOT NULL)",
    )
    _set_null_index("ix_club_whatsapp_conversations_assigned_member", CONVERSATIONS, "assigned_member_id")
    _set_null_index("ix_club_whatsapp_conversations_unread_since", CONVERSATIONS, "unread_since")
    _set_null_index("ix_club_whatsapp_conversations_awaiting", CONVERSATIONS, "awaiting_since")
    # What is unread now, as the alerts read it: the oldest message of each unread conversation.
    op.execute(
        """
        UPDATE club_whatsapp_conversations c SET unread_since = (
            SELECT min(m.created_at) FROM (
                SELECT created_at FROM club_whatsapp_messages
                WHERE conversation_id = c.id AND direction = 'in' AND type NOT IN ('reaction', 'system', 'unsupported')
                ORDER BY id DESC LIMIT c.unread_count
            ) m
        )
        WHERE c.unread_count > 0
        """
    )

    op.create_table(
        "club_whatsapp_optouts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("wa_id", sa.String(20), nullable=True),
        sa.Column("bsuid", sa.String(160), nullable=True),
        sa.Column("scope", sa.String(12), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("by_name", sa.String(120), nullable=True),
        sa.CheckConstraint("wa_id IS NOT NULL OR bsuid IS NOT NULL", name="ck_club_whatsapp_optouts_who"),
        sa.CheckConstraint("scope IN ('all', 'marketing')", name="ck_club_whatsapp_optouts_scope"),
        sa.CheckConstraint("source IN ('keyword', 'user_preferences', 'meta_131050', 'team')", name="ck_club_whatsapp_optouts_source"),
    )
    op.create_index("ix_club_whatsapp_optouts_club_id", "club_whatsapp_optouts", ["club_id"])
    op.create_index("uq_club_whatsapp_optouts_wa", "club_whatsapp_optouts", ["club_id", "scope", "wa_id"], unique=True,
                    postgresql_where=sa.text("wa_id IS NOT NULL"))
    op.create_index("uq_club_whatsapp_optouts_bsuid", "club_whatsapp_optouts", ["club_id", "scope", "bsuid"], unique=True,
                    postgresql_where=sa.text("bsuid IS NOT NULL"))
    _set_null_index("ix_club_whatsapp_optouts_by_user", "club_whatsapp_optouts", "by_user_id")
    _set_null_index("ix_club_whatsapp_optouts_by_member", "club_whatsapp_optouts", "by_member_id")

    op.create_table(
        "club_whatsapp_notes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.BigInteger(), sa.ForeignKey(f"{CONVERSATIONS}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_name", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index("ix_club_whatsapp_notes_club_id", "club_whatsapp_notes", ["club_id"])
    op.create_index("ix_club_whatsapp_notes_conversation", "club_whatsapp_notes", ["conversation_id", "id"])
    _set_null_index("ix_club_whatsapp_notes_author_user", "club_whatsapp_notes", "author_user_id")
    _set_null_index("ix_club_whatsapp_notes_author_member", "club_whatsapp_notes", "author_member_id")

    op.create_table(
        "club_whatsapp_saved_replies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(60), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("shortcut", sa.String(32), nullable=True),
        sa.Column("created_by_name", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index("ix_club_whatsapp_saved_replies_club_id", "club_whatsapp_saved_replies", ["club_id"])
    op.create_index("uq_club_whatsapp_saved_replies_shortcut", "club_whatsapp_saved_replies", ["club_id", "shortcut"], unique=True,
                    postgresql_where=sa.text("shortcut IS NOT NULL"))

    op.create_table(
        "club_whatsapp_inbox_settings",
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("alert_after_hours", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("stop_reply", sa.Text(), nullable=True),
        sa.Column("start_reply", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.CheckConstraint("alert_after_hours BETWEEN 1 AND 48", name="ck_club_whatsapp_inbox_settings_hours"),
    )

    op.create_table(
        "club_whatsapp_person_prefs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True),
        sa.Column("email_messages", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("email_assigned", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("messages_emailed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_club_whatsapp_person_prefs_club_id", "club_whatsapp_person_prefs", ["club_id"])
    op.create_index("uq_club_whatsapp_person_prefs_owner", "club_whatsapp_person_prefs", ["club_id"], unique=True,
                    postgresql_where=sa.text("member_id IS NULL"))
    op.create_index("uq_club_whatsapp_person_prefs_member", "club_whatsapp_person_prefs", ["member_id"], unique=True,
                    postgresql_where=sa.text("member_id IS NOT NULL"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_table("club_whatsapp_person_prefs")
    op.drop_table("club_whatsapp_inbox_settings")
    op.drop_table("club_whatsapp_saved_replies")
    op.drop_table("club_whatsapp_notes")
    op.drop_table("club_whatsapp_optouts")
    op.drop_index("ix_club_whatsapp_conversations_awaiting", table_name=CONVERSATIONS)
    op.drop_index("ix_club_whatsapp_conversations_unread_since", table_name=CONVERSATIONS)
    op.drop_index("ix_club_whatsapp_conversations_assigned_member", table_name=CONVERSATIONS)
    op.drop_constraint("ck_club_whatsapp_conversations_assignee", CONVERSATIONS, type_="check")
    for column in ("waiting_alerted_since", "unread_since", "blocked_by_name", "blocked_meta", "blocked_at",
                   "assigned_at", "assigned_member_id", "assigned_owner"):
        op.drop_column(CONVERSATIONS, column)
