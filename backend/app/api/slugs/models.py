"""The slugs a club or an event was once at.

A slug is made once, from the name (app/core/utils/keys.generate_slug), and
is printed on posters and QR codes, sent in mails and shared in links, and
for a club it is also its website's address ({slug}.myotherapp.in). When an
admin changes one (app/api/slugs/router.py), the old one becomes a row here
and every lookup by slug falls back to these rows, so the old address keeps
answering -- forever: with the canonical object, whose `slug` is the new one,
so a client that asked by the old one can redirect.

- kind: 'club' or 'event' (the two slug spaces are separate, as the columns are).
- slug: the old slug, exactly as it was (a legacy one need not pass today's rule).
- target_id: the club's or the event's id. No foreign key: one column names
  rows of two tables. A target that is gone simply finds nothing.
- created_by_user_id: the admin who made the change.

Unique per (kind, slug): an old slug names one thing. A live slug is never
also an alias of the same kind (the change refuses one that is either), and
changing back to an old slug takes its row away.
"""
from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, String, text

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

CLUB = "club"
EVENT = "event"
KINDS = (CLUB, EVENT)


class SlugAliases(AbstractSQLModel):
    __tablename__ = "slug_aliases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(8), nullable=False)
    slug = Column(String(120), nullable=False)
    target_id = Column(Integer, nullable=False)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('club', 'event')", name="ck_slug_aliases_kind"),
        Index("uq_slug_aliases_kind_slug", "kind", "slug", unique=True),
        Index("ix_slug_aliases_kind_target", "kind", "target_id"),
    )
