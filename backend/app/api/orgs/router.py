from fastapi import APIRouter

from app.db.core import SessionDep
from app.api.orgs.schema import OrganizationCreate, OrganizationPublic
from app.api.orgs import service

router = APIRouter(prefix="/orgs")


@router.post(
    "/create", response_model=OrganizationPublic, summary="Create a new organization"
)
async def create_organization(hero: OrganizationCreate, session: SessionDep):
    return await service.create_organization(hero, session)


@router.get(
    "/list", response_model=list[OrganizationPublic], summary="List all organizations"
)
async def create_organization(session: SessionDep):
    return await service.list_organizations(session)


@router.delete("/info/{id}", summary="Delete organization")
async def create_organization(id: int, session: SessionDep):
    return await service.delete_organization(id, session)
