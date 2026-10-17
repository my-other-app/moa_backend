from datetime import datetime, timezone
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from sqlalchemy import event
from sqlalchemy.orm import Query
from sqlalchemy.orm import with_loader_criteria


#: Execution option that lets one statement see soft-deleted rows:
#: `select(Clubs).where(Clubs.is_deleted == True).execution_options(include_deleted=True)`.
#: Without it the criteria below are ANDed on, and a query that asks for
#: deleted rows gets none -- which is how the admin's Trash came up empty in
#: production while every test (whose session does not add the criteria)
#: passed.
INCLUDE_DELETED = "include_deleted"


def add_loader_criteria(session):
    """Hide soft-deleted rows from every ORM statement on this session, unless
    the statement says `include_deleted=True` (the Trash and restore, and the
    revenue ledger's history)."""

    @event.listens_for(session.sync_session, "do_orm_execute")
    def _add_criteria(execute_state):
        if execute_state.execution_options.get(INCLUDE_DELETED):
            return
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                SoftDeleteMixin,
                lambda cls: cls.is_deleted.is_(False),
                include_aliases=True,
            )
        )
