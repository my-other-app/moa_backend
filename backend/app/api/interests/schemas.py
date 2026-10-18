from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.api.interests.models import SLUG_MAX, InterestIconType
from app.core.response.base_model import CustomBaseModel

#: A slug as it appears in an address: lower case letters and digits, words joined by single hyphens.
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


class _IconTypeAsText(CustomBaseModel):
    """icon_type read off a row is the enum; the answer carries its value."""

    @field_validator("icon_type", mode="before", check_fields=False)
    @classmethod
    def _icon_type_value(cls, value):
        return getattr(value, "value", value)


class InterestCategoryCreate(CustomBaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    icon: str | None = None
    icon_type: InterestIconType | None = None
    # Left out: made from the name (with -2, -3… when that one is taken).
    slug: str | None = Field(None, max_length=SLUG_MAX, pattern=SLUG_PATTERN)
    color: str | None = Field(None, pattern=COLOR_PATTERN)
    description: str | None = Field(None, max_length=500)
    # Left out: after every other topic.
    sort_order: int | None = Field(None, ge=0)


class InterestCategoryUpdate(CustomBaseModel):
    """A field left out of the body stays as it is; one sent as null is cleared
    (a slug cannot be cleared: null leaves it as it is)."""

    name: str = Field(..., min_length=2, max_length=100)
    slug: str | None = Field(None, max_length=SLUG_MAX, pattern=SLUG_PATTERN)
    icon: str | None = None
    icon_type: InterestIconType | None = None
    color: str | None = Field(None, pattern=COLOR_PATTERN)
    description: str | None = Field(None, max_length=500)
    sort_order: int | None = Field(None, ge=0)


class InterestCreate(CustomBaseModel):
    name: str
    icon: str | None = None
    icon_type: InterestIconType | None = None
    category_id: int
    # Left out: made from the name on create, kept as it is on update.
    slug: str | None = Field(None, max_length=SLUG_MAX, pattern=SLUG_PATTERN)
    # Left out: last in its topic on create (and on a move to another topic), kept on update.
    sort_order: int | None = Field(None, ge=0)


class TopicsReorder(CustomBaseModel):
    """`topics`: topic ids in their new order (any left out follow, in their
    old order). `interests`: per topic id, interest ids in their new order;
    an interest listed under another topic moves there (any of the topic's
    own left out follow). Either part may be left out."""

    topics: list[int] | None = None
    interests: dict[int, list[int]] | None = None


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


class TopicRef(BaseModel):
    """A topic on an event or a club: the distinct topics of its interests, in topic order."""

    id: int
    slug: str
    name: str
    color: Optional[str] = None


# RESPONSE MODELS


class InterestCategoryCreateUpdateResponse(_IconTypeAsText):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)
    slug: str = Field(...)
    color: str | None = Field(None)
    description: str | None = Field(None)
    sort_order: int = Field(0)


class InterestCreateUpdateResponse(_IconTypeAsText):
    id: int = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)
    category_id: int
    slug: str = Field(...)
    sort_order: int = Field(0)


class InterestListResponse(_IconTypeAsText):
    id: int = Field(...)
    slug: str = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)
    category_id: int = Field(...)
    sort_order: int = Field(0)

    class Config:
        from_attributes = True


class InterestCategoryWiseListResponse(_IconTypeAsText):
    id: int = Field(...)
    slug: str = Field(...)
    name: str = Field(..., min_length=2)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)
    color: str | None = Field(None)
    description: str | None = Field(None)
    sort_order: int = Field(0)
    interests: list[InterestListResponse] = Field(...)


class TopicInterestCount(BaseModel):
    id: int
    slug: str
    name: str
    upcoming_events: int


class TopicOut(BaseModel):
    """A topic with what is on: upcoming events as discover shows them (published
    or live, not over, by a listed club), and the listed clubs that pick it."""

    id: int
    slug: str
    name: str
    icon: Optional[str] = None
    icon_type: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    upcoming_events: int
    clubs: int
    interests: list[TopicInterestCount]


class TopicDeleteResponse(BaseModel):
    message: str
    # How many interests went to `move_to`.
    moved: int
    move_to: Optional[int] = None
