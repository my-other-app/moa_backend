import sqlalchemy as sa
from sqlalchemy import Boolean, Column, Enum, Integer, String
from sqlmodel import Field, SQLModel
from sqlalchemy.orm import deferred, relationship
import enum

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from app.core.storage.fields import S3ImageField


class OrgTypes(enum.Enum):
    school = "school"
    university = "university"
    college = "college"
    company = "company"
    ngo = "ngo"
    community = "community"
    other = "other"


class Organizations(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    type = Column(Enum(OrgTypes), nullable=False)
    address = Column(String(200), nullable=True)
    phone = Column(String(20), nullable=True)
    email = Column(String(100), nullable=True)
    website = Column(String(100), nullable=True)
    logo = Column(
        S3ImageField(
            upload_to="/organizations/logos/",
            variations={
                "thumbnail": {"width": 150, "height": 150},
                "medium": {"width": 500, "height": 500},
                "large": {"width": 800, "height": 800},
            },
        ),
        nullable=True,
    )
    is_verified = Column(Boolean, default=False, nullable=False)
    is_blocked = Column(Boolean, default=False, nullable=False)
    # The name and the address, lowercased, unaccented and with every run of
    # punctuation turned into one space (app.api.search.text), so "College of
    # Applied Science (IHRD)" and "College of Applied Science IHRD" are the
    # same text and a word is found by one LIKE on one trigram-indexed
    # column. Kept by a trigger; only SQL reads it.
    search_text = deferred(Column(sa.Text, nullable=True))

    clubs = relationship("Clubs", back_populates="org")

    __table_args__ = (
        # Everything an organisation is found by: substrings for the words of
        # a query, `%>` for a word with a typo in it (app.api.orgs.search).
        sa.Index(
            "ix_organizations_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
            postgresql_where=sa.text("NOT is_deleted"),
        ),
        # The picker's list and the admin's, both by name.
        sa.Index("ix_organizations_name", "name", postgresql_where=sa.text("NOT is_deleted")),
    )
