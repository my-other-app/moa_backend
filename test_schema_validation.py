from datetime import datetime, timezone
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional

# Mock CustomBaseModel
class CustomBaseModel(BaseModel):
    model_config = ConfigDict(extra='ignore') # Default behavior

class EventCategoryDetail(CustomBaseModel):
    id: int
    name: str
    icon: str | None = None
    icon_type: str | None = None

class EventListResponseSelf(CustomBaseModel):
    id: int
    name: str
    slug: str
    poster: dict | None = None
    event_datetime: datetime
    duration: float
    location_name: str | None = None
    has_fee: bool
    has_prize: bool = True
    prize_amount: float | None = None
    is_online: bool = False
    reg_startdate: datetime
    reg_enddate: datetime | None = None
    category: EventCategoryDetail
    page_views: int = 0
    registration_count: int = 0

# Mock data returned by get_club_events (jsonable_encoder output)
# Includes extra fields like 'club', 'images', 'about'
mock_data = {
    "id": 1,
    "name": "Test Event",
    "slug": "test-event",
    "poster": None,
    "event_datetime": datetime.now(timezone.utc).isoformat(),
    "duration": 2.0,
    "location_name": "Test Location",
    "has_fee": False,
    "reg_fee": None,
    "has_prize": False,
    "prize_amount": None,
    "is_online": False,
    "reg_startdate": datetime.now(timezone.utc).isoformat(),
    "reg_enddate": None,
    "page_views": 10,
    "category": {
        "id": 1,
        "name": "Tech",
        "icon": None,
        "icon_type": None,
        "created_by_id": 1 # Extra field in category
    },
    "club": { # Extra field in event
        "id": 1,
        "name": "Test Club"
    },
    "images": [], # Extra field
    "about": "Some description", # Extra field
    "registration_count": 5
}

print("Testing Schema Validation...")
try:
    validated = EventListResponseSelf.model_validate(mock_data)
    print("Success!")
    print(validated.model_dump())
except Exception as e:
    print(f"Failed: {e}")
