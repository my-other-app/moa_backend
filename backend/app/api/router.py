from fastapi import APIRouter
from app.api.auth.router import router as auth_router
from app.api.orgs.router import router as org_router
from app.api.users.router import router as user_router
from app.api.clubs.router import router as club_router
from app.api.events.router import router as events_router
from app.api.interests.router import router as interests_router
from app.api.home.router import router as home_router

api_router = APIRouter(
    prefix="/api/v1",
    responses={404: {"description": "Not found"}},
)

api_router.include_router(router=auth_router, tags=["auth"])
api_router.include_router(router=org_router, tags=["organization"])
api_router.include_router(router=user_router, tags=["user"])
api_router.include_router(router=club_router, tags=["clubs"])
api_router.include_router(router=events_router, tags=["events"])
api_router.include_router(router=interests_router, tags=["interests"])
api_router.include_router(router=home_router, tags=["home"])
