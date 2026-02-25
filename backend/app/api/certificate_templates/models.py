from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    JSON,
)
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class CertificateTemplates(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "certificate_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    name = Column(String(100), nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)

    # Style configuration JSON
    # Schema:
    # {
    #   "accent_color": "#1db9a0",         -- Primary decorative color
    #   "bg_color": "#ffffff",             -- Inner certificate background
    #   "text_color": "#000000",           -- Primary text color
    #   "font": "Inter",                   -- Google Font name
    #   "logo_url": null,                  -- Club logo URL for certificate
    #   "background_image_url": null,      -- Optional background image
    #   "signature_url": null,             -- Optional signature image
    #   "custom_text": null                -- Custom text description
    # }
    config = Column(JSON, nullable=False, default=dict)

    club = relationship("Clubs")
