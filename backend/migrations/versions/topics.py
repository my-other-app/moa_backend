"""Topics: main categories, each with its interests, wherever interests are
chosen or shown.

Production had one interest category, "arts", holding all 27 interests (Web
Development and Cybersecurity included). The two levels were always there:
interest_categories (a topic) over interests, which people, clubs and events
pick. This gives a topic what the site and the app need to show it, and
seeds a proper tree.

- interest_categories: slug (its address, unique among live topics),
  sort_order, color ("#RRGGBB"), description. The name's uniqueness moves
  from a constraint over every row to an index over live ones, so a topic
  deleted from the admin can be made again under its old name.
- interests: slug (unique among live interests), sort_order, and an index
  on category_id (a topic's interests: the topic filters read them).
- Every existing row gets the slug of its name, made unique in id order
  (-2, -3…).
- `seed`: the default tree below, matched to existing rows by name (case
  and surrounding spaces ignored). An existing interest keeps its id, so
  every user's, club's and event's link to it stands; it only moves to its
  topic and takes its place in the topic's order. The existing "arts" (id 1)
  becomes "Arts". Topics and interests the tree names but the database does
  not have are made; a deleted one of that name is brought back rather than
  duplicated. An interest the tree does not name stays in its topic, after
  the named ones; a topic the tree does not name comes after the tree's.
  Colour, icon and description are filled only where empty, so an admin's
  choice is never overwritten. The product's event types are added to
  event_categories (created_by_id NULL) where no live type of that name
  exists. Running it again changes nothing.

The tree is written out here, not imported: what this migration did must
not change when the app's code does.

Downgrade drops the columns and indexes and puts the name constraint back
(which fails if a deleted topic and a live one share a name). It cannot
un-move interests or un-make the topics, interests and event types the seed
added: those rows stay, in the old shape.

Revision ID: topics
Revises: discover_integrity
"""
import re
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "topics"
down_revision = "discover_integrity"
branch_labels = None
depends_on = None

#: (slug, name, colour, icon, description, its interests in order).
TREE = (
    ("development", "Development", "#2563EB", "💻", "Building software: the web, apps, the cloud, and keeping it all secure.", (
        "Web Development", "App Development", "Software Engineering", "Coding", "Open Source", "Cloud & DevOps",
        "Cybersecurity", "Data Science",
    )),
    ("deep-tech", "Deep Tech", "#7C3AED", "🔬", "AI, robots, hardware and the far edge of science.", (
        "Artificial Intelligence", "Robotics", "IoT & Hardware", "Blockchain", "VR/AR", "Quantum Computing", "Space Exploration",
    )),
    ("design", "Design", "#DB2777", "🎨", "Interfaces, visuals and products people love to use.", (
        "UI/UX", "Graphic Design", "Product Design", "Animation",
    )),
    ("arts", "Arts", "#EA580C", "🎭", "Drawing, music, dance, theatre, film and writing.", (
        "Drawing", "Music", "Singing", "Dance", "Theatre", "Photography", "Film", "Writing", "Fashion",
    )),
    ("business", "Business", "#059669", "💼", "Starting up, marketing, money and making products happen.", (
        "Entrepreneurship", "Product Management", "Marketing", "Finance", "Analytics",
    )),
    ("gaming-media", "Gaming & Media", "#DC2626", "🎮", "Games, e-sports, creators and the internet's culture.", (
        "Gaming", "E-Sports", "Content Creation", "Memes & Internet Culture",
    )),
    ("community", "Community", "#0D9488", "🤝", "Giving back, speaking up, debating and quizzing.", (
        "Volunteering", "Sustainability", "Public Speaking", "Debate & MUN", "Quizzing",
    )),
    ("sports-fitness", "Sports & Fitness", "#CA8A04", "⚽", "Playing, training and getting outdoors.", (
        "Sports", "Fitness", "Outdoors & Adventure",
    )),
)

#: The event types the product ships (event_categories with no creator).
EVENT_TYPES = (
    "Workshop", "Talk", "Hackathon", "Competition", "Meetup", "Conference", "Fest", "Webinar", "Performance",
    "Exhibition", "Bootcamp",
)

INDEXES = (
    ("ux_interest_categories_slug_live", "interest_categories", ["slug"], True),
    ("ux_interest_categories_name_live", "interest_categories", ["name"], True),
    ("ux_interests_slug_live", "interests", ["slug"], True),
)


SLUG_MAX = 60


def slugify(name: str) -> str:
    """As app.api.interests.models.slugify was when this was written."""
    ascii_ = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:SLUG_MAX].strip("-") or "topic"


def _norm(name) -> str:
    """How names are matched: case and surrounding spaces ignored."""
    return (name or "").strip().lower()


def _rows(bind, sql: str) -> list[dict]:
    return [dict(r._mapping) for r in bind.execute(sa.text(sql))]


def backfill_slugs(bind) -> int:
    """A slug for every row without one, from its name, unique in each table
    in id order (a later row with the same one gets -2, -3…). Returns how
    many it wrote."""
    written = 0
    for table in ("interest_categories", "interests"):
        rows = _rows(bind, f"SELECT id, name, slug FROM {table} ORDER BY id")
        taken = {r["slug"] for r in rows if r["slug"]}
        for r in rows:
            if r["slug"]:
                continue
            slug = _free(slugify(r["name"]), taken)
            taken.add(slug)
            bind.execute(sa.text(f"UPDATE {table} SET slug = :slug WHERE id = :id"), dict(slug=slug, id=r["id"]))
            written += 1
    return written


def _free(base: str, taken: set) -> str:
    """`base`, else base-2, base-3…, the first not in `taken`, within SLUG_MAX."""
    slug, n = base, 1
    while slug in taken:
        n += 1
        suffix = f"-{n}"
        slug = base[: SLUG_MAX - len(suffix)].rstrip("-") + suffix
    return slug


def _pick(rows: list[dict], name: str, slug: str | None = None):
    """The row a tree entry means: a live one with its slug, else with its
    exact name, else its name in any case; failing those, a deleted one of
    that name (brought back rather than duplicated)."""
    live = [r for r in rows if not r["is_deleted"]]
    for candidates in (
        [r for r in live if slug is not None and r["slug"] == slug],
        [r for r in live if r["name"] == name],
        [r for r in live if _norm(r["name"]) == _norm(name)],
        [r for r in rows if r["is_deleted"] and _norm(r["name"]) == _norm(name)],
    ):
        if candidates:
            return min(candidates, key=lambda r: r["id"])
    return None


def _update(bind, table: str, row: dict, values: dict) -> bool:
    """Write the values that differ from the row's; True when any did."""
    changed = {k: v for k, v in values.items() if row.get(k) != v}
    if not changed:
        return False
    sets = ", ".join(f"{k} = :{k}" for k in changed)
    bind.execute(sa.text(f"UPDATE {table} SET {sets}, updated_at = now() WHERE id = :id"), dict(changed, id=row["id"]))
    row.update(changed)
    return True


def _take_slug(bind, table: str, rows: list[dict], row: dict, slug: str) -> None:
    """Free `slug` for `row`: another live row holding it moves to the next free one."""
    for other in rows:
        if other is not row and not other["is_deleted"] and other["slug"] == slug:
            taken = {r["slug"] for r in rows}
            _update(bind, table, other, dict(slug=_free(slug, taken)))


def seed(bind) -> dict:
    """The default tree and the event types (see the module's docstring).
    Returns how many rows it made and changed; all zero on a second run."""
    done = dict(topics_created=0, topics_changed=0, interests_created=0, interests_changed=0, event_types_created=0)
    cats = _rows(bind, "SELECT id, name, slug, icon, color, description, sort_order, is_deleted, deleted_at FROM interest_categories ORDER BY id")
    ints = _rows(bind, "SELECT id, name, slug, category_id, sort_order, is_deleted, deleted_at FROM interests ORDER BY id")
    seeded: dict[int, tuple] = {}

    for position, (slug, name, color, icon, description, names) in enumerate(TREE, start=1):
        cat = _pick(cats, name, slug)
        if cat is None:
            _take_slug(bind, "interest_categories", cats, {}, slug)
            cat_id = bind.execute(sa.text(
                "INSERT INTO interest_categories (name, slug, icon, icon_type, color, description, sort_order, created_at, updated_at, is_deleted) "
                "VALUES (:name, :slug, :icon, CAST('emoji' AS interesticontype), :color, :description, :sort_order, now(), now(), false) RETURNING id"
            ), dict(name=name, slug=slug, icon=icon, color=color, description=description, sort_order=position)).scalar()
            cat = dict(id=cat_id, name=name, slug=slug, icon=icon, color=color, description=description, sort_order=position, is_deleted=False, deleted_at=None)
            cats.append(cat)
            done["topics_created"] += 1
        else:
            _take_slug(bind, "interest_categories", cats, cat, slug)
            values = dict(name=name, slug=slug, sort_order=position, is_deleted=False)
            if cat["is_deleted"]:
                values["deleted_at"] = None
            if cat["color"] is None:
                values["color"] = color
            if cat["description"] is None:
                values["description"] = description
            changed = _update(bind, "interest_categories", cat, values)
            if cat["icon"] is None:
                bind.execute(sa.text(
                    "UPDATE interest_categories SET icon = :icon, icon_type = CAST('emoji' AS interesticontype), updated_at = now() WHERE id = :id"
                ), dict(icon=icon, id=cat["id"]))
                cat["icon"] = icon
                changed = True
            done["topics_changed"] += changed

        for order, interest_name in enumerate(names, start=1):
            row = _pick(ints, interest_name)
            if row is None:
                interest_slug = _free(slugify(interest_name), {r["slug"] for r in ints if not r["is_deleted"]})
                interest_id = bind.execute(sa.text(
                    "INSERT INTO interests (name, slug, category_id, sort_order, created_at, updated_at, is_deleted) "
                    "VALUES (:name, :slug, :category_id, :sort_order, now(), now(), false) RETURNING id"
                ), dict(name=interest_name, slug=interest_slug, category_id=cat["id"], sort_order=order)).scalar()
                ints.append(dict(id=interest_id, name=interest_name, slug=interest_slug, category_id=cat["id"], sort_order=order, is_deleted=False, deleted_at=None))
                done["interests_created"] += 1
                continue
            values = dict(category_id=cat["id"], sort_order=order, is_deleted=False)
            if row["is_deleted"]:
                values["deleted_at"] = None
                # Its slug may have been given to a live interest meanwhile.
                if any(r["slug"] == row["slug"] and not r["is_deleted"] for r in ints if r is not row):
                    values["slug"] = _free(row["slug"], {r["slug"] for r in ints})
            done["interests_changed"] += _update(bind, "interests", row, values)
        seeded[cat["id"]] = names

    # Each topic's other interests, once every named one is in place: after the named ones, by name.
    for cat_id, names in seeded.items():
        named = {_norm(n) for n in names}
        others = sorted(
            (r for r in ints if r["category_id"] == cat_id and not r["is_deleted"] and _norm(r["name"]) not in named),
            key=lambda r: (_norm(r["name"]), r["id"]),
        )
        for order, row in enumerate(others, start=len(names) + 1):
            done["interests_changed"] += _update(bind, "interests", row, dict(sort_order=order))

    # Topics the tree does not name: after the tree's, in the order they were made.
    rest = [c for c in cats if c["id"] not in seeded and not c["is_deleted"]]
    for order, cat in enumerate(rest, start=len(TREE) + 1):
        done["topics_changed"] += _update(bind, "interest_categories", cat, dict(sort_order=order))

    for name in EVENT_TYPES:
        made = bind.execute(sa.text(
            "INSERT INTO event_categories (name, created_by_id, created_at, updated_at, is_deleted) "
            "SELECT CAST(:name AS VARCHAR), NULL, now(), now(), false "
            "WHERE NOT EXISTS (SELECT 1 FROM event_categories WHERE lower(trim(name)) = lower(CAST(:name AS VARCHAR)) AND NOT is_deleted) "
            "RETURNING id"
        ), dict(name=name)).scalar()
        done["event_types_created"] += made is not None
    return done


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("interest_categories", sa.Column("slug", sa.String(80), nullable=True))
    op.add_column("interest_categories", sa.Column("color", sa.String(7), nullable=True))
    op.add_column("interest_categories", sa.Column("description", sa.String(500), nullable=True))
    op.add_column("interest_categories", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("interests", sa.Column("slug", sa.String(80), nullable=True))
    op.add_column("interests", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"))

    backfill_slugs(bind)
    op.alter_column("interest_categories", "slug", nullable=False)
    op.alter_column("interests", "slug", nullable=False)

    # The name is unique among live topics only (the index below), not over every row.
    for constraint in sa.inspect(bind).get_unique_constraints("interest_categories"):
        if constraint["column_names"] == ["name"]:
            op.drop_constraint(constraint["name"], "interest_categories", type_="unique")
    for name, table, columns, unique in INDEXES:
        op.create_index(name, table, columns, unique=unique, postgresql_where=sa.text("NOT is_deleted"))
    op.create_index("ix_interests_category", "interests", ["category_id"])

    seed(bind)


def downgrade() -> None:
    op.drop_index("ix_interests_category", table_name="interests")
    for name, table, _, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
    op.create_unique_constraint("interest_categories_name_key", "interest_categories", ["name"])
    op.drop_column("interests", "sort_order")
    op.drop_column("interests", "slug")
    op.drop_column("interest_categories", "sort_order")
    op.drop_column("interest_categories", "description")
    op.drop_column("interest_categories", "color")
    op.drop_column("interest_categories", "slug")
