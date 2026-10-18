from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, Request, Response, UploadFile
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.api.threads import insights as threads_insights, service
from app.api.threads.schemas import CommentCreate, CommentOut, LikeOut, PollOut, ThreadInsights, ThreadOut, ThreadPage, ThreadPatch, ThreadsPageOut, VoteIn
from app.core.auth.dependencies import ClubAuth, OptionalAuth, UserAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

# The club's own posts. Mounted before the club router so "threads" is never read as a club id.
router = APIRouter(prefix="/clubs/threads")
# What everyone else sees: the club's page, the feed, and what a reader can do.
public_router = APIRouter()


async def _club_for(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


@router.get("", summary="The club's posts, pinned first")
async def list_threads(session: SessionDep, user: ClubAuth, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)) -> ThreadPage:
    club = await _club_for(session, user)
    return ThreadPage(**await service.list_for_club(session, club, offset, limit, viewer_id=user.id))


@router.get("/page", summary="The club's posts for the Threads page: scoped, sorted, each saying whether it waits for a reply")
async def list_threads_page(session: SessionDep, user: ClubAuth, scope: str = Query("all"), sort: str = Query("newest"), offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=50)) -> ThreadsPageOut:
    if scope not in threads_insights.SCOPES or sort not in threads_insights.SORTS:
        raise CustomHTTPException(422, message="Unknown scope or sort.")
    club = await _club_for(session, user)
    return ThreadsPageOut(**await threads_insights.list_for_page(session, club, scope=scope, sort=sort, offset=offset, limit=limit, viewer_id=user.id))


@router.get("/insights", summary="Reach, engagement and what kind of post works, over the last 30 days and the last 12 weeks")
async def thread_insights(session: SessionDep, user: ClubAuth) -> ThreadInsights:
    club = await _club_for(session, user)
    return ThreadInsights(**await threads_insights.insights(session, club))


@router.post("", summary="Post: text, maybe a photo, maybe a poll; followers told if asked", status_code=201)
@limiter.limit("30/hour")
async def create_thread(
    request: Request,
    session: SessionDep,
    user: ClubAuth,
    background_tasks: BackgroundTasks,
    body: str = Form(..., max_length=2000),
    notify_followers: bool = Form(True),
    poll_options: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
) -> ThreadOut:
    """The followers are told after the post is answered (service.tell_followers)."""
    club = await _club_for(session, user)
    options = service.parse_options(poll_options)
    out = await service.create(session, club, user, body=body, notify_followers=notify_followers, poll_options=options, image=image)
    if notify_followers:
        background_tasks.add_task(service.tell_followers, club.id, out["id"])
    return ThreadOut(**out)


@router.patch("/{thread_id}", summary="Pin or unpin; fix the text")
async def update_thread(session: SessionDep, user: ClubAuth, thread_id: str, patch: ThreadPatch) -> ThreadOut:
    club = await _club_for(session, user)
    t = await service.thread_for_club(session, club, thread_id)
    return ThreadOut(**await service.update(session, club, t, pinned=patch.pinned, body=patch.body))


@router.delete("/{thread_id}", summary="Remove a post")
async def delete_thread(session: SessionDep, user: ClubAuth, thread_id: str) -> dict:
    club = await _club_for(session, user)
    await service.delete(session, await service.thread_for_club(session, club, thread_id))
    return {"deleted": True}


@router.get("/{thread_id}/comments", summary="The comments under a post")
async def club_comments(session: SessionDep, user: ClubAuth, thread_id: str) -> list[CommentOut]:
    club = await _club_for(session, user)
    t = await service.thread_for_club(session, club, thread_id)
    return [CommentOut(**c) for c in await service.comments(session, t, club, viewer_id=user.id)]


@router.post("/{thread_id}/comments", summary="Reply as the club", status_code=201)
async def club_reply(session: SessionDep, user: ClubAuth, thread_id: str, body: CommentCreate) -> CommentOut:
    club = await _club_for(session, user)
    t = await service.thread_for_club(session, club, thread_id)
    return CommentOut(**await service.add_comment(session, t, club, user, body.body))


@router.delete("/{thread_id}/comments/{comment_id}", summary="Remove any comment under the club's post")
async def club_remove_comment(session: SessionDep, user: ClubAuth, thread_id: str, comment_id: str) -> dict:
    club = await _club_for(session, user)
    t = await service.thread_for_club(session, club, thread_id)
    await service.remove_comment(session, t, club, comment_id, by_user=user, as_club=True)
    return {"deleted": True}


# ─── Readers ─────────────────────────────────────────────────────────────


@public_router.get("/clubs/{club_id}/threads", summary="A club's posts, as its page shows them")
async def club_page_threads(request: Request, session: SessionDep, user: OptionalAuth, club_id: int, offset: int = Query(0, ge=0, le=1000), limit: int = Query(20, ge=1, le=50)) -> ThreadPage:
    club = await service.public_club(session, club_id)
    return ThreadPage(**await service.list_public(session, club, user, request, offset, limit))


@public_router.get("/threads/feed", summary="Posts from the clubs you follow, newest first")
async def my_feed(
    session: SessionDep, user: UserAuth,
    offset: int = Query(0, ge=0, le=1000), limit: int = Query(20, ge=1, le=50),
    after: Optional[str] = Query(None, max_length=200, description="The `next_cursor` of the page before: the page after it (offset is then ignored)"),
) -> ThreadPage:
    """`total` counts at most 1,000 (`total_capped` past it)."""
    return ThreadPage(**await service.feed(session, user, offset, limit, after))


# After /threads/feed, so "feed" is never read as a post id.
@public_router.get("/threads/{thread_id}", summary="One post, as a tapped notification opens it")
async def one_thread(request: Request, session: SessionDep, user: OptionalAuth, thread_id: str) -> ThreadOut:
    return ThreadOut(**await service.one_public(session, thread_id, user, request))


@public_router.post("/threads/{thread_id}/like", summary="Like, or take it back")
@limiter.limit("120/minute")
async def like(request: Request, session: SessionDep, user: UserAuth, thread_id: str) -> LikeOut:
    t, _ = await service.live_thread(session, thread_id)
    return LikeOut(**await service.toggle_like(session, t, user))


@public_router.post("/threads/{thread_id}/vote", summary="One vote per person; voting again moves it")
@limiter.limit("60/minute")
async def cast_vote(request: Request, session: SessionDep, user: UserAuth, thread_id: str, body: VoteIn) -> PollOut:
    t, _ = await service.live_thread(session, thread_id)
    if t.kind != "poll":
        raise CustomHTTPException(422, message="This post is not a poll")
    return PollOut(**await service.vote(session, t, user, body.option_id))


@public_router.get("/threads/{thread_id}/comments", summary="The comments under a post, oldest first, a page at a time")
async def read_comments(
    response: Response, session: SessionDep, user: OptionalAuth, thread_id: str,
    limit: int = Query(service.COMMENTS_PAGE, ge=1, le=service.COMMENTS_PAGE),
    after: Optional[str] = Query(None, max_length=200, description="The X-Next-Cursor of the page before"),
) -> list[CommentOut]:
    """At most `limit` comments; when there are more, the X-Next-Cursor
    header is the `after` that reads the next page."""
    t, club = await service.live_thread(session, thread_id)
    items, nxt = await service.comment_page(session, t, club, viewer_id=user.id if user else None, limit=limit, after=after)
    if nxt:
        response.headers["X-Next-Cursor"] = nxt
    return [CommentOut(**c) for c in items]


@public_router.post("/threads/{thread_id}/comments", summary="Comment on a post", status_code=201)
@limiter.limit("30/minute")
async def comment(request: Request, session: SessionDep, user: UserAuth, thread_id: str, body: CommentCreate) -> CommentOut:
    t, club = await service.live_thread(session, thread_id)
    return CommentOut(**await service.add_comment(session, t, club, user, body.body))


@public_router.delete("/threads/{thread_id}/comments/{comment_id}", summary="Remove your own comment")
async def remove_own_comment(session: SessionDep, user: UserAuth, thread_id: str, comment_id: str) -> dict:
    t, club = await service.live_thread(session, thread_id)
    await service.remove_comment(session, t, club, comment_id, by_user=user, as_club=False)
    return {"deleted": True}
