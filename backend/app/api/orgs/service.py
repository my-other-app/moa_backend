import io
import logging
from sqlalchemy import func, literal_column, select, update
from sqlalchemy.exc import IntegrityError
from app.response import CustomHTTPException
from app.api.orgs import search
from app.api.orgs.models import Organizations, OrgTypes
from app.api.clubs.models import Clubs, ClubUsersLink
from app.api.events.models import Events
from app.api.orgs.schema import OrganizationCreate, OrganizationAnalyticsResponse
from app.api.search import text as stext
from app.db.base import AbstractSQLModel
from sqlalchemy.ext.asyncio import AsyncSession
import pandas as pd
from fastapi import UploadFile
from typing import List

logger = logging.getLogger(__name__)

#: How many rows the type-ahead answers with when the caller does not say,
#: and the most it ever will: a picker shows a handful.
SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 25


async def create_organization(org: OrganizationCreate, session: AsyncSession):
    db_org = Organizations(
        name=org.name,
        type=org.type,
        address=org.address,
        phone=org.phone,
        email=org.email,
        website=org.website,
    )
    if org.logo:
        db_org.logo = {
            "bytes": io.BytesIO(await org.logo.read()),
            "filename": org.logo.filename,
        }
    session.add(db_org)
    await session.commit()
    await session.refresh(db_org)
    return db_org


async def delete_organization(id: int, session: AsyncSession):
    org = await session.get(Organizations, id)
    if not org:
        raise CustomHTTPException(status_code=404, message="Organization not found")
    org.soft_delete()
    await session.commit()
    return {"ok": True}


async def list_organizations(session: AsyncSession):
    orgs = await session.execute(select(Organizations).where(Organizations.is_deleted == False))
    return orgs.scalars().all()


async def update_organization(id: int, org_data, session: AsyncSession):
    org = await session.get(Organizations, id)
    if not org or org.is_deleted:
        raise CustomHTTPException(status_code=404, message="Organization not found")
        
    if org_data.name is not None:
        org.name = org_data.name
    if org_data.type is not None:
        org.type = org_data.type
    if org_data.address is not None:
        org.address = org_data.address
    if org_data.phone is not None:
        org.phone = org_data.phone
    if org_data.email is not None:
        org.email = org_data.email
    if org_data.website is not None:
        org.website = org_data.website
        
    if getattr(org_data, 'logo', None):
        org.logo = {
            "bytes": io.BytesIO(await org_data.logo.read()),
            "filename": org_data.logo.filename,
        }
    
    await session.commit()
    await session.refresh(org)
    return org


async def block_organization(
    id: int, session: AsyncSession, is_blocked: bool | None = None
):
    """
    Block or unblock an organisation.

    Pass `is_blocked` to set it outright. The endpoint used to toggle and
    ignore its request body, which is only safe when the caller's view of the
    current state is correct -- and the admin dashboard caches this list for
    ten minutes, so clicking "Block" on a stale row would have unblocked it.
    Setting an explicit value is idempotent and cannot invert.

    Omitting it still toggles, so any caller that relied on the old behaviour
    keeps working.
    """
    org = await session.get(Organizations, id)
    if not org or org.is_deleted:
        raise CustomHTTPException(status_code=404, message="Organization not found")

    org.is_blocked = (not org.is_blocked) if is_blocked is None else is_blocked
    await session.commit()
    return {"message": f"Organization {'blocked' if org.is_blocked else 'unblocked'} successfully", "is_blocked": org.is_blocked}


async def get_organization(id: int, session: AsyncSession):
    """One organisation. The list endpoint returns every row with no filter,
    so a detail page had to fetch all of them and pick one out."""
    org = await session.get(Organizations, id)
    if not org or org.is_deleted:
        raise CustomHTTPException(status_code=404, message="Organization not found")
    return org


async def import_organizations(file: UploadFile, session: AsyncSession):
    if not file.filename.endswith(('.csv', '.xlsx')):
        raise CustomHTTPException(status_code=400, message="Only CSV and XLSX files are supported")
        
    content = await file.read()
    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(content))
        else:
            df = pd.read_excel(io.BytesIO(content))
            
        df = df.fillna('')
        
        imported_count = 0
        for _, row in df.iterrows():
            if not row.get('name') or not row.get('type'):
                continue
                
            org = Organizations(
                name=str(row['name']),
                type=str(row['type']).lower(),
                address=str(row.get('address', '')),
                phone=str(row.get('phone', '')),
                email=str(row.get('email', '')),
                website=str(row.get('website', '')),
            )
            session.add(org)
            imported_count += 1
            
        await session.commit()
        return {"message": f"Successfully imported {imported_count} organizations"}
    except Exception as e:
        raise CustomHTTPException(status_code=400, message=f"Failed to parse file: {str(e)}")


async def get_organization_analytics(id: int, session: AsyncSession) -> OrganizationAnalyticsResponse:
    org = await session.get(Organizations, id)
    if not org or org.is_deleted:
        raise CustomHTTPException(status_code=404, message="Organization not found")

    # 1. Total Clubs
    clubs_query = select(func.count(Clubs.id)).where(Clubs.org_id == id, Clubs.is_deleted == False)
    total_clubs_result = await session.execute(clubs_query)
    total_clubs = total_clubs_result.scalar() or 0

    # 2. Total Events under those clubs
    # Subquery to get active club IDs for this organization
    active_club_ids_query = select(Clubs.id).where(Clubs.org_id == id, Clubs.is_deleted == False)
    
    events_query = select(func.count(Events.id)).where(
        Events.club_id.in_(active_club_ids_query),
        Events.is_deleted == False
    )
    total_events_result = await session.execute(events_query)
    total_events = total_events_result.scalar() or 0

    # 3. Total Followers mapping to those clubs
    followers_query = select(func.count(ClubUsersLink.id)).where(
        ClubUsersLink.club_id.in_(active_club_ids_query),
        ClubUsersLink.is_deleted == False,
        ClubUsersLink.is_following == True
    )
    total_followers_result = await session.execute(followers_query)
    total_followers = total_followers_result.scalar() or 0

    return OrganizationAnalyticsResponse(
        total_clubs=total_clubs,
        total_events=total_events,
        total_followers=total_followers
    )


async def list_deleted_organizations(
    session: AsyncSession, limit: int = 20, offset: int = 0
):
    # include_deleted on each: the session hides deleted rows from every ORM
    # query (app.db.listeners), so without it this finds none.
    total = await session.scalar(
        select(func.count(Organizations.id))
        .where(Organizations.is_deleted == True)
        .execution_options(include_deleted=True)
    )
    rows = await session.scalars(
        select(Organizations)
        .where(Organizations.is_deleted == True)
        .execution_options(include_deleted=True)
        .order_by(Organizations.deleted_at.desc().nulls_last(), Organizations.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), (total or 0)


async def restore_organization(session: AsyncSession, id: int):
    org = await session.scalar(
        select(Organizations)
        .where(Organizations.id == id, Organizations.is_deleted == True)
        .execution_options(include_deleted=True)
    )
    if not org:
        raise CustomHTTPException(404, "No deleted organization with that id")

    org.is_deleted = False
    org.deleted_at = None
    await session.commit()
    return org


# ─── Search, suggestions, merging and the admin's list ────────────────────


async def search_organizations(
    session: AsyncSession,
    q: str | None,
    *,
    type_: OrgTypes | None = None,
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> List[Organizations]:
    """The type-ahead behind the app's onboarding and the public registration
    form. Neither blocked nor deleted rows, best first (app.api.orgs.search)."""
    return await search.find(session, q, type_=type_, limit=limit)


async def suggest_organization(
    session: AsyncSession, proposal, actor
) -> tuple[Organizations, bool]:
    """An organisation a signed-in app user says is missing, and whether it
    was added. Unverified: an admin verifies it, and until then it ranks
    below the real rows (app.api.orgs.search).

    An obvious duplicate -- the same name and the same address, both
    normalised -- is not added; the row already there is returned instead, so
    onboarding selects that one. Branches of one college differ by their
    address and are not duplicates of each other.

    The caller commits."""
    existing = await search.same_organisation(session, proposal.name, proposal.address)
    if existing is not None:
        if existing.is_blocked:
            raise CustomHTTPException(
                status_code=409,
                message="That organisation is blocked",
                error_code="ORG_BLOCKED",
            )
        return existing, False

    org = Organizations(
        name=proposal.name.strip(),
        type=proposal.type,
        address=(proposal.address or "").strip() or None,
        is_verified=False,
    )
    session.add(org)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Nothing constrains the name, so this is a column the proposal
        # overran or a type the enum does not have; either way it is the
        # caller's, not ours.
        await session.rollback()
        raise CustomHTTPException(status_code=400, message="That organisation could not be added") from exc
    logger.info("organisation %s suggested by user %s", org.id, getattr(actor, "id", None))
    return org, True


def org_references() -> List:
    """Every column in the schema that points at organizations.id.

    Read from the metadata rather than listed here, so a table that gains an
    org_id later is moved by a merge without anyone remembering to come back.
    Today it is clubs.org_id and user_profiles.org_id; tests/test_org_search.py
    pins that, and will fail on a third so it can be thought about rather
    than silently moved."""
    found = []
    for table in AbstractSQLModel.metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == "organizations" and fk.column.name == "id":
                    found.append(column)
    return found


async def merge_organizations(
    session: AsyncSession, duplicate_id: int, target_id: int
) -> dict:
    """Move every reference to `duplicate_id` onto `target_id` and delete the
    duplicate. One transaction: either every reference moved and the
    duplicate is gone, or nothing happened.

    The updates are statements against the tables, not the ORM: a club or a
    profile an admin removed still points at the duplicate, and leaving it
    pointing at a row that is gone is how a restore from the trash comes back
    attached to nothing.

    The caller commits (and writes the audit line in the same transaction)."""
    if duplicate_id == target_id:
        raise CustomHTTPException(
            status_code=400,
            message="An organisation cannot be merged into itself",
            error_code="ORG_MERGE_SELF",
        )
    duplicate = await session.get(Organizations, duplicate_id)
    if not duplicate or duplicate.is_deleted:
        raise CustomHTTPException(status_code=404, message="Organization not found")
    target = await session.get(Organizations, target_id)
    if not target or target.is_deleted:
        raise CustomHTTPException(status_code=404, message="No organization to merge into")

    moved: dict[str, int] = {}
    for column in org_references():
        result = await session.execute(
            update(column.table)
            .where(column == duplicate_id)
            .values({column.name: target_id})
        )
        moved[f"{column.table.name}.{column.name}"] = int(result.rowcount or 0)
    duplicate.soft_delete()
    return {
        "merged": duplicate_id,
        "into": target_id,
        "moved": moved,
        "duplicate_name": duplicate.name,
        "target_name": target.name,
    }


async def admin_list_organizations(
    session: AsyncSession,
    *,
    q: str | None = None,
    type_: OrgTypes | None = None,
    is_verified: bool | None = None,
    is_blocked: bool | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[List[Organizations], int]:
    """The admin's list: a page of organisations and how many there are.

    Blocked rows are in it, unlike the public search's -- an admin blocked
    them and has to be able to find them again. The search is the same words
    over the same column, without the typo pass: a pager whose total comes
    from one set of conditions and whose rows come from another cannot be
    paged through."""
    criteria = [Organizations.is_deleted == False]  # noqa: E712
    if type_ is not None:
        criteria.append(Organizations.type == type_)
    if is_verified is not None:
        criteria.append(Organizations.is_verified.is_(is_verified))
    if is_blocked is not None:
        criteria.append(Organizations.is_blocked.is_(is_blocked))
    nq = await stext.normalise_org(session, q)
    words = search.words_of(nq)
    if words:
        criteria.append(search.every_word(Organizations.search_text, words))

    total = await session.scalar(select(func.count(Organizations.id)).where(*criteria))
    query = select(Organizations).where(*criteria)
    if words:
        query = search.ordered(query, nq, words)
    else:
        query = query.order_by(Organizations.name, Organizations.id)
    rows = await session.scalars(query.limit(limit).offset(offset))
    return list(rows), (total or 0)


async def duplicate_organizations(
    session: AsyncSession, *, limit: int = 20, offset: int = 0
) -> tuple[List[dict], int]:
    """Groups of organisations that share one normalised name, and how many
    groups there are.

    The same name with and without its punctuation is one group, which is
    what brings "College of Applied Science (IHRD)" together with "College of
    Applied Science IHRD". Most groups are not duplicates at all -- one
    college's teacher education centres sit in five towns under one name,
    each with its own address -- so a group where one row has no address
    beside siblings that do, the shape of a row imported twice, is flagged
    and sorted first.

    No index serves this (unaccent is STABLE, so no index may hold the
    normalised name): it reads the table, which is why it is the admin's and
    paged. At 1,936 rows it is a few milliseconds, and at fifty thousand a
    fraction of a second."""
    name_norm = search.NAME_NORM.label("norm")
    has_address = Organizations.address.isnot(None) & (func.btrim(Organizations.address) != "")
    groups = (
        select(
            name_norm,
            func.count(Organizations.id).label("count"),
            (func.bool_or(~has_address) & func.bool_or(has_address)).label("needs_attention"),
        )
        .where(Organizations.is_deleted == False)  # noqa: E712
        .group_by(name_norm)
        .having(func.count(Organizations.id) > 1)
    )
    total = await session.scalar(select(func.count()).select_from(groups.subquery()))
    page = (
        await session.execute(
            groups.order_by(
                literal_column("needs_attention").desc(),
                literal_column("count").desc(),
                literal_column("norm"),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    if not page:
        return [], (total or 0)

    names = [row.norm for row in page]
    rows = (
        await session.execute(
            select(Organizations, name_norm)
            .where(Organizations.is_deleted == False, name_norm.in_(names))  # noqa: E712
            .order_by(Organizations.is_verified.desc(), Organizations.id)
        )
    ).all()
    by_name: dict[str, list] = {name: [] for name in names}
    for org, norm in rows:
        by_name.setdefault(norm, []).append(org)
    return [
        {
            "name": row.norm,
            "count": row.count,
            "needs_attention": bool(row.needs_attention),
            "orgs": by_name.get(row.norm, []),
        }
        for row in page
    ], (total or 0)
