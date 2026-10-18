from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# --- quiz ----------------------------------------------------------------------


class QuizOption(BaseModel):
    key: str = Field(..., min_length=1, max_length=2)
    label: str = Field(..., min_length=1, max_length=120)


class QuizPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=160)
    intro: Optional[str] = Field(None, max_length=300)


class QuizQuestionIn(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=300)
    options: list[QuizOption] = Field(..., min_length=2, max_length=6)
    correct_key: str = Field(..., min_length=1, max_length=2)
    seconds: int = Field(20, ge=5, le=120)


class QuizQuestionPatch(BaseModel):
    prompt: Optional[str] = Field(None, min_length=3, max_length=300)
    options: Optional[list[QuizOption]] = Field(None, min_length=2, max_length=6)
    correct_key: Optional[str] = Field(None, min_length=1, max_length=2)
    seconds: Optional[int] = Field(None, ge=5, le=120)


class QuizQuestionOut(BaseModel):
    id: UUID
    prompt: str
    options: list[dict]
    correct_key: Optional[str] = None
    seconds: int
    sort_order: int


class QuizOut(BaseModel):
    id: UUID
    title: str
    intro: Optional[str] = None
    status: str
    join_code: Optional[str] = None
    current_index: int
    players: int
    questions: list[QuizQuestionOut]


class HostState(BaseModel):
    status: str
    join_code: Optional[str] = None
    title: str
    players: int
    current_index: int
    total: int
    question: Optional[dict] = None
    leaderboard: list[dict]


class JoinIn(BaseModel):
    code: str = Field(..., min_length=4, max_length=8)
    name: str = Field(..., min_length=1, max_length=60)


class JoinOut(BaseModel):
    event_id: int
    quiz_id: UUID
    title: str
    player_id: UUID
    player_key: str
    name: str


class PlayerState(BaseModel):
    status: str
    title: str
    name: str
    score: int
    current_index: int
    total: int
    question: Optional[dict] = None
    leaderboard: list[dict]
    rank: Optional[int] = None


class AnswerIn(BaseModel):
    option_key: str = Field(..., min_length=1, max_length=2)


class AnswerOut(BaseModel):
    correct: bool
    points: int
    score: int
    correct_key: str


# --- Q&A ----------------------------------------------------------------------


class QuestionOut(BaseModel):
    id: UUID
    text: str
    author_name: Optional[str] = None
    status: str
    pinned: bool
    votes: int
    created_at: datetime
    answered_at: Optional[datetime] = None
    voted: bool = False
    mine: bool = False


class QABoard(BaseModel):
    approve_first: bool
    to_review: list[QuestionOut]
    board: list[QuestionOut]
    answered: list[QuestionOut]
    rejected: int


class QASettings(BaseModel):
    approve_first: bool


class ModerateIn(BaseModel):
    action: Literal["approve", "reject", "answered", "reopen", "pin", "unpin"]


class AskIn(BaseModel):
    text: str = Field(..., min_length=3, max_length=500)
    author_name: Optional[str] = Field(None, max_length=60)


class VoteOut(BaseModel):
    id: UUID
    votes: int
    voted: bool


# --- polls ----------------------------------------------------------------------


class PollIn(BaseModel):
    kind: Literal["choice", "words", "rating"] = "choice"
    prompt: str = Field(..., min_length=1, max_length=300)
    options: list[str] = Field(default_factory=list, max_length=8)


class PollPatch(BaseModel):
    kind: Optional[Literal["choice", "words", "rating"]] = None
    prompt: Optional[str] = Field(None, min_length=1, max_length=300)
    options: Optional[list[str]] = Field(None, max_length=8)


class PollStatusIn(BaseModel):
    status: Literal["live", "closed"]


class PollOut(BaseModel):
    id: UUID
    kind: str
    prompt: str
    options: list[str]
    status: str
    launched_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    sort_order: int
    results: dict
    my_response: Optional[str] = None


class RespondIn(BaseModel):
    value: str = Field(..., min_length=1, max_length=120)


# --- feedback ----------------------------------------------------------------------


class SurveyQuestion(BaseModel):
    key: str = Field(..., min_length=1, max_length=40)
    kind: Literal["rating", "nps", "text"]
    label: str = Field(..., min_length=1, max_length=200)


class SurveyPatch(BaseModel):
    auto_send: Optional[bool] = None
    delay_minutes: Optional[int] = Field(None, ge=0, le=7 * 24 * 60)
    questions: Optional[list[SurveyQuestion]] = Field(None, max_length=12)


class SurveyOut(BaseModel):
    auto_send: bool
    delay_minutes: int
    questions: list[dict]
    sent_at: Optional[datetime] = None
    sent_count: int


class FeedbackOut(SurveyOut):
    sends_at: Optional[datetime] = None
    results: dict
    previous: Optional[dict] = None
    checked_in: int


class SendOut(SurveyOut):
    sent: int
    invited: int


class PublicSurvey(BaseModel):
    event_name: str
    club_name: str
    first_name: str
    questions: list[dict]
    submitted: bool


class SubmitIn(BaseModel):
    answers: dict[str, Any]


# --- updates ----------------------------------------------------------------------


class UpdateIn(BaseModel):
    kind: Literal["schedule", "room", "general"] = "general"
    text: str = Field(..., min_length=3, max_length=500)
    audience: Literal["checked_in", "registered"] = "checked_in"


class UpdateOut(BaseModel):
    id: UUID
    kind: str
    text: str
    audience: str
    recipients: int
    pushed: int
    sent_at: datetime


class ShiftIn(BaseModel):
    item_id: UUID
    minutes: int = Field(..., ge=-1440, le=1440)


class ShiftOut(BaseModel):
    update: UpdateOut
    items: list[dict]


class UpdatesPage(BaseModel):
    audience: dict
    items: list[UpdateOut]


# --- the room ----------------------------------------------------------------------


class Feed(BaseModel):
    event: dict
    banner: Optional[UpdateOut] = None
    updates: list[UpdateOut]
    poll: Optional[PollOut] = None
    questions: list[QuestionOut]
    qa: dict
    quiz: dict


class BannerFeed(BaseModel):
    """The event page's live strip (the stream's view=banner): what the
    room's feed says of the event and its banner, with the event's start and
    end -- a few hundred bytes where the whole feed is ~30 KB."""

    event: dict
    banner: Optional[UpdateOut] = None


class Screen(BaseModel):
    event: dict
    pinned: Optional[QuestionOut] = None
    board: list[QuestionOut]
    poll: Optional[PollOut] = None
    quiz: Optional[HostState] = None
    update: Optional[UpdateOut] = None
