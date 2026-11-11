"""The sales team's leads: organisations we want something from, their
people, their timeline, their files, and every follow-up on a date
(docs/SUPPORT_AND_LEADS.md).

A lead moves forward through its stages until it is won (linked to the real
club, sponsor or venue on the platform) or lost with a reason. Every open
lead keeps a next follow-up: completing one, or moving a stage, asks for the
next. Follow-ups also hang off support cases.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, Date, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

TYPES = ("club", "sponsor", "venue", "vendor", "other")
STAGES = ("new", "contacted", "meeting", "proposal", "negotiation", "won", "lost")
OPEN_STAGES = ("new", "contacted", "meeting", "proposal", "negotiation")
WON, LOST = "won", "lost"
SOURCES = ("for_clubs", "referral", "event", "outreach", "inbound_email", "import", "signup", "speaker", "other")
ACTIVITY_KINDS = ("call", "meeting", "email", "whatsapp", "note", "file", "stage_change", "system")
FOLLOW_UP_KINDS = ("call", "meeting", "email", "whatsapp", "visit", "other")
CHANNELS = ("email", "phone", "whatsapp", "meeting", "other")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Leads(AbstractSQLModel):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    # The name and city as the duplicate check compares them (service.key_of).
    name_key = Column(String(200), nullable=False)
    type = Column(String(10), nullable=False)
    stage = Column(String(12), nullable=False, default="new", server_default="new")
    source = Column(String(16), nullable=False, default="other", server_default="other")
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    city = Column(String(120), nullable=True)
    city_key = Column(String(120), nullable=True)
    website = Column(String(300), nullable=True)
    # The organisation's own mail domain (not a free mail service's), from
    # its website or its people: the other half of the duplicate check.
    email_domain = Column(String(200), nullable=True)
    # Rupees a year, for clubs and sponsors.
    value_inr = Column(Integer, nullable=True)
    expected_close = Column(Date, nullable=True)
    tags = Column(JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb"))
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True, index=True)
    # The support case it was made from (a case turned into a lead).
    case_id = Column(Integer, ForeignKey("support_cases.id", ondelete="SET NULL"), nullable=True)
    lost_reason = Column(String(60), nullable=True)
    lost_note = Column(String(500), nullable=True)
    retry_on = Column(Date, nullable=True)
    won_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    lost_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # A won partner stays an account with a renewal date.
    renewal_on = Column(Date, nullable=True)
    stage_changed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_activity_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    merged_into_id = Column(Integer, ForeignKey("leads.id", ondelete="SET NULL"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.CheckConstraint(_in("type", TYPES), name="ck_leads_type"),
        sa.CheckConstraint(_in("stage", STAGES), name="ck_leads_stage"),
        sa.CheckConstraint(_in("source", SOURCES), name="ck_leads_source"),
        sa.Index("ix_leads_stage", "stage", "updated_at", postgresql_where=sa.text("merged_into_id IS NULL")),
        # The duplicate check: the name (and city), and the mail domain.
        sa.Index("ix_leads_name_key", "name_key", postgresql_where=sa.text("merged_into_id IS NULL")),
        sa.Index("ix_leads_email_domain", "email_domain", postgresql_where=sa.text("email_domain IS NOT NULL AND merged_into_id IS NULL")),
    )


class LeadContacts(AbstractSQLModel):
    __tablename__ = "lead_contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    role = Column(String(120), nullable=True)
    email = Column(String(254), nullable=True)
    phone = Column(String(30), nullable=True)
    whatsapp = Column(String(30), nullable=True)
    preferred_channel = Column(String(10), nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.CheckConstraint(f"preferred_channel IS NULL OR {_in('preferred_channel', CHANNELS)}", name="ck_lead_contacts_channel"),
        # Mail to sales@ finds its lead by who it is from or to.
        sa.Index("ix_lead_contacts_email", sa.text("lower(email)"), postgresql_where=sa.text("email IS NOT NULL")),
    )


class LeadActivities(AbstractSQLModel):
    """One line on a lead's timeline. A mail copied to sales@ is kept once
    (`provider`, `provider_message_id`)."""

    __tablename__ = "lead_activities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(14), nullable=False)
    body = Column(Text, nullable=True)
    occurred_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    contact_id = Column(Integer, ForeignKey("lead_contacts.id", ondelete="SET NULL"), nullable=True)
    file_id = Column(Integer, ForeignKey("lead_files.id", ondelete="SET NULL"), nullable=True)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    author_name = Column(String(120), nullable=True)
    # Whatever the kind needs: a stage change's from and to, a mail's subject and direction.
    details = Column(JSONB, nullable=True)
    provider = Column(String(12), nullable=True)
    provider_message_id = Column(String(200), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.CheckConstraint(_in("kind", ACTIVITY_KINDS), name="ck_lead_activities_kind"),
        sa.Index("ix_lead_activities_lead", "lead_id", "occurred_at"),
        sa.Index(
            "uq_lead_activities_mail", "lead_id", "provider", "provider_message_id", unique=True,
            postgresql_where=sa.text("provider_message_id IS NOT NULL"),
        ),
    )


class LeadFiles(AbstractSQLModel):
    """A proposal PDF, an MoU, a mail's attachment: private, read only through the API."""

    __tablename__ = "lead_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    storage_key = Column(String(300), nullable=False)
    filename = Column(String(255), nullable=True)
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    uploaded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))


class FollowUps(AbstractSQLModel):
    """What to do, when, and by whom: on a lead or on a support case."""

    __tablename__ = "follow_ups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=True)
    case_id = Column(Integer, ForeignKey("support_cases.id", ondelete="CASCADE"), nullable=True)
    kind = Column(String(10), nullable=False)
    title = Column(String(200), nullable=False)
    due_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    done = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    done_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    done_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Told to the admin owners once it was three days overdue (the sweep).
    overdue_flagged_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.CheckConstraint(_in("kind", FOLLOW_UP_KINDS), name="ck_follow_ups_kind"),
        sa.CheckConstraint("(lead_id IS NULL) <> (case_id IS NULL)", name="ck_follow_ups_one_parent"),
        # My day and the badge: each person's open ones by date.
        sa.Index("ix_follow_ups_open", "owner_id", "due_at", postgresql_where=sa.text("NOT done")),
        sa.Index("ix_follow_ups_lead", "lead_id", postgresql_where=sa.text("lead_id IS NOT NULL")),
        sa.Index("ix_follow_ups_case", "case_id", postgresql_where=sa.text("case_id IS NOT NULL")),
    )


class LeadLostReasons(AbstractSQLModel):
    """Why a lead was lost. Five are reference rows the migration inserts."""

    __tablename__ = "lead_lost_reasons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(40), nullable=False, unique=True)
    label = Column(String(80), nullable=False)
    position = Column(Integer, nullable=False, default=0, server_default="0")
    active = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
