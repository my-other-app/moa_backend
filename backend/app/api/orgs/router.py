from typing import List
from fastapi import APIRouter, Depends

from app.db.core import SessionDep
from app.api.orgs.schema import (
    OrganizationCreate,
    OrganizationUpdate,
    OrganizationDetailResponse,
    OrganizationPublic,
)
from fastapi import UploadFile, File
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
async def delete_organization(id: int, session: SessionDep, user: AdminAuth):
    return await service.delete_organization(id, session)


@router.put("/update/{id}", summary="Update organization details")
async def update_organization(
    id: int, session: SessionDep, user: AdminAuth, org: OrganizationUpdate = Depends()
) -> OrganizationDetailResponse:
    return await service.update_organization(id, org, session)


@router.put("/block/{id}", summary="Block/Unblock organization")
async def block_organization(id: int, session: SessionDep, user: AdminAuth):
    return await service.block_organization(id, session)


@router.post("/import", summary="Bulk import organizations from CSV/Excel")
async def import_organizations(
    session: SessionDep, user: AdminAuth, file: UploadFile = File(...)
):
    return await service.import_organizations(file, session)
