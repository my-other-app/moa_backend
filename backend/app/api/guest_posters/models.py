"""The posters' tables. New tables only: nothing existing gains a column."""
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

NOW = sa.text("now()")


class PosterSettings(AbstractSQLModel):
    """An event's switch and words. No row: posters are off."""

    __tablename__ = "poster_settings"

    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    #: The formats offered, in order: a subset of square, portrait, story.
    formats = Column(JSONB, nullable=False, default=lambda: ["square", "portrait", "story"],
                     server_default=sa.text("'[\"square\", \"portrait\", \"story\"]'::jsonb"))
    #: {before|during|after: {enabled, phrase, caption}}; what is missing takes the default.
    moments = Column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())


class PosterDesigns(AbstractSQLModel):
    """One variant of an event's poster: a layout v2 per format.

    `variant`: "default", "role:<attendee|speaker|volunteer|organiser>" or
    "type:<ticket type id>". `revision` counts its saves (the render's key)."""

    __tablename__ = "poster_designs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    variant = Column(String(40), nullable=False)
    layouts = Column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    revision = Column(Integer, nullable=False, default=0, server_default="0")
    #: The starter it was last made from, if any (poster/01-...).
    starter = Column(String(80), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())

    __table_args__ = (UniqueConstraint("event_id", "variant", name="uq_poster_designs_event_variant"),)


class GuestPosters(AbstractSQLModel):
    """One guest's poster: their photo and framing, their share link, what
    was drawn for them, and what they did with it (counts only)."""

    __tablename__ = "guest_posters"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)
    #: Their own link (tracking_links.kind 'poster_guest').
    tracking_link_id = Column(UUID(as_uuid=True), ForeignKey("tracking_links.id", ondelete="SET NULL"), nullable=True)
    #: The photo, cleaned (doc_art), in the private bucket; never on the CDN.
    photo_key = Column(String(300), nullable=True)
    photo_sha = Column(String(64), nullable=True)
    photo_width = Column(Integer, nullable=True)
    photo_height = Column(Integer, nullable=True)
    photo_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: {format: {zoom, x, y}}; "*" for every format.
    framing = Column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    #: {format: {fingerprint, key, state: queued|ready|failed, at, moment, error?}}
    renders = Column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    #: The first poster drawn for them (a poster "made").
    made_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    render_count = Column(Integer, nullable=False, default=0, server_default="0")
    downloads = Column(Integer, nullable=False, default=0, server_default="0")
    shares = Column(Integer, nullable=False, default=0, server_default="0")
    copies = Column(Integer, nullable=False, default=0, server_default="0")
    #: When the retention sweep removed the photo and renders.
    purged_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=NOW, default=sa.func.now())

    __table_args__ = (
        UniqueConstraint("registration_id", name="uq_guest_posters_registration"),
        sa.Index("ix_guest_posters_event", "event_id"),
        sa.Index("ix_guest_posters_tracking_link", "tracking_link_id", postgresql_where=sa.text("tracking_link_id IS NOT NULL")),
    )
