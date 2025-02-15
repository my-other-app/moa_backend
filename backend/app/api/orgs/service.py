from sqlalchemy import select
from app.response import CustomHTTPException
from app.api.orgs.models import Organizations
from app.api.orgs.schema import OrganizationCreate
from sqlalchemy.ext.asyncio import AsyncSession


async def create_organization(org: OrganizationCreate, session: AsyncSession):
    print(org.model_dump())
    org = Organizations(**org.model_dump())
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


async def delete_organization(id: int, session: AsyncSession):
    org = await session.get(Organizations, id)
    if not org:
        raise CustomHTTPException(status_code=404, message="Organization not found")
    org.soft_delete()
    await session.commit()
    return {"ok": True}


async def list_organizations(session: AsyncSession):
    orgs = await session.execute(select(Organizations))
    orgs = orgs.scalars().all()
    return orgs
