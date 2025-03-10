from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, Column, DateTime
from app.db.base import AbstractSQLModel
from app.core.utils.db_fields import TZAwareDateTime


class SoftDeleteMixin:
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)
    deleted_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = datetime.now(timezone.utc)

    @classmethod
    def active(cls):
        return cls.query.filter(cls.is_deleted == False)


class TimestampsMixin:
    created_at = Column(
        TZAwareDateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        TZAwareDateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
