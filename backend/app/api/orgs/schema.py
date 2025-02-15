from pydantic import BaseModel, Field
from typing import Optional
from app.api.orgs.models import OrgTypes


class OrganizationBaseMin(BaseModel):
    name: str = Field(..., min_length=3, max_length=100)
    type: OrgTypes = Field(...)
    logo: Optional[str] = Field(None, max_length=100)


class OrganizationBase(OrganizationBaseMin):
    address: Optional[str] = Field(None, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=100)
    website: Optional[str] = Field(None, max_length=100)


class OrganizationPublic(OrganizationBase):
    id: int = Field(..., gt=0)


class OrganizationPublicMin(OrganizationBaseMin):
    id: int = Field(..., gt=0)


class OrganizationCreate(OrganizationBase):
    pass
