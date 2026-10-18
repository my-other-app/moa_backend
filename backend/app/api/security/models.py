"""Sign-in attempts and where they came from.

Every try at the password door leaves a row — signed in, wrong password,
blocked — with the device and address it came from. The Security page
reads them back; the lockout counts them.
"""
import uuid

from sqlalchemy import Column, ForeignKey, Index, Integer, String, UUID

from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


class SignInAttempts(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "sign_in_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    # What was typed in the username box, lowercased — so tries at an account
    # that does not exist, or before the user is known, still have a home.
    identifier = Column(String(120), nullable=True, index=True)
    ip_address = Column(String(45), nullable=True, index=True)
    user_agent = Column(String(255), nullable=True)
    method = Column(String(20), nullable=False)  # password | google | apple
    result = Column(String(30), nullable=False)  # signed_in | wrong_password | totp_required | totp_invalid | blocked
    session_id = Column(UUID(as_uuid=True), nullable=True)
    place = Column(String(120), nullable=True)

    __table_args__ = (
        Index("ix_sign_in_attempts_user_created", "user_id", "created_at"),
        # The lockout across every address counts one account's tries in the
        # last hour; the sweep removes the oldest.
        Index("ix_sign_in_attempts_identifier_created", "identifier", "created_at"),
        Index("ix_sign_in_attempts_created", "created_at"),
    )


class IpPlaces(AbstractSQLModel):
    """What an address resolved to, kept so each one is looked up once."""

    __tablename__ = "ip_places"

    ip_address = Column(String(45), primary_key=True)
    place = Column(String(120), nullable=True)
    looked_up_at = Column(TimestampsMixin.created_at.type, nullable=False)
