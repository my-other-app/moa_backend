from typing import Annotated, Generic, TypeVar, List, Type
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Query
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends, Query as GetQuery

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)


class _PaginationParams(BaseModel):
    """Pagination parameters as a Pydantic model"""

    page: int = 1
    limit: int = 10


def get_pagination_params(
    page: Annotated[int, GetQuery(ge=1)] = 1,
    limit: Annotated[int, GetQuery(ge=1, le=100)] = 10,
) -> _PaginationParams:
    return _PaginationParams(page=page, limit=limit)


class PaginatedResponse(BaseModel, Generic[T]):
    total: int
    page: int
    limit: int
    items: List[T]


async def paginate(
    query: Query,
    schema: Type[M],
    pagination: _PaginationParams,
    db_session: AsyncSession,
) -> PaginatedResponse[M]:
    """
    Generic pagination function for SQLAlchemy async queries with FastAPI.
    """
    # Get total count
    count_query = select(func.count()).select_from(query)
    total = await db_session.scalar(count_query)

    # Get paginated results
    offset = (pagination.page - 1) * pagination.limit
    query = query.offset(offset).limit(pagination.limit)

    result = await db_session.execute(query)
    items = result.scalars().all()

    model_items = []
    for item in items:
        # Convert SQLAlchemy model to dict, handling relationships
        item_dict = {}
        for key, value in item.__dict__.items():
            if key.startswith("_"):
                continue
            if hasattr(value, "__dict__"):  # This is a relationship
                if hasattr(value, "__table__"):  # This is a SQLAlchemy model
                    item_dict[key] = value.__dict__
            else:
                item_dict[key] = value

        # Create Pydantic model
        model_items.append(schema.model_validate(item_dict))

    print(model_items)

    return PaginatedResponse[M](
        total=total,
        page=pagination.page,
        limit=pagination.limit,
        items=model_items,
    )


PaginationParams = Annotated[_PaginationParams, Depends(get_pagination_params)]
