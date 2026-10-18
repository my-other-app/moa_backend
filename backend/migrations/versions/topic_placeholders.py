"""Placeholder icons out, and the two empty topics made with them.

Every interest that came before the topics, and the old "arts" topic,
carried the icon "string" marked as an emoji: the example value the API's
docs page fills in, sent as it was. The clients each check for a real emoji
and draw nothing, but it is not an icon, and it kept the topics migration
from giving Arts its emoji (that one fills only empty icons). Two topics
were made the same way and never given an interest ("tech", "emerging trend
interests"); the topics list shows empty topics too, so since the topics
deploy they show on the site's /topics and in the app.

- `clean`: a live topic with no live interest, whose icon is that
  placeholder and whose slug the topics tree does not name, is deleted
  (softly, as the admin's delete does). Then every icon that is the
  placeholder ("string", any case, spaces ignored) becomes no icon, and
  the tree's topics left without one get the tree's emoji (Arts: 🎭).
  Running it again changes nothing.

The API now refuses such icons (app/api/interests/schemas.py), so they do
not come back.

Downgrade changes nothing: the placeholders are not worth putting back.

Revision ID: topic_placeholders
Revises: topics
"""
import sqlalchemy as sa
from alembic import op

revision = "topic_placeholders"
down_revision = "topics"
branch_labels = None
depends_on = None

PLACEHOLDER = "string"

#: The topics tree's own emoji, by slug (migrations/versions/topics.py).
TREE_ICONS = {
    "development": "💻",
    "deep-tech": "🔬",
    "design": "🎨",
    "arts": "🎭",
    "business": "💼",
    "gaming-media": "🎮",
    "community": "🤝",
    "sports-fitness": "⚽",
}


def clean(bind) -> dict:
    """Put the placeholders right; returns what changed, by kind."""
    is_placeholder = "lower(trim(icon)) = :placeholder"
    deleted = bind.execute(
        sa.text(
            "UPDATE interest_categories c SET is_deleted = true, deleted_at = now(), updated_at = now() "
            f"WHERE NOT c.is_deleted AND {is_placeholder} AND NOT (c.slug = ANY(:tree)) "
            "AND NOT EXISTS (SELECT 1 FROM interests i WHERE i.category_id = c.id AND NOT i.is_deleted)"
        ),
        {"placeholder": PLACEHOLDER, "tree": list(TREE_ICONS)},
    ).rowcount
    interests = bind.execute(
        sa.text(f"UPDATE interests SET icon = NULL, icon_type = NULL, updated_at = now() WHERE {is_placeholder}"),
        {"placeholder": PLACEHOLDER},
    ).rowcount
    topics = bind.execute(
        sa.text(f"UPDATE interest_categories SET icon = NULL, icon_type = NULL, updated_at = now() WHERE {is_placeholder}"),
        {"placeholder": PLACEHOLDER},
    ).rowcount
    given = 0
    for slug, icon in TREE_ICONS.items():
        given += bind.execute(
            sa.text(
                "UPDATE interest_categories SET icon = :icon, icon_type = CAST('emoji' AS interesticontype), updated_at = now() "
                "WHERE slug = :slug AND NOT is_deleted AND (icon IS NULL OR trim(icon) = '')"
            ),
            {"icon": icon, "slug": slug},
        ).rowcount
    return {"topics_deleted": deleted, "interest_icons_cleared": interests, "topic_icons_cleared": topics, "topic_icons_given": given}


def upgrade() -> None:
    bind = op.get_bind()
    # Small tables, but a deploy must never queue behind a long read of them.
    bind.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
    clean(bind)


def downgrade() -> None:
    pass
