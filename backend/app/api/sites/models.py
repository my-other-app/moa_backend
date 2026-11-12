"""A club's public website: one page, made from the club's own data in one
of the designer's templates (app/api/sites/registry.py), arranged by the
club -- which sections, in what order, under what titles, which of its
photos where -- and stored as a single HTML file. Nothing on it is edited by
hand; it is regenerated from the profile, the team, the partners, the
events, the speakers and the photo library.

- club_sites: the club's choices (template, headline, subline, `layout`)
  and the page as last published.
- club_site_images: the club's photo library for the site, up to
  MAX_IMAGES, in the club's order, each with its alt text and its size.

The columns `theme`, `font` and `sections` of the first version are still
in the table (with their defaults) but no longer read: the templates fix
their own look, and the migration site_templates_v2 moved the sections a
club had turned off into `layout.hidden`.
"""
import sqlalchemy as sa
from sqlalchemy import JSON, Column, ForeignKey, Integer, String, Text

from app.core.storage.fields import S3ImageField
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

#: The template a new site starts on, and the one the first version's sites moved to.
DEFAULT_TEMPLATE = "classic"

#: The photo library: how many photos a club keeps for its site, and how large each upload may be.
MAX_IMAGES = 20
IMAGE_MB = 10

#: The library's sizes, each a width (the box is four times as tall, so a
#: portrait photo is never cut down by its height first): the browser picks
#: one from the img's srcset for the slot's width at the screen's density.
IMAGE_SIZES = {
    "w480": {"width": 480, "height": 1920},
    "w960": {"width": 960, "height": 3840},
    "w1440": {"width": 1440, "height": 5760},
    "w2400": {"width": 2400, "height": 9600},
}
IMAGE_FOLDER = "clubs/site/"


class ClubSites(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_sites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    template = Column(String(20), nullable=False, default=DEFAULT_TEMPLATE)
    headline = Column(String(120), nullable=True)
    subline = Column(String(240), nullable=True)
    # How the club arranged the template (app/api/sites/layout.py):
    # {"order": [section, ...], "hidden": [section, ...], "titles": {section: str},
    #  "slots": {"hero.cover": image id, ...}, "accent": null | "#rrggbb"}.
    layout = Column(JSON, nullable=False, default=dict)
    # The page as last published: what the public URL serves.
    html = Column(Text, nullable=True)
    published_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # When a choice last changed; later than published_at means the page is behind.
    settings_at = Column(TZAwareDateTime(timezone=True), nullable=True)


class ClubSiteImages(AbstractSQLModel, TimestampsMixin):
    """One photo in a club's site library. Hard-deleted (its files with it):
    a photo the club takes down leaves the page at once."""

    __tablename__ = "club_site_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    image = Column(
        S3ImageField(
            upload_to=IMAGE_FOLDER, variations=IMAGE_SIZES, public=True,
            max_size=IMAGE_MB * 1024 * 1024, allowed_extensions=["jpg", "jpeg", "png", "webp"],
        ),
        nullable=False,
    )
    alt = Column(String(200), nullable=True)
    # 0..n-1 within the club, rewritten whole on every reorder and delete.
    position = Column(Integer, nullable=False, default=0)
    # The stored original's size in pixels, for the img's width and height.
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)

    __table_args__ = (sa.Index("ix_club_site_images_club_position", "club_id", "position"),)
