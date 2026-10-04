from pydantic import BaseModel
from typing import List
from fastapi import APIRouter, Depends

from app.db.core import SessionDep
from app.api.orgs.schema import (
    OrganizationCreate,
    OrganizationUpdate,
    OrganizationDetailResponse,
    OrganizationPublic,
    OrganizationAnalyticsResponse
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


class OrganizationBlockRequest(BaseModel):
    """Optional body. Absent means toggle, for older callers."""

    is_blocked: bool | None = None


@router.put("/block/{id}", summary="Block/Unblock organization")
async def block_organization(
    id: int,
    session: SessionDep,
    user: AdminAuth,
    body: OrganizationBlockRequest | None = None,
):
    return await service.block_organization(
        id, session, is_blocked=body.is_blocked if body else None
    )


@router.post("/import", summary="Bulk import organizations from CSV/Excel")
async def import_organizations(
    session: SessionDep, user: AdminAuth, file: UploadFile = File(...)
):
    return await service.import_organizations(file, session)


@router.get("/admin/{id}/analytics", summary="Get aggregated Club & Event statistics for an Organization")
async def get_organization_analytics(id: int, session: SessionDep, user: AdminAuth) -> OrganizationAnalyticsResponse:
    return await service.get_organization_analytics(id, session)


@router.get("/{id}", summary="Get one organization")
async def get_organization_endpoint(
    id: int, session: SessionDep, user: AdminAuth
) -> OrganizationDetailResponse:
    """
    Declared last so /list, /create and /admin/... keep priority over the
    bare {id}.
    """
    return await service.get_organization(id, session)
