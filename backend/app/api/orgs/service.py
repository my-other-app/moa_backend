import io
from sqlalchemy import select
from app.response import CustomHTTPException
from app.api.orgs.models import Organizations
from app.api.orgs.schema import OrganizationCreate
from sqlalchemy.ext.asyncio import AsyncSession


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
    orgs = await session.execute(select(Organizations))
    orgs = orgs.scalars().all()
    return orgs
