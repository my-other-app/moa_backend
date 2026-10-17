"""The live room: the console's side under ClubAuth, and the room's side for
anyone with the browser key (and a player key, for the quiz)."""
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.deliveries import service as deliveries
from app.api.events.models import Events
from app.api.live import service
from app.api.live.schemas import (
    AnswerIn,
    AnswerOut,
    AskIn,
    Feed,
    FeedbackOut,
    HostState,
    JoinIn,
    JoinOut,
    ModerateIn,
    PlayerState,
    PollIn,
    PollOut,
    PollPatch,
    PollStatusIn,
    PublicSurvey,
    QABoard,
    QASettings,
    QuestionOut,
    QuizOut,
    QuizPatch,
    QuizQuestionIn,
    QuizQuestionOut,
    QuizQuestionPatch,
    RespondIn,
    Screen,
    SendOut,
    ShiftIn,
    ShiftOut,
    SubmitIn,
    SurveyPatch,
    SurveyOut,
    UpdateIn,
    UpdateOut,
    UpdatesPage,
    VoteOut,
)
from app.core.auth.dependencies import ClubAuth, OptionalAuth
from app.api.subscriptions import gates
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/live")
public_router = APIRouter()


async def _public_event(session, event_id: int) -> Events:
    event = await session.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == event_id, Events.is_deleted == False))  # noqa: E712
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    return event


# --- quiz: the host ----------------------------------------------------------------------


@router.get("/quiz", summary="The quiz with its questions and answers")
async def get_quiz(session: SessionDep, user: ClubAuth, event_id: int) -> QuizOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return QuizOut(**await service.quiz_out(session, await service.quiz_of(session, event)))


@router.patch("/quiz", summary="Rename the quiz or change its intro")
async def patch_quiz(session: SessionDep, user: ClubAuth, event_id: int, body: QuizPatch) -> QuizOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return QuizOut(**await service.update_quiz(session, await service.quiz_of(session, event), body))


@router.post("/quiz/questions", summary="Add a question", status_code=201)
async def add_question(session: SessionDep, user: ClubAuth, event_id: int, body: QuizQuestionIn) -> QuizQuestionOut:
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "quiz", actor=user, event=event)
    return QuizQuestionOut(**await service.add_question(session, await service.quiz_of(session, event), body))


@router.patch("/quiz/questions/{question_id}", summary="Change a question")
async def patch_question(session: SessionDep, user: ClubAuth, event_id: int, question_id: UUID, body: QuizQuestionPatch) -> QuizQuestionOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return QuizQuestionOut(**await service.update_question(session, await service.quiz_of(session, event), question_id, body))


@router.delete("/quiz/questions/{question_id}", summary="Remove a question")
async def delete_question(session: SessionDep, user: ClubAuth, event_id: int, question_id: UUID) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.delete_question(session, await service.quiz_of(session, event), question_id)
    return {"ok": True}


@router.get("/quiz/host", summary="What the host sees: players, the open question's tally, the board")
async def host_state(session: SessionDep, user: ClubAuth, event_id: int) -> HostState:
    event = await deliveries.event_for_club(session, user, event_id)
    return HostState(**await service.host_state(session, await service.quiz_of(session, event)))


@router.post("/quiz/host/{action}", summary="start · next · end · reset")
async def host_action(session: SessionDep, user: ClubAuth, event_id: int, action: str) -> HostState:
    event = await deliveries.event_for_club(session, user, event_id)
    fn = {"start": service.start_quiz, "next": service.next_question, "end": service.end_quiz, "reset": service.reset_quiz}.get(action)
    if fn is None:
        raise CustomHTTPException(404, message="Unknown action")
    if action == "start":
        # Starting is the plan's; a quiz already running can always be moved on or ended.
        await gates.require_feature(session, event.club_id, "quiz", actor=user, event=event)
    quiz = await service.quiz_of(session, event)
    return HostState(**await fn(session, quiz))


# --- quiz: the players ----------------------------------------------------------------------


@public_router.post("/live/quiz/join", summary="Join the quiz on the screen with its code", status_code=201)
@limiter.limit("30/minute")
async def join_quiz(request: Request, session: SessionDep, body: JoinIn, user: OptionalAuth = None) -> JoinOut:
    out = await service.join_quiz(session, body.code, body.name)
    if user is not None:
        event = await _public_event(session, out["event_id"])
        reg = await service._registration_of(session, event, user)
        if reg is not None:
            player = await session.get(service.LiveQuizPlayers, out["player_id"])
            player.registration_id = reg.id
            await session.commit()
    return JoinOut(**out)


@public_router.get("/events/{event_id}/live/quiz/play", summary="The player's view of the quiz right now")
async def play_state(request: Request, session: SessionDep, event_id: int) -> PlayerState:
    event = await _public_event(session, event_id)
    return PlayerState(**await service.player_state(session, event, request.headers.get(service.PLAYER_HEADER, "")))


@public_router.post("/events/{event_id}/live/quiz/answer", summary="Answer the open question")
@limiter.limit("120/minute")
async def play_answer(request: Request, session: SessionDep, event_id: int, body: AnswerIn) -> AnswerOut:
    event = await _public_event(session, event_id)
    return AnswerOut(**await service.answer(session, event, request.headers.get(service.PLAYER_HEADER, ""), body.option_key))


# --- Q&A ----------------------------------------------------------------------


@router.get("/questions", summary="The floor's questions: to review, on the board, answered")
async def list_questions(session: SessionDep, user: ClubAuth, event_id: int) -> QABoard:
    event = await deliveries.event_for_club(session, user, event_id)
    return QABoard(**await service.questions_for_club(session, event))


@router.patch("/questions/settings", summary="Whether a question waits for approval")
async def qa_settings(session: SessionDep, user: ClubAuth, event_id: int, body: QASettings) -> QABoard:
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "qa", actor=user, event=event)
    return QABoard(**await service.set_qa(session, event, body.approve_first))


@router.post("/questions/{question_id}", summary="approve · reject · answered · reopen · pin · unpin")
async def moderate(session: SessionDep, user: ClubAuth, event_id: int, question_id: UUID, body: ModerateIn) -> QuestionOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return QuestionOut(**await service.moderate(session, event, question_id, body.action))


@public_router.post("/events/{event_id}/live/questions", summary="Ask the room a question", status_code=201)
@limiter.limit("20/hour")
async def ask(request: Request, session: SessionDep, event_id: int, body: AskIn, user: OptionalAuth = None) -> QuestionOut:
    event = await _public_event(session, event_id)
    # The floor is open only on a plan with Q&A; the guest is not told why.
    if not await gates.allows(session, event.club_id, "qa", actor=user, event=event):
        raise CustomHTTPException(409, message="Questions aren't open for this event right now.", error_code="QA_CLOSED")
    key = service.voter_key_of(request)
    reg = await service._registration_of(session, event, user)
    return QuestionOut(**await service.ask(session, event, body.text, body.author_name, key, reg.id if reg else None))


@public_router.post("/events/{event_id}/live/questions/{question_id}/vote", summary="Vote a question up, or take the vote back")
@limiter.limit("120/minute")
async def vote(request: Request, session: SessionDep, event_id: int, question_id: UUID) -> VoteOut:
    event = await _public_event(session, event_id)
    return VoteOut(**await service.vote(session, event, question_id, service.voter_key_of(request)))


# --- polls ----------------------------------------------------------------------


@router.get("/polls", summary="Every poll with its results")
async def list_polls(session: SessionDep, user: ClubAuth, event_id: int) -> list[PollOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [PollOut(**p) for p in await service.list_polls(session, event)]


@router.post("/polls", summary="A new poll, as a draft", status_code=201)
async def create_poll(session: SessionDep, user: ClubAuth, event_id: int, body: PollIn) -> PollOut:
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "polls", actor=user, event=event)
    return PollOut(**await service.create_poll(session, event, body))


@router.patch("/polls/{poll_id}", summary="Change a draft poll")
async def patch_poll(session: SessionDep, user: ClubAuth, event_id: int, poll_id: UUID, body: PollPatch) -> PollOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return PollOut(**await service.update_poll(session, event, poll_id, body))


@router.post("/polls/{poll_id}/status", summary="Launch or close a poll; launching closes the one that was up")
async def poll_status(session: SessionDep, user: ClubAuth, event_id: int, poll_id: UUID, body: PollStatusIn) -> PollOut:
    event = await deliveries.event_for_club(session, user, event_id)
    if body.status == "live":
        # Launching is the plan's; closing a poll that is up never is.
        await gates.require_feature(session, event.club_id, "polls", actor=user, event=event)
    return PollOut(**await service.set_poll_status(session, event, poll_id, body.status))


@router.delete("/polls/{poll_id}", summary="Remove a poll")
async def delete_poll(session: SessionDep, user: ClubAuth, event_id: int, poll_id: UUID) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.delete_poll(session, event, poll_id)
    return {"ok": True}


@public_router.post("/events/{event_id}/live/polls/{poll_id}/respond", summary="Answer the poll that is up")
@limiter.limit("120/minute")
async def respond(request: Request, session: SessionDep, event_id: int, poll_id: UUID, body: RespondIn) -> PollOut:
    event = await _public_event(session, event_id)
    return PollOut(**await service.respond(session, event, poll_id, service.voter_key_of(request), body.value))


# --- feedback ----------------------------------------------------------------------


@router.get("/feedback", summary="The survey, when it goes out, and what came back — this time and last time")
async def feedback(session: SessionDep, user: ClubAuth, event_id: int) -> FeedbackOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return FeedbackOut(**await service.feedback_for_club(session, event))


@router.put("/feedback", summary="The survey's questions and timing")
async def put_feedback(session: SessionDep, user: ClubAuth, event_id: int, body: SurveyPatch) -> SurveyOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return SurveyOut(**await service.update_survey(session, event, body))


@router.post("/feedback/send", summary="Send the survey now to everyone checked in")
@limiter.limit("6/hour")
async def send_feedback(request: Request, session: SessionDep, user: ClubAuth, event_id: int) -> SendOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return SendOut(**await service.send_survey(session, event))


@public_router.get("/events/{event_id}/feedback/{registration_id}", summary="The survey, for the guest the link was sent to")
async def public_survey(session: SessionDep, event_id: int, registration_id: UUID, k: str = Query("")) -> PublicSurvey:
    event = await _public_event(session, event_id)
    return PublicSurvey(**await service.public_survey(session, event, registration_id, k))


@public_router.post("/events/{event_id}/feedback/{registration_id}", summary="The guest's answers")
@limiter.limit("20/hour")
async def submit_survey(request: Request, session: SessionDep, event_id: int, registration_id: UUID, body: SubmitIn, k: str = Query("")) -> dict:
    event = await _public_event(session, event_id)
    return await service.submit_survey(session, event, registration_id, k, body.answers)


# --- updates ----------------------------------------------------------------------


@router.get("/updates", summary="What was pushed tonight, and who it can reach")
async def list_updates(session: SessionDep, user: ClubAuth, event_id: int) -> UpdatesPage:
    event = await deliveries.event_for_club(session, user, event_id)
    return UpdatesPage(audience=await service.audience_counts(session, event), items=[UpdateOut(**u) for u in await service.list_updates(session, event)])


@router.post("/updates", summary="Push an update: a banner on the event page and a phone notification", status_code=201)
@limiter.limit("60/hour")
async def push_update(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: UpdateIn) -> UpdateOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return UpdateOut(**await service.push_update(session, event, body.kind, body.text, body.audience, user))


@router.post("/schedule/shift", summary="Move a block and everything after it; the room is told")
async def shift(session: SessionDep, user: ClubAuth, event_id: int, body: ShiftIn) -> ShiftOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return ShiftOut(**await service.shift_schedule(session, event, body.item_id, body.minutes, user))


# --- the room and the screen ----------------------------------------------------------------------


@public_router.get("/events/{event_id}/live", summary="What the room sees: the banner, the poll up, the questions, whether a quiz runs")
async def feed(request: Request, session: SessionDep, event_id: int) -> Feed:
    event = await _public_event(session, event_id)
    key = (request.headers.get(service.VOTER_HEADER) or "").strip() or None
    out = await service.feed(session, event, key)
    # The floor shows as closed when the club's plan has no Q&A (and the event
    # is not grandfathered), so the ask box never appears only to refuse.
    if out["qa"]["open"] and not await gates.allows_for_feed(session, event.club_id, "qa", event=event):
        out["qa"]["open"] = False
    return Feed(**out)


@router.get("/screen", summary="The big screen")
async def screen(session: SessionDep, user: ClubAuth, event_id: int) -> Screen:
    event = await deliveries.event_for_club(session, user, event_id)
    return Screen(**await service.screen(session, event))
