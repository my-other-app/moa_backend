from pydantic import BaseModel
from typing import List
from fastapi import APIRouter, Depends, Query, Request

from app.db.core import SessionDep
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.api.orgs.models import OrgTypes
from app.api.orgs.schema import (
    OrganizationCreate,
    OrganizationUpdate,
    OrganizationDetailResponse,
    OrganizationAnalyticsResponse,
    OrganizationAdminRow,
    OrganizationDuplicateGroup,
    OrganizationMergeRequest,
    OrganizationMergeResult,
    OrganizationSearchHit,
    OrganizationSuggestRequest,
    OrganizationSuggestion,
)
from fastapi import UploadFile, File
from app.api.orgs import service
from app.api.audit import service as audit
from app.core.auth.dependencies import AdminAuth, UserAuth
from app.core.rate_limit import limiter, signed_in_key

from app.core.auth.permissions import ContentAuth, DestroyAuth, ModerateAuth

router = APIRouter(prefix="/orgs")

#: The type-ahead is a public read, so it shares the budget the discover
#: site's reads use: generous enough for a key-by-key picker, and spent per
#: address rather than globally.
SEARCH_LIMIT = "120/minute"
#: Writing a row is another matter. Anyone signed in may propose one, and a
#: proposal is a row in the table every picker reads, so the limit is tight
#: and counted per account as well as by the hour.
SUGGEST_LIMIT = "5/minute;20/hour;50/day"


def _caller(request: Request) -> str:
    """Whose budget a call spends: the signed-in account
    (rate_limit.signed_in_key) when the route asked who is calling, else the
    caller's address as app.core.net reads it -- the forwarded headers only
    from our own proxy, so a caller can neither write a fresh address to
    dodge its own budget nor spend everyone else's."""
    from app.api.security.service import client_ip

    return signed_in_key(request) or client_ip(request) or "unknown"


@router.post("/create", summary="Create a new organization")
async def create_organization(
    session: SessionDep,
    user: ContentAuth,
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
    id: int, session: SessionDep, user: DestroyAuth, request: Request = None
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
    user: ContentAuth,
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
    user: ModerateAuth,
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
    session: SessionDep, user: ContentAuth, file: UploadFile = File(...)
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


@router.get("/search", summary="Find an organisation by name or address (public)")
@limiter.limit(SEARCH_LIMIT, key_func=_caller)
async def search_organizations_endpoint(
    request: Request,
    session: SessionDep,
    q: str | None = Query(None, max_length=100, description="Words of the name or the address, in any order, each a part of a word; a typo is forgiven"),
    type: OrgTypes | None = Query(None, description="Only organisations of this type"),
    limit: int = Query(service.SEARCH_LIMIT_DEFAULT, ge=1, le=service.SEARCH_LIMIT_MAX),
) -> List[OrganizationSearchHit]:
    """The type-ahead behind the app's onboarding ("which college?") and the
    public registration form, so it takes no sign-in; its budget is the
    caller's address. Blocked and deleted organisations are never in it.
    Without `q`, the verified ones alphabetically: a picker's first screen."""
    return await service.search_organizations(session, q, type_=type, limit=limit)


@router.post("/suggest", summary="Propose an organisation that is missing")
@limiter.limit(SUGGEST_LIMIT, key_func=_caller)
async def suggest_organization_endpoint(
    request: Request,
    session: SessionDep,
    user: UserAuth,
    proposal: OrganizationSuggestRequest,
) -> OrganizationSuggestion:
    """A signed-in app user adds the college the import missed, and selects it
    straight away. It is added unverified, and an admin verifies it.

    Anyone signed in can write a row here, so the limit is tight. An
    organisation already there under the same name and address is not added
    twice: that one comes back instead, with `created` false."""
    org, created = await service.suggest_organization(session, proposal, user)
    if created:
        await audit.record(
            session, actor=user, action="org.suggest", entity_type="org",
            entity_id=org.id, summary=f"Suggested organisation “{org.name}”",
            details={"type": org.type.value, "address": org.address}, request=request,
        )
    await session.commit()
    return OrganizationSuggestion(
        id=org.id, name=org.name, type=org.type, address=org.address,
        is_verified=org.is_verified, created=created,
    )


@router.get("/admin/list", summary="Organizations, searched and filtered (admin)")
async def admin_list_organizations_endpoint(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
    q: str | None = Query(None, max_length=100, description="Words of the name or the address"),
    type: OrgTypes | None = Query(None),
    is_verified: bool | None = Query(None),
    is_blocked: bool | None = Query(None),
) -> PaginatedResponse[OrganizationAdminRow]:
    """The admin's list. Unlike /orgs/list it is paged, searchable and shows
    whether a row is verified; blocked rows are in it, since an admin has to
    be able to find what they blocked. Declared before /{id} so the literal
    path wins."""
    orgs, total = await service.admin_list_organizations(
        session, q=q, type_=type, is_verified=is_verified, is_blocked=is_blocked,
        limit=pagination.limit, offset=pagination.offset,
    )
    return paginated_response(orgs, request, schema=OrganizationAdminRow, total=total)


@router.get("/admin/duplicates", summary="Organizations that share a name (admin)")
async def admin_duplicate_organizations_endpoint(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
) -> PaginatedResponse[OrganizationDuplicateGroup]:
    """The groups to work through, the ones that look like a row imported
    twice first (`needs_attention`: one of them has no address while another
    has one). Most groups are one college's branches in several towns and
    want leaving alone."""
    groups, total = await service.duplicate_organizations(
        session, limit=pagination.limit, offset=pagination.offset
    )
    return paginated_response(groups, request, schema=OrganizationDuplicateGroup, total=total)


@router.post("/{id}/merge", summary="Merge a duplicate organization into another (admin)")
async def merge_organizations_endpoint(
    id: int,
    body: OrganizationMergeRequest,
    session: SessionDep,
    user: DestroyAuth,
    request: Request = None,
) -> OrganizationMergeResult:
    """`id` is the duplicate: every reference to it moves to `into`, and it is
    then deleted. One transaction with its audit line, so a merge is never
    half done."""
    result = await service.merge_organizations(session, id, body.into)
    await audit.record(
        session, actor=user, action="org.merge", entity_type="org",
        entity_id=id,
        summary=f"Merged organisation “{result['duplicate_name']}” into “{result['target_name']}”",
        details={"into": body.into, "moved": result["moved"]}, request=request,
    )
    await session.commit()
    return OrganizationMergeResult(
        merged=result["merged"], into=result["into"], moved=result["moved"]
    )


@router.post("/{id}/restore", summary="Restore a deleted organization (admin)")
async def restore_organization_endpoint(
    id: int, session: SessionDep, user: ModerateAuth, request: Request = None
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
