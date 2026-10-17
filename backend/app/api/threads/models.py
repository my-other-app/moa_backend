"""Threads: what a club posts to its page and its followers' feeds, and what came back."""
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, UUID, UniqueConstraint

from app.core.storage.fields import S3ImageField
from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class ClubThreads(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "club_threads"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    body = Column(String(500), nullable=False)
    image = Column(
        S3ImageField(upload_to="clubs/threads/", variations={"thumbnail": {"width": 300, "height": 300}, "medium": {"width": 900, "height": 900}, "large": {"width": 1600, "height": 1600}}),
        nullable=True,
    )
    kind = Column(String(10), nullable=False, default="post")  # post | poll
    pinned = Column(Boolean, nullable=False, default=False)
    notify_followers = Column(Boolean, nullable=False, default=True)
    # Kept beside the rows they count, so a feed page is one query.
    likes_count = Column(Integer, nullable=False, default=0)
    comments_count = Column(Integer, nullable=False, default=0)
    views_count = Column(Integer, nullable=False, default=0)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        # The newest posts across every club: the discover feed and the front page.
        sa.Index("ix_club_threads_newest", sa.text("created_at DESC"), "id", postgresql_where=sa.text("NOT is_deleted")),
    )


class ThreadPollOptions(AbstractSQLModel):
    __tablename__ = "thread_poll_options"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    thread_id = Column(UUID(as_uuid=True), ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String(60), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    votes_count = Column(Integer, nullable=False, default=0)


class ThreadVotes(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "thread_votes"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    thread_id = Column(UUID(as_uuid=True), ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    option_id = Column(UUID(as_uuid=True), ForeignKey("thread_poll_options.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (UniqueConstraint("thread_id", "user_id", name="uq_thread_votes_thread_user"),)


class ThreadLikes(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "thread_likes"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    thread_id = Column(UUID(as_uuid=True), ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    __table_args__ = (UniqueConstraint("thread_id", "user_id", name="uq_thread_likes_thread_user"),)


class ThreadComments(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "thread_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    thread_id = Column(UUID(as_uuid=True), ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    body = Column(String(500), nullable=False)


class ThreadViews(AbstractSQLModel):
    """One row per viewer per post: a signed-in user, or a hash of address and browser."""

    __tablename__ = "thread_views"

    thread_id = Column(UUID(as_uuid=True), ForeignKey("club_threads.id", ondelete="CASCADE"), primary_key=True)
    visitor = Column(String(64), primary_key=True)
