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
    )
    task_name = Column(sa.String, nullable=False)
    task_type = Column(sa.String(100), nullable=False)
    status = Column(sa.String, nullable=False)
    logs = Column(JSONB, nullable=False, default=[])
