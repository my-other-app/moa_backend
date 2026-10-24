"""The club's WhatsApp inbox, worked as a team: who has opted out, notes,
saved replies, the inbox's settings and each person's notifications.

- `club_whatsapp_optouts`: the people who told the club to stop (STOP, or
  WhatsApp's own "stop marketing" setting, or Meta failing a marketing
  template with 131050, or the team on their word). Keyed by the club and the
  person's WhatsApp id and/or BSUID, one row per scope (`all`: nothing but
  replies inside a window they open; `marketing`: no marketing templates).
  NOT inbox content: retention never deletes it -- an opt-out lasts until
  the person (or the team, on their word) opts back in, which deletes it.
- `club_whatsapp_notes`: the team's notes on a conversation. Never sent,
  shown only to the inbox's people, deleted with the conversation.
- `club_whatsapp_saved_replies`: the club's own canned answers.
- `club_whatsapp_inbox_settings`: one row per club (absent: the defaults):
  how long a message may wait before the team is told, and the words of the
  automatic answer to STOP and START.
- `club_whatsapp_person_prefs`: one row per person with the inbox (the
  owner: `member_id` null; absent: everything on) -- their mails, and when
  the last "new messages" mail went to them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text, text

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

ALL = "all"
MARKETING = "marketing"
SCOPES = (ALL, MARKETING)

#: Why someone is opted out.
KEYWORD = "keyword"
USER_PREFERENCES = "user_preferences"
META_131050 = "meta_131050"
TEAM = "team"
SOURCES = (KEYWORD, USER_PREFERENCES, META_131050, TEAM)

DEFAULT_ALERT_HOURS = 4
NOTE_MAX = 2000
REPLY_TITLE_MAX = 60
REPLY_BODY_MAX = 4096
AUTO_REPLY_MAX = 1000


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ClubWhatsAppOptOuts(AbstractSQLModel):
    __tablename__ = "club_whatsapp_optouts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    #: The person: their WhatsApp id (digits with the country code) and/or their BSUID.
    wa_id = Column(String(20), nullable=True)
    bsuid = Column(String(160), nullable=True)
    #: all | marketing
    scope = Column(String(12), nullable=False)
    #: keyword | user_preferences | meta_131050 | team
    source = Column(String(20), nullable=False)
    at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    #: Who set it, for `team`: the club's own account, or a member. Null for the person or Meta.
    by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    by_name = Column(String(120), nullable=True)

    __table_args__ = (
        CheckConstraint("wa_id IS NOT NULL OR bsuid IS NOT NULL", name="ck_club_whatsapp_optouts_who"),
        CheckConstraint("scope IN ('all', 'marketing')", name="ck_club_whatsapp_optouts_scope"),
        CheckConstraint("source IN ('keyword', 'user_preferences', 'meta_131050', 'team')", name="ck_club_whatsapp_optouts_source"),
        Index("uq_club_whatsapp_optouts_wa", "club_id", "scope", "wa_id", unique=True, postgresql_where=text("wa_id IS NOT NULL")),
        Index("uq_club_whatsapp_optouts_bsuid", "club_id", "scope", "bsuid", unique=True, postgresql_where=text("bsuid IS NOT NULL")),
        Index("ix_club_whatsapp_optouts_by_user", "by_user_id", postgresql_where=text("by_user_id IS NOT NULL")),
        Index("ix_club_whatsapp_optouts_by_member", "by_member_id", postgresql_where=text("by_member_id IS NOT NULL")),
    )


class ClubWhatsAppNotes(AbstractSQLModel):
    __tablename__ = "club_whatsapp_notes"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id = Column(BigInteger, ForeignKey("club_whatsapp_conversations.id", ondelete="CASCADE"), nullable=False)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    body = Column(Text, nullable=False)
    #: The author: the club's own account, or a member.
    author_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    author_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    author_name = Column(String(120), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        Index("ix_club_whatsapp_notes_conversation", "conversation_id", "id"),
        Index("ix_club_whatsapp_notes_author_user", "author_user_id", postgresql_where=text("author_user_id IS NOT NULL")),
        Index("ix_club_whatsapp_notes_author_member", "author_member_id", postgresql_where=text("author_member_id IS NOT NULL")),
    )


class ClubWhatsAppSavedReplies(AbstractSQLModel):
    __tablename__ = "club_whatsapp_saved_replies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(REPLY_TITLE_MAX), nullable=False)
    body = Column(Text, nullable=False)
    #: Typed after "/" in the composer; lower case, unique in the club.
    shortcut = Column(String(32), nullable=True)
    created_by_name = Column(String(120), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        Index("uq_club_whatsapp_saved_replies_shortcut", "club_id", "shortcut", unique=True,
              postgresql_where=text("shortcut IS NOT NULL")),
    )


class ClubWhatsAppInboxSettings(AbstractSQLModel):
    __tablename__ = "club_whatsapp_inbox_settings"

    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), primary_key=True)
    #: A conversation waiting this long for an answer tells the team, once a wait.
    alert_after_hours = Column(Integer, nullable=False, default=DEFAULT_ALERT_HOURS, server_default=str(DEFAULT_ALERT_HOURS))
    #: The automatic answer to STOP and to START; null: the default words (optout.py).
    stop_reply = Column(Text, nullable=True)
    start_reply = Column(Text, nullable=True)
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("alert_after_hours BETWEEN 1 AND 48", name="ck_club_whatsapp_inbox_settings_hours"),
    )


class ClubWhatsAppPersonPrefs(AbstractSQLModel):
    __tablename__ = "club_whatsapp_person_prefs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)
    #: Null: the club's own account (its owner).
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True)
    #: Mail me when a WhatsApp message waits unread, or a conversation waits too long.
    email_messages = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    #: Mail me when a conversation is assigned to me.
    email_assigned = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    #: When the last "new messages" mail went to them: at most one an hour.
    messages_emailed_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("uq_club_whatsapp_person_prefs_owner", "club_id", unique=True, postgresql_where=text("member_id IS NULL")),
        Index("uq_club_whatsapp_person_prefs_member", "member_id", unique=True, postgresql_where=text("member_id IS NOT NULL")),
    )
