from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

Stage = Literal["entered", "shortlisted", "finalist"]


def _trimmed(v):
    if v is None:
        return None
    v = v.strip()
    return v or None


# ─── Settings ────────────────────────────────────────────────────────────


class Requirement(BaseModel):
    key: str = Field(..., min_length=1, max_length=30, pattern=r"^[a-z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=80)
    required: bool = True


class Criterion(BaseModel):
    key: str = Field(..., min_length=1, max_length=30, pattern=r"^[a-z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=80)
    weight: int = Field(..., ge=0, le=100)


class SettingsPatch(BaseModel):
    enabled: Optional[bool] = None
    team_min: Optional[int] = Field(None, ge=1, le=20)
    team_max: Optional[int] = Field(None, ge=1, le=20)
    teams_locked: Optional[bool] = None
    submissions_close_at: Optional[datetime] = None
    requirements: Optional[list[Requirement]] = None
    rubric: Optional[list[Criterion]] = None
    leaderboard_public: Optional[bool] = None
    shortlist_size: Optional[int] = Field(None, ge=1, le=100)
    final_size: Optional[int] = Field(None, ge=1, le=100)
    slot_minutes: Optional[int] = Field(None, ge=5, le=120)
    #: How judges' totals make a team's score: their mean (the default), or
    #: each judge's put on their own scale first (z-scores; service.team_scores).
    score_method: Optional[Literal["mean", "zscore"]] = None


class Settings(BaseModel):
    enabled: bool
    team_min: int
    team_max: int
    teams_locked: bool
    submissions_close_at: Optional[datetime]
    submissions_closed_at: Optional[datetime]
    submissions_open: bool
    requirements: list[Requirement]
    rubric: list[Criterion]
    leaderboard_public: bool
    shortlist_size: int
    final_size: int
    slot_minutes: int
    score_method: Literal["mean", "zscore"] = "mean"


# ─── Tracks ──────────────────────────────────────────────────────────────


class TrackIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=600)
    sponsor: Optional[str] = Field(None, max_length=120)
    prize: Optional[str] = Field(None, max_length=120)

    _t = field_validator("description", "sponsor", "prize")(_trimmed)


class TrackPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=600)
    sponsor: Optional[str] = Field(None, max_length=120)
    prize: Optional[str] = Field(None, max_length=120)
    sort_order: Optional[int] = Field(None, ge=0, le=1000)

    _t = field_validator("description", "sponsor", "prize")(_trimmed)


class TrackOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    sponsor: Optional[str]
    prize: Optional[str]
    sort_order: int
    teams: int


# ─── Teams ───────────────────────────────────────────────────────────────


class MemberOut(BaseModel):
    registration_id: UUID
    name: str
    email: Optional[str] = None


class SubmissionOut(BaseModel):
    fields: dict[str, str]
    status: Literal["nothing", "in_progress", "complete"]
    missing: list[str]
    completed_at: Optional[datetime]
    updated_at: Optional[datetime]


class TeamOut(BaseModel):
    id: int
    name: str
    track_id: Optional[int]
    track: Optional[str]
    stage: Stage
    members: list[MemberOut]
    open_spots: int
    submission: SubmissionOut
    score: Optional[float]
    judges_scored: int
    created_at: datetime


class TeamIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    track_id: Optional[int] = None
    registration_ids: list[UUID] = Field(default_factory=list)


class TeamPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    track_id: Optional[int] = None
    stage: Optional[Stage] = None


class MemberIn(BaseModel):
    registration_id: UUID


class SeekerOut(BaseModel):
    registration_id: UUID
    name: str
    skills: list[str]
    note: Optional[str]


# ─── Submissions ─────────────────────────────────────────────────────────


class SubmissionIn(BaseModel):
    fields: dict[str, str]

    @field_validator("fields")
    @classmethod
    def _links(cls, v):
        out = {}
        for k, val in v.items():
            val = (val or "").strip()
            if len(val) > 500:
                raise ValueError("Keep each link under 500 characters")
            if val:
                out[k] = val
        return out


# ─── Judging ─────────────────────────────────────────────────────────────


class JudgeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: Optional[str] = Field(None, max_length=255)

    _e = field_validator("email")(_trimmed)


class JudgePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    email: Optional[str] = Field(None, max_length=255)
    sort_order: Optional[int] = Field(None, ge=0, le=1000)


class JudgeOut(BaseModel):
    id: int
    name: str
    email: Optional[str]
    sort_order: int
    scored: int


class ScoreIn(BaseModel):
    scores: dict[str, int]
    note: Optional[str] = Field(None, max_length=500)

    @field_validator("scores")
    @classmethod
    def _range(cls, v):
        for k, n in v.items():
            if not 0 <= n <= 10:
                raise ValueError(f"{k}: a score is 0 to 10")
        return v


class ScoreOut(BaseModel):
    judge_id: int
    team_id: int
    scores: dict[str, int]
    note: Optional[str]
    total: Optional[float]


# ─── Rounds · leaderboard ────────────────────────────────────────────────


class AdvanceIn(BaseModel):
    to: Literal["shortlisted", "finalist"]
    size: Optional[int] = Field(None, ge=1, le=100)


class BoardRow(BaseModel):
    rank: Optional[int]
    team_id: int
    name: str
    track: Optional[str]
    stage: Stage
    score: Optional[float]
    judges_scored: int
    judges_total: int


class Rounds(BaseModel):
    entered: list[BoardRow]
    shortlisted: list[BoardRow]
    finalists: list[BoardRow]


class PublicBoard(BaseModel):
    event_id: int
    #: Whether the event runs a competition at all — the pages use it to show the way in.
    enabled: bool
    public: bool
    rows: list[BoardRow]


# ─── Office hours ────────────────────────────────────────────────────────


class MentorIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    topic: Optional[str] = Field(None, max_length=120)

    _t = field_validator("topic")(_trimmed)


class MentorPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    topic: Optional[str] = Field(None, max_length=120)
    sort_order: Optional[int] = Field(None, ge=0, le=1000)


class SlotOut(BaseModel):
    id: int
    starts_at: datetime
    minutes: int
    team_id: Optional[int]
    team: Optional[str]


class MentorOut(BaseModel):
    id: int
    name: str
    topic: Optional[str]
    sort_order: int
    slots: list[SlotOut]


class SlotsIn(BaseModel):
    starts_at: datetime
    ends_at: datetime


class SlotPatch(BaseModel):
    team_id: Optional[int] = None


# ─── The whole thing ─────────────────────────────────────────────────────


class Figures(BaseModel):
    teams: int
    people: int
    seekers: int
    submitted: int
    judges: int
    scored_pairs: int


class CompetitionState(BaseModel):
    event_id: int
    settings: Settings
    figures: Figures
    tracks: list[TrackOut]
    teams: list[TeamOut]
    seekers: list[SeekerOut]
    judges: list[JudgeOut]
    scores: list[ScoreOut]
    board: list[BoardRow]
    rounds: Rounds
    mentors: list[MentorOut]
    generated_at: datetime


# ─── A participant's view ────────────────────────────────────────────────


class MyTeamIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    track_id: Optional[int] = None


class MyTeamPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    track_id: Optional[int] = None


class SeekingIn(BaseModel):
    on: bool
    skills: list[str] = Field(default_factory=list)
    note: Optional[str] = Field(None, max_length=200)

    @field_validator("skills")
    @classmethod
    def _skills(cls, v):
        out = [s.strip()[:30] for s in v if s and s.strip()]
        return out[:8]


class OpenTeam(BaseModel):
    id: int
    name: str
    track: Optional[str]
    members: int
    open_spots: int


class MyState(BaseModel):
    event_id: int
    enabled: bool
    registration_id: UUID
    name: str
    team_min: int
    team_max: int
    teams_locked: bool
    tracks: list[TrackOut]
    requirements: list[Requirement]
    submissions_open: bool
    submissions_close_at: Optional[datetime]
    team: Optional[TeamOut]
    open_teams: list[OpenTeam]
    seeking: Optional[SeekerOut]
    seekers: list[SeekerOut]
    mentors: list[MentorOut]
    board: Optional[list[BoardRow]]
