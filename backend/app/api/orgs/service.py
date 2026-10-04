import io
from sqlalchemy import select, func
from app.response import CustomHTTPException
from app.api.orgs.models import Organizations
from app.api.clubs.models import Clubs, ClubUsersLink
from app.api.events.models import Events
from app.api.orgs.schema import OrganizationCreate, OrganizationAnalyticsResponse
from sqlalchemy.ext.asyncio import AsyncSession
import pandas as pd
from fastapi import UploadFile
from typing import List


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
