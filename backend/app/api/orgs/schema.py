from fastapi import File, Form, UploadFile
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from typing import List, Optional
from app.api.orgs.models import OrgTypes


class OrganizationBaseMin(BaseModel):
    name: str = Field(..., min_length=3, max_length=100)
    type: OrgTypes = Field(...)
    logo: Optional[dict] = Field(None)


class OrganizationBase(OrganizationBaseMin):
    address: Optional[str] = Field(None, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=100)
    website: Optional[str] = Field(None, max_length=100)


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


class OrganizationUpdate:
    def __init__(
        self,
        name: Optional[str] = Form(None, min_length=3, max_length=100),
        type: Optional[OrgTypes] = Form(None),
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
    # Validated straight from an ORM row by paginated_response, which the
    # other org routes avoided by returning objects for FastAPI to serialise.
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., gt=0)
    name: str = Field(..., min_length=3, max_length=100)
    type: OrgTypes = Field(...)
    address: Optional[str] = Field(None, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=100)
    website: Optional[str] = Field(None, max_length=100)
    logo: Optional[dict] = Field(None)
    is_blocked: bool = Field(False)


class OrganizationAnalyticsResponse(BaseModel):
    total_clubs: int = Field(0, description="Total number of clubs affiliated with this organization")
    total_events: int = Field(0, description="Total number of events hosted by all affiliated clubs")
    total_followers: int = Field(0, description="Total number of followers across all affiliated clubs")


class OrganizationSearchHit(BaseModel):
    """One row of the type-ahead: what a picker shows and sends back."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., gt=0)
    name: str = Field(...)
    type: OrgTypes = Field(...)
    address: Optional[str] = Field(None)
    is_verified: bool = Field(False)


class OrganizationSuggestion(OrganizationSearchHit):
    """A suggested organisation, or the one already there that answers it."""

    created: bool = Field(..., description="False when an organisation under this name and address was already there")


class OrganizationSuggestRequest(BaseModel):
    """A missing organisation, as a signed-in app user proposes it. `place`
    is accepted for `address`: the app's onboarding asks for a place."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=3, max_length=100)
    type: OrgTypes = Field(...)
    address: Optional[str] = Field(None, max_length=200, validation_alias=AliasChoices("address", "place"))


class OrganizationMergeRequest(BaseModel):
    """Which organisation the duplicate's references move to."""

    into: int = Field(..., gt=0, description="The organisation to keep")


class OrganizationMergeResult(BaseModel):
    merged: int = Field(..., description="The duplicate, now deleted")
    into: int = Field(..., description="The organisation kept")
    moved: dict[str, int] = Field(..., description="Rows moved, per table")


class OrganizationAdminRow(BaseModel):
    """One row of the admin's organisation list."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., gt=0)
    name: str = Field(...)
    type: OrgTypes = Field(...)
    address: Optional[str] = Field(None)
    phone: Optional[str] = Field(None)
    email: Optional[str] = Field(None)
    website: Optional[str] = Field(None)
    logo: Optional[dict] = Field(None)
    is_verified: bool = Field(False)
    is_blocked: bool = Field(False)


class OrganizationDuplicateGroup(BaseModel):
    """Rows sharing one normalised name: the 55 groups an admin works through."""

    name: str = Field(..., description="The normalised name the rows share")
    count: int = Field(..., description="How many rows share it")
    needs_attention: bool = Field(
        ...,
        description="One of them has no address while another has one -- the shape of a real duplicate, "
        "rather than the same name in several towns",
    )
    orgs: List[OrganizationAdminRow] = Field(...)
