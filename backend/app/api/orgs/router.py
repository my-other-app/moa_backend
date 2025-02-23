from typing import List
from fastapi import APIRouter, Depends

from app.db.core import SessionDep
from app.api.orgs.schema import (
    OrganizationCreate,
    OrganizationDetailResponse,
    OrganizationPublic,
)
from app.api.orgs import service
from app.core.auth.dependencies import AdminAuth

router = APIRouter(prefix="/orgs")


@router.post("/create", summary="Create a new organization")
async def create_organization(
    session: SessionDep, user: AdminAuth, org: OrganizationCreate = Depends()
) -> OrganizationDetailResponse:
    return await service.create_organization(org, session)


@router.get("/list", summary="List all organizations")
async def create_organization(session: SessionDep) -> List[OrganizationDetailResponse]:
    return await service.list_organizations(session)


@router.delete("/delete/{id}", summary="Delete organization")
async def create_organization(id: int, session: SessionDep, user: AdminAuth):
    return await service.delete_organization(id, session)
