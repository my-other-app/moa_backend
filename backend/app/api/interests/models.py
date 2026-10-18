import enum
import re
import unicodedata

import sqlalchemy as sa
from sqlalchemy import Column, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class InterestIconType(enum.Enum):
    emoji = "emoji"
    url = "url"


#: The longest slug the endpoints take or make.
SLUG_MAX = 60


def slugify(name: str) -> str:
    """A name as it appears in an address: lower case ASCII letters and
    digits, words joined by single hyphens ("Gaming & Media" is
    gaming-media, "UI/UX" ui-ux), at most SLUG_MAX characters. "topic" for
    a name with none of those."""
    ascii_ = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:SLUG_MAX].strip("-") or "topic"


def numbered(base: str, n: int) -> str:
    """The n-th slug made from `base` (base, base-2, base-3…), within SLUG_MAX."""
    if n <= 1:
        return base
    suffix = f"-{n}"
    return base[: SLUG_MAX - len(suffix)].rstrip("-") + suffix


def _slug_default(context) -> str:
    """A row made without a slug (the suite, a script) gets its name's. The
    endpoints pick one that is free among live rows themselves (service.free_slug)."""
    return slugify(context.get_current_parameters().get("name") or "")


class InterestCategory(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """A topic: the main category people browse by (Development, Arts…).
    People, clubs and events pick interests; their topics follow from them."""

    __tablename__ = "interest_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Unique among live topics (ux_interest_categories_name_live), so a
    # deleted topic's name can be used again.
    name = Column(String, nullable=False)
    # The topic's address on the site (/topics/{slug}), unique among live topics.
    slug = Column(String(80), nullable=False, default=_slug_default)
    icon = Column(String, nullable=True)
    icon_type = Column(Enum(InterestIconType), nullable=True)
    # "#RRGGBB": the topic's colour on chips and cards.
    color = Column(String(7), nullable=True)
    description = Column(String(500), nullable=True)
    # Lists are in this order, then by name.
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        sa.Index("ux_interest_categories_slug_live", "slug", unique=True, postgresql_where=sa.text("NOT is_deleted")),
        sa.Index("ux_interest_categories_name_live", "name", unique=True, postgresql_where=sa.text("NOT is_deleted")),
    )


class Interests(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "interests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    # Unique among live interests (ux_interests_slug_live).
    slug = Column(String(80), nullable=False, default=_slug_default)
    icon = Column(String, nullable=True)
    icon_type = Column(Enum(InterestIconType), nullable=True)
    category_id = Column(Integer, ForeignKey("interest_categories.id"), nullable=False)
    # Within its topic, lists are in this order, then by name.
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")

    category = relationship("InterestCategory")
    events = relationship(
        "Events", secondary="event_interests_link", back_populates="interests"
    )
    clubs = relationship(
        "Clubs", secondary="clubs_interests_link", back_populates="interests"
    )

    __table_args__ = (
        sa.Index("ux_interests_slug_live", "slug", unique=True, postgresql_where=sa.text("NOT is_deleted")),
        # A topic's interests: the topic filters and the topic pages read them this way.
        sa.Index("ix_interests_category", "category_id"),
    )
