"""An event's sponsors and partners, shown on its public page (docs/EVENT_PARTNERS.md).

The club's year-round partners are `clubs.models.ClubPartners`; these belong
to one event. One copied from a club partner keeps a note of which
(`club_partner_id`) and a logo of its own, so changing or removing the club
partner never touches the event."""
from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, String, text

from app.core.storage.fields import S3ImageField
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin

KINDS = ("sponsor", "partner")
#: Live rows an event may list, sponsors and partners together.
MAX_PER_EVENT = 50
NAME_MAX = 100
LABEL_MAX = 60
LINK_MAX = 500

#: A logo goes in a square tile, whole: each size is the picture fitted inside
#: the box, never cropped or enlarged (fields.prepare_image). 1080 is the size
#: we ask clubs for.
LOGO_SIZES = {
    "thumbnail": {"width": 150, "height": 150},
    "medium": {"width": 400, "height": 400},
    "large": {"width": 1080, "height": 1080},
}


class EventPartners(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    """One sponsor or partner of one event: a name, a logo, an optional label
    ("Title sponsor") and link, in its place among the others of its kind."""

    __tablename__ = "event_partners"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)
    name = Column(String(NAME_MAX), nullable=False)
    label = Column(String(LABEL_MAX), nullable=True)
    link = Column(String(LINK_MAX), nullable=True)
    # Order within its kind, 0 first.
    position = Column(Integer, nullable=False, default=0, server_default="0")
    # The club partner this was copied from, if it was. Informational only.
    club_partner_id = Column(Integer, ForeignKey("club_partners.id", ondelete="SET NULL"), nullable=True)
    logo = Column(S3ImageField(upload_to="events/partners/", variations=LOGO_SIZES, public=True), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('sponsor', 'partner')", name="ck_event_partners_kind"),
        # The page's read: one event's live rows, in order.
        Index("ix_event_partners_event_live", "event_id", "kind", "position", postgresql_where=text("NOT is_deleted")),
    )
