from sqlalchemy import JSON, UUID, Column
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class BackgroundTaskLogs(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "background_task_logs"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        default=sa.text("gen_random_uuid()"),
    )
    task_name = Column(sa.String, nullable=False)
    task_type = Column(sa.String(100), nullable=False)
    status = Column(sa.String, nullable=False)
    logs = Column(JSONB, nullable=False, default=[])
    # The event a run belongs to, when it belongs to one -- so an organiser can
    # see the history of their own imports.
    event_id = Column(sa.Integer, sa.ForeignKey("events.id"), nullable=True, index=True)


class ScheduledRuns(AbstractSQLModel):
    """When each periodic job last ran, one row per job, shared by every
    worker process. A job's own schedule used to live in a module variable,
    so every worker ran it on its first sweep after a deploy -- four full
    counter checks at once with four workers. The worker holding the job's
    advisory lock reads its row and runs it only when it is due
    (app.api.discover.counters)."""

    __tablename__ = "scheduled_runs"

    name = Column(sa.String(60), primary_key=True)
    started_at = Column(sa.DateTime(timezone=True), nullable=True)
    finished_at = Column(sa.DateTime(timezone=True), nullable=True)
    # What the last run found or did, for whoever is looking.
    result = Column(JSONB, nullable=True)
