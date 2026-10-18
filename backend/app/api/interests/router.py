from typing import List, Optional

from fastapi import APIRouter, Path, Query, Request, Response

from app.db.core import SessionDep
from app.api.interests import service, topics
from app.api.audit import service as audit
from app.api.interests.schemas import (
    InterestCategoryCreate,
    InterestCategoryCreateUpdateResponse,
    InterestCategoryUpdate,
    InterestCategoryWiseListResponse,
    InterestCreate,
    InterestCreateUpdateResponse,
    TopicDeleteResponse,
    TopicOut,
    TopicsReorder,
)

from app.core import site_revalidate
from app.core.auth.permissions import ContentAuth, DestroyAuth
from app.core.rate_limit import limiter
from app.response import CustomHTTPException

router = APIRouter(prefix="/interests")

PUBLIC_CACHE = "public, max-age=300"


def _caller(request: Request) -> str:
    from app.core.net import client_address

    return client_address(request) or "unknown"


async def _changed(session) -> None:
    """After an admin's change to the tree: commit it with its audit line,
    have the site told once it is in (site_revalidate: "topics"), and drop
    this process's kept counts (topics.forget) so the next read has it."""
    site_revalidate.topics_changed(session)
    await session.commit()
    topics.forget()


@router.get(
    "/list", summary="List all interests (public)", description="List based on categories"
)
@limiter.limit("120/minute", key_func=_caller)
async def list_interests(
    request: Request, response: Response, session: SessionDep
) -> List[InterestCategoryWiseListResponse]:
    """
    Every topic (interest category) with its interests: ids, slugs, names,
    icons and colours, nothing about anyone. In order: topic sort_order, then
    each interest's, then name. A topic with no interests yet is listed too.

    Public because the club sign-up form asks for interests before there is
    an account to sign in with. The app, signed in, gets the same answer.
    """
    response.headers["Cache-Control"] = PUBLIC_CACHE
    return await service.list_interests(session=session)


@router.get("/topics", summary="Topics with what is on in each (public)")
@limiter.limit("120/minute", key_func=_caller)
async def list_topics(request: Request, response: Response, session: SessionDep) -> List[TopicOut]:
    """Every topic, in order, with its upcoming events (as discover shows
    them), the clubs that pick it, and each interest's upcoming events: for
    the explore and landing pages. Worked out every few minutes
    (topics.TTL), and at once after an admin changes the tree."""
    response.headers["Cache-Control"] = PUBLIC_CACHE
    return await topics.with_counts(session)


@router.get("/topics/{slug}", summary="One topic with what is on in it (public)")
@limiter.limit("120/minute", key_func=_caller)
async def get_topic(
    request: Request, response: Response, session: SessionDep,
    slug: str = Path(..., max_length=80, description="The topic's slug (its id works too)"),
) -> TopicOut:
    """As one entry of /interests/topics. 404 TOPIC_NOT_FOUND for a slug no live topic has."""
    found = topics.find(await topics.with_counts(session), slug)
    if found is None:
        raise CustomHTTPException(404, "Topic not found", error_code=service.TOPIC_NOT_FOUND)
    response.headers["Cache-Control"] = PUBLIC_CACHE
    return found


@router.post("/create", summary="Create a interest")
async def create_interest(
    user: ContentAuth, session: SessionDep, interest: InterestCreate, request: Request = None,
) -> InterestCreateUpdateResponse:
    created = await service.create_interest(
        session=session,
        name=interest.name,
        category_id=interest.category_id,
        icon=interest.icon,
        icon_type=interest.icon_type,
        slug=interest.slug,
        sort_order=interest.sort_order,
    )
    await audit.record(
        session, actor=user, action="interest.create", entity_type="interest",
        entity_id=created.id, summary=f"Added interest “{created.name}”",
        details={"category_id": created.category_id, "slug": created.slug}, request=request,
    )
    await _changed(session)
    return created


@router.post("/category/create", summary="Create a topic (interest category)")
async def create_interest_category(
    user: ContentAuth, session: SessionDep, category: InterestCategoryCreate, request: Request = None,
) -> InterestCategoryCreateUpdateResponse:
    """The slug is made from the name when left out; the topic goes last
    unless a sort_order is given."""
    created = await service.create_interest_category(
        session=session,
        name=category.name,
        icon=category.icon,
        icon_type=category.icon_type,
        slug=category.slug,
        color=category.color,
        description=category.description,
        sort_order=category.sort_order,
    )
    await audit.record(
        session, actor=user, action="topic.create", entity_type="topic",
        entity_id=created.id, summary=f"Added topic “{created.name}”",
        details={"slug": created.slug}, request=request,
    )
    await _changed(session)
    return created


@router.put("/category/{category_id}", summary="Update a topic (interest category)")
async def update_interest_category(
    category_id: int,
    category: InterestCategoryUpdate,
    user: ContentAuth,
    session: SessionDep,
    request: Request = None,
) -> InterestCategoryCreateUpdateResponse:
    """What the body leaves out stays as it is. Changing the slug changes the
    topic's address: links to the old one stop working."""
    fields = category.model_dump(exclude_unset=True)
    updated = await service.update_interest_category(session, category_id, fields)
    await audit.record(
        session, actor=user, action="topic.update", entity_type="topic",
        entity_id=category_id, summary=f"Updated topic “{updated.name}”",
        details={k: v for k, v in fields.items() if k != "icon"}, request=request,
    )
    await _changed(session)
    return updated


@router.delete("/category/{category_id}", summary="Delete a topic (interest category)")
async def delete_interest_category(
    category_id: int,
    user: DestroyAuth,
    session: SessionDep,
    request: Request = None,
    move_to: Optional[int] = Query(None, description="The topic its interests move to; needed when it has any"),
) -> TopicDeleteResponse:
    """Its interests move to `move_to` (and stay chosen by everyone who chose
    them), then the topic is deleted. 409 TOPIC_NOT_EMPTY when it has
    interests and no `move_to`; 400 when `move_to` is the topic itself or no
    live topic."""
    result = await service.delete_interest_category(session, category_id, move_to)
    await audit.record(
        session, actor=user, action="topic.delete", entity_type="topic",
        entity_id=category_id,
        summary=f"Deleted topic, {result['moved']} interests moved" + (f" to topic {move_to}" if result["moved"] else ""),
        details=result, request=request,
    )
    await _changed(session)
    return TopicDeleteResponse(**result)


@router.post("/reorder", summary="Put topics, and interests within topics, in order")
async def reorder(
    body: TopicsReorder,
    user: ContentAuth,
    session: SessionDep,
    request: Request = None,
) -> List[InterestCategoryWiseListResponse]:
    """`topics`: topic ids in order; `interests`: {topic id: interest ids in
    order}, which also moves an interest listed under another topic. Either
    may be left out. Answers with the tree as /interests/list has it."""
    moved = await service.reorder(session, body.topics, body.interests)
    await audit.record(
        session, actor=user, action="topics.reorder", entity_type="topic",
        summary=f"Reordered {moved['topics']} topics and {moved['interests']} interests ({moved['moved_topic']} moved topic)",
        details={"topics": body.topics, "interests": {str(k): v for k, v in (body.interests or {}).items()}}, request=request,
    )
    await _changed(session)
    return await service.list_interests(session)


@router.put("/{interest_id}", summary="Update an interest")
async def update_interest(
    interest_id: int,
    interest: InterestCreate,
    user: ContentAuth,
    session: SessionDep,
    request: Request = None,
) -> InterestCreateUpdateResponse:
    """
    Interests were create-only: once added, a typo was permanent and an
    obsolete one could not be removed. They appear on every club and user
    profile, so that is reference data the product could not correct.

    `category_id` moves it to another topic (last there unless a
    sort_order is sent); slug and sort_order are kept when left out.
    """
    updated = await service.update_interest(
        session,
        interest_id=interest_id,
        name=interest.name,
        category_id=interest.category_id,
        icon=interest.icon,
        icon_type=interest.icon_type,
        slug=interest.slug,
        sort_order=interest.sort_order,
    )
    await audit.record(
        session, actor=user, action="interest.update", entity_type="interest",
        entity_id=interest_id, summary=f"Renamed interest to “{interest.name}”",
        details={"category_id": updated.category_id, "slug": updated.slug, "sort_order": updated.sort_order},
        request=request,
    )
    await _changed(session)
    return updated


@router.delete("/{interest_id}", summary="Delete an interest")
async def delete_interest(
    interest_id: int,
    user: DestroyAuth,
    session: SessionDep,
    request: Request = None,
):
    result = await service.delete_interest(session, interest_id)
    await audit.record(
        session, actor=user, action="interest.delete", entity_type="interest",
        entity_id=interest_id,
        summary=f"Deleted interest, detached from {result['detached_from']} records",
        details=result, request=request,
    )
    await _changed(session)
    return result
