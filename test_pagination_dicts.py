from typing import List, Type, Any, Optional
from pydantic import BaseModel
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from urllib.parse import urlencode

# Mock classes
class MockSchema(BaseModel):
    id: int
    name: str
    registration_count: int

class MockRequest:
    def __init__(self):
        self.query_params = {"limit": "10", "offset": "0"}
        self.url = type('obj', (object,), {'path': '/test'})

class PaginatedResponse(BaseModel):
    limit: int
    offset: int
    total: int
    next: Optional[str] = None
    items: List[MockSchema]

def paginated_response(
    result: List[Any], request: Request, schema: Type[MockSchema], total: int | None = None
) -> PaginatedResponse:
    limit = int(request.query_params.get("limit", 10))
    offset = int(request.query_params.get("offset", 0))

    if total is not None:
        has_next = offset + len(result) < total
    else:
        has_next = len(result) > limit
        if has_next:
            result = result[:limit]
        total = 0 

    if has_next:
        query_params = dict(request.query_params)
        query_params["offset"] = str(offset + limit)
        next_url = f"{request.url.path}?{urlencode(query_params)}"
    else:
        next_url = None
    
    model_dicts = jsonable_encoder(result)
    validated_items = [schema.model_validate(item_dict) for item_dict in model_dicts]

    return PaginatedResponse(
        limit=limit,
        offset=offset,
        total=total,
        next=next_url,
        items=validated_items,
    )

# Test Case: List of dicts (like get_club_events)
print("Testing List of Dicts...")
items = [{"id": i, "name": f"Event {i}", "registration_count": i*2} for i in range(5)] # 5 items, limit 10
req = MockRequest()
try:
    res = paginated_response(items, req, MockSchema)
    print(f"Success: total={res.total}, items={len(res.items)}")
    print(res.items[0])
except Exception as e:
    print(f"Failed: {e}")
