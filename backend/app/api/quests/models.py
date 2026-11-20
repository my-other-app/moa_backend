"""Event quests: tasks, milestones and swag rewards (feature-register/quests/CONTRACT.md).

New tables only, plus three defaulted columns elsewhere (doors.purpose,
merch_items.reserved_count, merch_variants.reserved_count). One quest per
event, off until the organiser turns it on.

- quests: the switch and the settings.
- quest_tasks: what a guest does; `kind` names the rule (kinds.py), `config` its details.
- quest_milestones / quest_rewards: a threshold, and the swag items it unlocks.
- quest_progress: one guest and one task (count towards the target, when done, how).
- quest_entries: one guest's totals, read by the card and the leaderboard.
- quest_unlocks: a milestone a guest reached (told once).
- quest_claims: one reward for one guest, and its unit of stock (stock.py).
- quest_station_visits: a staff scan at a station door.
- quest_audit: grants, revokes, releases and re-offers, with who and when.
"""

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

NOW = sa.text("now()")

#: Door purposes (doors.purpose): an entrance has none.
STATION, SWAG_DESK = "station", "swag_desk"
PURPOSES = (STATION, SWAG_DESK)

CHECKMARKS, POINTS = "checkmarks", "points"
MODES = (CHECKMARKS, POINTS)

FIRST_COME, WAITLIST = "first_come", "waitlist"
SOLD_OUT_MODES = (FIRST_COME, WAITLIST)

#: Claim statuses (CONTRACT §4.1).
RESERVED, WAITLISTED, SOLD_OUT, COLLECTED, RELEASED = "reserved", "waitlisted", "sold_out", "collected", "released"
CLAIM_STATUSES = (RESERVED, WAITLISTED, SOLD_OUT, COLLECTED, RELEASED)


def _uuid_pk():
    return Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)


def _at(nullable: bool = True, **kw):
    return Column(TZAwareDateTime(timezone=True), nullable=nullable, **kw)


def _stamps():
    return (
        Column("created_at", TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now()),
        Column("updated_at", TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now()),
    )


class Quests(AbstractSQLModel):
    __tablename__ = "quests"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    title = Column(String(80), nullable=True)
    intro = Column(String(400), nullable=True)
    mode = Column(String(12), nullable=False, default=CHECKMARKS, server_default=CHECKMARKS)
    starts_at = _at()
    ends_at = _at()
    leaderboard = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    leaderboard_names = Column(String(8), nullable=False, default="initial", server_default="initial")
    code_attempts_per_hour = Column(Integer, nullable=False, default=10, server_default="10")
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at, updated_at = _stamps()


class QuestTasks(AbstractSQLModel):
    __tablename__ = "quest_tasks"

    id = _uuid_pk()
    quest_id = Column(UUID(as_uuid=True), ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(24), nullable=False)
    title = Column(String(80), nullable=False)
    description = Column(String(300), nullable=True)
    points = Column(Integer, nullable=False, default=10, server_default="10")
    required = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    starts_at = _at()
    ends_at = _at()
    config = Column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    #: A station task's door, out of `config` so a door can be found by it.
    door_id = Column(UUID(as_uuid=True), ForeignKey("doors.id", ondelete="SET NULL"), nullable=True)
    #: A code task's code, normalised (kinds.normal_code): matched by index.
    code = Column(String(40), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    #: A task some guests had, taken off the quest: kept for the record.
    archived_at = _at()
    created_at, updated_at = _stamps()

    __table_args__ = (
        sa.Index("ix_quest_tasks_quest", "quest_id", "sort_order"),
        sa.Index("ix_quest_tasks_door", "door_id", postgresql_where=sa.text("door_id IS NOT NULL")),
        # Two live code tasks of one quest never share a code.
        sa.Index("ux_quest_tasks_code", "quest_id", "code", unique=True, postgresql_where=sa.text("code IS NOT NULL AND archived_at IS NULL")),
    )


class QuestMilestones(AbstractSQLModel):
    __tablename__ = "quest_milestones"

    id = _uuid_pk()
    quest_id = Column(UUID(as_uuid=True), ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(80), nullable=False)
    threshold = Column(Integer, nullable=False)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_at, updated_at = _stamps()

    __table_args__ = (sa.Index("ix_quest_milestones_quest", "quest_id", "sort_order"),)


class QuestRewards(AbstractSQLModel):
    __tablename__ = "quest_rewards"

    id = _uuid_pk()
    quest_id = Column(UUID(as_uuid=True), ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    milestone_id = Column(UUID(as_uuid=True), ForeignKey("quest_milestones.id", ondelete="CASCADE"), nullable=False)
    merch_item_id = Column(UUID(as_uuid=True), ForeignKey("merch_items.id", ondelete="CASCADE"), nullable=False)
    sold_out_mode = Column(String(12), nullable=False, default=FIRST_COME, server_default=FIRST_COME)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_at, updated_at = _stamps()

    __table_args__ = (
        # An item is a reward once per quest: a guest is handed one of an item once.
        sa.UniqueConstraint("quest_id", "merch_item_id", name="uq_quest_rewards_item"),
        sa.Index("ix_quest_rewards_milestone", "milestone_id"),
        # The merch pages ask "is this item a reward?" by event.
        sa.Index("ix_quest_rewards_event_item", "event_id", "merch_item_id"),
    )


class QuestProgress(AbstractSQLModel):
    __tablename__ = "quest_progress"

    id = _uuid_pk()
    quest_id = Column(UUID(as_uuid=True), ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(UUID(as_uuid=True), ForeignKey("quest_tasks.id", ondelete="CASCADE"), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)
    count = Column(Integer, nullable=False, default=0, server_default="0")
    done_at = _at()
    #: "auto" | "station" | "code" | "grant"
    source = Column(String(10), nullable=True)
    granted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoked_at = _at()
    revoked_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())

    __table_args__ = (
        sa.UniqueConstraint("task_id", "registration_id", name="uq_quest_progress_task_registration"),
        # A guest's card: every task of theirs in one probe.
        sa.Index("ix_quest_progress_registration", "registration_id", "quest_id"),
    )


class QuestEntries(AbstractSQLModel):
    __tablename__ = "quest_entries"

    id = _uuid_pk()
    quest_id = Column(UUID(as_uuid=True), ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    tasks_done = Column(Integer, nullable=False, default=0, server_default="0")
    points = Column(Integer, nullable=False, default=0, server_default="0")
    required_left = Column(Integer, nullable=False, default=0, server_default="0")
    #: The leaderboard's score (tasks done or points, by the quest's mode) and when it was reached.
    score = Column(Integer, nullable=False, default=0, server_default="0")
    score_at = _at()
    #: Wrong codes since `code_window_at` (an hour from the first of them).
    code_fails = Column(Integer, nullable=False, default=0, server_default="0")
    code_window_at = _at()
    #: When the card last looked at the kinds it refreshes itself (§5.1).
    refreshed_at = _at()
    created_at, updated_at = _stamps()

    __table_args__ = (
        sa.UniqueConstraint("quest_id", "registration_id", name="uq_quest_entries_quest_registration"),
        # The leaderboard: best first, the earliest to get there first.
        sa.Index("ix_quest_entries_board", "quest_id", sa.text("score DESC"), "score_at"),
    )


class QuestUnlocks(AbstractSQLModel):
    __tablename__ = "quest_unlocks"

    id = _uuid_pk()
    quest_id = Column(UUID(as_uuid=True), ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    milestone_id = Column(UUID(as_uuid=True), ForeignKey("quest_milestones.id", ondelete="CASCADE"), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)
    unlocked_at = _at(nullable=False, server_default=NOW, default=sa.func.now())
    revoked_at = _at()
    notified_at = _at()

    __table_args__ = (
        sa.UniqueConstraint("milestone_id", "registration_id", name="uq_quest_unlocks_milestone_registration"),
        sa.Index("ix_quest_unlocks_registration", "registration_id", "quest_id"),
        sa.Index("ix_quest_unlocks_quest", "quest_id", sa.text("unlocked_at DESC")),
    )


class QuestClaims(AbstractSQLModel):
    __tablename__ = "quest_claims"

    id = _uuid_pk()
    quest_id = Column(UUID(as_uuid=True), ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    reward_id = Column(UUID(as_uuid=True), ForeignKey("quest_rewards.id", ondelete="CASCADE"), nullable=False)
    milestone_id = Column(UUID(as_uuid=True), ForeignKey("quest_milestones.id", ondelete="CASCADE"), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)
    merch_item_id = Column(UUID(as_uuid=True), ForeignKey("merch_items.id", ondelete="CASCADE"), nullable=False)
    #: The size held, once known. A reserved claim on a sized item without one holds an unsized unit.
    variant_id = Column(UUID(as_uuid=True), ForeignKey("merch_variants.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(12), nullable=False)
    #: Whether a unit is held right now (a reserved claim): what the item's counters count.
    holds = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    unlocked_at = _at(nullable=False, server_default=NOW, default=sa.func.now())
    reserved_at = _at()
    waitlisted_at = _at()
    collected_at = _at()
    collection_id = Column(UUID(as_uuid=True), ForeignKey("merch_collections.id", ondelete="SET NULL"), nullable=True)
    released_at = _at()
    held_notified_at = _at()
    created_at, updated_at = _stamps()

    __table_args__ = (
        sa.UniqueConstraint("reward_id", "registration_id", name="uq_quest_claims_reward_registration"),
        sa.Index("ix_quest_claims_registration", "registration_id", "quest_id"),
        # The waiting list of an item, in order.
        sa.Index("ix_quest_claims_waiting", "merch_item_id", "unlocked_at", postgresql_where=sa.text("status = 'waitlisted'")),
        sa.Index("ix_quest_claims_item_status", "merch_item_id", "status"),
    )


class QuestStationVisits(AbstractSQLModel):
    __tablename__ = "quest_station_visits"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    door_id = Column(UUID(as_uuid=True), ForeignKey("doors.id", ondelete="CASCADE"), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)
    visited_at = _at(nullable=False)
    scanned_at = _at()
    recorded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: "gate" | "offline_sync" | "desk"
    via = Column(String(14), nullable=False, default="gate", server_default="gate")
    client_ref = Column(String(64), nullable=True, unique=True)
    created_at = _at(nullable=False, server_default=NOW, default=sa.func.now())

    __table_args__ = (
        # One visit per station per guest counts.
        sa.UniqueConstraint("door_id", "registration_id", name="uq_quest_station_visits_door_registration"),
        sa.Index("ix_quest_station_visits_registration", "registration_id"),
        sa.Index("ix_quest_station_visits_event", "event_id", sa.text("visited_at DESC")),
    )


class QuestAudit(AbstractSQLModel):
    __tablename__ = "quest_audit"

    id = _uuid_pk()
    quest_id = Column(UUID(as_uuid=True), ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True)
    task_id = Column(UUID(as_uuid=True), ForeignKey("quest_tasks.id", ondelete="SET NULL"), nullable=True)
    reward_id = Column(UUID(as_uuid=True), ForeignKey("quest_rewards.id", ondelete="SET NULL"), nullable=True)
    #: "grant" | "revoke" | "release" | "reoffer" | "handover"
    action = Column(String(12), nullable=False)
    by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    by_name = Column(String(120), nullable=True)
    note = Column(String(300), nullable=True)
    detail = Column(JSONB, nullable=True)
    # The clock, not the transaction's start: two entries of one request keep their order.
    created_at = _at(nullable=False, server_default=NOW, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        sa.Index("ix_quest_audit_quest", "quest_id", sa.text("created_at DESC")),
        sa.Index("ix_quest_audit_registration", "registration_id", postgresql_where=sa.text("registration_id IS NOT NULL")),
    )
