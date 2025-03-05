from datetime import datetime, timedelta, timezone
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, field_validator, field_serializer

# Create IST timezone explicitly
IST = timezone(timedelta(hours=5, minutes=30))


class CustomBaseModel(BaseModel):
    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})

    @field_validator("*", mode="before")
    @classmethod
    def ensure_ist_timezone(cls, value: Any) -> Any:
        """
        Validate and convert input datetime to IST timezone.

        Handles:
        - Naive datetimes (assume IST)
        - Datetimes in other timezones (convert to IST)
        """
        if isinstance(value, datetime):
            # If no timezone, explicitly set to IST
            if value.tzinfo is None:
                return value.replace(tzinfo=IST)

            # Convert to IST if not already in IST
            if value.tzinfo != IST:
                return value.astimezone(IST)

        return value

    @field_serializer("*")
    def serialize_datetime(self, value: Any, _info: Any) -> Union[str, Any]:
        """
        Ensure datetime is in IST before serialization.
        """
        if isinstance(value, datetime):
            # Ensure the datetime is in IST before serializing
            if value.tzinfo is None:
                value = value.replace(tzinfo=IST)
            elif value.tzinfo != IST:
                value = value.astimezone(IST)
            return value.isoformat()
        return value
