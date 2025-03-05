from pydantic import BaseModel, Field

from app.api.interests.models import InterestIconType
from app.core.response.base_model import CustomBaseModel


class InterestCategoryCreate(CustomBaseModel):
    name: str
    icon: str | None = None
    icon_type: InterestIconType | None = None


class InterestCreate(CustomBaseModel):
    name: str
    icon: str | None = None
    icon_type: InterestIconType | None = None
    category_id: int


class InterestCategoryPublic(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class InterestPublic(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)
    category: InterestCategoryPublic = Field(...)

    class Config:
        from_attributes = True


# RESPONSE MODELS


class InterestCategoryCreateUpdateResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class InterestCreateUpdateResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)
    category_id: int


class InterestListResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)

    class Config:
        from_attributes = True


class InterestCategoryWiseListResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)
    interests: list[InterestListResponse] = Field(...)
