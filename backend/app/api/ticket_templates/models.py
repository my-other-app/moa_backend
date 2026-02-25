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


class TicketTemplates(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "ticket_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    name = Column(String(100), nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)

    # Style configuration JSON
    # Schema:
    # {
    #   "accent_color": "#1db9a0",         -- Outer border / decorative color
    #   "bg_color": "#121212",             -- Inner ticket background
    #   "text_color": "#ffffff",           -- Primary text color
    #   "font": "Inter",                   -- Google Font name
    #   "logo_url": null,                  -- Club logo URL for ticket
    #   "background_image_url": null,      -- Optional background image
    #   "show_prizes": true,               -- Show prize info on ticket
    #   "show_guidelines_link": true,      -- Show guidelines link
    #   "ticket_type_label": "GENERAL",    -- "EARLY BIRD", "VIP", "GENERAL"
    #   "custom_footer_text": null         -- Custom text at bottom
    # }
    config = Column(JSON, nullable=False, default=dict)

    club = relationship("Clubs")
