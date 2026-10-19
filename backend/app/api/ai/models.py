"""AI assist's three tables.

- `ai_usage`: one row per action a club asked for -- a draft, a piece of
  writing, a feedback summary -- written before the provider is called (`ok`
  null: under way) and finished after (`ok` true or false). The monthly
  count against `ai_actions_per_month` is the club's rows this month (IST)
  on the platform's key that succeeded, plus any still under way from the
  last few minutes (access.PENDING_FOR), so two requests at once cannot both
  take the last action. A club's own key (`byok`) is logged and never
  counted. No prompt and no answer is ever stored here.
- `club_ai_keys`: a club's own AI key, at most one per club, encrypted
  (app/core/ai/crypto.py). Never returned, logged or audited; `key_last4`
  is all anyone sees of it again.
- `ai_feedback_summaries`: the last summary of an event's survey comments,
  kept with the number of responses it was made from; a new response makes
  it stale (feedback.py).
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, Column, ForeignKey, Index, Integer, String, Text, text

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AIUsage(AbstractSQLModel):
    __tablename__ = "ai_usage"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    #: The account the request signed in as (the club's own, or an admin's).
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    #: The team member behind it, when it was one (club_access).
    member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    #: event_draft | write | feedback_summary | key_check
    action = Column(String(32), nullable=False)
    provider = Column(String(20), nullable=False)
    #: The model that answered (or the last one tried).
    model = Column(String(80), nullable=True)
    #: On the club's own key: logged, never counted against the plan.
    byok = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    input_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    output_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    latency_ms = Column(Integer, nullable=True)
    #: Null while under way; true counts as an action, false never does.
    ok = Column(Boolean, nullable=True)
    #: Why it failed: rate_limited, timeout, auth, billing, invalid_output...
    error = Column(String(40), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        # The monthly count: one club's rows since the first of the month.
        Index("ix_ai_usage_club_created", "club_id", "created_at"),
    )


class ClubAIKeys(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_ai_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, unique=True)
    #: gemini | anthropic (app.core.ai.providers.PROVIDERS)
    provider = Column(String(20), nullable=False)
    #: Fernet, with settings.AI_KEYS_SECRET. Never returned, logged or audited.
    encrypted_key = Column(Text, nullable=False)
    #: The model the club picked; lighter ones after it are the fallback.
    model = Column(String(80), nullable=False)
    key_last4 = Column(String(4), nullable=False)
    #: When a test call last succeeded with it (it is checked before it is saved).
    verified_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class AIFeedbackSummaries(AbstractSQLModel):
    __tablename__ = "ai_feedback_summaries"

    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    #: How many responses with comments it was made from: a different count is a stale summary.
    responses = Column(Integer, nullable=False)
    summary = Column(JSON, nullable=False)
    provider = Column(String(20), nullable=True)
    model = Column(String(80), nullable=True)
    generated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
