from pydantic import BaseModel, Field


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
