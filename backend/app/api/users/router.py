from fastapi import APIRouter
from app.api.users.schemas import User, UserCreate
from app.db.core import SessionDep
from app.api.users import service

router = APIRouter(prefix="/user")


@router.post("/register", response_model=User, summary="Register a new user")
async def register_user(
    user: UserCreate,
    session: SessionDep,
):
    user = await service.create_user(user, session)
    return user
