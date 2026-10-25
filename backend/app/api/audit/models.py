"""Record of what admins did.

The admin dashboard can delete clubs and events, approve and reject
registrations, award and revoke badges, block organisations, record winners
and refund money -- and until this table there was no record of any of it. An
incident ("who deleted that club?") had no answer, and none could be
reconstructed, because nothing was written down.

Deliberately append-only: nothing in the application updates or deletes a row.
A log an admin can edit answers the question it exists to answer only when
nobody wants it to.
"""

from sqlalchemy import Column, ForeignKey, Integer, String, JSON, Index, text
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


def search_sql(table: str = "") -> str:
    """What the log is searched in, as SQL: the actor's name and address, the
    summary, the record ("club #12", as the page shows it) and the reason
    given (an impersonation's). One expression for the index and for the
    query, written out as constants rather than bound parameters: the
    planner uses the trigram index only for the very expression it was
    built on. Immutable throughout (casts to text, `||`, coalesce, ->>),
    as an index expression has to be."""
    t = f"{table}." if table else ""
    return (
        f"(coalesce({t}actor_name::text, '') || ' ' || coalesce({t}actor_email::text, '') || ' ' || "
        f"coalesce({t}summary::text, '') || ' ' || coalesce({t}entity_type::text, '') || ' #' || "
        f"coalesce({t}entity_id::text, '') || ' ' || coalesce({t}details ->> 'reason', ''))"
    )


class AdminAuditLogs(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "admin_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Kept alongside the foreign key on purpose: an account can be deleted and
    # the log still has to say who acted. A dangling id is not an answer.
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_email = Column(String(100), nullable=True)
    # Their name as it was when they acted, for the same reason, and so the
    # log can be searched by who without reading every account
    # (migration audit_search).
    actor_name = Column(String(100), nullable=True)

    action = Column(String(60), nullable=False, index=True)
    entity_type = Column(String(40), nullable=True, index=True)
    entity_id = Column(String(64), nullable=True)

    # One line an admin can read without expanding anything.
    summary = Column(String(255), nullable=True)
    # Whatever the action needs: amounts, before/after, the impersonated user.
    details = Column(JSON, nullable=True)

    ip_address = Column(String(45), nullable=True)

    actor = relationship("Users", foreign_keys=[actor_id])

    __table_args__ = (
        # The two reads this table gets: the newest page, and everything that
        # happened to one record.
        Index("ix_audit_created_at", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
        # The Activity page's search: words anywhere in who, what, which
        # record and why, on a trigram index (migration audit_search).
        Index("ix_audit_search_trgm", text(f"{search_sql()} gin_trgm_ops"), postgresql_using="gin"),
    )
