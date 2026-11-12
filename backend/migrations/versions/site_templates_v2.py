"""Club websites v2: the designer's ten templates, sections that move, a photo library
(feature-register/site-templates-contract.md, builder B).

- club_site_images (new): a club site's photo library, up to 20 photos per
  club (the API holds the limit), each an S3 image under clubs/site/ with
  its widths beside it, alt text, its place in the club's order, and the
  stored original's width and height. Gone with the club (cascade).
  Index (club_id, position): the library in order.
- club_sites.layout (JSON, '{}'): the club's arrangement of its template --
  {order, hidden, titles, slots, accent} (app/api/sites/layout.py). Added
  with a constant default: a catalog change, no rewrite.
- club_sites.template: every site moves to `classic` (the four templates of
  the first version are gone), keeping its headline and subline; the
  sections a club had turned off become `layout.hidden` (partners ->
  sponsors, follow -> join; hero and contact are part of every template).
  Its default becomes `classic`. `settings_at` is set to now on every row,
  so a published page is behind its settings and the sites sweep makes it
  again in the new template within five minutes (its stored HTML carries the
  old stylesheet, which the new CSP no longer names);
  scripts/republish_sites.py does all of them at once.
- club_sites.theme, .font, .sections stay, with their defaults, unread.

club_sites holds one row per club that opened its website page (tens):
its rows are updated one by one, by id.

Downgrade: the library table and `layout` are dropped (the photos' files
stay in the bucket), every site goes back to `launchpad` with the sections
it hides turned off again.

Revision ID: site_templates_v2
Revises: platform_mail
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "site_templates_v2"
down_revision = "platform_mail"
branch_labels = None
depends_on = None

#: The first version's section toggles, as the new section keys.
OLD_TO_NEW = {"upcoming": "upcoming", "about": "about", "past": "past", "team": "team", "partners": "sponsors", "follow": "join"}


def _as_dict(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.create_table(
        "club_site_images",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("image", sa.String(), nullable=False),
        sa.Column("alt", sa.String(200), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_club_site_images_club_position", "club_site_images", ["club_id", "position"])

    op.add_column("club_sites", sa.Column("layout", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.alter_column("club_sites", "template", server_default="classic")

    bind = op.get_bind()
    for site_id, sections in bind.execute(sa.text("SELECT id, sections FROM club_sites ORDER BY id")).all():
        hidden = [new for old, new in OLD_TO_NEW.items() if _as_dict(sections).get(old) is False]
        bind.execute(
            sa.text("UPDATE club_sites SET template = 'classic', layout = CAST(:layout AS json), settings_at = now() WHERE id = :id"),
            {"id": site_id, "layout": json.dumps({"hidden": hidden} if hidden else {})},
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    bind = op.get_bind()
    back = {new: old for old, new in OLD_TO_NEW.items()}
    for site_id, sections, layout in bind.execute(sa.text("SELECT id, sections, layout FROM club_sites ORDER BY id")).all():
        toggles = _as_dict(sections)
        for k in _as_dict(layout).get("hidden") or []:
            if k in back:
                toggles[back[k]] = False
        bind.execute(
            sa.text("UPDATE club_sites SET template = 'launchpad', sections = CAST(:sections AS json), settings_at = now() WHERE id = :id"),
            {"id": site_id, "sections": json.dumps(toggles)},
        )
    op.alter_column("club_sites", "template", server_default="launchpad")
    op.drop_column("club_sites", "layout")
    op.drop_index("ix_club_site_images_club_position", table_name="club_site_images")
    op.drop_table("club_site_images")
