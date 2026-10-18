"""Reports and blocks: what a reader can do about a comment, a post or a
person they don't want to see (App Store guideline 1.2).

A report is one person's complaint about one thing -- a thread, a comment, a
user or a club -- kept until an admin resolves it. One person holds at most
one open report on a thing; the admin's decision resolves every open report
on it at once. A comment with REPORTS_TO_HIDE open reports from different
people is hidden from everyone but its author until then.

A block is one person not wanting to see another: the blocked person's
comments never appear in the blocker's comment lists.
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, String

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

TARGET_TYPES = ("thread", "comment", "user", "club")
REASONS = ("spam", "harassment", "hate", "sexual", "violence", "misinformation", "other")
STATUSES = ("open", "resolved")
ACTIONS = ("dismiss", "remove_content", "suspend_author")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ContentReports(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "content_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reporter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    target_type = Column(String(10), nullable=False)
    #: A thread's or a comment's UUID, or a user's or a club's id, as text.
    target_id = Column(String(64), nullable=False)
    reason = Column(String(20), nullable=False)
    note = Column(String(500), nullable=True)
    status = Column(String(10), nullable=False, default="open", server_default="open")
    #: Whose it is, as it stood when reported: the comment's writer, the
    #: club's account for a thread or a club, the user themselves. The account
    #: `suspend_author` suspends.
    target_author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: The club it sits under: the thread's, the comment's thread's, or the
    #: club reported. Null for a user.
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True)
    resolved_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    resolved_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolution = Column(String(20), nullable=True)
    resolution_note = Column(String(500), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("target_type", TARGET_TYPES), name="ck_content_reports_target_type"),
        CheckConstraint(_in("reason", REASONS), name="ck_content_reports_reason"),
        CheckConstraint(_in("status", STATUSES), name="ck_content_reports_status"),
        CheckConstraint(f"resolution IS NULL OR {_in('resolution', ACTIONS)}", name="ck_content_reports_resolution"),
        # One open report per person per thing: a second tap finds the first.
        sa.Index(
            "uq_content_reports_open",
            "reporter_id",
            "target_type",
            "target_id",
            unique=True,
            postgresql_where=sa.text("status = 'open'"),
        ),
        # The open reports on a thing: the auto-hide count and the resolve.
        sa.Index("ix_content_reports_target", "target_type", "target_id", "status"),
        # The admin's queue, newest first.
        sa.Index("ix_content_reports_queue", "status", sa.text("created_at DESC")),
        # One person's reports in the last hour: the rate limit.
        sa.Index("ix_content_reports_reporter_created", "reporter_id", "created_at"),
    )


class UserBlocks(AbstractSQLModel):
    __tablename__ = "user_blocks"

    blocker_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    blocked_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (
        CheckConstraint("blocker_id <> blocked_id", name="ck_user_blocks_not_self"),
        sa.Index("ix_user_blocks_blocked", "blocked_id"),
    )
