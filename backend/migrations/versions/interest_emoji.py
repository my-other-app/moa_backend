"""Every interest of the topics tree gets its emoji.

The topics migration made the tree's 45 interests (and moved production's
27 under their topics) without an icon, and topic_placeholders took away
the "string" the early ones carried. The topics have had their emoji since;
the interests had none, so every interest chip in the app, on the site and
in the dashboards was drawn without the emoji it was designed with.

- `give`: each interest whose slug is in ICONS, and whose icon is none,
  blank, or the docs page's placeholder ("string", any case, spaces
  ignored), gets that emoji, marked as an emoji. An icon an admin chose is
  never touched, and a slug no interest has is passed over. Running it again
  changes nothing.

The emoji are written out here, not imported: what this migration did must
not change when the app's code does.

Downgrade (`take_back`): no icon again on each interest whose icon is still
this migration's emoji for its slug. One an admin changed since keeps theirs.

One UPDATE each way, on a table of tens of rows, under a short lock_timeout.
Nothing is cached that would keep the old answer: /interests/list reads the
table on every request (public, max-age=300 for browsers), and the topic
counts kept in process (/interests/topics) carry no interest icons.

Revision ID: interest_emoji
Revises: w2_live
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "interest_emoji"
down_revision = "w2_live"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

#: Each interest's emoji, by slug, topic by topic as the tree has them.
ICONS = {
    # Development
    "web-development": "🌐",
    "app-development": "📱",
    "software-engineering": "🧑‍💻",
    "coding": "⌨️",
    "open-source": "🧩",
    "cloud-devops": "☁️",
    "cybersecurity": "🔐",
    "data-science": "📊",
    # Deep Tech
    "artificial-intelligence": "🦾",
    "robotics": "🤖",
    "iot-hardware": "🔌",
    "blockchain": "⛓️",
    "vr-ar": "🥽",
    "quantum-computing": "⚛️",
    "space-exploration": "🚀",
    # Design
    "ui-ux": "🎨",
    "graphic-design": "🖌️",
    "product-design": "📐",
    "animation": "🐰",
    # Arts
    "drawing": "✏️",
    "music": "🥁",
    "singing": "🎤",
    "dance": "💃",
    "theatre": "🎭",
    "photography": "📷",
    "film": "🎬",
    "writing": "📝",
    "fashion": "👗",
    # Business
    "entrepreneurship": "👨‍💼",
    "product-management": "🏭",
    "marketing": "🏷️",
    "finance": "💸",
    "analytics": "📉",
    # Gaming & Media
    "gaming": "🎮",
    "e-sports": "🏆",
    "content-creation": "🎥",
    "memes-internet-culture": "😂",
    # Community
    "volunteering": "🙌",
    "sustainability": "🌱",
    "public-speaking": "🎙️",
    "debate-mun": "🗣️",
    "quizzing": "🧠",
    # Sports & Fitness
    "sports": "🏅",
    "fitness": "💪",
    "outdoors-adventure": "🏕️",
}

#: The docs page's example value, which the early interests carried as their icon.
PLACEHOLDER = "string"

#: ICONS as rows (slug, icon), for the statements below.
_PAIRS = "unnest(CAST(:slugs AS text[]), CAST(:icons AS text[])) AS m(slug, icon)"

GIVE = (
    "UPDATE interests AS i SET icon = m.icon, icon_type = CAST('emoji' AS interesticontype), updated_at = now() "
    f"FROM {_PAIRS} "
    "WHERE i.slug = m.slug AND (i.icon IS NULL OR trim(i.icon) = '' OR lower(trim(i.icon)) = :placeholder) "
    "RETURNING i.slug"
)

TAKE_BACK = (
    "UPDATE interests AS i SET icon = NULL, icon_type = NULL, updated_at = now() "
    f"FROM {_PAIRS} "
    "WHERE i.slug = m.slug AND i.icon = m.icon "
    "RETURNING i.slug"
)


def _params(icons: dict) -> dict:
    return dict(slugs=list(icons), icons=list(icons.values()), placeholder=PLACEHOLDER)


def give(bind, icons: dict = ICONS) -> list[str]:
    """Each interest of these slugs with no icon of its own gets its emoji.
    Returns the slugs given one, sorted ([] on a second run)."""
    return sorted(bind.execute(sa.text(GIVE), _params(icons)).scalars().all())


def take_back(bind, icons: dict = ICONS) -> list[str]:
    """No icon again where an interest's icon is still its emoji here.
    Returns the slugs it was taken from, sorted."""
    return sorted(bind.execute(sa.text(TAKE_BACK), _params(icons)).scalars().all())


def upgrade() -> None:
    bind = op.get_bind()
    # A tiny table, but a deploy must never queue behind a long read of it.
    bind.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
    given = give(bind)
    log.info("interest_emoji: %d of %d interests given their emoji", len(given), len(ICONS))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
    taken = take_back(bind)
    log.info("interest_emoji: emoji taken back from %d interests", len(taken))
