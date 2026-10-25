"""The live room: the console's side under ClubAuth, and the room's side for
anyone with the browser key (and a player key, for the quiz). Each of the
room's polls can also be had as a stream of server-sent events (stream.py,
behind APP_LIVE_SSE)."""
from typing import Optional
from uuid import UUID

import orjson

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from app.api.deliveries import service as deliveries
from app.api.live import limits, notify, room, service, stream
from app.api.live.schemas import (
    AnswerIn,
    AnswerOut,
    AskIn,
    Feed,
    FeedbackOut,
    HostActionIn,
    HostActionOut,
    HostState,
    BannerFeed,
    JoinIn,
    JoinOut,
    ModerateIn,
    ModerateOut,
    PlayerState,
    PollIn,
    PollOut,
    PollPatch,
    PollStatusIn,
    PollStatusOut,
    PublicSurvey,
    QABoard,
    QABoardOut,
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
from app.core.rate_limit import account_or_address, limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/live")
public_router = APIRouter()

# The room's public side sees an event that is shown or held (not a draft,
# not deleted) of a listed club; anything else is a 404, as on the site.
_public_event = service.public_event


def _registration_key(request: Request) -> str:
    """A survey link belongs to one guest: its limit is theirs, not their network's."""
    return f"survey:{request.path_params.get('registration_id', '')}"


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
async def host_action(
    request: Request, session: SessionDep, user: ClubAuth, event_id: int, action: str, body: Optional[HostActionIn] = None,
) -> HostActionOut:
    """`start` tells everyone checked in that the quiz is starting (a push
    and an inbox line, at most one per event every three minutes and ten a
    day) unless the body says `{"notify": false}`; with no body it does.
    What came of it is `notified` -- the quiz starts whatever it says.

    Guards: `start` on a quiz that is running is 409 QUIZ_RUNNING (it would
    clear every score) unless the body says `{"restart": true}`. `next` with
    `{"expected_index": n}` is 409 QUIZ_MOVED_ON when the quiz is no longer
    on question n (a double press, a second console); without it, it moves
    on as before. The other actions answer `notified: null`."""
    event = await deliveries.event_for_club(session, user, event_id)
    if action not in ("start", "next", "end", "reset"):
        raise CustomHTTPException(404, message="Unknown action")
    if action == "start":
        # Starting is the plan's; a quiz already running can always be moved on or ended.
        await gates.require_feature(session, event.club_id, "quiz", actor=user, event=event)
    quiz = await service.quiz_of(session, event)
    quiz_id = quiz.id
    if action == "start":
        out = await service.start_quiz(session, quiz, restart=bool(body and body.restart))
    elif action == "next":
        out = await service.next_question(session, quiz, expected_index=body.expected_index if body else None)
    elif action == "end":
        out = await service.end_quiz(session, quiz)
    else:
        out = await service.reset_quiz(session, quiz)
    notified = None
    if action == "start" and (body is None or body.notify is not False):
        notified = await notify.tell_room(session, event, "quiz", actor=user, ref=quiz_id, request=request)
    return HostActionOut(**out, notified=notified)


# --- quiz: the players ----------------------------------------------------------------------


@public_router.post("/live/quiz/join", summary="Join the quiz on the screen with its code", status_code=201)
async def join_quiz(request: Request, session: SessionDep, body: JoinIn, user: OptionalAuth = None) -> JoinOut:
    """Who joins: the account, for a caller signed in (one player on every
    device); else the client's key, `player_key` in the body (or the
    X-Live-Key it sends the room). The same one joining again in the same
    run gets the same player back -- its name, score and player_key. With
    neither, each join is a new player (clients from before). Limited per
    person (that account or key) and, generously, per address."""
    if body.player_key is not None and room.valid_key(body.player_key) is None:
        raise CustomHTTPException(422, message="player_key is 8 to 64 letters, digits, - and _.")
    person = room.person_key(user, body.player_key or request.headers.get(service.VOTER_HEADER))
    limits.check(request, "join", person)
    out = await service.join_quiz(session, body.code, body.name, person=person)
    if user is not None:
        event = await _public_event(session, out["event_id"])
        reg = await service._registration_of(session, event, user)
        if reg is not None:
            player = await session.get(service.LiveQuizPlayers, out["player_id"])
            if player.registration_id is None:
                player.registration_id = reg.id
                await session.commit()
    return JoinOut(**out)


@public_router.get("/events/{event_id}/live/quiz/play", response_model=PlayerState, summary="The player's view of the quiz right now")
async def play_state(request: Request, session: SessionDep, event_id: int) -> Response:
    """Polled every two seconds by every player: read from the quiz snapshot
    and written straight out (the shape is PlayerState's), with no model
    built per poll."""
    out = await service.player_state(session, event_id, request.headers.get(service.PLAYER_HEADER, ""))
    # default=str: ids come back from asyncpg as its own UUID type, which orjson does not know.
    return Response(content=orjson.dumps(out, default=str), media_type="application/json")


@public_router.post("/events/{event_id}/live/quiz/answer", summary="Answer the open question")
async def play_answer(request: Request, session: SessionDep, event_id: int, body: AnswerIn) -> AnswerOut:
    player = request.headers.get(service.PLAYER_HEADER, "")
    limits.check(request, "answer", player or None)
    return AnswerOut(**await service.answer(session, event_id, player, body.option_key))


# --- Q&A ----------------------------------------------------------------------


@router.get("/questions", summary="The floor's questions: to review, on the board, answered")
async def list_questions(session: SessionDep, user: ClubAuth, event_id: int) -> QABoard:
    event = await deliveries.event_for_club(session, user, event_id)
    return QABoard(**await service.questions_for_club(session, event))


@router.patch("/questions/settings", summary="Whether a question waits for approval")
async def qa_settings(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: QASettings) -> QABoardOut:
    """Switching approval off opens the floor: everyone checked in is told
    Q&A is open (`event_live_qa`, once per event, under the room's limit)
    unless the body says `"notify": false`. What came of it is `notified`;
    null when approval is switched on."""
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "qa", actor=user, event=event)
    out = await service.set_qa(session, event, body.approve_first)
    notified = None
    if not body.approve_first and body.notify is not False:
        notified = await notify.tell_room(session, event, "qa", actor=user, request=request)
    return QABoardOut(**out, notified=notified)


@router.post("/questions/{question_id}", summary="approve · reject · answered · reopen · pin · unpin")
async def moderate(request: Request, session: SessionDep, user: ClubAuth, event_id: int, question_id: UUID, body: ModerateIn) -> ModerateOut:
    """Approving or pinning a question puts it on the room's board: the first
    time, everyone checked in is told Q&A is open (`event_live_qa`, once per
    event, under the room's limit) unless the body says `"notify": false`.
    What came of it is `notified` (`already_sent` once it went); null for
    the other actions."""
    event = await deliveries.event_for_club(session, user, event_id)
    out = await service.moderate(session, event, question_id, body.action)
    notified = None
    if body.action in ("approve", "pin") and body.notify is not False:
        notified = await notify.tell_room(session, event, "qa", actor=user, ref=question_id, request=request)
    return ModerateOut(**out, notified=notified)


@public_router.post("/events/{event_id}/live/questions", summary="Ask the room a question", status_code=201)
async def ask(request: Request, session: SessionDep, event_id: int, body: AskIn, user: OptionalAuth = None) -> QuestionOut:
    """A caller signed in asks as the account (room.person_key)."""
    key = service.voter_key_of(request, user)
    limits.check(request, "ask", key)
    event = await _public_event(session, event_id)
    # The floor is open only on a plan with Q&A; the guest is not told why.
    if not await gates.allows(session, event.club_id, "qa", actor=user, event=event):
        raise CustomHTTPException(409, message="Questions aren't open for this event right now.", error_code="QA_CLOSED")
    reg = await service._registration_of(session, event, user)
    return QuestionOut(**await service.ask(session, event, body.text, body.author_name, key, reg.id if reg else None))


@public_router.post("/events/{event_id}/live/questions/{question_id}/vote", summary="Vote a question up, or take the vote back")
async def vote(request: Request, session: SessionDep, event_id: int, question_id: UUID, user: OptionalAuth = None) -> VoteOut:
    """One vote per person: a caller signed in votes as the account, on
    every device (room.person_key); a guest as the browser's key."""
    key = service.voter_key_of(request, user)
    limits.check(request, "vote", key)
    return VoteOut(**await service.vote(session, event_id, question_id, key))


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
async def poll_status(request: Request, session: SessionDep, user: ClubAuth, event_id: int, poll_id: UUID, body: PollStatusIn) -> PollStatusOut:
    """Launching also tells everyone checked in that a poll is live (as
    starting the quiz does, under the same limit) unless the body says
    `"notify": false`. What came of it is `notified` (null when not asked,
    and for a close) -- the poll is launched whatever it says."""
    event = await deliveries.event_for_club(session, user, event_id)
    if body.status == "live":
        # Launching is the plan's; closing a poll that is up never is.
        await gates.require_feature(session, event.club_id, "polls", actor=user, event=event)
    out = await service.set_poll_status(session, event, poll_id, body.status)
    notified = None
    if body.status == "live" and body.notify is not False:
        notified = await notify.tell_room(session, event, "poll", actor=user, ref=poll_id, request=request)
    return PollStatusOut(**out, notified=notified)


@router.delete("/polls/{poll_id}", summary="Remove a poll")
async def delete_poll(session: SessionDep, user: ClubAuth, event_id: int, poll_id: UUID) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.delete_poll(session, event, poll_id)
    return {"ok": True}


@public_router.post("/events/{event_id}/live/polls/{poll_id}/respond", summary="Answer the poll that is up")
async def respond(request: Request, session: SessionDep, event_id: int, poll_id: UUID, body: RespondIn, user: OptionalAuth = None) -> PollOut:
    """One answer per person: a caller signed in answers as the account (room.person_key)."""
    key = service.voter_key_of(request, user)
    limits.check(request, "respond", key)
    return PollOut(**await service.respond(session, event_id, poll_id, key, body.value))


# --- feedback ----------------------------------------------------------------------


@router.get("/feedback", summary="The survey, when it goes out, and what came back — this time and last time")
async def feedback(session: SessionDep, user: ClubAuth, event_id: int) -> FeedbackOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return FeedbackOut(**await service.feedback_for_club(session, event))


@router.put("/feedback", summary="The survey's questions and timing")
async def put_feedback(session: SessionDep, user: ClubAuth, event_id: int, body: SurveyPatch) -> SurveyOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return SurveyOut(**await service.update_survey(session, event, body))


@router.post("/feedback/send", summary="Send the survey now to everyone checked in not sent it yet")
@limiter.limit("6/hour", key_func=account_or_address)
async def send_feedback(request: Request, session: SessionDep, user: ClubAuth, event_id: int) -> SendOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return SendOut(**await service.send_survey(session, event))


@public_router.get("/events/{event_id}/feedback/{registration_id}", summary="The survey, for the guest the link was sent to")
async def public_survey(session: SessionDep, event_id: int, registration_id: UUID, k: str = Query("")) -> PublicSurvey:
    event = await _public_event(session, event_id)
    return PublicSurvey(**await service.public_survey(session, event, registration_id, k))


@public_router.post("/events/{event_id}/feedback/{registration_id}", summary="The guest's answers")
@limiter.limit("20/hour", key_func=_registration_key)
async def submit_survey(request: Request, session: SessionDep, event_id: int, registration_id: UUID, body: SubmitIn, k: str = Query("")) -> dict:
    event = await _public_event(session, event_id)
    return await service.submit_survey(session, event, registration_id, k, body.answers)


# --- updates ----------------------------------------------------------------------


@router.get("/updates", summary="What was pushed tonight, and who it can reach")
async def list_updates(session: SessionDep, user: ClubAuth, event_id: int) -> UpdatesPage:
    event = await deliveries.event_for_club(session, user, event_id)
    return UpdatesPage(audience=await service.audience_counts(session, event), items=[UpdateOut(**u) for u in await service.list_updates(session, event)])


@router.post("/updates", summary="Push an update: a banner on the event page and a phone notification", status_code=201)
@limiter.limit("60/hour", key_func=account_or_address)
async def push_update(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: UpdateIn) -> UpdateOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return UpdateOut(**await service.push_update(session, event, body.kind, body.text, body.audience, user))


@router.post("/schedule/shift", summary="Move a block and everything after it; the room is told")
async def shift(session: SessionDep, user: ClubAuth, event_id: int, body: ShiftIn) -> ShiftOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return ShiftOut(**await service.shift_schedule(session, event, body.item_id, body.minutes, user))


# --- the room and the screen ----------------------------------------------------------------------


@public_router.get(
    "/events/{event_id}/live", response_model=Feed,
    summary="What the room sees: the banner, the poll up, the questions, whether a quiz runs",
)
async def feed(request: Request, session: SessionDep, event_id: int) -> Response:
    """Read from the room's snapshot (at most a second old); the browser's
    own flags (voted, mine, my_response) from what it did -- the account's,
    when the request carries a sign-in (read from the token alone,
    room.shown_key). The floor shows as closed when the club's plan has no
    Q&A (and the event is not grandfathered), so the ask box never appears
    only to refuse."""
    body = await room.feed_body(session, event_id, room.shown_key(request, request.headers.get(service.VOTER_HEADER)))
    return Response(content=body, media_type="application/json")


#: The strip is the same for everyone and changes a few times an evening: a
#: shared cache may keep it as long as a phone waits between two polls.
BANNER_CACHE = "public, max-age=15"


@public_router.get(
    "/events/{event_id}/live/banner", response_model=BannerFeed,
    summary="The event page's live strip: the event's status, the banner, whether a poll, the quiz or the floor is on",
)
async def banner(session: SessionDep, event_id: int) -> Response:
    """A few hundred bytes, made once a second at most for everyone (the
    room's snapshot) and sent as made: no key, no sign-in, nothing personal.
    It carries a weak ETag (app.core.middlewares.etag_middleware): a poll
    with If-None-Match is a 304 while nothing changed. Polled every 30
    seconds by the event page in the app and on the site."""
    body = await room.banner_body(session, event_id)
    return Response(content=body, media_type="application/json", headers={"Cache-Control": BANNER_CACHE})


@router.get("/screen", summary="The big screen")
async def screen(session: SessionDep, user: ClubAuth, event_id: int) -> Screen:
    event = await deliveries.event_for_club(session, user, event_id)
    return Screen(**await service.screen(session, event))


# --- the room, pushed ----------------------------------------------------------------------


async def _club_user(request: Request, session):
    """The club (or admin) behind the request, checked as ClubAuth checks it."""
    from app.core.auth.authentication import oauth2_scheme
    from app.core.auth.dependencies import check_user_type, get_current_user

    user = await get_current_user(request, await oauth2_scheme(request), session)
    return await check_user_type(["club", "admin"])(request=request, current_user=user, session=session)


@public_router.get(
    "/events/{event_id}/live/stream",
    summary="The room's polls as server-sent events: room, banner, quiz, host, screen, questions, polls",
    response_class=Response,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def live_stream(
    request: Request, session: SessionDep, event_id: int, view: str = Query("room"),
    key: str = Query("", description="The browser key, for a client that cannot send X-Live-Key (EventSource)"),
    player: str = Query("", description="The player key, for a client that cannot send X-Live-Player"),
    last_event_id: str = Query("", description="The last id seen, for a client that cannot send Last-Event-ID"),
) -> Response:
    """Each `snapshot` is the body the view's poll returns now, for this
    client, with the version as its `id`; a `ping` every quiet 15 s; `gone`
    when the room (or this player) is not there any more. The same
    credentials as the poll: X-Live-Key (room), X-Live-Player (quiz), the
    club's session (host, screen, questions, polls). `banner` is the event
    page's strip, open to whoever may see the room: the event's status,
    start and end and the banner (BannerFeed), sent when one of them
    changes. 404 while APP_LIVE_SSE is off: the client polls."""
    from app.core.net import client_address

    if not stream.on():
        raise CustomHTTPException(404, message="The live room is not pushed here; poll it.", error_code="LIVE_STREAM_OFF")
    if view not in stream.VIEWS:
        raise CustomHTTPException(422, message=f"view is one of: {', '.join(stream.VIEWS)}.")
    # The account's flags when the stream is opened with a sign-in (the app's
    # client sends Authorization; a browser's EventSource cannot), else the browser's.
    voter_key = room.shown_key(request, request.headers.get(service.VOTER_HEADER) or key) if view == "room" else None
    player_key = (request.headers.get(service.PLAYER_HEADER) or player).strip() if view == "quiz" else None
    limits.check(request, "stream", voter_key or player_key or None)
    address = client_address(request) or "unknown"
    try:
        stream.hub.admit(event_id, address)
    except stream.Full as full:
        # Answered here rather than raised: the API's error handler drops an
        # exception's headers, and the client waits by Retry-After.
        refusal = CustomHTTPException(503, message=f"Too many live streams here ({full}); poll for now.", error_code="LIVE_STREAM_FULL")
        return JSONResponse(refusal.to_dict(), status_code=503, headers={"Retry-After": "60"})
    voter = found = None
    try:
        if view in ("room", "banner"):
            await room.room_of(session, event_id)
            if voter_key is not None:
                voter = await room.voter_of(session, event_id, voter_key)
        elif view == "quiz":
            quiz = await service._view_for(session, event_id)
            found = await service._player(session, quiz, player_key)
        else:
            await deliveries.event_for_club(session, await _club_user(request, session), event_id)
        # Nothing of the request's session is held while the stream is open:
        # its transaction ends here and its connection goes back to the pool.
        await session.commit()
    except BaseException:
        stream.hub.release(address)
        raise
    feed = stream.hub.feed(event_id)
    listener = stream.Listener(view=view, event_id=event_id, key=voter_key or player_key, address=address, player=found)
    feed.add(listener, voter)
    last = stream.parse_version(request.headers.get("last-event-id") or last_event_id)
    return stream.EventStream(feed, listener, last, stream.CLUB_LIFETIME if view in stream.CLUB_VIEWS else stream.LIFETIME)
