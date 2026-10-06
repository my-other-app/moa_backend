from typing import List
from fastapi import APIRouter, Request

from app.db.core import SessionDep
from app.core.auth.dependencies import AdminAuth, DependsAuth
from app.api.interests import service
from app.api.audit import service as audit
from app.api.interests.schemas import (
    InterestCategoryCreate,
    InterestCategoryCreateUpdateResponse,
    InterestCategoryWiseListResponse,
    InterestCreate,
    InterestCreateUpdateResponse,
)

from app.core.auth.permissions import ContentAuth, DestroyAuth

router = APIRouter(prefix="/interests")


@router.get(
    "/list", summary="List all interests", description="List based on categories"
)
async def list_interests(
    user: DependsAuth, session: SessionDep = SessionDep()
) -> List[InterestCategoryWiseListResponse]:
    return await service.list_interests(session=session)


@router.post("/create", summary="Create a interest")
async def create_interest(
    user: ContentAuth, session: SessionDep, interest: InterestCreate
) -> InterestCreateUpdateResponse:
    return await service.create_interest(
        session=session,
        name=interest.name,
        category_id=interest.category_id,
        icon=interest.icon,
        icon_type=interest.icon_type,
    )


@router.post("/category/create", summary="Create an interest category")
async def create_interest_category(
    user: ContentAuth, session: SessionDep, category: InterestCategoryCreate
) -> InterestCategoryCreateUpdateResponse:
    return await service.create_interest_category(
        session=session,
        name=category.name,
        icon=category.icon,
        icon_type=category.icon_type,
    )


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
    """
    updated = await service.update_interest(
        session,
        interest_id=interest_id,
        name=interest.name,
        category_id=interest.category_id,
        icon=interest.icon,
        icon_type=interest.icon_type,
    )
    await audit.record(
        session, actor=user, action="interest.update", entity_type="interest",
        entity_id=interest_id, summary=f"Renamed interest to “{interest.name}”",
        request=request,
    )
    await session.commit()
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
    await session.commit()
    return result
