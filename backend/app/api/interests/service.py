"""Topics (interest categories) and their interests: the tree, and the
admin's changes to it.

The changes flush and leave the commit to the router, which writes the
audit line in the same transaction and then drops the kept counts
(topics.forget)."""
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.interests import topics
from app.api.interests.models import InterestCategory, InterestIconType, Interests, numbered, slugify
from app.response import CustomHTTPException

TOPIC_NOT_FOUND = "TOPIC_NOT_FOUND"
TOPIC_NOT_EMPTY = "TOPIC_NOT_EMPTY"
INTEREST_NOT_FOUND = "INTEREST_NOT_FOUND"


def _invalid(**errors) -> CustomHTTPException:
    return CustomHTTPException(400, "Invalid Request", errors=errors)


async def free_slug(session: AsyncSession, model, base: str, exclude_id: Optional[int] = None) -> str:
    """`base`, or base-2, base-3… (models.numbered): the first no other live row of `model` holds."""
    stem = numbered(base, 10)[: -len("-10")]  # what every numbered one starts with, however long base is
    stmt = select(model.slug).where(model.is_deleted == False, model.slug.like(f"{stem}%"))  # noqa: E712
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    taken = set((await session.scalars(stmt)).all())
    n = 1
    while numbered(base, n) in taken:
        n += 1
    return numbered(base, n)


async def _slug_taken(session: AsyncSession, model, slug: str, exclude_id: Optional[int] = None) -> bool:
    stmt = select(model.id).where(model.slug == slug, model.is_deleted == False)  # noqa: E712
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    return await session.scalar(stmt.limit(1)) is not None


async def _name_taken(session: AsyncSession, model, name: str, exclude_id: Optional[int] = None) -> bool:
    """Interests' names are unique over every row (a constraint); topics' over live ones."""
    stmt = select(model.id).where(model.name == name)
    if model is InterestCategory:
        stmt = stmt.where(model.is_deleted == False)  # noqa: E712
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    return await session.scalar(stmt.limit(1).execution_options(include_deleted=True)) is not None


async def _next_order(session: AsyncSession, model, *where) -> int:
    """One after the last in the list (sort orders start at 1)."""
    return int(await session.scalar(select(func.coalesce(func.max(model.sort_order), 0)).where(model.is_deleted == False, *where)) or 0) + 1  # noqa: E712


async def live_topic(session: AsyncSession, category_id: int) -> InterestCategory:
    category = await session.scalar(
        select(InterestCategory).where(InterestCategory.id == category_id, InterestCategory.is_deleted == False)  # noqa: E712
    )
    if category is None:
        raise CustomHTTPException(404, "Topic not found", error_code=TOPIC_NOT_FOUND)
    return category


async def _topic_exists(session: AsyncSession, category_id: int) -> bool:
    return await session.scalar(
        select(InterestCategory.id).where(InterestCategory.id == category_id, InterestCategory.is_deleted == False)  # noqa: E712
    ) is not None


def _check_icon(icon, icon_type) -> None:
    if icon_type and not icon:
        raise _invalid(icon="This field is required.")


# ─── Topics ──────────────────────────────────────────────────────────────


async def create_interest_category(
    session: AsyncSession,
    name: str,
    icon: str | None = None,
    icon_type: InterestIconType | None = None,
    *,
    slug: str | None = None,
    color: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
) -> InterestCategory:
    name = name.strip()
    if await _name_taken(session, InterestCategory, name):
        raise _invalid(name="name already exists")
    _check_icon(icon, icon_type)
    if slug is not None:
        if await _slug_taken(session, InterestCategory, slug):
            raise _invalid(slug="slug already exists")
    else:
        slug = await free_slug(session, InterestCategory, slugify(name))
    if sort_order is None:
        sort_order = await _next_order(session, InterestCategory)
    category = InterestCategory(
        name=name, slug=slug, icon=icon, icon_type=icon_type, color=color, description=description, sort_order=sort_order,
    )
    session.add(category)
    await session.flush()
    return category


async def update_interest_category(session: AsyncSession, category_id: int, fields: dict) -> InterestCategory:
    """`fields`: what the request sent (name always). A slug sent as null is left as it is."""
    category = await live_topic(session, category_id)
    name = fields["name"].strip()
    if name != category.name and await _name_taken(session, InterestCategory, name, exclude_id=category.id):
        raise _invalid(name="name already exists")
    slug = fields.get("slug")
    if slug is not None and slug != category.slug and await _slug_taken(session, InterestCategory, slug, exclude_id=category.id):
        raise _invalid(slug="slug already exists")
    icon = fields.get("icon", category.icon)
    icon_type = fields.get("icon_type", category.icon_type)
    _check_icon(icon, icon_type)

    category.name = name
    if slug is not None:
        category.slug = slug
    category.icon, category.icon_type = icon, icon_type
    for key in ("color", "description"):
        if key in fields:
            setattr(category, key, fields[key])
    if fields.get("sort_order") is not None:
        category.sort_order = fields["sort_order"]
    await session.flush()
    return category


async def delete_interest_category(session: AsyncSession, category_id: int, move_to: Optional[int] = None) -> dict:
    """Soft-delete a topic. Its interests (and with them every person's,
    club's and event's choice of them) move to `move_to` first, after that
    topic's own; a topic that still has interests needs one (409
    TOPIC_NOT_EMPTY)."""
    category = await live_topic(session, category_id)
    if move_to is not None and (move_to == category_id or not await _topic_exists(session, move_to)):
        raise _invalid(move_to="Pick another topic to move its interests to.")
    held = list((await session.scalars(
        select(Interests)
        .where(Interests.category_id == category_id, Interests.is_deleted == False)  # noqa: E712
        .order_by(Interests.sort_order, func.lower(Interests.name), Interests.id)
    )).all())
    if held and move_to is None:
        raise CustomHTTPException(
            409, "This topic still has interests: pick a topic to move them to (move_to).", error_code=TOPIC_NOT_EMPTY,
            errors={"interests": len(held)},
        )
    if held:
        start = await _next_order(session, Interests, Interests.category_id == move_to)
        for offset, interest in enumerate(held):
            interest.category_id = move_to
            interest.sort_order = start + offset
    category.soft_delete()
    await session.flush()
    return {"message": "Topic deleted", "moved": len(held), "move_to": move_to if held else None}


async def reorder(session: AsyncSession, topic_ids: Optional[list[int]], interest_ids: Optional[dict[int, list[int]]]) -> dict:
    """Put topics, and interests within topics, in the order given (see
    schemas.TopicsReorder). Returns what moved, for the audit line."""
    topic_ids = list(topic_ids or [])
    interest_ids = {int(k): list(v) for k, v in (interest_ids or {}).items()}
    cats = list((await session.scalars(
        select(InterestCategory).where(InterestCategory.is_deleted == False)  # noqa: E712
        .order_by(InterestCategory.sort_order, func.lower(InterestCategory.name), InterestCategory.id)
    )).all())
    live = {c.id: c for c in cats}
    if len(set(topic_ids)) != len(topic_ids):
        raise _invalid(topics="A topic is listed twice.")
    unknown = [t for t in [*topic_ids, *interest_ids] if t not in live]
    if unknown:
        raise _invalid(topics=f"No such topics: {sorted(set(unknown))}.")
    listed = [i for ids in interest_ids.values() for i in ids]
    if len(set(listed)) != len(listed):
        raise _invalid(interests="An interest is listed twice.")
    rows = {i.id: i for i in (await session.scalars(
        select(Interests).where(Interests.is_deleted == False)  # noqa: E712
        .order_by(Interests.sort_order, func.lower(Interests.name), Interests.id)
    )).all()}
    missing = [i for i in listed if i not in rows]
    if missing:
        raise _invalid(interests=f"No such interests: {sorted(missing)}.")

    if topic_ids:
        order = topic_ids + [c.id for c in cats if c.id not in set(topic_ids)]
        for position, cid in enumerate(order, start=1):
            live[cid].sort_order = position
    moved = 0
    for cid, ids in interest_ids.items():
        rest = [i for i in rows.values() if i.category_id == cid and i.id not in set(listed)]
        for position, interest in enumerate([rows[i] for i in ids] + rest, start=1):
            moved += interest.category_id != cid
            interest.category_id = cid
            interest.sort_order = position
    await session.flush()
    return {"topics": len(topic_ids), "interests": len(listed), "moved_topic": moved}


# ─── Interests ───────────────────────────────────────────────────────────


async def create_interest(
    session: AsyncSession,
    name: str,
    category_id: int,
    icon_type: InterestIconType | None = None,
    icon: str | None = None,
    *,
    slug: str | None = None,
    sort_order: int | None = None,
) -> Interests:
    name = name.strip()
    if not await _topic_exists(session, category_id):
        raise _invalid(category_id="invalid category_id")
    if await _name_taken(session, Interests, name):
        raise _invalid(name="name already exists")
    _check_icon(icon, icon_type)
    if slug is not None:
        if await _slug_taken(session, Interests, slug):
            raise _invalid(slug="slug already exists")
    else:
        slug = await free_slug(session, Interests, slugify(name))
    if sort_order is None:
        sort_order = await _next_order(session, Interests, Interests.category_id == category_id)
    interest = Interests(name=name, slug=slug, icon=icon, icon_type=icon_type, category_id=category_id, sort_order=sort_order)
    session.add(interest)
    await session.flush()
    return interest


async def list_interests(session: AsyncSession) -> list[dict]:
    """Every live topic, in order, with its live interests in order; a topic
    with none is listed too."""
    return await topics.tree(session)


async def update_interest(
    session: AsyncSession,
    interest_id: int,
    name: str,
    category_id: int,
    icon: str | None = None,
    icon_type=None,
    *,
    slug: str | None = None,
    sort_order: int | None = None,
) -> Interests:
    """Name, topic and icon as sent (as before); the slug and the place in
    its topic only when sent. Moved to another topic without a place, it goes last there."""
    interest = await session.scalar(select(Interests).where(Interests.id == interest_id, Interests.is_deleted == False))  # noqa: E712
    if not interest:
        raise CustomHTTPException(404, "Interest not found", error_code=INTEREST_NOT_FOUND)
    name = name.strip()
    if category_id != interest.category_id and not await _topic_exists(session, category_id):
        raise _invalid(category_id="invalid category_id")
    if name != interest.name and await _name_taken(session, Interests, name, exclude_id=interest.id):
        raise _invalid(name="name already exists")
    if slug is not None and slug != interest.slug and await _slug_taken(session, Interests, slug, exclude_id=interest.id):
        raise _invalid(slug="slug already exists")
    _check_icon(icon, icon_type)

    if sort_order is None and category_id != interest.category_id:
        sort_order = await _next_order(session, Interests, Interests.category_id == category_id)
    interest.name = name
    interest.category_id = category_id
    interest.icon = icon
    interest.icon_type = icon_type
    if slug is not None:
        interest.slug = slug
    if sort_order is not None:
        interest.sort_order = sort_order
    await session.flush()
    return interest


async def delete_interest(session: AsyncSession, interest_id: int) -> dict:
    """
    Remove an interest and detach it from everything that selected it.

    The link rows reference it by foreign key, so they go first. Clubs and
    users keep their other interests; only this one is dropped.
    """
    from app.api.clubs.models import ClubInterestsLink
    from app.api.events.models import EventInterestsLink
    from app.api.users.models import UserInterests

    interest = await session.get(Interests, interest_id)
    if not interest:
        raise CustomHTTPException(404, "Interest not found", error_code=INTEREST_NOT_FOUND)

    detached = 0
    for model in (ClubInterestsLink, EventInterestsLink, UserInterests):
        count = await session.scalar(
            select(func.count()).select_from(model).where(
                model.interest_id == interest_id
            )
        )
        detached += count or 0
        await session.execute(delete(model).where(model.interest_id == interest_id))

    await session.delete(interest)
    await session.commit()
    return {"message": "Interest deleted", "detached_from": detached}
