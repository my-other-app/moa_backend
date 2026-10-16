"""A competition on an event: tracks, teams, submissions, judges and scores, mentors and their slots.

Revision ID: add_competitions
Revises: add_event_types
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_competitions"
down_revision = "add_event_types"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def _stamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "competitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("team_min", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("team_max", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("teams_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("submissions_close_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submissions_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requirements", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("rubric", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("leaderboard_public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("shortlist_size", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("final_size", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("slot_minutes", sa.Integer(), nullable=False, server_default="15"),
        *_stamps(),
    )
    op.create_index("ix_competitions_event_id", "competitions", ["event_id"])
    op.create_table(
        "competition_tracks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(600), nullable=True),
        sa.Column("sponsor", sa.String(120), nullable=True),
        sa.Column("prize", sa.String(120), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
    )
    op.create_index("ix_competition_tracks_competition_id", "competition_tracks", ["competition_id"])
    op.create_table(
        "competition_teams",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("competition_tracks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stage", sa.String(12), nullable=False, server_default="entered"),
        sa.Column("created_by_registration_id", UUID, sa.ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_competition_teams_competition_id", "competition_teams", ["competition_id"])
    op.create_index("ix_competition_teams_track_id", "competition_teams", ["track_id"])
    op.create_table(
        "competition_team_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("competition_teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", UUID, sa.ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False),
        *_stamps(),
        sa.UniqueConstraint("team_id", "registration_id", name="uq_competition_team_members_once"),
    )
    op.create_index("ix_competition_team_members_team_id", "competition_team_members", ["team_id"])
    op.create_index("ix_competition_team_members_registration_id", "competition_team_members", ["registration_id"])
    op.create_table(
        "competition_seekers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", UUID, sa.ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("note", sa.String(200), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("competition_id", "registration_id", name="uq_competition_seekers_once"),
    )
    op.create_index("ix_competition_seekers_competition_id", "competition_seekers", ["competition_id"])
    op.create_table(
        "competition_submissions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("competition_teams.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("fields", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_table(
        "competition_judges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
    )
    op.create_index("ix_competition_judges_competition_id", "competition_judges", ["competition_id"])
    op.create_table(
        "competition_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("judge_id", sa.Integer(), sa.ForeignKey("competition_judges.id", ondelete="CASCADE"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("competition_teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("note", sa.String(500), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("judge_id", "team_id", name="uq_competition_scores_once"),
    )
    op.create_index("ix_competition_scores_judge_id", "competition_scores", ["judge_id"])
    op.create_index("ix_competition_scores_team_id", "competition_scores", ["team_id"])
    op.create_table(
        "competition_mentors",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("topic", sa.String(120), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
    )
    op.create_index("ix_competition_mentors_competition_id", "competition_mentors", ["competition_id"])
    op.create_table(
        "competition_slots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mentor_id", sa.Integer(), sa.ForeignKey("competition_mentors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("competition_teams.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("mentor_id", "starts_at", name="uq_competition_slots_once"),
    )
    op.create_index("ix_competition_slots_mentor_id", "competition_slots", ["mentor_id"])
    op.create_index("ix_competition_slots_team_id", "competition_slots", ["team_id"])


def downgrade() -> None:
    for t in ("competition_slots", "competition_mentors", "competition_scores", "competition_judges", "competition_submissions", "competition_seekers", "competition_team_members", "competition_teams", "competition_tracks", "competitions"):
        op.drop_table(t)
