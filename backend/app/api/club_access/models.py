"""The people a club lets into its dashboard, besides the club's own account.

A club signed in with one shared login: everyone who ran its events knew the
same password, nobody could be removed without changing it for all, and the
sign-in history could not say who did what. A member here has their own email
and password and a role. Signing in as a member opens a session *as the club*
— every club route keeps resolving "the club" from the club's own account, so
none of them changes — and the session carries the member's id, which the
auth dependency checks on every request (still a member, role allows this).

Who may do what is in `policy.py`; the club's own account is the owner.
"""
from sqlalchemy import Column, ForeignKey, Index, Integer, String, text

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

ROLES = ("admin", "editor", "door")
STATUSES = ("invited", "active", "removed")


class ClubMembers(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    # Lower-cased. One live membership per address: signing in by email has to
    # land on exactly one club.
    email = Column(String(100), nullable=False)
    full_name = Column(String(80), nullable=True)
    role = Column(String(16), nullable=False, default="editor")
    status = Column(String(16), nullable=False, default="invited")
    password = Column(String(255), nullable=True)
    # sha256 of the link's token; the token itself only ever exists in the mail.
    invite_token_hash = Column(String(64), nullable=True, index=True)
    invite_expires_at = Column(TZAwareDateTime(), nullable=True)
    invited_at = Column(TZAwareDateTime(), nullable=True)
    accepted_at = Column(TZAwareDateTime(), nullable=True)
    removed_at = Column(TZAwareDateTime(), nullable=True)
    last_seen_at = Column(TZAwareDateTime(), nullable=True)

    __table_args__ = (
        Index("uq_club_members_live_email", "email", unique=True, postgresql_where=text("status <> 'removed'")),
    )
