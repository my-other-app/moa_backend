import io
from sqlalchemy import select
from app.response import CustomHTTPException
from app.api.orgs.models import Organizations
from app.api.orgs.schema import OrganizationCreate
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


async def block_organization(id: int, session: AsyncSession):
    org = await session.get(Organizations, id)
    if not org or org.is_deleted:
        raise CustomHTTPException(status_code=404, message="Organization not found")
        
    org.is_blocked = not org.is_blocked
    await session.commit()
    return {"message": f"Organization {'blocked' if org.is_blocked else 'unblocked'} successfully", "is_blocked": org.is_blocked}


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
