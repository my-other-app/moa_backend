"""Tracking links: where the people came from.

A link is a short URL the club shares in one place — an Instagram story, a
poster's QR, a WhatsApp group — carrying UTM tags and a code. Opening it is
a click, recorded here; signing up for the event (or following the club)
with that code attached is a conversion, recorded on the registration or
the follow. The two together say which channel brought people, not just
how many came.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy import UUID, Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin


class TrackingLinks(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "tracking_links"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False, index=True)
    # Null: the link points at the club's page and counts follows.
    event_id = Column(Integer, ForeignKey("events.id"), nullable=True, index=True)
    # Only the club sees the name: "Freshers fair QR", "Instagram bio".
    name = Column(String(80), nullable=False)
    # The short token in the public URL, and the `ref` a sign-up carries back.
    code = Column(String(12), nullable=False, unique=True, index=True)
    utm_source = Column(String(60), nullable=False)
    utm_medium = Column(String(60), nullable=False)
    utm_campaign = Column(String(80), nullable=True)
    utm_content = Column(String(80), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    club = relationship("Clubs")
    event = relationship("Events")


class TrackingClicks(AbstractSQLModel, TimestampsMixin):
    """One opening of a link. The visitor is a hash of address and browser,
    so a repeat opening by the same person counts once among the uniques
    without anyone's address being kept."""

    __tablename__ = "tracking_clicks"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)
    link_id = Column(UUID(as_uuid=True), ForeignKey("tracking_links.id", ondelete="CASCADE"), nullable=False, index=True)
    visitor = Column(String(64), nullable=True)
    referrer = Column(String(300), nullable=True)
    user_agent = Column(String(200), nullable=True)
