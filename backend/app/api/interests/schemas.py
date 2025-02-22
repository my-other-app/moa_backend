from pydantic import BaseModel, Field

from app.api.interests.models import InterestIconType


class InterestCategoryCreate(BaseModel):
    name: str
    icon: str | None = None
    icon_type: InterestIconType | None = None


class InterestCreate(BaseModel):
    name: str
    icon: str | None = None
    icon_type: InterestIconType | None = None
    category_id: int


class InterestCategoryPublic(BaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class InterestPublic(BaseModel):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)
    category: InterestCategoryPublic = Field(...)

    class Config:
        from_attributes = True
