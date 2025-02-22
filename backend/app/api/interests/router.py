from fastapi import APIRouter

from app.db.core import SessionDep
from app.core.auth.dependencies import AdminAuth, DependsAuth
from app.api.interests import service
from app.api.interests.schemas import (
    InterestCategoryCreate,
    InterestCategoryPublic,
    InterestCreate,
)

router = APIRouter(prefix="/interests")


@router.get("/list", summary="List all interests")
async def list_interests(user: DependsAuth, session: SessionDep = SessionDep()):
    return await service.list_interests(session=session)


@router.post("/create", summary="Create a interest")
async def create_interest(
    user: AdminAuth, session: SessionDep, interest: InterestCreate
):
    return await service.create_interest(
        session=session,
        name=interest.name,
        category_id=interest.category_id,
        icon=interest.icon,
        icon_type=interest.icon_type,
    )


@router.post("/category/create", summary="Create an interest category")
async def create_interest(
    user: AdminAuth, session: SessionDep, category: InterestCategoryCreate
) -> InterestCategoryPublic:
    return await service.create_interest_category(
        session=session,
        name=category.name,
        icon=category.icon,
        icon_type=category.icon_type,
    )
