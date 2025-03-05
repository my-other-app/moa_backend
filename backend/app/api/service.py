from typing import List, Dict, Any, Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.api.models import BackgroundTaskLogs


async def create_background_task_log(
    session: AsyncSession,
    task_name: str,
    task_type: str,
    initial_status: str = "STARTED",
    initial_logs: Optional[List[Dict[str, Any]]] = None,
) -> BackgroundTaskLogs:
    """
    Create a new background task log entry.

    :param session: SQLAlchemy AsyncSession
    :param task_name: Name of the background task
    :param task_type: Type of the background task (max 20 characters)
    :param initial_status: Initial status of the task (default: "STARTED")
    :param initial_logs: Optional initial logs to include
    :return: Created BackgroundTaskLogs instance
    """
    # Validate task type length
    if len(task_type) > 100:
        raise ValueError("task_type must be 20 characters or less")

    # Prepare logs (use empty list if None)
    logs = initial_logs or []

    # Create new log entry
    new_log = BackgroundTaskLogs(
        task_name=task_name, task_type=task_type, status=initial_status, logs=logs
    )

    # Add and commit to database
    session.add(new_log)
    await session.commit()
    await session.refresh(new_log)

    return new_log


async def update_background_task_log(
    session: AsyncSession,
    task_id: UUID,
    new_logs: Optional[List[Dict[str, Any]]] = None,
    new_status: Optional[str] = None,
) -> BackgroundTaskLogs:
    """
    Update an existing background task log.

    :param session: SQLAlchemy AsyncSession
    :param task_id: UUID of the task log to update
    :param new_logs: Optional new logs to append
    :param new_status: Optional new status to set
    :return: Updated BackgroundTaskLogs instance
    """
    # Prepare update data
    update_data = {}

    # If new logs are provided, append to existing logs
    if new_logs is not None:
        update_data["logs"] = BackgroundTaskLogs.logs + new_logs

    # If new status is provided, update status
    if new_status is not None:
        update_data["status"] = new_status

    # Perform update
    stmt = (
        update(BackgroundTaskLogs)
        .where(BackgroundTaskLogs.id == task_id)
        .values(**update_data)
        .returning(BackgroundTaskLogs)
    )

    # Execute update and fetch the updated record
    result = await session.execute(stmt)
    updated_log = result.scalars().first()

    # Commit changes
    await session.commit()
    await session.refresh(updated_log)

    return updated_log


async def get_background_task_log(
    session: AsyncSession, task_id: UUID
) -> Optional[BackgroundTaskLogs]:
    """
    Retrieve a background task log by its ID.

    :param session: SQLAlchemy AsyncSession
    :param task_id: UUID of the task log to retrieve
    :return: BackgroundTaskLogs instance or None
    """
    return await session.scalar(
        select(BackgroundTaskLogs).filter(BackgroundTaskLogs.id == task_id)
    )
