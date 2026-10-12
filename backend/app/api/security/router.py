from fastapi import APIRouter, Request

from app.api.security import service
from app.api.security.schemas import PreferencesPatch, SecurityOverview
from app.core.auth.dependencies import DependsAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/auth/security")


@router.get("/overview", summary="Who is signed in, from where, and what happened at the door")
async def security_overview(request: Request, session: SessionDep, user: DependsAuth) -> SecurityOverview:
    return SecurityOverview(**await service.overview(session, user, service.sid_of(request)))


@router.post("/sign-out-others", summary="End every session but this one")
async def sign_out_others(request: Request, session: SessionDep, user: DependsAuth) -> dict:
    return {"signed_out": await service.sign_out_others(session, user, service.sid_of(request))}


@router.patch("/preferences", summary="Whether a new device earns an email")
async def preferences(session: SessionDep, user: DependsAuth, body: PreferencesPatch) -> dict:
    user.notify_new_device = body.notify_new_device
    await session.commit()
    return {"notify_new_device": user.notify_new_device}
