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


class Notified(BaseModel):
    """Whether the room was told (app/api/live/notify.py). `sent`: the people
    the push and the inbox line went to. When it was not: why, and for
    `rate_limited` when the last one went and how many seconds until the
    next may."""

    sent: int
    #: `already_sent`: the event-live and Q&A notifications go once per event, and this one went before.
    skipped_reason: Optional[Literal["rate_limited", "nobody_checked_in", "all_muted", "disabled", "failed", "already_sent"]] = None
    last_sent_at: Optional[datetime] = None
    retry_after: Optional[int] = None


class HostActionIn(BaseModel):
    """The host's action's body, all of it optional: a console that sends
    none (or `{}`) gets the defaults."""

    notify: Optional[bool] = Field(None, description="start only: tell everyone checked in. Not given: yes.")
    restart: Optional[bool] = Field(None, description="start only: start again a quiz that is running, clearing every player's score. Without it, starting a running quiz is 409 QUIZ_RUNNING.")
    expected_index: Optional[int] = Field(None, ge=-1, description="next only: the question index the console shows. When the quiz has moved on from it (a second press, another console), 409 QUIZ_MOVED_ON and nothing moves.")


class HostActionOut(HostState):
    #: For `start`, what telling the room came to; null when it was not asked (and for next, end, reset).
    notified: Optional[Notified] = None


class JoinIn(BaseModel):
    code: str = Field(..., min_length=4, max_length=8)
    name: str = Field(..., min_length=1, max_length=60)
    player_key: Optional[str] = Field(
        None, max_length=64,
        description="The client's stable key (the browser's, the install's): joining again with it in the same quiz run returns the same player. A signed-in caller is the account instead, on any device.",
    )


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
    """The answer taken. Whether it was right, and its points, are told once
    the question's answers close (the state's my_correct and my_points); until
    then `correct` and `points` are null and `score` is the score before it."""

    correct: Optional[bool] = None
    points: Optional[int] = None
    score: int
    answered: bool = True


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
    notify: Optional[bool] = Field(None, description="Switching approval off opens the floor: tell everyone checked in Q&A is open (once per event). Not given: yes.")


class QABoardOut(QABoard):
    #: For switching approval off, what telling the room came to; null otherwise.
    notified: Optional[Notified] = None


class ModerateIn(BaseModel):
    action: Literal["approve", "reject", "answered", "reopen", "pin", "unpin"]
    notify: Optional[bool] = Field(None, description="approve or pin: tell everyone checked in Q&A is open (once per event). Not given: yes.")


class AskIn(BaseModel):
    text: str = Field(..., min_length=3, max_length=500)
    author_name: Optional[str] = Field(None, max_length=60)


class ModerateOut(QuestionOut):
    #: For an approve or a pin, what telling the room Q&A is open came to; null otherwise.
    notified: Optional[Notified] = None


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
    notify: Optional[bool] = Field(None, description="Launching (live) only: tell everyone checked in. Not given: yes.")


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


class PollStatusOut(PollOut):
    #: For a launch, what telling the room came to; null when it was not asked (and for a close).
    notified: Optional[Notified] = None


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
    #: Which channels the survey may go on; each guest is sent on the
    #: cheapest of them that reaches them (app/api/live/service.pick_channel).
    #: Left out, it stays as it is; an empty list is refused.
    channels: Optional[list[Literal["push", "whatsapp", "email"]]] = Field(None, max_length=3)
    questions: Optional[list[SurveyQuestion]] = Field(None, max_length=12)


class SurveyOut(BaseModel):
    auto_send: bool
    delay_minutes: int
    channels: list[str]
    questions: list[dict]
    sent_at: Optional[datetime] = None
    sent_count: int


class FeedbackOut(SurveyOut):
    sends_at: Optional[datetime] = None
    results: dict
    previous: Optional[dict] = None
    checked_in: int
    #: How the sends went out: {"push": n, "whatsapp": n, "email": n, "none": n}.
    by_channel: dict


class SendOut(SurveyOut):
    sent: int
    invited: int
    by_channel: dict
    #: Why the free push did not go, when it did not: the live room's own
    #: reasons (rate_limited, already_sent, nobody_checked_in, all_muted,
    #: disabled). Null when it went, or when push is not one of the channels.
    push_skipped_reason: Optional[str] = None


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


class BannerPoll(BaseModel):
    id: UUID
    kind: str
    prompt: str


class BannerQA(BaseModel):
    open: bool
    #: Whether the room's board has a question on it (approved or answered):
    #: a yes or no, so a new question does not resend every event page's strip.
    has_questions: bool


class BannerFeed(BaseModel):
    """The event page's live strip (GET .../live/banner, and the stream's
    view=banner): what the room's feed says of the event and its banner, with
    the event's start and end, and whether a poll, the quiz or the floor is
    on -- a few hundred bytes where the whole feed is ~30 KB."""

    event: dict
    banner: Optional[UpdateOut] = None
    poll: Optional[BannerPoll] = None
    quiz: dict = Field(default_factory=lambda: {"live": False, "title": None})
    qa: BannerQA = Field(default_factory=lambda: BannerQA(open=False, has_questions=False))


class Screen(BaseModel):
    event: dict
    pinned: Optional[QuestionOut] = None
    board: list[QuestionOut]
    poll: Optional[PollOut] = None
    quiz: Optional[HostState] = None
    update: Optional[UpdateOut] = None
