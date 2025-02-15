from datetime import datetime, timezone
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from sqlalchemy import event
from sqlalchemy.orm import Query
from sqlalchemy.orm import with_loader_criteria


def add_loader_criteria(session):
    @event.listens_for(session.sync_session, "do_orm_execute")
    def _add_criteria(execute_state):
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                SoftDeleteMixin,
                lambda cls: cls.is_deleted.is_(False),
                include_aliases=True,
            )
        )
