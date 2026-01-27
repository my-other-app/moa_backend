from typing import Annotated, Any, Generic, TypeVar, List, Type, Optional, Dict
from urllib.parse import urlencode
from pydantic import BaseModel
from fastapi import Depends, Query as GetQuery, Request
from fastapi.encoders import jsonable_encoder

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)


class _PaginationParams(BaseModel):
    """Pagination parameters as a Pydantic model"""

    offset: int = 0
    limit: int = 10


def get_pagination_params(
    offset: Annotated[int, GetQuery(ge=0)] = 0,
    limit: Annotated[int, GetQuery(ge=1, le=100)] = 10,
) -> _PaginationParams:
    return _PaginationParams(offset=offset, limit=limit + 1)


class PaginatedResponse(BaseModel, Generic[M]):
    limit: int
    offset: int
    total: Optional[int] = None
    next: Optional[str] = None
    items: List[M]


def paginated_response(
    result: List[Any], request: Request, schema: Type[M], total: int | None = None
) -> PaginatedResponse[M]:
    """
    Create a paginated response from a list of SQLAlchemy models

    Args:
        result: List of SQLAlchemy model instances
        request: FastAPI Request object
        schema: Pydantic model class to convert results into
        total: Total number of items matching the query (before pagination). Optional.

    Returns:
        PaginatedResponse object with properly formatted items
    """
    limit = int(request.query_params.get("limit", 10))
    offset = int(request.query_params.get("offset", 0))

    # Check if we have more items than requested limit
    # has_next = len(result) > limit
    # paginated_result = result[:limit] if has_next else result
    
    # If total is provided, use it to determine has_next
    if total is not None:
        has_next = offset + len(result) < total
    else:
        # Fallback to old behavior (assuming result contains all items or limit+1)
        # But wait, if result is already paginated (limit), we can't know has_next unless we fetched limit+1
        # The old implementation fetched limit+1?
        # Let's check the old implementation.
        # "paginated_result = result[:limit] if has_next else result"
        # This implies result had limit+1 items.
        has_next = len(result) > limit
        if has_next:
            result = result[:limit]
        # total = 0 # Default if not provided

    # Prepare next URL if we have more results
    if has_next:
        query_params = dict(request.query_params)
        query_params["offset"] = str(offset + limit)
        next_url = f"{request.url.path}?{urlencode(query_params)}"
    else:
        next_url = None
    model_dicts = jsonable_encoder(result)
    
    # Use Pydantic model_validate_json for more reliable serialization
    validated_items = [schema.model_validate(item_dict) for item_dict in model_dicts]

    return PaginatedResponse[M](
        limit=limit,
        offset=offset,
        total=total,
        next=next_url,
        items=validated_items,
    )


# async def paginate(
#     query: Query,
#     schema: Type[M],
#     pagination: _PaginationParams,
#     db_session: AsyncSession,
# ) -> PaginatedResponse[M]:
#     """
#     Generic pagination function for SQLAlchemy async queries with FastAPI.
#     """
#     # Get total count
#     count_query = select(func.count()).select_from(query)
#     total = await db_session.scalar(count_query)

#     # Get paginated results
#     offset = (pagination.page - 1) * pagination.limit
#     query = query.offset(offset).limit(pagination.limit)

#     result = await db_session.execute(query)
#     items = result.scalars().all()

#     model_items = []
#     for item in items:
#         # Convert SQLAlchemy model to dict, handling relationships
#         item_dict = {}
#         for key, value in item.__dict__.items():
#             if key.startswith("_"):
#                 continue
#             if hasattr(value, "__dict__"):  # This is a relationship
#                 if hasattr(value, "__table__"):  # This is a SQLAlchemy model
#                     item_dict[key] = value.__dict__
#             else:
#                 item_dict[key] = value

#         # Create Pydantic model
#         model_items.append(schema.model_validate(item_dict))

#     print(model_items)

#     return PaginatedResponse[M](
#         total=total,
#         page=pagination.page,
#         limit=pagination.limit,
#         items=model_items,
#     )


PaginationParams = Annotated[_PaginationParams, Depends(get_pagination_params)]
