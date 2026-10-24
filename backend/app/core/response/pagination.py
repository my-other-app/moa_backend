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

    # If total is provided, use it to determine has_next
    if total is not None:
        # Trimmed first: callers fetch one more row than asked for, and
        # counting it made the last full page before the end claim there was
        # no next page (a Trash of three in pages of two never reached the
        # third).
        if len(result) > limit:
            result = result[:limit]
        has_next = offset + len(result) < total
    else:
        # Fallback to old behavior
        has_next = len(result) > limit
        if has_next:
            result = result[:limit]

    # Prepare next URL if we have more results
    if has_next:
        query_params = dict(request.query_params)
        query_params["offset"] = str(offset + limit)
        next_url = f"{request.url.path}?{urlencode(query_params)}"
    else:
        next_url = None
    # Use Pydantic model_validate for direct ORM to Pydantic conversion
    # This ensures from_attributes=True works correctly and relationships are accessed
    validated_items = [schema.model_validate(item) for item in result]

    return PaginatedResponse[M](
        limit=limit,
        offset=offset,
        total=total,
        next=next_url,
        items=validated_items,
    )


PaginationParams = Annotated[_PaginationParams, Depends(get_pagination_params)]
