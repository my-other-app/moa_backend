"""A competition on an event: tracks the teams pick, the teams and who is in
them, what each team submits and by when, the judges and their scores on a
rubric, the rounds a team advances through, the leaderboard, and the mentor
office hours a team can book. Everything hangs off the event; a person is
their registration for it."""
import enum

from sqlalchemy import JSON, Boolean, Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class TeamStage(str, enum.Enum):
    entered = "entered"
    shortlisted = "shortlisted"
    finalist = "finalist"


DEFAULT_REQUIREMENTS = [
    {"key": "repo", "label": "Code repository link", "required": True},
    {"key": "video", "label": "Demo video (2 min max)", "required": True},
    {"key": "deck", "label": "Pitch deck", "required": False},
    {"key": "photo", "label": "Photo of the hardware", "required": False},
]
DEFAULT_RUBRIC = [
    {"key": "technical", "label": "Technical depth", "weight": 35},
    {"key": "innovation", "label": "Innovation", "weight": 25},
    {"key": "demo", "label": "Demo", "weight": 25},
    {"key": "impact", "label": "Impact", "weight": 15},
]


class Competitions(AbstractSQLModel, TimestampsMixin):
    """One per event, made on first look; `enabled` is the switch the organiser throws."""

    __tablename__ = "competitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    enabled = Column(Boolean, nullable=False, default=False)
    team_min = Column(Integer, nullable=False, default=1)
    team_max = Column(Integer, nullable=False, default=4)
    # Once locked, nobody joins, leaves or makes a team from their ticket; the organiser still can.
    teams_locked = Column(Boolean, nullable=False, default=False)
    submissions_close_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # Set when the organiser closes by hand; the clock closes them too.
    submissions_closed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # [{"key", "label", "required"}] — what a team must hand in.
    requirements = Column(JSON, nullable=False, default=list)
    # [{"key", "label", "weight"}] — what a judge scores, out of ten each, weighted.
    rubric = Column(JSON, nullable=False, default=list)
    leaderboard_public = Column(Boolean, nullable=False, default=False)
    shortlist_size = Column(Integer, nullable=False, default=4)
    final_size = Column(Integer, nullable=False, default=3)
    slot_minutes = Column(Integer, nullable=False, default=15)

    tracks = relationship("CompetitionTracks", order_by="CompetitionTracks.sort_order", cascade="all, delete-orphan", lazy="selectin")
    judges = relationship("CompetitionJudges", order_by="CompetitionJudges.sort_order", cascade="all, delete-orphan", lazy="selectin")
    mentors = relationship("CompetitionMentors", order_by="CompetitionMentors.sort_order", cascade="all, delete-orphan", lazy="selectin")


class CompetitionTracks(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "competition_tracks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    competition_id = Column(Integer, ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    description = Column(String(600), nullable=True)
    sponsor = Column(String(120), nullable=True)
    prize = Column(String(120), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)


class CompetitionTeams(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "competition_teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    competition_id = Column(Integer, ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    track_id = Column(Integer, ForeignKey("competition_tracks.id", ondelete="SET NULL"), nullable=True, index=True)
    stage = Column(String(12), nullable=False, default=TeamStage.entered.value)
    created_by_registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True)

    members = relationship("CompetitionTeamMembers", cascade="all, delete-orphan", lazy="selectin", order_by="CompetitionTeamMembers.created_at")
    submission = relationship("CompetitionSubmissions", uselist=False, cascade="all, delete-orphan", lazy="selectin")


class CompetitionTeamMembers(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "competition_team_members"
    __table_args__ = (UniqueConstraint("team_id", "registration_id", name="uq_competition_team_members_once"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("competition_teams.id", ondelete="CASCADE"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False, index=True)


class CompetitionSeekers(AbstractSQLModel, TimestampsMixin):
    """A solo attendee who ticked "find me teammates", with what they bring."""

    __tablename__ = "competition_seekers"
    __table_args__ = (UniqueConstraint("competition_id", "registration_id", name="uq_competition_seekers_once"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    competition_id = Column(Integer, ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)
    skills = Column(JSON, nullable=False, default=list)
    note = Column(String(200), nullable=True)


class CompetitionSubmissions(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "competition_submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey("competition_teams.id", ondelete="CASCADE"), nullable=False, unique=True)
    # {requirement key: a link}
    fields = Column(JSON, nullable=False, default=dict)
    # Set the first time everything required is in.
    completed_at = Column(TZAwareDateTime(timezone=True), nullable=True)


class CompetitionJudges(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "competition_judges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    competition_id = Column(Integer, ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)


class CompetitionScores(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "competition_scores"
    __table_args__ = (UniqueConstraint("judge_id", "team_id", name="uq_competition_scores_once"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    judge_id = Column(Integer, ForeignKey("competition_judges.id", ondelete="CASCADE"), nullable=False, index=True)
    team_id = Column(Integer, ForeignKey("competition_teams.id", ondelete="CASCADE"), nullable=False, index=True)
    # {criterion key: 0..10}
    scores = Column(JSON, nullable=False, default=dict)
    note = Column(String(500), nullable=True)


class CompetitionMentors(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "competition_mentors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    competition_id = Column(Integer, ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    topic = Column(String(120), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)

    slots = relationship("CompetitionSlots", order_by="CompetitionSlots.starts_at", cascade="all, delete-orphan", lazy="selectin")


class CompetitionSlots(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "competition_slots"
    __table_args__ = (UniqueConstraint("mentor_id", "starts_at", name="uq_competition_slots_once"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    mentor_id = Column(Integer, ForeignKey("competition_mentors.id", ondelete="CASCADE"), nullable=False, index=True)
    starts_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    minutes = Column(Integer, nullable=False, default=15)
    team_id = Column(Integer, ForeignKey("competition_teams.id", ondelete="SET NULL"), nullable=True, index=True)
