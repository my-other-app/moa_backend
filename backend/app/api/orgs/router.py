from pydantic import BaseModel
from typing import List
from fastapi import APIRouter, Depends, Request

from app.db.core import SessionDep
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.api.orgs.schema import (
    OrganizationCreate,
    OrganizationUpdate,
    OrganizationDetailResponse,
    OrganizationPublic,
    OrganizationAnalyticsResponse
)
from fastapi import UploadFile, File
from app.api.orgs import service
from app.api.audit import service as audit
from app.core.auth.dependencies import AdminAuth

router = APIRouter(prefix="/orgs")


@router.post("/create", summary="Create a new organization")
async def create_organization(
    session: SessionDep,
    user: AdminAuth,
    org: OrganizationCreate = Depends(),
    request: Request = None,
) -> OrganizationDetailResponse:
    created = await service.create_organization(org, session)
    await audit.record(
        session, actor=user, action="org.create", entity_type="org",
        entity_id=created.id, summary=f"Created organisation “{created.name}”",
        request=request,
    )
    await session.commit()
    return created


@router.get("/list", summary="List all organizations")
async def create_organization(session: SessionDep) -> List[OrganizationDetailResponse]:
    return await service.list_organizations(session)


@router.delete("/delete/{id}", summary="Delete organization")
async def delete_organization(
    id: int, session: SessionDep, user: AdminAuth, request: Request = None
):
    result = await service.delete_organization(id, session)
    await audit.record(
        session, actor=user, action="org.delete", entity_type="org",
        entity_id=id, summary="Deleted organisation", request=request,
    )
    await session.commit()
    return result


@router.put("/update/{id}", summary="Update organization details")
async def update_organization(
    id: int,
    session: SessionDep,
    user: AdminAuth,
    org: OrganizationUpdate = Depends(),
    request: Request = None,
) -> OrganizationDetailResponse:
    updated = await service.update_organization(id, org, session)
    await audit.record(
        session, actor=user, action="org.update", entity_type="org",
        entity_id=id, summary=f"Edited organisation “{updated.name}”",
        request=request,
    )
    await session.commit()
    return updated


class OrganizationBlockRequest(BaseModel):
    """Optional body. Absent means toggle, for older callers."""

    is_blocked: bool | None = None


@router.put("/block/{id}", summary="Block/Unblock organization")
async def block_organization(
    id: int,
    session: SessionDep,
    user: AdminAuth,
    body: OrganizationBlockRequest | None = None,
    request: Request = None,
):
    result = await service.block_organization(
        id, session, is_blocked=body.is_blocked if body else None
    )
    await audit.record(
        session, actor=user, action="org.block", entity_type="org",
        entity_id=id,
        summary=f"{'Blocked' if result['is_blocked'] else 'Unblocked'} organisation",
        details={"is_blocked": result["is_blocked"]}, request=request,
    )
    await session.commit()
    return result


@router.post("/import", summary="Bulk import organizations from CSV/Excel")
async def import_organizations(
    session: SessionDep, user: AdminAuth, file: UploadFile = File(...)
):
    return await service.import_organizations(file, session)


@router.get("/admin/{id}/analytics", summary="Get aggregated Club & Event statistics for an Organization")
async def get_organization_analytics(id: int, session: SessionDep, user: AdminAuth) -> OrganizationAnalyticsResponse:
    return await service.get_organization_analytics(id, session)


@router.get("/deleted/list", summary="Deleted organizations (admin)")
async def list_deleted_organizations_endpoint(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
) -> PaginatedResponse[OrganizationDetailResponse]:
    """Declared before /{id} so the literal path wins."""
    orgs, total = await service.list_deleted_organizations(
        session, limit=pagination.limit, offset=pagination.offset
    )
    return paginated_response(
        orgs, request, schema=OrganizationDetailResponse, total=total
    )


@router.post("/{id}/restore", summary="Restore a deleted organization (admin)")
async def restore_organization_endpoint(
    id: int, session: SessionDep, user: AdminAuth, request: Request = None
) -> OrganizationDetailResponse:
    org = await service.restore_organization(session, id)
    await audit.record(
        session, actor=user, action="org.restore", entity_type="org",
        entity_id=id, summary=f"Restored organisation “{org.name}”", request=request,
    )
    await session.commit()
    return org


@router.get("/{id}", summary="Get one organization")
async def get_organization_endpoint(
    id: int, session: SessionDep, user: AdminAuth
) -> OrganizationDetailResponse:
    """
    Declared last so /list, /create and /admin/... keep priority over the
    bare {id}.
    """
    return await service.get_organization(id, session)
