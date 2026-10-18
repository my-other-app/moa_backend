"""Threads: what a club posts to its page and its followers' feeds, and what came back."""
import uuid

import sqlalchemy as sa
from sqlalchemy import DDL, Boolean, Column, ForeignKey, Integer, String, UUID, UniqueConstraint
from sqlalchemy import event as sa_event

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
    # Kept beside the rows they count, so a feed page is one query. Likes and
    # comments by triggers on their rows (THREAD_COUNTS_SQL below), in the
    # writer's transaction; views by the batched view writer, which adds the
    # rows it inserted. discover.counters checks all three against the rows.
    likes_count = Column(Integer, nullable=False, default=0)
    comments_count = Column(Integer, nullable=False, default=0)
    views_count = Column(Integer, nullable=False, default=0)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        # The newest posts across every club: the discover feed and the front page.
        sa.Index("ix_club_threads_newest", sa.text("created_at DESC"), "id", postgresql_where=sa.text("NOT is_deleted")),
        # One club's posts, newest first: its page, and each followed club's
        # first few in a follower's feed (a LATERAL top-k per club).
        sa.Index("ix_club_threads_club_newest", "club_id", sa.text("created_at DESC"), "id", postgresql_where=sa.text("NOT is_deleted")),
    )


class ThreadPollOptions(AbstractSQLModel):
    __tablename__ = "thread_poll_options"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    thread_id = Column(UUID(as_uuid=True), ForeignKey("club_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String(60), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    # Kept by a trigger on thread_votes (THREAD_COUNTS_SQL).
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

    __table_args__ = (
        # A post's comments, oldest first, a page at a time (keyset on created_at, id).
        sa.Index("ix_thread_comments_thread_created", "thread_id", "created_at", "id", postgresql_where=sa.text("NOT is_deleted")),
    )


class ThreadViews(AbstractSQLModel):
    """One row per viewer per post: a signed-in user, or a hash of address and browser."""

    __tablename__ = "thread_views"

    thread_id = Column(UUID(as_uuid=True), ForeignKey("club_threads.id", ondelete="CASCADE"), primary_key=True)
    visitor = Column(String(64), primary_key=True)


# ─── The counters on a post, kept by the database ─────────────────────────
#
# A like or a vote was counted by reading the post's (or option's) number,
# adding one in Python and writing it back: two at once lost one, and a
# double tap on like or vote hit the unique key and answered 500. Now the
# rows are written with ON CONFLICT (threads.service) and these triggers move
# the numbers in the same transaction, whoever writes the rows (the app, an
# admin's removal, a cascade). The migration discover_integrity installs the
# same and recounts what is there.

THREAD_COUNTS_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_thread_likes_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        UPDATE club_threads SET likes_count = likes_count + 1 WHERE id = NEW.thread_id;
      ELSIF TG_OP = 'DELETE' THEN
        UPDATE club_threads SET likes_count = likes_count - 1 WHERE id = OLD.thread_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_thread_comments_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.thread_id = NEW.thread_id AND OLD.is_deleted = NEW.is_deleted THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') AND NOT OLD.is_deleted THEN
        UPDATE club_threads SET comments_count = comments_count - 1 WHERE id = OLD.thread_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') AND NOT NEW.is_deleted THEN
        UPDATE club_threads SET comments_count = comments_count + 1 WHERE id = NEW.thread_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_thread_votes_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.option_id = NEW.option_id THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        UPDATE thread_poll_options SET votes_count = votes_count - 1 WHERE id = OLD.option_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') THEN
        UPDATE thread_poll_options SET votes_count = votes_count + 1 WHERE id = NEW.option_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_thread_likes_count AFTER INSERT OR DELETE ON thread_likes
    FOR EACH ROW EXECUTE FUNCTION moa_thread_likes_count()
    """,
    """
    CREATE TRIGGER trg_thread_comments_count AFTER INSERT OR DELETE OR UPDATE OF thread_id, is_deleted ON thread_comments
    FOR EACH ROW EXECUTE FUNCTION moa_thread_comments_count()
    """,
    """
    CREATE TRIGGER trg_thread_votes_count AFTER INSERT OR DELETE OR UPDATE OF option_id ON thread_votes
    FOR EACH ROW EXECUTE FUNCTION moa_thread_votes_count()
    """,
)

# Each function goes with its trigger, on the table the trigger is on (which is made after club_threads).
_ON = {"likes": ThreadLikes, "comments": ThreadComments, "votes": ThreadVotes}
for _sql in THREAD_COUNTS_SQL:
    _which = next(k for k in _ON if f"moa_thread_{k}_count" in _sql)
    sa_event.listen(_ON[_which].__table__, "after_create", DDL(_sql).execute_if(dialect="postgresql"))
