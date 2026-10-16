"""A club's public website: one page, made from the club's own data and a
handful of choices — a template, a look, a headline, which sections —
and stored as a single HTML file. Nothing on it is edited by hand; it is
regenerated from the profile, the team, the partners and the events."""
from sqlalchemy import JSON, Column, ForeignKey, Integer, String, Text

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

TEMPLATES = ("launchpad", "chapter", "campus", "arena")
THEMES = ("light", "dark")
FONTS = {
    "dm_sans": ("DM Sans", "DM+Sans:wght@400;500;600;700"),
    "inter": ("Inter", "Inter:wght@400;500;600;700"),
    "fraunces": ("Fraunces", "Fraunces:wght@400;500;600;700"),
    "space_grotesk": ("Space Grotesk", "Space+Grotesk:wght@400;500;600;700"),
    "manrope": ("Manrope", "Manrope:wght@400;500;600;700;800"),
}
SECTIONS = ("hero", "upcoming", "about", "past", "team", "partners", "follow", "contact")


class ClubSites(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_sites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    template = Column(String(20), nullable=False, default="launchpad")
    theme = Column(String(10), nullable=False, default="light")
    font = Column(String(20), nullable=False, default="dm_sans")
    headline = Column(String(120), nullable=True)
    subline = Column(String(240), nullable=True)
    # {"hero": true, "upcoming": true, ...}; a missing key means on.
    sections = Column(JSON, nullable=False, default=dict)
    # The page as last published: what the public URL serves.
    html = Column(Text, nullable=True)
    published_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # When a choice last changed; later than published_at means the page is behind.
    settings_at = Column(TZAwareDateTime(timezone=True), nullable=True)
