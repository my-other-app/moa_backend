from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.response import CustomHTTPException
from app.db.mixins import SoftDeleteMixin


async def validate_relations(session: AsyncSession, validation: dict[str, tuple]):
    errors = {}
    for key, (schema, value) in validation.items():
        if value == None:
            continue
        if not await session.scalar(select(exists().where(schema.id == value))):
            errors[key] = f"invalid {key}"
    if errors:
        raise CustomHTTPException(
            status_code=400, message="Invalid Request", errors=errors
        )
    return True


async def validate_unique(session: AsyncSession, **kwargs):
    unique = kwargs.get("unique", {})
    check_deleted = kwargs.get("check_deleted", True)
    errors = {}
    for key, (schema, value) in unique.items():
        if not value:
            continue
        query = select(exists().where(getattr(schema, key) == value))
        if check_deleted and issubclass(schema, SoftDeleteMixin):
            query = query.where(schema.is_deleted == False)
        if await session.scalar(query):
            errors[key] = f"{key} already exists"

    unique_together = kwargs.get("unique_together", [])
    for entry in unique_together:
        query = exists()
        skip = False

        if not isinstance(entry, dict):
            continue
        for key, (schema, value) in entry.items():
            if not value:
                skip = True
                continue

            query = query.where(getattr(schema, key) == value)
            if check_deleted and issubclass(schema, SoftDeleteMixin):
                query = query.where(schema.is_deleted == False)
        if not skip and await session.scalar(select(query)):
            key = list(entry.keys())[0]
            errors[key] = f"{key} already exists"
    if errors:
        raise CustomHTTPException(
            status_code=400, message="Invalid Request", errors=errors
        )
    return True
