"""The team's support desk and sales leads (app/api/support, app/api/leads,
feature-register/support-leads-contract.md, docs/SUPPORT_AND_LEADS.md).

Fourteen new tables and one sequence, nothing changed in an existing table:

- admin_teams (the two teams, and the desk's settings on the support row),
  admin_team_members;
- support_topics, support_saved_replies, support_help_answers;
- support_cases (its number from support_case_number_seq, from 1001, shown
  as #1001), support_messages, support_attachments;
- leads, lead_contacts, lead_activities, lead_files, follow_ups,
  lead_lost_reasons.

Reference rows, as the code expects them (app/api/support/reference.py):
the teams `support` and `sales`; the seven topics (account, tickets,
payments, dashboard, report, bug, other -- payments and report high
priority); the five lost reasons. Nothing else is inserted.

Lock-safe: every table is new and empty, so its indexes are built on
nothing. The only locks on busy tables are the brief SHARE ROW EXCLUSIVE the
foreign keys take on users, clubs and themselves, under lock_timeout 3s.

Downgrade drops the fourteen tables and the sequence: every case, message,
lead and follow-up goes with them, and the desk's files stay in the bucket
(support/, leads/) unreferenced.

Revision ID: support_desk
Revises: ticket_self_cancel
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "support_desk"
down_revision = "ticket_self_cancel"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")
TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY_LIST = sa.text("'[]'::jsonb")
EMPTY_DICT = sa.text("'{}'::jsonb")

TEAMS = (("support", "Support"), ("sales", "Sales"))
TOPICS = (
    ("account", "Account and sign-in", "normal"),
    ("tickets", "Tickets and registration", "normal"),
    ("payments", "Payments and refunds", "high"),
    ("dashboard", "Club dashboard", "normal"),
    ("report", "Report a club or event", "high"),
    ("bug", "Bug", "normal"),
    ("other", "Other", "normal"),
)
LOST_REASONS = (
    ("no_budget", "No budget"),
    ("timing", "Timing"),
    ("chose_another", "Chose another"),
    ("no_reply", "No reply"),
    ("not_a_fit", "Not a fit"),
)


def _in(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _stamps():
    return [
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    ]


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("CREATE SEQUENCE IF NOT EXISTS support_case_number_seq START WITH 1001")

    op.create_table(
        "admin_teams",
        sa.Column("key", sa.String(20), primary_key=True),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("settings", JSONB, nullable=True),
        *_stamps(),
        if_not_exists=True,
    )
    op.create_table(
        "admin_team_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("team", sa.String(20), sa.ForeignKey("admin_teams.key", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("away", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("away_until", TS, nullable=True),
        sa.Column("last_assigned_at", TS, nullable=True),
        *_stamps(),
        sa.UniqueConstraint("team", "user_id", name="uq_admin_team_members_team_user"),
        if_not_exists=True,
    )
    op.create_index("ix_admin_team_members_user_id", "admin_team_members", ["user_id"], if_not_exists=True)

    op.create_table(
        "support_topics",
        sa.Column("key", sa.String(30), primary_key=True),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("assign", sa.String(12), nullable=False, server_default="round_robin"),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("default_priority", sa.String(8), nullable=False, server_default="normal"),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_stamps(),
        sa.CheckConstraint("assign IN ('round_robin', 'person')", name="ck_support_topics_assign"),
        sa.CheckConstraint(_in("default_priority", ("low", "normal", "high", "urgent")), name="ck_support_topics_priority"),
        if_not_exists=True,
    )
    op.create_table(
        "support_saved_replies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("topic", sa.String(30), sa.ForeignKey("support_topics.key", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        if_not_exists=True,
    )
    op.create_table(
        "support_help_answers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("topic", sa.String(30), sa.ForeignKey("support_topics.key", ondelete="SET NULL"), nullable=True),
        sa.Column("question", sa.String(300), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
        if_not_exists=True,
    )

    op.create_table(
        "support_cases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("number", sa.BigInteger(), nullable=False, server_default=sa.text("nextval('support_case_number_seq')")),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="new"),
        sa.Column("priority", sa.String(8), nullable=False, server_default="normal"),
        sa.Column("topic", sa.String(30), sa.ForeignKey("support_topics.key"), nullable=True),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("requester_kind", sa.String(8), nullable=False),
        sa.Column("requester_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requester_club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requester_member_id", sa.Integer(), nullable=True),
        sa.Column("requester_email", sa.String(254), nullable=True),
        sa.Column("requester_name", sa.String(120), nullable=True),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tags", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("flags", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("links", JSONB, nullable=False, server_default=EMPTY_DICT),
        sa.Column("context", JSONB, nullable=True),
        sa.Column("first_reply_due", TS, nullable=True),
        sa.Column("resolution_due", TS, nullable=True),
        sa.Column("first_replied_at", TS, nullable=True),
        sa.Column("waiting_since", TS, nullable=True),
        sa.Column("reminded_at", TS, nullable=True),
        sa.Column("resolved_at", TS, nullable=True),
        sa.Column("closed_at", TS, nullable=True),
        sa.Column("close_reason", sa.String(20), nullable=True),
        sa.Column("last_message_at", TS, nullable=True),
        sa.Column("last_inbound_at", TS, nullable=True),
        sa.Column("last_outbound_at", TS, nullable=True),
        sa.Column("requester_read_at", TS, nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("survey_token", sa.String(64), nullable=True),
        sa.Column("survey_sent_at", TS, nullable=True),
        sa.Column("survey_solved", sa.Boolean(), nullable=True),
        sa.Column("survey_comment", sa.String(500), nullable=True),
        sa.Column("survey_at", TS, nullable=True),
        sa.Column("parent_case_id", sa.Integer(), sa.ForeignKey("support_cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("merged_into_id", sa.Integer(), sa.ForeignKey("support_cases.id", ondelete="SET NULL"), nullable=True),
        # Its foreign key to leads is added below, once leads exists.
        sa.Column("lead_id", sa.Integer(), nullable=True),
        sa.Column("purged_at", TS, nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("number", name="support_cases_number_key"),
        sa.UniqueConstraint("survey_token", name="support_cases_survey_token_key"),
        sa.CheckConstraint(_in("status", ("new", "open", "waiting", "resolved", "closed")), name="ck_support_cases_status"),
        sa.CheckConstraint(_in("priority", ("low", "normal", "high", "urgent")), name="ck_support_cases_priority"),
        sa.CheckConstraint(_in("channel", ("app", "web", "dashboard", "email", "manual")), name="ck_support_cases_channel"),
        sa.CheckConstraint(_in("requester_kind", ("user", "guest", "club")), name="ck_support_cases_requester_kind"),
        if_not_exists=True,
    )
    op.execute("ALTER SEQUENCE support_case_number_seq OWNED BY support_cases.number")
    op.create_index("ix_support_cases_requester_user_id", "support_cases", ["requester_user_id"], if_not_exists=True)
    op.create_index("ix_support_cases_requester_club_id", "support_cases", ["requester_club_id"], if_not_exists=True)
    op.create_index("ix_support_cases_assignee_id", "support_cases", ["assignee_id"], if_not_exists=True)
    op.create_index("ix_support_cases_status_updated", "support_cases", ["status", "updated_at"], if_not_exists=True)
    op.create_index("ix_support_cases_waiting", "support_cases", ["waiting_since"],
                    postgresql_where=sa.text("status = 'waiting'"), if_not_exists=True)
    op.create_index("ix_support_cases_resolved", "support_cases", ["resolved_at"],
                    postgresql_where=sa.text("status = 'resolved'"), if_not_exists=True)
    op.create_index("ix_support_cases_closed", "support_cases", ["closed_at"],
                    postgresql_where=sa.text("status = 'closed' AND purged_at IS NULL"), if_not_exists=True)
    op.create_index("ix_support_cases_requester_email", "support_cases", [sa.text("lower(requester_email)")], if_not_exists=True)

    op.create_table(
        "support_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("support_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("author_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_name", sa.String(120), nullable=True),
        sa.Column("from_address", sa.String(254), nullable=True),
        sa.Column("to_addresses", JSONB, nullable=True),
        sa.Column("saved_reply_id", sa.Integer(), sa.ForeignKey("support_saved_replies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("email_message_id", sa.String(400), nullable=True),
        sa.Column("in_reply_to", sa.String(400), nullable=True),
        sa.Column("references", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(12), nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("alt_message_id", sa.String(400), nullable=True),
        sa.Column("delivery_status", sa.String(12), nullable=True),
        sa.Column("delivery_error", sa.String(400), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint(_in("kind", ("reply_in", "reply_out", "note", "system")), name="ck_support_messages_kind"),
        if_not_exists=True,
    )
    op.create_index("ix_support_messages_case", "support_messages", ["case_id", "created_at"], if_not_exists=True)
    op.create_index("uq_support_messages_inbound", "support_messages", ["provider", "provider_message_id"], unique=True,
                    postgresql_where=sa.text("kind = 'reply_in' AND provider_message_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_support_messages_provider_id", "support_messages", ["provider_message_id"],
                    postgresql_where=sa.text("provider_message_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_support_messages_email_id", "support_messages", ["email_message_id"],
                    postgresql_where=sa.text("email_message_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_support_messages_alt_id", "support_messages", ["alt_message_id"],
                    postgresql_where=sa.text("alt_message_id IS NOT NULL"), if_not_exists=True)

    op.create_table(
        "support_attachments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("upload_key", sa.String(64), nullable=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("support_cases.id", ondelete="CASCADE"), nullable=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("support_messages.id", ondelete="CASCADE"), nullable=True),
        sa.Column("storage_key", sa.String(300), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("content_type", sa.String(120), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("upload_key", name="support_attachments_upload_key_key"),
        if_not_exists=True,
    )
    op.create_index("ix_support_attachments_case", "support_attachments", ["case_id"], if_not_exists=True)
    op.create_index("ix_support_attachments_message", "support_attachments", ["message_id"], if_not_exists=True)
    op.create_index("ix_support_attachments_loose", "support_attachments", ["created_at"],
                    postgresql_where=sa.text("case_id IS NULL"), if_not_exists=True)

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("stage", sa.String(12), nullable=False, server_default="new"),
        sa.Column("source", sa.String(16), nullable=False, server_default="other"),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("city_key", sa.String(120), nullable=True),
        sa.Column("website", sa.String(300), nullable=True),
        sa.Column("email_domain", sa.String(200), nullable=True),
        sa.Column("value_inr", sa.Integer(), nullable=True),
        sa.Column("expected_close", sa.Date(), nullable=True),
        sa.Column("tags", JSONB, nullable=False, server_default=EMPTY_LIST),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("support_cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lost_reason", sa.String(60), nullable=True),
        sa.Column("lost_note", sa.String(500), nullable=True),
        sa.Column("retry_on", sa.Date(), nullable=True),
        sa.Column("won_at", TS, nullable=True),
        sa.Column("lost_at", TS, nullable=True),
        sa.Column("renewal_on", sa.Date(), nullable=True),
        sa.Column("stage_changed_at", TS, nullable=True),
        sa.Column("last_activity_at", TS, nullable=True),
        sa.Column("merged_into_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        sa.CheckConstraint(_in("type", ("club", "sponsor", "venue", "vendor", "other")), name="ck_leads_type"),
        sa.CheckConstraint(_in("stage", ("new", "contacted", "meeting", "proposal", "negotiation", "won", "lost")), name="ck_leads_stage"),
        sa.CheckConstraint(_in("source", ("for_clubs", "referral", "event", "outreach", "inbound_email", "import", "signup", "speaker",
                                          "other")), name="ck_leads_source"),
        if_not_exists=True,
    )
    op.create_index("ix_leads_owner_id", "leads", ["owner_id"], if_not_exists=True)
    op.create_index("ix_leads_club_id", "leads", ["club_id"], if_not_exists=True)
    op.create_index("ix_leads_stage", "leads", ["stage", "updated_at"], postgresql_where=sa.text("merged_into_id IS NULL"), if_not_exists=True)
    op.create_index("ix_leads_name_key", "leads", ["name_key"], postgresql_where=sa.text("merged_into_id IS NULL"), if_not_exists=True)
    op.create_index("ix_leads_email_domain", "leads", ["email_domain"],
                    postgresql_where=sa.text("email_domain IS NOT NULL AND merged_into_id IS NULL"), if_not_exists=True)
    op.create_foreign_key("fk_support_cases_lead", "support_cases", "leads", ["lead_id"], ["id"], ondelete="SET NULL")

    op.create_table(
        "lead_contacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("role", sa.String(120), nullable=True),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("whatsapp", sa.String(30), nullable=True),
        sa.Column("preferred_channel", sa.String(10), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_stamps(),
        sa.CheckConstraint("preferred_channel IS NULL OR " + _in("preferred_channel", ("email", "phone", "whatsapp", "meeting", "other")),
                           name="ck_lead_contacts_channel"),
        if_not_exists=True,
    )
    op.create_index("ix_lead_contacts_lead_id", "lead_contacts", ["lead_id"], if_not_exists=True)
    op.create_index("ix_lead_contacts_email", "lead_contacts", [sa.text("lower(email)")],
                    postgresql_where=sa.text("email IS NOT NULL"), if_not_exists=True)

    op.create_table(
        "lead_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("storage_key", sa.String(300), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("content_type", sa.String(120), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        if_not_exists=True,
    )
    op.create_index("ix_lead_files_lead_id", "lead_files", ["lead_id"], if_not_exists=True)

    op.create_table(
        "lead_activities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(14), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("occurred_at", TS, nullable=False, server_default=NOW),
        sa.Column("contact_id", sa.Integer(), sa.ForeignKey("lead_contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("lead_files.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_name", sa.String(120), nullable=True),
        sa.Column("details", JSONB, nullable=True),
        sa.Column("provider", sa.String(12), nullable=True),
        sa.Column("provider_message_id", sa.String(200), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint(_in("kind", ("call", "meeting", "email", "whatsapp", "note", "file", "stage_change", "system")),
                           name="ck_lead_activities_kind"),
        if_not_exists=True,
    )
    op.create_index("ix_lead_activities_lead", "lead_activities", ["lead_id", "occurred_at"], if_not_exists=True)
    op.create_index("uq_lead_activities_mail", "lead_activities", ["lead_id", "provider", "provider_message_id"], unique=True,
                    postgresql_where=sa.text("provider_message_id IS NOT NULL"), if_not_exists=True)

    op.create_table(
        "follow_ups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("support_cases.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("due_at", TS, nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("done_at", TS, nullable=True),
        sa.Column("done_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("overdue_flagged_at", TS, nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        sa.CheckConstraint(_in("kind", ("call", "meeting", "email", "whatsapp", "visit", "other")), name="ck_follow_ups_kind"),
        sa.CheckConstraint("(lead_id IS NULL) <> (case_id IS NULL)", name="ck_follow_ups_one_parent"),
        if_not_exists=True,
    )
    op.create_index("ix_follow_ups_open", "follow_ups", ["owner_id", "due_at"], postgresql_where=sa.text("NOT done"), if_not_exists=True)
    op.create_index("ix_follow_ups_lead", "follow_ups", ["lead_id"], postgresql_where=sa.text("lead_id IS NOT NULL"), if_not_exists=True)
    op.create_index("ix_follow_ups_case", "follow_ups", ["case_id"], postgresql_where=sa.text("case_id IS NOT NULL"), if_not_exists=True)

    op.create_table(
        "lead_lost_reasons",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("key", name="lead_lost_reasons_key_key"),
        if_not_exists=True,
    )

    seed()


def seed() -> None:
    """The reference rows: the two teams, the seven topics, the five lost reasons."""
    teams = sa.table("admin_teams", sa.column("key"), sa.column("label"))
    topics = sa.table("support_topics", sa.column("key"), sa.column("label"), sa.column("default_priority"), sa.column("position"))
    reasons = sa.table("lead_lost_reasons", sa.column("key"), sa.column("label"), sa.column("position"))
    bind = op.get_bind()
    for key, label in TEAMS:
        bind.execute(postgresql.insert(teams).values(key=key, label=label).on_conflict_do_nothing())
    for n, (key, label, priority) in enumerate(TOPICS):
        bind.execute(postgresql.insert(topics).values(key=key, label=label, default_priority=priority, position=n).on_conflict_do_nothing())
    for n, (key, label) in enumerate(LOST_REASONS):
        bind.execute(postgresql.insert(reasons).values(key=key, label=label, position=n).on_conflict_do_nothing())


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    for table in (
        "follow_ups", "lead_activities", "lead_files", "lead_contacts", "lead_lost_reasons",
        "support_attachments", "support_messages",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("ALTER TABLE IF EXISTS support_cases DROP CONSTRAINT IF EXISTS fk_support_cases_lead")
    op.execute("DROP TABLE IF EXISTS leads")
    for table in ("support_cases", "support_help_answers", "support_saved_replies", "support_topics", "admin_team_members", "admin_teams"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP SEQUENCE IF EXISTS support_case_number_seq")
