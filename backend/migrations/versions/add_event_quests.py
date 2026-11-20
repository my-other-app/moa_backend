"""Event quests: tasks, milestones and swag rewards (feature-register/quests/CONTRACT.md).

Ten new tables, and three columns on existing ones:

- doors.purpose (nullable; NULL is an entrance, as every door is today) with
  a check added NOT VALID and validated in its own statement;
- merch_items.reserved_count, merch_variants.reserved_count (NOT NULL
  DEFAULT 0: a constant default, so no row is rewritten).

Adding a column takes each table's ACCESS EXCLUSIVE lock for as long as the
catalogue change takes (milliseconds); `lock_timeout` makes the migration
give up rather than queue behind a long transaction and stall the door. The
check's VALIDATE takes only SHARE UPDATE EXCLUSIVE (reads and writes go on).
The new tables are empty, and their indexes are made with them.

Revision ID: add_event_quests
Revises: add_guest_posters
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "add_event_quests"
down_revision = "add_guest_posters"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def _uuid_pk():
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _at(name, nullable=True, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kw)


def _stamps():
    return [_at("created_at", False, server_default=NOW), _at("updated_at", False, server_default=NOW)]


def _fk(name, target, ondelete, nullable=True, kind=None):
    return sa.Column(name, kind if kind is not None else UUID(as_uuid=True), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Existing tables: defaulted or nullable columns only.
    op.add_column("doors", sa.Column("purpose", sa.String(16), nullable=True))
    op.execute("ALTER TABLE doors ADD CONSTRAINT ck_doors_purpose CHECK (purpose IS NULL OR purpose IN ('station', 'swag_desk')) NOT VALID")
    op.add_column("merch_items", sa.Column("reserved_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("merch_variants", sa.Column("reserved_count", sa.Integer(), nullable=False, server_default="0"))
    op.execute("ALTER TABLE doors VALIDATE CONSTRAINT ck_doors_purpose")

    op.create_table(
        "quests",
        _uuid_pk(),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("title", sa.String(80), nullable=True),
        sa.Column("intro", sa.String(400), nullable=True),
        sa.Column("mode", sa.String(12), nullable=False, server_default="checkmarks"),
        _at("starts_at"),
        _at("ends_at"),
        sa.Column("leaderboard", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("leaderboard_names", sa.String(8), nullable=False, server_default="initial"),
        sa.Column("code_attempts_per_hour", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
    )
    op.create_table(
        "quest_tasks",
        _uuid_pk(),
        _fk("quest_id", "quests.id", "CASCADE", nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("description", sa.String(300), nullable=True),
        sa.Column("points", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.false()),
        _at("starts_at"),
        _at("ends_at"),
        sa.Column("config", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        _fk("door_id", "doors.id", "SET NULL"),
        sa.Column("code", sa.String(40), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        _at("archived_at"),
        *_stamps(),
    )
    op.create_index("ix_quest_tasks_quest", "quest_tasks", ["quest_id", "sort_order"])
    op.create_index("ix_quest_tasks_door", "quest_tasks", ["door_id"], postgresql_where=sa.text("door_id IS NOT NULL"))
    op.create_index(
        "ux_quest_tasks_code", "quest_tasks", ["quest_id", "code"], unique=True, postgresql_where=sa.text("code IS NOT NULL AND archived_at IS NULL")
    )

    op.create_table(
        "quest_milestones",
        _uuid_pk(),
        _fk("quest_id", "quests.id", "CASCADE", nullable=False),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
    )
    op.create_index("ix_quest_milestones_quest", "quest_milestones", ["quest_id", "sort_order"])

    op.create_table(
        "quest_rewards",
        _uuid_pk(),
        _fk("quest_id", "quests.id", "CASCADE", nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        _fk("milestone_id", "quest_milestones.id", "CASCADE", nullable=False),
        _fk("merch_item_id", "merch_items.id", "CASCADE", nullable=False),
        sa.Column("sold_out_mode", sa.String(12), nullable=False, server_default="first_come"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
        sa.UniqueConstraint("quest_id", "merch_item_id", name="uq_quest_rewards_item"),
    )
    op.create_index("ix_quest_rewards_milestone", "quest_rewards", ["milestone_id"])
    op.create_index("ix_quest_rewards_event_item", "quest_rewards", ["event_id", "merch_item_id"])

    op.create_table(
        "quest_progress",
        _uuid_pk(),
        _fk("quest_id", "quests.id", "CASCADE", nullable=False),
        _fk("task_id", "quest_tasks.id", "CASCADE", nullable=False),
        _fk("registration_id", "event_registrations_link.id", "CASCADE", nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        _at("done_at"),
        sa.Column("source", sa.String(10), nullable=True),
        sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _at("revoked_at"),
        sa.Column("revoked_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _at("updated_at", False, server_default=NOW),
        sa.UniqueConstraint("task_id", "registration_id", name="uq_quest_progress_task_registration"),
    )
    op.create_index("ix_quest_progress_registration", "quest_progress", ["registration_id", "quest_id"])

    op.create_table(
        "quest_entries",
        _uuid_pk(),
        _fk("quest_id", "quests.id", "CASCADE", nullable=False),
        _fk("registration_id", "event_registrations_link.id", "CASCADE", nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tasks_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("required_left", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        _at("score_at"),
        sa.Column("code_fails", sa.Integer(), nullable=False, server_default="0"),
        _at("code_window_at"),
        _at("refreshed_at"),
        *_stamps(),
        sa.UniqueConstraint("quest_id", "registration_id", name="uq_quest_entries_quest_registration"),
    )
    op.create_index("ix_quest_entries_board", "quest_entries", ["quest_id", sa.text("score DESC"), "score_at"])

    op.create_table(
        "quest_unlocks",
        _uuid_pk(),
        _fk("quest_id", "quests.id", "CASCADE", nullable=False),
        _fk("milestone_id", "quest_milestones.id", "CASCADE", nullable=False),
        _fk("registration_id", "event_registrations_link.id", "CASCADE", nullable=False),
        _at("unlocked_at", False, server_default=NOW),
        _at("revoked_at"),
        _at("notified_at"),
        sa.UniqueConstraint("milestone_id", "registration_id", name="uq_quest_unlocks_milestone_registration"),
    )
    op.create_index("ix_quest_unlocks_registration", "quest_unlocks", ["registration_id", "quest_id"])
    op.create_index("ix_quest_unlocks_quest", "quest_unlocks", ["quest_id", sa.text("unlocked_at DESC")])

    op.create_table(
        "quest_claims",
        _uuid_pk(),
        _fk("quest_id", "quests.id", "CASCADE", nullable=False),
        _fk("reward_id", "quest_rewards.id", "CASCADE", nullable=False),
        _fk("milestone_id", "quest_milestones.id", "CASCADE", nullable=False),
        _fk("registration_id", "event_registrations_link.id", "CASCADE", nullable=False),
        _fk("merch_item_id", "merch_items.id", "CASCADE", nullable=False),
        _fk("variant_id", "merch_variants.id", "SET NULL"),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("holds", sa.Boolean(), nullable=False, server_default=sa.false()),
        _at("unlocked_at", False, server_default=NOW),
        _at("reserved_at"),
        _at("waitlisted_at"),
        _at("collected_at"),
        _fk("collection_id", "merch_collections.id", "SET NULL"),
        _at("released_at"),
        _at("held_notified_at"),
        *_stamps(),
        sa.UniqueConstraint("reward_id", "registration_id", name="uq_quest_claims_reward_registration"),
    )
    op.create_index("ix_quest_claims_registration", "quest_claims", ["registration_id", "quest_id"])
    op.create_index("ix_quest_claims_waiting", "quest_claims", ["merch_item_id", "unlocked_at"], postgresql_where=sa.text("status = 'waitlisted'"))
    op.create_index("ix_quest_claims_item_status", "quest_claims", ["merch_item_id", "status"])

    op.create_table(
        "quest_station_visits",
        _uuid_pk(),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        _fk("door_id", "doors.id", "CASCADE", nullable=False),
        _fk("registration_id", "event_registrations_link.id", "CASCADE", nullable=False),
        _at("visited_at", False),
        _at("scanned_at"),
        sa.Column("recorded_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("via", sa.String(14), nullable=False, server_default="gate"),
        sa.Column("client_ref", sa.String(64), nullable=True, unique=True),
        _at("created_at", False, server_default=NOW),
        sa.UniqueConstraint("door_id", "registration_id", name="uq_quest_station_visits_door_registration"),
    )
    op.create_index("ix_quest_station_visits_registration", "quest_station_visits", ["registration_id"])
    op.create_index("ix_quest_station_visits_event", "quest_station_visits", ["event_id", sa.text("visited_at DESC")])

    op.create_table(
        "quest_audit",
        _uuid_pk(),
        _fk("quest_id", "quests.id", "CASCADE", nullable=False),
        _fk("registration_id", "event_registrations_link.id", "SET NULL"),
        _fk("task_id", "quest_tasks.id", "SET NULL"),
        _fk("reward_id", "quest_rewards.id", "SET NULL"),
        sa.Column("action", sa.String(12), nullable=False),
        sa.Column("by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("by_name", sa.String(120), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("detail", JSONB(), nullable=True),
        _at("created_at", False, server_default=NOW),
    )
    op.create_index("ix_quest_audit_quest", "quest_audit", ["quest_id", sa.text("created_at DESC")])
    op.create_index("ix_quest_audit_registration", "quest_audit", ["registration_id"], postgresql_where=sa.text("registration_id IS NOT NULL"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in (
        "quest_audit",
        "quest_station_visits",
        "quest_claims",
        "quest_unlocks",
        "quest_entries",
        "quest_progress",
        "quest_rewards",
        "quest_milestones",
        "quest_tasks",
        "quests",
    ):
        op.drop_table(table)
    op.drop_column("merch_variants", "reserved_count")
    op.drop_column("merch_items", "reserved_count")
    op.execute("ALTER TABLE doors DROP CONSTRAINT IF EXISTS ck_doors_purpose")
    op.drop_column("doors", "purpose")
