"""Profiles and the Network (feature-register/profiles-networking-contract.md).

- user_profiles gains headline, bio, city, the section levels (`visibility`,
  JSONB), who sees the address and number (`share_email`, `share_phone`),
  the switches (is_private, indexable, notify_saves, recap_emails,
  scanning_enabled, show_events/_certificates/_talks/_clubs) and the
  admin's hide (`suspended_at`). Every one nullable or with a constant
  default: a catalogue change, no rewrite.
- users: an index on lower(username), the handle's case-blind lookup.
  users holds thousands of rows; the build takes moments.
- New tables: profile_links, profile_projects, profile_codes,
  network_people, network_encounters, network_save_notices, network_recaps,
  each with the indexes the contract lists and the ones its queries need.
- content_reports.target_type may be `profile` (target_id = the owner's
  user id in digits): ck_content_reports_target_type is dropped and made
  again with it. content_reports is small; the check reads it once.

lock_timeout bounds every wait for a lock at three seconds: on a busy
moment the migration fails and is run again rather than holding up
sign-ins behind its ALTER TABLE.

Downgrade drops the new tables and columns, and puts the check back without
`profile` (reports on profiles, which it would refuse, are deleted first).

Revision ID: profiles_networking
Revises: site_templates_v2
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "profiles_networking"
down_revision = "site_templates_v2"
branch_labels = None
depends_on = None

VISIBILITY = '{"links": "everyone", "projects": "everyone", "achievements": "everyone", "interests": "everyone", "org": "everyone"}'
OLD_TARGETS = "target_type IN ('thread', 'comment', 'user', 'club')"
NEW_TARGETS = "target_type IN ('thread', 'comment', 'user', 'club', 'profile')"

#: (name, type, server default) of each new user_profiles column.
PROFILE_COLUMNS = (
    ("headline", sa.String(120), None),
    ("bio", sa.String(300), None),
    ("city", sa.String(80), None),
    ("visibility", JSONB(), sa.text(f"'{VISIBILITY}'::jsonb")),
    ("share_email", sa.String(12), "nobody"),
    ("share_phone", sa.String(12), "nobody"),
    ("is_private", sa.Boolean(), sa.false()),
    ("indexable", sa.Boolean(), sa.false()),
    ("notify_saves", sa.Boolean(), sa.true()),
    ("recap_emails", sa.Boolean(), sa.true()),
    ("scanning_enabled", sa.Boolean(), sa.true()),
    ("show_events", sa.Boolean(), sa.false()),
    ("show_certificates", sa.Boolean(), sa.true()),
    ("show_talks", sa.Boolean(), sa.true()),
    ("show_clubs", sa.Boolean(), sa.false()),
    ("suspended_at", sa.DateTime(timezone=True), None),
)


def _now():
    return sa.text("now()")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    for name, type_, default in PROFILE_COLUMNS:
        # Without a default: nullable (the texts, the hide). With one: NOT NULL.
        op.add_column("user_profiles", sa.Column(name, type_, nullable=default is None, server_default=default))
    op.create_index("ix_users_username_lower", "users", [sa.text("lower(username)")])

    op.create_table(
        "profile_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("label", sa.String(60), nullable=True),
        sa.Column("position", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_profile_links_user_position", "profile_links", ["user_id", "position"])

    op.create_table(
        "profile_projects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("summary", sa.String(160), nullable=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("image_key", sa.String(255), nullable=True),
        sa.Column("tags", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("role", sa.String(60), nullable=True),
        sa.Column("month", sa.Date(), nullable=True),
        sa.Column("position", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index("ix_profile_projects_user_position", "profile_projects", ["user_id", "position"])

    op.create_table(
        "profile_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(16), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ux_profile_codes_user_live", "profile_codes", ["user_id"], unique=True, postgresql_where=sa.text("replaced_at IS NULL"))

    op.create_table(
        "network_people",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("tags", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("mutual", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint("owner_id", "person_id", name="uq_network_people_owner_person"),
        sa.CheckConstraint("owner_id <> person_id", name="ck_network_people_not_self"),
    )
    op.create_index("ix_network_people_owner_created", "network_people", ["owner_id", sa.text("created_at DESC")])
    op.create_index("ix_network_people_person", "network_people", ["person_id"])
    op.create_index("ix_network_people_created", "network_people", ["created_at"])

    op.create_table(
        "network_encounters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("network_person_id", sa.Integer(), sa.ForeignKey("network_people.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("met_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("method IN ('qr', 'discovery', 'link', 'search')", name="ck_network_encounters_method"),
    )
    op.create_index(
        "ux_network_encounters_person_event", "network_encounters", ["network_person_id", "event_id"],
        unique=True, postgresql_where=sa.text("event_id IS NOT NULL"),
    )
    op.create_index("ix_network_encounters_person_met", "network_encounters", ["network_person_id", sa.text("met_at DESC")])
    op.create_index("ix_network_encounters_event_met", "network_encounters", ["event_id", "met_at"], postgresql_where=sa.text("event_id IS NOT NULL"))

    op.create_table(
        "network_save_notices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=True),
        sa.Column("saver_ids", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
    )
    op.create_index(
        "ux_network_save_notices_open", "network_save_notices", ["person_id", sa.text("COALESCE(event_id, 0)")],
        unique=True, postgresql_where=sa.text("sent_at IS NULL"),
    )

    op.create_table(
        "network_recaps",
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_network_recaps_event", "network_recaps", ["event_id"])

    op.drop_constraint("ck_content_reports_target_type", "content_reports", type_="check")
    op.create_check_constraint("ck_content_reports_target_type", "content_reports", NEW_TARGETS)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("DELETE FROM content_reports WHERE target_type = 'profile'")
    op.drop_constraint("ck_content_reports_target_type", "content_reports", type_="check")
    op.create_check_constraint("ck_content_reports_target_type", "content_reports", OLD_TARGETS)
    for table in ("network_recaps", "network_save_notices", "network_encounters", "network_people", "profile_codes", "profile_projects", "profile_links"):
        op.drop_table(table)
    op.drop_index("ix_users_username_lower", table_name="users")
    for name, _, _ in reversed(PROFILE_COLUMNS):
        op.drop_column("user_profiles", name)
