from datetime import datetime, timedelta, timezone
from sqlalchemy.types import TypeDecorator, DateTime
from sqlalchemy import func

# Define IST timezone
IST = timezone(timedelta(hours=5, minutes=30))


class TZAwareDateTime(TypeDecorator):
    """
    Custom SQLAlchemy DateTime type that ensures timezone handling

    Handles:
    - Preserving existing timezones
    - Adding IST timezone to naive datetimes
    - Works with both ORM and core SQLAlchemy
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """
        Convert datetime before inserting into database

        Args:
            value (datetime): Input datetime
            dialect: SQLAlchemy dialect

        Returns:
            datetime: Timezone-aware datetime
        """
        if value is None:
            return None

        if value.tzinfo is None:
            return value.replace(tzinfo=IST)

        return value

    def process_result_value(self, value, dialect):
        """
        Optional: Process datetime when reading from database

        Args:
            value (datetime): Datetime from database
            dialect: SQLAlchemy dialect

        Returns:
            datetime: Processed datetime
        """
        if value is None:
            return None

        return value if value.tzinfo is not None else value.replace(tzinfo=IST)
