from fastapi import File, Form, UploadFile
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


class OrganizationCreate:
    def __init__(
        self,
        name: str = Form(..., min_length=3, max_length=100),
        type: OrgTypes = Form(...),
        address: Optional[str] = Form(None, max_length=200),
        phone: Optional[str] = Form(None, max_length=20),
        email: Optional[str] = Form(None, max_length=100),
        website: Optional[str] = Form(None, max_length=100),
        logo: Optional[UploadFile] = File(None),
    ):
        self.name = name
        self.type = type
        self.address = address
        self.phone = phone
        self.email = email
        self.website = website
        self.logo = logo


class OrganizationDetailResponse(BaseModel):
    id: int = Field(..., gt=0)
    name: str = Field(..., min_length=3, max_length=100)
    type: OrgTypes = Field(...)
    address: Optional[str] = Field(None, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=100)
    website: Optional[str] = Field(None, max_length=100)
    logo: Optional[dict] = Field(None, max_length=100)
