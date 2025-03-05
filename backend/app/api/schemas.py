from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID
from pydantic import BaseModel, Field

from app.core.response.base_model import CustomBaseModel


class BackgroundTaskLogEntrySchema(CustomBaseModel):
    """
    Schema for individual log entries within a task log
    """

    level: str = Field(..., description="Log level (e.g., INFO, DEBUG, ERROR)")
    message: str = Field(..., description="Log message content")
    timestamp: Optional[datetime] = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp of the log entry",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional additional metadata for the log entry"
    )


class BackgroundTaskLogResponseSchema(CustomBaseModel):
    """
    Comprehensive schema for background task log response
    """

    id: UUID = Field(..., description="Unique identifier for the task log")
    task_name: str = Field(..., description="Name of the background task")
    task_type: str = Field(
        ..., max_length=100, description="Type of the background task"
    )
    status: str = Field(..., description="Current status of the task")
    logs: List[BackgroundTaskLogEntrySchema] = Field(
        default_factory=list, description="List of log entries for the task"
    )
    created_at: datetime = Field(
        ..., description="Timestamp when the task log was created"
    )
    updated_at: datetime = Field(
        ..., description="Timestamp when the task log was last updated"
    )
